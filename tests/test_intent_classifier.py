"""
Tests for the local semantic intent classifier and its integration in PortfolioAgent.

All tests use a MOCKED embedding model (one axis per intent, vectors written by hand), so they
verify the decision policy, routing and safety properties without downloading or running the real
model and without any Gemini call. The real model is covered separately by the optional
tests/smoke_intent_model.py and by scripts/calibrate_intents.py.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, Sequence
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

import numpy as np

from intent_validation_set import HOLDOUT, VALIDATION
from portfolio_agent.agent import CONVERSATION_EXAMPLE_QUESTIONS, PortfolioAgent
from portfolio_agent.database import PortfolioDatabase
from portfolio_agent.financial_verifier import VERIFIED, VerificationResult
from portfolio_agent.intent_classifier import (
    ANALYTICAL,
    CONVERSATIONAL,
    DEFAULT_MIN_SCORE,
    FastEmbedder,
    Intent,
    IntentClassifierConfig,
    IntentClassifierUnavailable,
    IntentThresholds,
    LocalIntentClassifier,
    build_default_classifier,
)
from portfolio_agent.intent_examples import INTENT_EXAMPLES
from portfolio_agent.sql_generator import GeminiSQLGenerator, RuleBasedSQLGenerator
from portfolio_agent.tools import ExposureCalculator, SQLQueryTool

AXES = [i.value for i in Intent]


def vec(**weights: float) -> np.ndarray:
    """A vector over the intent axes. cosine(vec(A=1), vec(A=a, B=b)) == a / sqrt(a^2 + b^2)."""
    v = np.zeros(len(AXES), dtype="float32")
    for name, w in weights.items():
        v[AXES.index(name)] = w
    return v


def mix(**weights: float) -> np.ndarray:
    """Vector whose cosine with each intent's examples is exactly the given score (others ~0)."""
    rest = 1.0 - sum(w * w for w in weights.values())
    assert rest >= 0, "scores too large for an orthonormal mix"
    v = vec(**weights)
    v[AXES.index("UNKNOWN")] += 0.0
    # spare mass goes to a dimension that no example uses, so it does not create a score
    return np.concatenate([v, [np.sqrt(rest)]]).astype("float32")


class FakeEmbedder:
    """Maps known texts to vectors; every example text is a one-hot vector on its intent's axis."""

    def __init__(self, table: Dict[str, np.ndarray]) -> None:
        self.table = dict(table)
        self.calls: list = []

    def __call__(self, texts: Sequence[str]):
        self.calls.append(list(texts))
        out = []
        for t in texts:
            if t.startswith("EX:"):
                out.append(np.concatenate([vec(**{t[3:].split("#")[0]: 1.0}), [0.0]]))
            elif t in self.table:
                v = self.table[t]
                out.append(v if v.shape[0] == len(AXES) + 1 else np.concatenate([v, [0.0]]))
            else:
                raise KeyError(f"unscripted message: {t!r}")
        return np.asarray(out, dtype="float32")


EXAMPLES = {name: [f"EX:{name}#1", f"EX:{name}#2"] for name in AXES}


class _AcceptingVerifier:
    """Test double for routing tests with fake tool results: declares every result verified."""

    def verify(self, result):
        return VerificationResult(VERIFIED, 1)


def make_classifier(table: Dict[str, np.ndarray], thresholds: IntentThresholds | None = None) -> LocalIntentClassifier:
    return LocalIntentClassifier(embedder=FakeEmbedder(table), examples=EXAMPLES, thresholds=thresholds or IntentThresholds())


def make_agent(table, thresholds=None, use_gemini=False, sql_tool=None, exposure_tool=None, db=None):
    clf = make_classifier(table, thresholds)
    sql_tool = sql_tool or MagicMock()
    if sql_tool is not None and isinstance(sql_tool, MagicMock):
        sql_tool.use_gemini = use_gemini
    # Routing tests inject FAKE tool results. The production policy withholds exposure figures when no verifier is
    # available, so these fixtures supply an explicit test double that accepts the fake result; real verification is
    # covered by tests/test_financial_verifier.py and tests/test_answer_verifier.py.
    agent = PortfolioAgent(db=db or MagicMock(), sql_tool=sql_tool, exposure_tool=exposure_tool or MagicMock(), intent_classifier=clf, financial_verifier=_AcceptingVerifier())
    return agent, clf


# ======================================================================================
# Decision policy
# ======================================================================================
class TestDecisionPolicy(unittest.TestCase):
    def setUp(self):
        self.clf = make_classifier({})

    def test_confident_conversational_intents(self):
        for intent in ["GREETING", "THANKS", "GOODBYE", "IDENTITY", "CAPABILITIES"]:
            with self.subTest(intent=intent):
                r = self.clf.decide({i: 0.05 for i in AXES} | {intent: 0.85})
                self.assertEqual(r.intent.value, intent)
                self.assertEqual(r.reason, "confident")

    def test_per_intent_thresholds_are_applied(self):
        th = IntentThresholds(min_score={**DEFAULT_MIN_SCORE, "IDENTITY": 0.8, "THANKS": 0.3})
        clf = make_classifier({}, th)
        base = {i: 0.0 for i in AXES}
        self.assertEqual(clf.decide(base | {"IDENTITY": 0.7}).intent, Intent.UNKNOWN)     # below IDENTITY's 0.8
        self.assertEqual(clf.decide(base | {"IDENTITY": 0.85}).intent, Intent.IDENTITY)
        self.assertEqual(clf.decide(base | {"THANKS": 0.35}).intent, Intent.THANKS)        # above THANKS's low 0.3

    def test_low_confidence_returns_unknown(self):
        r = self.clf.decide({i: 0.30 for i in AXES} | {"CAPABILITIES": 0.40})
        self.assertEqual((r.intent, r.reason), (Intent.UNKNOWN, "low_confidence"))
        self.assertEqual(r.best_guess, Intent.CAPABILITIES)

    def test_score_margin_requirement(self):
        base = {i: 0.0 for i in AXES}
        close = self.clf.decide(base | {"IDENTITY": 0.70, "CAPABILITIES": 0.68})
        self.assertEqual((close.intent, close.reason), (Intent.UNKNOWN, "ambiguous"))
        self.assertAlmostEqual(close.margin, 0.02, places=6)
        clear = self.clf.decide(base | {"IDENTITY": 0.70, "CAPABILITIES": 0.60})
        self.assertEqual(clear.intent, Intent.IDENTITY)
        # the required margin is configurable
        strict = LocalIntentClassifier(embedder=FakeEmbedder({}), examples=EXAMPLES, thresholds=IntentThresholds(min_margin=0.2))
        self.assertEqual(strict.decide(base | {"IDENTITY": 0.70, "CAPABILITIES": 0.60}).intent, Intent.UNKNOWN)

    def test_two_analytical_intents_are_not_ambiguous_with_each_other(self):
        base = {i: 0.0 for i in AXES}
        self.assertEqual(self.clf.decide(base | {"PORTFOLIO_ANALYTICS": 0.80, "SECTOR_EXPOSURE": 0.79}).intent, Intent.PORTFOLIO_ANALYTICS)
        self.assertEqual(self.clf.decide(base | {"SECTOR_EXPOSURE": 0.85, "PORTFOLIO_ANALYTICS": 0.70}).intent, Intent.SECTOR_EXPOSURE)
        # SECTOR_EXPOSURE must clearly beat the generic intent, otherwise the generic one is used
        self.assertEqual(self.clf.decide(base | {"SECTOR_EXPOSURE": 0.80, "PORTFOLIO_ANALYTICS": 0.79}).intent, Intent.PORTFOLIO_ANALYTICS)

    def test_analytical_intent_close_to_conversational_is_not_forced(self):
        base = {i: 0.0 for i in AXES}
        r = self.clf.decide(base | {"PORTFOLIO_ANALYTICS": 0.62, "CAPABILITIES": 0.60})
        self.assertEqual(r.intent, Intent.UNKNOWN)

    def test_mixed_conversational_with_analytical_goes_to_analytics(self):
        base = {i: 0.0 for i in AXES}
        r = self.clf.decide(base | {"CAPABILITIES": 0.75, "PORTFOLIO_ANALYTICS": 0.60})
        self.assertEqual((r.intent, r.reason), (Intent.PORTFOLIO_ANALYTICS, "mixed_conversational_and_analytical"))
        r = self.clf.decide(base | {"GREETING": 0.75, "SECTOR_EXPOSURE": 0.70})
        self.assertEqual(r.intent, Intent.SECTOR_EXPOSURE)

    def test_clearly_conversational_message_is_not_treated_as_mixed(self):
        """Sharing some vocabulary with analytical examples must not turn a help request into a database query."""
        base = {i: 0.0 for i in AXES}
        r = self.clf.decide(base | {"CAPABILITIES": 0.95, "PORTFOLIO_ANALYTICS": 0.65})
        self.assertEqual((r.intent, r.reason), (Intent.CAPABILITIES, "confident"))
        # ... but the same analytical score close to the conversational one is mixed
        self.assertEqual(self.clf.decide(base | {"CAPABILITIES": 0.80, "PORTFOLIO_ANALYTICS": 0.65}).intent, Intent.PORTFOLIO_ANALYTICS)

    def test_mixed_with_unconvincing_analytics_is_unresolved_not_dropped(self):
        th = IntentThresholds(min_score={**DEFAULT_MIN_SCORE, "PORTFOLIO_ANALYTICS": 0.7}, mixed_floor=0.5)
        r = make_classifier({}, th).decide({i: 0.0 for i in AXES} | {"CAPABILITIES": 0.78, "PORTFOLIO_ANALYTICS": 0.6})
        self.assertEqual((r.intent, r.reason), (Intent.UNKNOWN, "mixed_unresolved"))

    def test_unsupported_wins_only_without_analytical_content(self):
        base = {i: 0.0 for i in AXES}
        self.assertEqual(self.clf.decide(base | {"UNSUPPORTED": 0.7}).intent, Intent.UNSUPPORTED)
        self.assertEqual(self.clf.decide(base | {"UNSUPPORTED": 0.7, "PORTFOLIO_ANALYTICS": 0.6}).intent, Intent.PORTFOLIO_ANALYTICS)

    def test_vague_examples_win_as_unknown(self):
        r = self.clf.decide({i: 0.0 for i in AXES} | {"UNKNOWN": 0.9})
        self.assertEqual((r.intent, r.reason), (Intent.UNKNOWN, "vague_message"))

    def test_intent_taxonomy(self):
        self.assertEqual({i.value for i in CONVERSATIONAL}, {"GREETING", "THANKS", "GOODBYE", "IDENTITY", "CAPABILITIES"})
        self.assertEqual({i.value for i in ANALYTICAL}, {"PORTFOLIO_ANALYTICS", "SECTOR_EXPOSURE"})
        for needed in ("UNSUPPORTED", "UNKNOWN"):
            self.assertIn(needed, {i.value for i in Intent})


class TestClassifierMechanics(unittest.TestCase):
    def test_cosine_similarity_with_normalised_embeddings(self):
        # Scale must not matter: only direction does.
        clf = make_classifier({"msg": 5.0 * mix(IDENTITY=0.8)})
        r = clf.classify("msg")
        self.assertAlmostEqual(r.scores["IDENTITY"], 0.8, places=5)
        self.assertEqual(r.intent, Intent.IDENTITY)

    def test_model_loads_once_and_prototypes_are_cached(self):
        emb = FakeEmbedder({"a": mix(GREETING=0.9), "b": mix(THANKS=0.9), "c": mix(GOODBYE=0.9)})
        clf = LocalIntentClassifier(embedder=emb, examples=EXAMPLES)
        self.assertFalse(clf.ready)
        for m in ("a", "b", "c", "a"):
            clf.classify(m)
        self.assertTrue(clf.ready)
        proto_batches = [c for c in emb.calls if len(c) > 1]
        self.assertEqual(len(proto_batches), 1, "prototype embeddings must be computed once, not per question")
        self.assertEqual(sum(1 for c in emb.calls if len(c) == 1), 4)  # one embedding per question only

    def test_lazy_nothing_loaded_at_construction(self):
        emb = FakeEmbedder({})
        LocalIntentClassifier(embedder=emb, examples=EXAMPLES)
        self.assertEqual(emb.calls, [])

    def test_initialisation_failure_raises_unavailable_and_is_remembered(self):
        emb = MagicMock(side_effect=RuntimeError("model file corrupt"))
        clf = LocalIntentClassifier(embedder=emb, examples=EXAMPLES)
        for _ in range(3):
            with self.assertRaises(IntentClassifierUnavailable):
                clf.classify("hello there")
        self.assertEqual(emb.call_count, 1, "a broken model is not retried on every request")
        self.assertIn("model file corrupt", clf.failure)
        self.assertIn("download_intent_model", clf.failure)  # setup guidance
        emb.side_effect = None
        clf.reset()  # e.g. after the user downloads the model
        emb.return_value = np.zeros((len(sum(EXAMPLES.values(), [])), 3))
        clf.warm_up()  # no exception: retried after reset

    def test_inference_failure_is_reported_not_guessed(self):
        class Flaky(FakeEmbedder):
            def __call__(self, texts):
                if len(texts) == 1:
                    raise RuntimeError("onnx runtime error")
                return super().__call__(texts)

        clf = LocalIntentClassifier(embedder=Flaky({}), examples=EXAMPLES)
        with self.assertRaises(IntentClassifierUnavailable):
            clf.classify("anything")

    def test_fastembed_not_cached_gives_setup_message_and_never_downloads(self):
        try:
            import fastembed  # noqa: F401
        except ImportError:
            self.skipTest("fastembed not installed")
        with tempfile.TemporaryDirectory() as empty_cache:
            emb = FastEmbedder(cache_dir=empty_cache, allow_download=False)
            with self.assertRaises(IntentClassifierUnavailable) as ctx:
                emb(["hello"])
        self.assertIn("download_intent_model.py", str(ctx.exception))

    def test_prototype_examples_cover_every_intent(self):
        self.assertEqual(set(INTENT_EXAMPLES), set(AXES))
        for name, items in INTENT_EXAMPLES.items():
            self.assertGreaterEqual(len(items), 10, name)
            self.assertEqual(len(items), len(set(items)), f"duplicate examples in {name}")

    def test_prototypes_do_not_leak_into_validation_sets(self):
        norm = lambda t: " ".join(t.lower().replace("?", "").replace(".", "").replace("!", "").replace(",", "").split())  # noqa: E731
        protos = {norm(x) for v in INTENT_EXAMPLES.values() for x in v}
        for name, ds in (("VALIDATION", VALIDATION), ("HOLDOUT", HOLDOUT)):
            leaked = [t for t, _e, cat in ds if cat != "regression" and norm(t) in protos]
            self.assertEqual(leaked, [], f"{name} items also used as prototypes: {leaked}")

    def test_validation_labels_are_valid_intents(self):
        for t, expected, cat in VALIDATION + HOLDOUT:
            Intent(expected)
            self.assertIn(cat, {"positive", "hard_negative", "mixed", "ambiguous", "regression"})
        self.assertEqual(sum(1 for *_x, c in VALIDATION if c == "regression"), 10)  # Q1-Q10

    def test_configuration_from_environment(self):
        self.assertFalse(IntentClassifierConfig.from_env({"PORTFOLIO_INTENT_CLASSIFIER": "off"}).enabled)
        self.assertFalse(IntentClassifierConfig.from_env({"PORTFOLIO_INTENT_CLASSIFIER": "0"}).enabled)
        self.assertTrue(IntentClassifierConfig.from_env({}).enabled)
        self.assertIsNone(build_default_classifier(IntentClassifierConfig.from_env({"PORTFOLIO_INTENT_CLASSIFIER": "off"})))
        cfg = IntentClassifierConfig.from_env({
            "PORTFOLIO_INTENT_THRESHOLDS": '{"min_score": {"identity": 0.7}, "min_margin": 0.1, "mixed_floor": 0.6, "mixed_gap": 0.1}',
            "PORTFOLIO_INTENT_CACHE_DIR": "C:/models",
        })
        self.assertEqual(cfg.thresholds.score_for(Intent.IDENTITY), 0.7)
        self.assertEqual(cfg.thresholds.score_for(Intent.GREETING), DEFAULT_MIN_SCORE["GREETING"])  # untouched ones keep defaults
        self.assertEqual((cfg.thresholds.min_margin, cfg.thresholds.mixed_floor, cfg.thresholds.mixed_gap), (0.1, 0.6, 0.1))
        self.assertEqual(str(cfg.cache_dir).replace("\\", "/"), "C:/models")
        bad = IntentClassifierConfig.from_env({"PORTFOLIO_INTENT_THRESHOLDS": "not json"})
        self.assertEqual(bad.thresholds.min_margin, IntentThresholds().min_margin)  # invalid override ignored
        self.assertFalse(IntentClassifierConfig.from_env({}).allow_download)  # no implicit download at request time

    def test_default_classifier_is_shared_per_process(self):
        cfg = IntentClassifierConfig.from_env({})
        self.assertIs(build_default_classifier(cfg), build_default_classifier(cfg))


# ======================================================================================
# Agent integration (mock tools: nothing real can be called)
# ======================================================================================
class TestAgentRouting(unittest.TestCase):
    def assert_no_tools(self, agent):
        agent.sql_tool.execute.assert_not_called()
        agent.exposure_tool.calculate.assert_not_called()

    def test_greetings_and_acknowledgements_are_answered_locally(self):
        table = {"yo what's up": mix(GREETING=0.85), "cheers, that was great": mix(THANKS=0.8), "laters": mix(GOODBYE=0.8)}
        agent, _ = make_agent(table)
        for text, intent in [("yo what's up", "greeting"), ("cheers, that was great", "thanks"), ("laters", "goodbye")]:
            with self.subTest(text=text):
                r = agent.answer_question(text)
                self.assertEqual((r["status"], r["selected_tool"], r["error"]), ("conversation", None, None))
                self.assertEqual(r["result"]["intent"], intent)
                self.assertEqual(r["question"], text)
        self.assert_no_tools(agent)

    def test_existing_exact_greeting_replies_are_unchanged(self):
        agent, clf = make_agent({})
        r = agent.answer_question("hello")  # handled by the existing rules before the model is even used
        self.assertEqual((r["status"], r["result"]["intent"]), ("conversation", "greeting"))
        self.assertEqual(r["result"]["suggested_questions"], CONVERSATION_EXAMPLE_QUESTIONS)
        self.assertFalse(clf.ready)

    def test_identity_variations(self):
        phrases = ["so who exactly am I talking to", "could you tell me what you're called", "which bot is this"]
        agent, _ = make_agent({p: mix(IDENTITY=0.7 + 0.05 * i) for i, p in enumerate(phrases)})
        for p in phrases:
            r = agent.answer_question(p)
            self.assertEqual((r["status"], r["result"]["intent"], r["selected_tool"]), ("conversation", "identity", None))
            self.assertIn("PL Capital Portfolio Analytics Agent", r["result"]["message"])
            self.assertNotIn("score", str(r).lower())
        self.assert_no_tools(agent)

    def test_capability_paraphrases_describe_only_real_features(self):
        phrases = ["what sort of stuff can this thing handle", "how should i go about using this", "which analyses can I run"]
        agent, _ = make_agent({p: mix(CAPABILITIES=0.75) for p in phrases})
        for p in phrases:
            r = agent.answer_question(p)
            self.assertEqual((r["status"], r["result"]["intent"], r["selected_tool"]), ("conversation", "capabilities", None))
            self.assertGreaterEqual(len(r["result"]["details"]), 5)
            self.assertEqual(r["result"]["suggested_questions"], CONVERSATION_EXAMPLE_QUESTIONS)
        self.assert_no_tools(agent)
        text = " ".join(r["result"]["details"]).lower()
        for claim in ("real-time", "predict", "forecast", "recommend", "buy"):
            self.assertNotIn(claim, text.replace("investment advice", ""))  # nothing the project does not do

    def test_suggested_questions_are_actually_answerable(self):
        """The examples offered to users must work in the deterministic generator / exposure calculator."""
        gen = RuleBasedSQLGenerator()
        for q in CONVERSATION_EXAMPLE_QUESTIONS:
            if "exposure" in q.lower():
                self.assertTrue(PortfolioAgent._is_exposure_question(MagicMock(), q))
            else:
                gen.generate(q)  # raises UnsupportedQuestionError if not supported

    def test_capability_text_reflects_generation_mode(self):
        p = "what can i do around here"
        for use_gemini, needle in ((True, "Gemini"), (False, "rule-based SQL generator only")):
            agent, _ = make_agent({p: mix(CAPABILITIES=0.8)}, use_gemini=use_gemini)
            details = agent.answer_question(p)["result"]["details"]
            self.assertIn(needle, details[-1])

    def test_normal_analytics_reach_sql_tool_with_the_original_question(self):
        q = "Which portfolios hold more than ten securities?"
        agent, _ = make_agent({q: mix(PORTFOLIO_ANALYTICS=0.85)})
        agent.sql_tool.execute.return_value = {"status": "success", "rows": []}
        r = agent.answer_question(q)
        agent.sql_tool.execute.assert_called_once_with(q)
        agent.exposure_tool.calculate.assert_not_called()
        self.assertEqual((r["status"], r["selected_tool"]), ("success", "sql_query"))
        self.assertEqual(r["route_reason"], "Question routed to SQL query tool for database analysis.")  # unchanged contract

    def test_sector_exposure_uses_the_exposure_calculator_never_the_sql_tool(self):
        q = "How is the Alpha Growth Fund spread across industries?"  # no exposure keyword at all
        db = MagicMock()
        db.execute_read_only.return_value = {"rows": [{"portfolio_name": "Alpha Growth Fund"}, {"portfolio_name": "Beta Fund"}]}
        exposure = MagicMock()
        exposure.calculate.return_value = {"status": "success", "exposures": []}
        agent, _ = make_agent({q: mix(SECTOR_EXPOSURE=0.85)}, exposure_tool=exposure, db=db)
        r = agent.answer_question(q)
        exposure.calculate.assert_called_once_with("Alpha Growth Fund")
        agent.sql_tool.execute.assert_not_called()
        self.assertEqual((r["status"], r["selected_tool"]), ("success", "exposure_calculator"))

    def test_keyword_exposure_routing_is_unchanged(self):
        q = "What are the sector exposures for the Tech Innovation Fund?"
        exposure = MagicMock()
        exposure.calculate.return_value = {"status": "success"}
        agent, _ = make_agent({q: mix(PORTFOLIO_ANALYTICS=0.9)}, exposure_tool=exposure)  # even if the classifier says "generic"
        agent.answer_question(q)
        exposure.calculate.assert_called_once_with("Tech Innovation Fund")
        agent.sql_tool.execute.assert_not_called()

    def test_q7_style_market_value_question_still_goes_to_sql_even_if_classified_as_exposure(self):
        q = "For each portfolio show the technology share of total portfolio value"
        agent, _ = make_agent({q: mix(SECTOR_EXPOSURE=0.9)})
        agent.sql_tool.execute.return_value = {"status": "success"}
        agent.answer_question(q)
        agent.sql_tool.execute.assert_called_once_with(q)
        agent.exposure_tool.calculate.assert_not_called()

    def test_mixed_greeting_plus_analytics_preserves_the_question(self):
        q = "Hey there, could you tell me how many portfolios are active?"
        agent, _ = make_agent({q: mix(GREETING=0.62, PORTFOLIO_ANALYTICS=0.74)})
        agent.sql_tool.execute.return_value = {"status": "success"}
        agent.answer_question(q)
        agent.sql_tool.execute.assert_called_once_with(q)  # whole message, nothing dropped

    def test_mixed_help_plus_analytics_preserves_the_question(self):
        q = "can you help me with the Growth Equity Fund holdings"
        agent, _ = make_agent({q: mix(CAPABILITIES=0.78, PORTFOLIO_ANALYTICS=0.62)})
        agent.sql_tool.execute.return_value = {"status": "success"}
        agent.answer_question(q)
        agent.sql_tool.execute.assert_called_once_with(q)

    def test_mixed_help_plus_exposure_reaches_exposure_calculator(self):
        q = "Can you help me calculate Alpha Growth Fund exposure?"
        db = MagicMock()
        db.execute_read_only.return_value = {"rows": [{"portfolio_name": "Alpha Growth Fund"}]}
        exposure = MagicMock()
        exposure.calculate.return_value = {"status": "success"}
        agent, _ = make_agent({q: mix(CAPABILITIES=0.7, SECTOR_EXPOSURE=0.68)}, exposure_tool=exposure, db=db)
        agent.answer_question(q)
        exposure.calculate.assert_called_once_with("Alpha Growth Fund")

    def test_unresolved_mixed_message_asks_for_clarification_instead_of_guessing(self):
        q = "what can you do and also something about funds maybe"
        th = IntentThresholds(min_score={**DEFAULT_MIN_SCORE, "PORTFOLIO_ANALYTICS": 0.7})
        agent, _ = make_agent({q: mix(CAPABILITIES=0.8, PORTFOLIO_ANALYTICS=0.6)}, thresholds=th)
        r = agent.answer_question(q)
        self.assertEqual((r["status"], r["result"]["intent"]), ("conversation", "clarification"))
        self.assertTrue(r["result"]["suggested_questions"])
        self.assert_no_tools(agent)

    def test_unrelated_questions_are_rejected_locally(self):
        qs = ["who won the cricket final", "what's a good pasta recipe", "how far is the moon"]
        agent, _ = make_agent({q: mix(UNSUPPORTED=0.7) for q in qs})
        for q in qs:
            r = agent.answer_question(q)
            self.assertEqual((r["status"], r["selected_tool"], r["result"]), ("unsupported", None, None))
            self.assertIn("outside what I can help with", r["error"])
        self.assert_no_tools(agent)

    def test_ambiguous_and_low_confidence_questions_get_clarification(self):
        table = {"more please": mix(UNKNOWN=0.9), "hmm what about that": mix(GREETING=0.2, UNSUPPORTED=0.18), "identity or help": mix(IDENTITY=0.7, CAPABILITIES=0.69)}
        agent, _ = make_agent(table)
        for q in table:
            with self.subTest(q=q):
                r = agent.answer_question(q)
                self.assertEqual((r["status"], r["selected_tool"], r["result"]["intent"]), ("conversation", None, "clarification"))
                self.assertGreaterEqual(len(r["result"]["suggested_questions"]), 3)
        self.assert_no_tools(agent)

    def test_responses_never_expose_similarity_scores(self):
        q = "what things are you able to do"
        agent, _ = make_agent({q: mix(CAPABILITIES=0.8123)})
        text = str(agent.answer_question(q))
        self.assertNotIn("0.81", text)
        self.assertNotIn("similarity", text.lower())
        self.assertNotIn("confidence", text.lower())

    def test_response_contract_keys_are_stable(self):
        table = {"x1": mix(IDENTITY=0.8), "x2": mix(UNSUPPORTED=0.8), "x3": mix(PORTFOLIO_ANALYTICS=0.8)}
        agent, _ = make_agent(table)
        agent.sql_tool.execute.return_value = {"status": "success"}
        for q in table:
            self.assertEqual(set(agent.answer_question(q)), {"status", "question", "selected_tool", "route_reason", "result", "error"})

    def test_empty_input_is_still_validated_first(self):
        agent, clf = make_agent({})
        self.assertEqual(agent.answer_question("   ")["status"], "error")
        self.assertFalse(clf.ready)


# ======================================================================================
# Gemini is never involved in local decisions (real SQLQueryTool, mocked Gemini client)
# ======================================================================================
class TestNoGeminiForLocalDecisions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = PortfolioDatabase()
        if not cls.db.is_initialized():
            cls.db.initialize_database()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def build(self, table, thresholds=None):
        self.client = MagicMock()
        gemini = GeminiSQLGenerator(client=self.client, db=self.db)
        tool = SQLQueryTool(db=self.db, gemini_generator=gemini, use_gemini=True)
        clf = make_classifier(table, thresholds)
        return PortfolioAgent(db=self.db, sql_tool=tool, intent_classifier=clf), tool

    def test_conversational_unsupported_and_uncertain_messages_never_call_gemini(self):
        table = {
            "who are you really": mix(IDENTITY=0.8), "what can I do here": mix(CAPABILITIES=0.8), "greetings friend": mix(GREETING=0.8),
            "ta very much": mix(THANKS=0.8), "adios": mix(GOODBYE=0.8), "whats the score": mix(UNSUPPORTED=0.8),
            "tell me more": mix(UNKNOWN=0.9), "something low": mix(GREETING=0.1),
        }
        agent, tool = self.build(table)
        with patch.object(tool, "execute", wraps=tool.execute) as spy:
            for q in table:
                self.assertNotEqual(agent.answer_question(q)["status"], "success", q)
            spy.assert_not_called()  # the SQL tool (and therefore Gemini) is never reached
        self.client.models.generate_content.assert_not_called()

    def test_analytical_questions_still_use_gemini_when_enabled(self):
        """Behaviour for real analytical questions is preserved: Gemini is asked, then falls back as before."""
        q = "Which portfolios hold the most technology stocks?"
        agent, tool = self.build({q: mix(PORTFOLIO_ANALYTICS=0.9)})
        self.client.models.generate_content.side_effect = RuntimeError("network down")
        with patch.object(GeminiSQLGenerator, "is_configured", True):
            r = agent.answer_question(q)
        self.client.models.generate_content.assert_called()
        self.assertTrue(r["result"]["fallback_used"])

    def test_exposure_is_never_calculated_by_gemini(self):
        q = "What are the sector exposures for the Tech Innovation Fund?"
        agent, _ = self.build({q: mix(SECTOR_EXPOSURE=0.9)})
        r = agent.answer_question(q)
        self.assertEqual(r["selected_tool"], "exposure_calculator")
        self.client.models.generate_content.assert_not_called()


# ======================================================================================
# Failure and configuration behaviour
# ======================================================================================
class TestFailSafe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = PortfolioDatabase()
        if not cls.db.is_initialized():
            cls.db.initialize_database()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def broken_agent(self, use_gemini=True):
        self.client = MagicMock()
        gemini = GeminiSQLGenerator(client=self.client, db=self.db)
        tool = SQLQueryTool(db=self.db, gemini_generator=gemini, use_gemini=use_gemini)
        clf = LocalIntentClassifier(embedder=MagicMock(side_effect=RuntimeError("model missing")), examples=EXAMPLES)
        return PortfolioAgent(db=self.db, sql_tool=tool, intent_classifier=clf), clf

    def test_initialisation_failure_never_triggers_gemini(self):
        agent, _ = self.broken_agent()
        with patch.object(GeminiSQLGenerator, "is_configured", True):
            for q in ("What can you do?", "What's the weather today?", "How many portfolios do we have in total?"):
                agent.answer_question(q)
        self.client.models.generate_content.assert_not_called()

    def test_analytics_still_work_deterministically_when_classifier_is_unavailable(self):
        agent, _ = self.broken_agent()
        with patch.object(GeminiSQLGenerator, "is_configured", True):
            r = agent.answer_question("How many portfolios do we have in total?")
        self.assertEqual((r["status"], r["selected_tool"]), ("success", "sql_query"))
        self.assertEqual(r["result"]["rows"][0]["COUNT(*)"], 13)
        self.assertEqual(r["result"]["generation_method"], "rule_based")
        self.assertTrue(r["result"]["fallback_used"])
        self.assertIn("intent classifier is unavailable", r["result"]["fallback_reason"])

    def test_exposure_still_works_when_classifier_is_unavailable(self):
        agent, _ = self.broken_agent()
        r = agent.answer_question("What are the sector exposures for the Tech Innovation Fund?")
        self.assertEqual((r["status"], r["selected_tool"]), ("success", "exposure_calculator"))

    def test_existing_exact_greetings_still_work_without_the_model(self):
        agent, clf = self.broken_agent()
        self.assertEqual(agent.answer_question("hi")["result"]["intent"], "greeting")
        self.assertFalse(clf.ready)

    def test_prepare_reports_a_setup_problem_without_raising(self):
        agent, _ = self.broken_agent()
        problem = agent.prepare_intent_classifier()
        self.assertIn("download_intent_model", problem)
        ok, _ = make_agent({})
        self.assertIsNone(ok.prepare_intent_classifier())

    def test_no_llm_mode_is_unaffected_by_classifier_failure(self):
        agent, _ = self.broken_agent(use_gemini=False)
        r = agent.answer_question("How many portfolios do we have in total?")
        self.assertEqual(r["result"]["generation_method"], "rule_based")
        self.assertFalse(r["result"]["fallback_used"])

    def test_classifier_can_be_disabled_for_debugging(self):
        """Disabled = the previous routing, byte for byte: everything non-greeting goes to the SQL tool."""
        sql_tool = MagicMock()
        sql_tool.execute.return_value = {"status": "unsupported", "error": "x"}
        agent = PortfolioAgent(db=MagicMock(), sql_tool=sql_tool, exposure_tool=MagicMock(), intent_classifier=None)
        for q in ("What can you do?", "What's the weather today?"):
            agent.answer_question(q)
        self.assertEqual(sql_tool.execute.call_count, 2)
        with patch.dict(os.environ, {"PORTFOLIO_INTENT_CLASSIFIER": "off"}):
            self.assertIsNone(PortfolioAgent(db=MagicMock(), sql_tool=MagicMock(), exposure_tool=MagicMock()).intent_classifier)


# ======================================================================================
# Existing behaviour is preserved (real database, real tools, classifier says "analytical")
# ======================================================================================
class TestExistingBehaviourPreserved(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = PortfolioDatabase()
        if not cls.db.is_initialized():
            cls.db.initialize_database()
        import json

        cls.ground_truth = json.loads((PROJECT_ROOT / "ground_truth_dataset.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def questions(self):
        return {int(q["id"]): q["question"] for q in self.ground_truth["questions"]} if "questions" in self.ground_truth else {}

    def test_q1_to_q10_give_identical_results_with_and_without_the_classifier(self):
        qs = {int(k): v for k, v in self.questions().items()}
        self.assertEqual(sorted(qs), list(range(1, 11)))
        table = {q: mix(SECTOR_EXPOSURE=0.9) if n >= 9 else mix(PORTFOLIO_ANALYTICS=0.9) for n, q in qs.items()}
        with_clf = PortfolioAgent(db=self.db, sql_tool=SQLQueryTool(db=self.db, use_gemini=False), intent_classifier=make_classifier(table))
        without = PortfolioAgent(db=self.db, sql_tool=SQLQueryTool(db=self.db, use_gemini=False), intent_classifier=None)
        for n, q in qs.items():
            with self.subTest(q=n):
                self.assertEqual(with_clf.answer_question(q), without.answer_question(q))
                self.assertEqual(with_clf.answer_question(q)["status"], "success")

    def test_exposure_results_match_the_calculator_exactly(self):
        q = "Calculate the sector exposure breakdown for international equity"
        agent = PortfolioAgent(db=self.db, sql_tool=SQLQueryTool(db=self.db, use_gemini=False), intent_classifier=make_classifier({q: mix(SECTOR_EXPOSURE=0.9)}))
        r = agent.answer_question(q)
        calculated = ExposureCalculator(db=self.db).calculate("international equity")
        verification = r["result"].pop("verification")  # added by the independent verifier; everything else is untouched
        self.assertEqual(r["result"].pop("answer_verification")["status"], "verified")  # added by the answer verifier
        self.assertEqual(r["result"], calculated)
        self.assertEqual(verification["status"], "verified")

    def test_sql_safety_is_untouched(self):
        q = "Delete all the holdings from the database"
        agent = PortfolioAgent(db=self.db, sql_tool=SQLQueryTool(db=self.db, use_gemini=False), intent_classifier=make_classifier({q: mix(PORTFOLIO_ANALYTICS=0.9)}))
        before = self.db.execute_read_only("SELECT COUNT(*) AS n FROM holdings")["rows"][0]["n"]
        r = agent.answer_question(q)
        self.assertNotEqual(r["status"], "success")  # still rejected by the existing pipeline
        self.assertEqual(self.db.execute_read_only("SELECT COUNT(*) AS n FROM holdings")["rows"][0]["n"], before)


if __name__ == "__main__":
    unittest.main()
