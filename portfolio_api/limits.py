"""
Request counters and a small revocation list for the hosted API.

Two interchangeable stores implement the same four operations (``hit``, ``reset``, ``mark``, ``is_marked``):

* ``MemoryStore`` keeps everything in the current process. On a serverless platform each instance has its own copy,
  so it only limits what *one instance* sees. It is a best-effort brake, **not** a deployment-wide guarantee.
* ``UpstashStore`` talks to an Upstash Redis database over its REST API (standard library only, no new dependency)
  and is shared by every instance. It is used only when ``UPSTASH_REDIS_REST_URL`` and ``UPSTASH_REDIS_REST_TOKEN``
  are both set. If it is unreachable the call falls back to the per-instance store and logs a warning.

No value passed to these classes (client hashes, session ids) is ever a secret, and the REST token is never logged.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

_KEY_PREFIX = "plcap:"


@dataclass(frozen=True)
class Decision:
    """Outcome of counting one request: allowed or not, how many remain in the window, seconds until it resets."""

    allowed: bool
    remaining: int
    retry_after: int


class MemoryStore:
    """Per-process fixed-window counters and expiring marks (see the module docstring for its limits)."""

    shared = False

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._counters: Dict[str, List[float]] = {}   # key -> [count, window_end]
        self._marks: Dict[str, float] = {}            # key -> expiry

    def _prune(self, now: float) -> None:
        self._counters = {k: v for k, v in self._counters.items() if v[1] > now}
        self._marks = {k: v for k, v in self._marks.items() if v > now}

    def hit(self, key: str, limit: int, window: int) -> Decision:
        now = self._clock()
        with self._lock:
            if len(self._counters) + len(self._marks) > 4096:
                self._prune(now)
            slot = self._counters.get(key)
            if slot is None or now >= slot[1]:
                slot = [0, now + window]
                self._counters[key] = slot
            slot[0] += 1
            count, window_end = int(slot[0]), slot[1]
        return Decision(count <= limit, max(0, limit - count), max(1, math.ceil(window_end - now)))

    def reset(self, key: str) -> None:
        with self._lock:
            self._counters.pop(key, None)

    def mark(self, key: str, ttl: int) -> None:
        with self._lock:
            self._marks[key] = self._clock() + max(1, ttl)

    def is_marked(self, key: str) -> bool:
        with self._lock:
            expiry = self._marks.get(key)
            if expiry is None:
                return False
            if expiry <= self._clock():
                del self._marks[key]
                return False
            return True


class UpstashStore:
    """Shared store backed by Upstash Redis (REST). Falls back to a per-instance store when the service is unreachable."""

    shared = True

    def __init__(
        self,
        url: str,
        token: str,
        *,
        fallback: Optional[MemoryStore] = None,
        timeout: float = 2.0,
        opener: Optional[Callable[..., Any]] = None,
    ) -> None:
        url = url.strip().rstrip("/")
        if not url.lower().startswith("https://"):
            raise ValueError("UPSTASH_REDIS_REST_URL must be an https:// URL.")
        self._url = url
        self._token = token.strip()
        self._timeout = timeout
        self._fallback = fallback or MemoryStore()
        self._open = opener or urllib.request.urlopen

    def _post(self, path: str, payload: Any) -> Any:
        request = urllib.request.Request(
            self._url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
            method="POST",
        )
        with self._open(request, timeout=self._timeout) as response:  # noqa: S310 - https enforced in __init__
            return json.loads(response.read().decode("utf-8"))

    def hit(self, key: str, limit: int, window: int) -> Decision:
        redis_key = _KEY_PREFIX + key
        try:
            result = self._post("/pipeline", [["INCR", redis_key], ["EXPIRE", redis_key, window, "NX"], ["TTL", redis_key]])
            count, ttl = int(result[0]["result"]), int(result[2]["result"])
            return Decision(count <= limit, max(0, limit - count), max(1, ttl if ttl > 0 else window))
        except Exception as exc:  # network, auth, parsing: never let the limiter take the API down
            logger.warning("Shared rate-limit store unavailable (%s); using the per-instance limiter.", type(exc).__name__)
            return self._fallback.hit(key, limit, window)

    def reset(self, key: str) -> None:
        self._fallback.reset(key)
        try:
            self._post("", ["DEL", _KEY_PREFIX + key])
        except Exception as exc:
            logger.warning("Shared rate-limit store unavailable (%s) while resetting a counter.", type(exc).__name__)

    def mark(self, key: str, ttl: int) -> None:
        self._fallback.mark(key, ttl)
        try:
            self._post("", ["SET", _KEY_PREFIX + key, "1", "EX", max(1, ttl)])
        except Exception as exc:
            logger.warning("Shared store unavailable (%s) while recording a sign-out.", type(exc).__name__)

    def is_marked(self, key: str) -> bool:
        if self._fallback.is_marked(key):
            return True
        try:
            return int(self._post("", ["EXISTS", _KEY_PREFIX + key])["result"]) > 0
        except Exception as exc:
            logger.warning("Shared store unavailable (%s) while checking a sign-out.", type(exc).__name__)
            return False


def store_from_env(env: Mapping[str, str]) -> "MemoryStore | UpstashStore":
    """Shared Upstash store when both variables are set, otherwise the per-instance in-memory store."""
    url = (env.get("UPSTASH_REDIS_REST_URL") or "").strip()
    token = (env.get("UPSTASH_REDIS_REST_TOKEN") or "").strip()
    if url and token:
        try:
            return UpstashStore(url, token)
        except ValueError as exc:
            logger.error("Ignoring the Upstash configuration: %s", exc)
    return MemoryStore()


def _int(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(str(env.get(name, "")).strip())
    except ValueError:
        return default
    return min(high, max(low, value))


@dataclass(frozen=True)
class RequestLimits:
    """Tunable limits. ``ask_per_day`` of 0 means no daily cap. Defaults suit a handful of reviewers."""

    login_attempts: int = 5          # attempts per client per window
    login_global: int = 60           # attempts across all clients per window
    login_window: int = 600          # seconds
    ask_per_minute: int = 15         # /api/ask per session (each question may call Gemini)
    api_per_minute: int = 120        # every other protected endpoint per session
    ask_per_day: int = 0

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "RequestLimits":
        d = cls()
        return cls(
            login_attempts=_int(env, "PORTFOLIO_LOGIN_MAX_ATTEMPTS", d.login_attempts, 1, 100),
            login_global=_int(env, "PORTFOLIO_LOGIN_GLOBAL_MAX_ATTEMPTS", d.login_global, 1, 10_000),
            login_window=_int(env, "PORTFOLIO_LOGIN_WINDOW_SECONDS", d.login_window, 30, 86_400),
            ask_per_minute=_int(env, "PORTFOLIO_ASK_PER_MINUTE", d.ask_per_minute, 1, 600),
            api_per_minute=_int(env, "PORTFOLIO_API_PER_MINUTE", d.api_per_minute, 1, 6_000),
            ask_per_day=_int(env, "PORTFOLIO_ASK_PER_DAY", d.ask_per_day, 0, 1_000_000),
        )
