"""
Local semantic intent classification for the PL Capital AI Portfolio Analytics Agent.

What it is
----------
A small, independent layer that decides *what kind of message* the user sent before any analytical
tool runs. It embeds the message with a local sentence-embedding model (FastEmbed + ONNX Runtime,
``sentence-transformers/all-MiniLM-L6-v2``, CPU only) and compares it, by cosine similarity, with
cached embeddings of representative examples for each intent (see ``intent_examples.py``).

What it is not
--------------
* It never calls Gemini or any hosted API. The model runs in-process after a one-off download.
* It contains no SQL or portfolio logic. It returns an :class:`IntentResult`; the agent decides
  what to do with it.
* It does not pick "the closest embedding" blindly: every intent has its own minimum score, the
  winner must beat the runner-up by a margin, and a conversational winner is not accepted when the
  message also carries analytical content (mixed messages). Anything uncertain is ``UNKNOWN``.

Failure model
-------------
Problems with the model (not installed, not cached, corrupt, inference error) raise
:class:`IntentClassifierUnavailable`. That is deliberately different from ``UNKNOWN``: "I could not
classify" and "I classified it as uncertain" lead to different, both Gemini-free, behaviour in the
agent.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .intent_examples import INTENT_EXAMPLES

logger = logging.getLogger(__name__)

DEFAULT_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_DIR = PROJECT_ROOT / ".model_cache" / "fastembed"

ENV_ENABLED = "PORTFOLIO_INTENT_CLASSIFIER"      # on (default) | off
ENV_MODEL = "PORTFOLIO_INTENT_MODEL"
ENV_CACHE_DIR = "PORTFOLIO_INTENT_CACHE_DIR"
ENV_ALLOW_DOWNLOAD = "PORTFOLIO_INTENT_ALLOW_DOWNLOAD"  # 1 = fetch the model on first use if missing
ENV_THRESHOLDS = "PORTFOLIO_INTENT_THRESHOLDS"    # JSON overrides, see IntentThresholds

SETUP_HINT = (
    "The local intent model is not available. One-off setup (needs internet once):\n"
    "    pip install fastembed\n"
    "    python scripts/download_intent_model.py\n"
    "After that it runs fully offline. To run without it, set PORTFOLIO_INTENT_CLASSIFIER=off."
)


class Intent(str, Enum):
    GREETING = "GREETING"
    THANKS = "THANKS"
    GOODBYE = "GOODBYE"
    IDENTITY = "IDENTITY"
    CAPABILITIES = "CAPABILITIES"
    PORTFOLIO_ANALYTICS = "PORTFOLIO_ANALYTICS"
    SECTOR_EXPOSURE = "SECTOR_EXPOSURE"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"


CONVERSATIONAL = frozenset({Intent.GREETING, Intent.THANKS, Intent.GOODBYE, Intent.IDENTITY, Intent.CAPABILITIES})
ANALYTICAL = frozenset({Intent.PORTFOLIO_ANALYTICS, Intent.SECTOR_EXPOSURE})


class IntentClassifierUnavailable(RuntimeError):
    """The embedding model could not be loaded or run. The message says how to fix it."""


@dataclass(frozen=True)
class IntentThresholds:
    """
    Decision parameters (calibrated against tests/intent_validation_set.py; see
    ``scripts/calibrate_intents.py``). Scores are cosine similarities in [-1, 1].

    min_score            per-intent minimum best-example similarity to accept that intent.
    min_margin           the winner must beat the best *unrelated* intent by at least this much.
    mixed_floor          if a conversational intent wins but an analytical intent scores at least
                         this much, ...
    mixed_gap            ... and is within this distance of the conversational winner, the message is
                         treated as mixed (conversational + analytical). A clearly conversational message
                         ("What kind of portfolio analysis is available?") keeps its conversational intent
                         even though it shares words with analytical examples.
    analytic_pair_margin SECTOR_EXPOSURE must beat PORTFOLIO_ANALYTICS by this much to be chosen
                         over the generic analytical intent.
    """

    min_score: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_MIN_SCORE))
    min_margin: float = 0.04
    mixed_floor: float = 0.50
    mixed_gap: float = 0.20
    analytic_pair_margin: float = 0.02

    def score_for(self, intent: Intent) -> float:
        return float(self.min_score.get(intent.value, 0.6))

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "IntentThresholds":
        raw = (env if env is not None else os.environ).get(ENV_THRESHOLDS)
        base = cls()
        if not raw:
            return base
        try:
            data = json.loads(raw)
            min_score = {**base.min_score, **{str(k).upper(): float(v) for k, v in (data.get("min_score") or {}).items()}}
            return cls(
                min_score=min_score,
                min_margin=float(data.get("min_margin", base.min_margin)),
                mixed_floor=float(data.get("mixed_floor", base.mixed_floor)),
                mixed_gap=float(data.get("mixed_gap", base.mixed_gap)),
                analytic_pair_margin=float(data.get("analytic_pair_margin", base.analytic_pair_margin)),
            )
        except (ValueError, TypeError, AttributeError) as exc:
            logger.warning("Ignoring invalid %s (%s); using calibrated defaults.", ENV_THRESHOLDS, exc)
            return base


# Filled in from the calibration run (see docs/INTENT_CLASSIFIER_CALIBRATION.md).
DEFAULT_MIN_SCORE: Dict[str, float] = {
    "GREETING": 0.55,
    "THANKS": 0.45,
    "GOODBYE": 0.50,
    "IDENTITY": 0.50,
    "CAPABILITIES": 0.50,
    "PORTFOLIO_ANALYTICS": 0.50,
    "SECTOR_EXPOSURE": 0.50,
    "UNSUPPORTED": 0.40,
    "UNKNOWN": 0.55,
}


@dataclass(frozen=True)
class IntentResult:
    """
    Outcome of one classification. ``score`` and ``margin`` are internal cosine similarities used
    for the decision; they are not probabilities and must not be shown to users as confidence.
    """

    intent: Intent
    score: float
    margin: float
    reason: str
    best_guess: Intent
    scores: Mapping[str, float] = field(default_factory=dict)

    @property
    def is_conversational(self) -> bool:
        return self.intent in CONVERSATIONAL

    @property
    def is_analytical(self) -> bool:
        return self.intent in ANALYTICAL


Embedder = Callable[[Sequence[str]], Any]  # list of texts -> array-like of shape (n, dim)


class FastEmbedder:
    """Lazy FastEmbed/ONNX wrapper. The model is created on first use, once per instance."""

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL_NAME,
        cache_dir: Optional[os.PathLike] = None,
        allow_download: bool = False,
        threads: Optional[int] = None,
    ) -> None:
        self.model_name = model_name
        self.cache_dir = str(cache_dir or DEFAULT_CACHE_DIR)
        self.allow_download = allow_download
        self.threads = threads
        self._model: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        with self._lock:
            if self._model is not None:
                return self._model
            try:
                from fastembed import TextEmbedding  # imported lazily: optional dependency
            except ImportError as exc:  # pragma: no cover - depends on environment
                raise IntentClassifierUnavailable(f"fastembed is not installed. {SETUP_HINT}") from exc
            try:
                kwargs: Dict[str, Any] = {"model_name": self.model_name, "cache_dir": self.cache_dir}
                if self.threads:
                    kwargs["threads"] = self.threads
                if not self.allow_download:
                    kwargs["local_files_only"] = True  # never reach the network at request time
                self._model = TextEmbedding(**kwargs)
            except Exception as exc:
                raise IntentClassifierUnavailable(
                    f"Could not load '{self.model_name}' from {self.cache_dir} "
                    f"({type(exc).__name__}: {str(exc)[:160]}). {SETUP_HINT}"
                ) from exc
            return self._model

    def __call__(self, texts: Sequence[str]) -> Any:
        import numpy as np

        model = self._load()
        return np.asarray(list(model.embed(list(texts))), dtype="float32")


class LocalIntentClassifier:
    """
    Semantic intent classifier. Thread-safe; loads lazily and only once.

    ``embedder`` is injectable so tests (and other models) never need the real model.
    """

    def __init__(
        self,
        embedder: Optional[Embedder] = None,
        examples: Optional[Mapping[str, Sequence[str]]] = None,
        thresholds: Optional[IntentThresholds] = None,
    ) -> None:
        self._embedder: Embedder = embedder or FastEmbedder()
        self._examples = {k: list(v) for k, v in (examples or INTENT_EXAMPLES).items()}
        self.thresholds = thresholds or IntentThresholds()
        self._lock = threading.Lock()
        self._labels: List[Intent] = []
        self._matrix: Any = None          # (n_examples, dim) L2-normalised prototype embeddings
        self._failure: Optional[str] = None

    # ------------------------------------------------------------------ lifecycle

    @property
    def ready(self) -> bool:
        return self._matrix is not None

    @property
    def failure(self) -> Optional[str]:
        """Why the classifier is unavailable, or None."""
        return self._failure

    def warm_up(self) -> None:
        """Load the model and embed the prototypes now (idempotent). Raises IntentClassifierUnavailable."""
        if self._matrix is not None:
            return
        with self._lock:
            if self._matrix is not None:
                return
            if self._failure is not None:
                raise IntentClassifierUnavailable(self._failure)  # do not retry a broken model per request
            try:
                import numpy as np

                texts: List[str] = []
                labels: List[Intent] = []
                for name, items in self._examples.items():
                    intent = Intent(name)
                    texts.extend(items)
                    labels.extend([intent] * len(items))
                matrix = self._normalise(np.asarray(self._embedder(texts), dtype="float32"))
                if matrix.shape[0] != len(texts):
                    raise ValueError("embedder returned a different number of vectors than texts")
            except IntentClassifierUnavailable as exc:
                self._failure = str(exc)
                logger.warning("Local intent classifier unavailable: %s", self._failure)
                raise
            except Exception as exc:
                self._failure = f"Intent model failed to initialise ({type(exc).__name__}: {str(exc)[:160]}). {SETUP_HINT}"
                logger.warning("Local intent classifier unavailable: %s", self._failure)
                raise IntentClassifierUnavailable(self._failure) from exc
            self._labels, self._matrix = labels, matrix

    def reset(self) -> None:
        """Forget a recorded failure so the next call retries loading (e.g. after downloading the model)."""
        with self._lock:
            self._failure = None
            self._matrix = None
            self._labels = []

    @staticmethod
    def _normalise(m: Any) -> Any:
        import numpy as np

        m = np.atleast_2d(m)
        norms = np.linalg.norm(m, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return m / norms

    # ------------------------------------------------------------------ scoring

    def similarities(self, text: str) -> Dict[str, float]:
        """Best cosine similarity between ``text`` and the examples of each intent."""
        self.warm_up()
        import numpy as np

        try:
            query = self._normalise(np.asarray(self._embedder([text]), dtype="float32"))[0]
            sims = self._matrix @ query
        except IntentClassifierUnavailable:
            raise
        except Exception as exc:  # inference failure: report, never guess
            raise IntentClassifierUnavailable(f"Intent inference failed ({type(exc).__name__}: {str(exc)[:160]}).") from exc
        best: Dict[str, float] = {}
        for label, s in zip(self._labels, sims):
            if s > best.get(label.value, -2.0):
                best[label.value] = float(s)
        return best

    def classify(self, text: str) -> IntentResult:
        """Classify one message. Raises IntentClassifierUnavailable if the model cannot run."""
        scores = self.similarities(text)
        return self.decide(scores)

    def decide(self, scores: Mapping[str, float]) -> IntentResult:
        """Turn per-intent similarities into a decision. Pure function of ``scores`` and the thresholds."""
        th = self.thresholds
        ranked = sorted(((Intent(k), v) for k, v in scores.items()), key=lambda kv: kv[1], reverse=True)
        if not ranked:
            return IntentResult(Intent.UNKNOWN, 0.0, 0.0, "no_scores", Intent.UNKNOWN, dict(scores))
        best, s1 = ranked[0]

        def unknown(reason: str, margin: float) -> IntentResult:
            return IntentResult(Intent.UNKNOWN, s1, margin, reason, best, dict(scores))

        def runner_up_excluding(*excluded: Intent) -> float:
            return max((v for k, v in ranked if k not in excluded), default=-1.0)

        if best == Intent.UNKNOWN:  # nearest examples are vague/context-free messages ("tell me more", "portfolio")
            return unknown("vague_message", s1 - runner_up_excluding(best))

        # Mixed messages: a conversational (or "unsupported") winner while analytical content is also clearly
        # present. Never swallow a real analytical request behind a greeting, help request or local rejection.
        if best in CONVERSATIONAL or best == Intent.UNSUPPORTED:
            a_intent, a_score = max(((k, v) for k, v in ranked if k in ANALYTICAL), key=lambda kv: kv[1], default=(None, -1.0))
            if a_intent is not None and a_score >= th.mixed_floor and (s1 - a_score) <= th.mixed_gap:
                if a_score >= th.score_for(a_intent):
                    return self._analytical(a_intent, a_score, scores, th, reason="mixed_conversational_and_analytical")
                return unknown("mixed_unresolved", s1 - a_score)

        sibling = {Intent.PORTFOLIO_ANALYTICS: Intent.SECTOR_EXPOSURE, Intent.SECTOR_EXPOSURE: Intent.PORTFOLIO_ANALYTICS}.get(best)
        margin = s1 - (runner_up_excluding(best, sibling) if sibling else runner_up_excluding(best))

        if s1 < th.score_for(best):
            return unknown("low_confidence", margin)
        if margin < th.min_margin:
            return unknown("ambiguous", margin)
        if best in ANALYTICAL:
            return self._analytical(best, s1, scores, th, reason="analytical", margin=margin)
        return IntentResult(best, s1, margin, "confident", best, dict(scores))

    @staticmethod
    def _analytical(intent: Intent, score: float, scores: Mapping[str, float], th: IntentThresholds, reason: str, margin: Optional[float] = None) -> IntentResult:
        """SECTOR_EXPOSURE only stands if it clearly beats the generic analytical intent."""
        generic = scores.get(Intent.PORTFOLIO_ANALYTICS.value, -1.0)
        if intent == Intent.SECTOR_EXPOSURE and score - generic < th.analytic_pair_margin:
            intent, score = Intent.PORTFOLIO_ANALYTICS, generic
        return IntentResult(intent, score, margin if margin is not None else 0.0, reason, intent, dict(scores))


@dataclass(frozen=True)
class IntentClassifierConfig:
    enabled: bool = True
    model_name: str = DEFAULT_MODEL_NAME
    cache_dir: Path = DEFAULT_CACHE_DIR
    allow_download: bool = False
    thresholds: IntentThresholds = field(default_factory=IntentThresholds)

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "IntentClassifierConfig":
        e = env if env is not None else os.environ
        enabled = str(e.get(ENV_ENABLED, "on")).strip().lower() not in {"off", "0", "false", "no", "disabled"}
        return cls(
            enabled=enabled,
            model_name=e.get(ENV_MODEL) or DEFAULT_MODEL_NAME,
            cache_dir=Path(e.get(ENV_CACHE_DIR) or DEFAULT_CACHE_DIR),
            allow_download=str(e.get(ENV_ALLOW_DOWNLOAD, "0")).strip().lower() in {"1", "true", "yes", "on"},
            thresholds=IntentThresholds.from_env(e),
        )

    def with_enabled(self, enabled: bool) -> "IntentClassifierConfig":
        return replace(self, enabled=enabled)


# One classifier (and so one loaded model + one prototype cache) per process.
_shared: Dict[Any, LocalIntentClassifier] = {}
_shared_lock = threading.Lock()


def build_default_classifier(config: Optional[IntentClassifierConfig] = None) -> Optional[LocalIntentClassifier]:
    """
    The process-wide classifier built from environment configuration, or None when disabled
    (``PORTFOLIO_INTENT_CLASSIFIER=off``). Nothing is loaded until the first message is classified.
    """
    cfg = config or IntentClassifierConfig.from_env()
    if not cfg.enabled:
        return None
    key = (cfg.model_name, str(cfg.cache_dir), cfg.allow_download, repr(cfg.thresholds))
    with _shared_lock:
        if key not in _shared:
            embedder = FastEmbedder(cfg.model_name, cfg.cache_dir, cfg.allow_download)
            _shared[key] = LocalIntentClassifier(embedder=embedder, thresholds=cfg.thresholds)
        return _shared[key]
