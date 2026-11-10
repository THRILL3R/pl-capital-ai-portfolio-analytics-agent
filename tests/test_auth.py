"""
Tests for reviewer access control, request limits and the Gemini-enabled hosted path.

Hermetic: tests/__init__.py clears API keys; Gemini is always a mock client (no network). The access code, session secret and
"Gemini key" below are throwaway test values. These tests skip when FastAPI is not installed.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from fastapi.testclient import TestClient

    from portfolio_api import auth as auth_module
    from portfolio_api.app import create_app
    from portfolio_api.auth import COOKIE_NAME, DISABLED, ENABLED, MISCONFIGURED, AuthManager
    from portfolio_api.limits import MemoryStore, RequestLimits, UpstashStore, store_from_env
    from portfolio_api.service import PortfolioService

    HAVE_FASTAPI = True
except ImportError:  # pragma: no cover - depends on environment
    HAVE_FASTAPI = False

from portfolio_agent.database import PortfolioDatabase
from portfolio_agent.sql_generator import GeminiSQLGenerator

CODE = "reviewer-access-code-4f9c1b7e"
SECRET = "session-signing-secret-0123456789abcdef0123"
GEMINI_KEY = "test-only-placeholder-gemini-key-0123456789"   # deliberately not shaped like a real Google key
ASK_Q = "How many portfolios do we have in total?"
PROTECTED = [
    ("POST", "/api/ask", {"question": ASK_Q}),
    ("POST", "/api/exposure", {"portfolio_name": "International Equity Fund"}),
    ("POST", "/api/scenario", {"portfolio_name": "International Equity Fund", "scenario_id": "severe_market_stress"}),
    ("GET", "/api/scenarios", None),
    ("GET", "/api/portfolios", None),
    ("GET", "/api/portfolios/7", None),
    ("GET", "/api/evaluation", None),
]


class Clock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def call(client, method, path, body=None, **kw):
    return client.request(method, path, json=body, **kw) if body is not None else client.request(method, path, **kw)


def ensure_database() -> None:
    db = PortfolioDatabase()
    if not db.is_initialized():
        db.initialize_database()
    db.close()


def gemini_reply(sql: str, parameters=(), description="test"):
    client = MagicMock()
    client.models.generate_content.return_value = MagicMock(text=json.dumps({"sql": sql, "parameters": list(parameters), "description": description}))
    return client


@unittest.skipUnless(HAVE_FASTAPI, "FastAPI is not installed")
class Base(unittest.TestCase):
    use_llm = False

    @classmethod
    def setUpClass(cls):
        ensure_database()

    def setUp(self):
        sleep = patch("portfolio_api.app.time.sleep")  # skip the small per-failure delay
        sleep.start()
        self.addCleanup(sleep.stop)
        self.clock = Clock()
        self.service = PortfolioService(use_llm=self.use_llm, use_intent_classifier=False)
        self.addCleanup(self.service.close)

    def make(self, *, base_url="http://testserver", limits=None, store=None, manager=None, **auth_kwargs):
        store = store or MemoryStore(clock=self.clock)
        manager = manager or AuthManager(CODE, SECRET, clock=self.clock, revocations=store, **auth_kwargs)
        app = create_app(self.service, static_dir=Path(tempfile.gettempdir()) / "no-such-dist", auth=manager, store=store, limits=limits or RequestLimits())
        self.manager, self.store = manager, store
        return TestClient(app, base_url=base_url)

    def login(self, client, code=CODE, **kw):
        return client.post("/api/auth/login", json={"code": code}, **kw)


# ----------------------------------------------------------------------------- the manager itself


@unittest.skipUnless(HAVE_FASTAPI, "FastAPI is not installed")
class TestAuthManager(unittest.TestCase):
    def new(self, code=CODE, secret=SECRET, **kw):
        return AuthManager(code, secret, clock=kw.pop("clock", Clock()), **kw)

    def test_modes(self):
        self.assertEqual(self.new().mode, ENABLED)
        self.assertEqual(AuthManager(None, None).mode, DISABLED)
        self.assertEqual(AuthManager(None, None, required=True).mode, MISCONFIGURED)

    def test_only_one_value_set_fails_closed(self):
        self.assertEqual(AuthManager(CODE, None).mode, MISCONFIGURED)
        self.assertEqual(AuthManager(None, SECRET).mode, MISCONFIGURED)

    def test_weak_or_reused_secrets_are_rejected(self):
        self.assertEqual(self.new(code="short").mode, MISCONFIGURED)
        self.assertEqual(self.new(secret="too-short-secret").mode, MISCONFIGURED)
        self.assertEqual(self.new(code=SECRET).mode, MISCONFIGURED)                        # secret reused as the code
        self.assertEqual(self.new(secret=GEMINI_KEY + "x" * 5, forbidden_secrets=[GEMINI_KEY + "x" * 5]).mode, MISCONFIGURED)
        self.assertEqual(self.new(code=GEMINI_KEY, forbidden_secrets=[GEMINI_KEY]).mode, MISCONFIGURED)
        self.assertEqual(self.new(forbidden_secrets=[GEMINI_KEY]).mode, ENABLED)           # a different Gemini key is fine

    def test_from_env_never_accepts_the_gemini_key_as_the_session_secret(self):
        env = {"REVIEWER_ACCESS_CODE": CODE, "SESSION_SECRET": GEMINI_KEY + "-pad-to-32-characters", "GEMINI_API_KEY": GEMINI_KEY + "-pad-to-32-characters"}
        self.assertEqual(AuthManager.from_env(env).mode, MISCONFIGURED)

    def test_problem_text_never_contains_a_secret_value(self):
        for manager in (self.new(code="short"), self.new(secret="too-short"), self.new(code=SECRET)):
            self.assertNotIn(CODE, manager.problem or "")
            self.assertNotIn(SECRET, manager.problem or "")

    def test_code_check_is_constant_time_and_exact(self):
        manager = self.new()
        self.assertTrue(manager.verify_code(CODE))
        for wrong in ("", CODE[:-1], CODE + "x", CODE.upper(), " " + CODE, "x" * 5000):
            self.assertFalse(manager.verify_code(wrong), wrong[:20])
        with patch.object(auth_module.hmac, "compare_digest", wraps=auth_module.hmac.compare_digest) as spy:
            manager.verify_code("whatever")
        self.assertTrue(spy.called)

    def test_token_round_trip_and_contents(self):
        manager = self.new()
        token = manager.issue_token()
        session = manager.verify_token(token)
        self.assertIsNotNone(session)
        self.assertEqual(session["exp"] - session["iat"], manager.ttl)
        self.assertNotIn(CODE, token)
        self.assertNotIn(SECRET, token)

    def test_tampered_or_malformed_tokens_are_rejected(self):
        manager = self.new()
        payload, signature = manager.issue_token().split(".")
        forged = auth_module._b64(json.dumps({"v": 1, "sid": "x", "iat": 1, "exp": 9_999_999_999, "ck": "x"}).encode())
        for bad in (None, "", "garbage", "a.b.c", payload, payload + ".", "." + signature, payload + "." + signature[:-2] + "AA", forged + "." + signature, "x" * 2000):
            self.assertIsNone(manager.verify_token(bad), str(bad)[:30])

    def test_a_token_signed_with_another_secret_is_rejected(self):
        other = AuthManager(CODE, "a-completely-different-secret-0123456789ab", clock=Clock())
        self.assertIsNone(self.new().verify_token(other.issue_token()))

    def test_expired_and_future_tokens_are_rejected(self):
        clock = Clock()
        manager = self.new(clock=clock, ttl_seconds=600)
        token = manager.issue_token()
        clock.advance(599)
        self.assertIsNotNone(manager.verify_token(token))
        clock.advance(2)
        self.assertIsNone(manager.verify_token(token))
        later = self.new(clock=Clock(clock.now + 10_000), ttl_seconds=600).issue_token()   # issued far in the future
        self.assertIsNone(manager.verify_token(later))

    def test_rotating_the_access_code_invalidates_existing_sessions(self):
        token = self.new().issue_token()
        self.assertIsNone(self.new(code="a-different-reviewer-code-99").verify_token(token))

    def test_ttl_is_clamped(self):
        self.assertEqual(self.new(ttl_seconds=1).ttl, auth_module.MIN_TTL_SECONDS)
        self.assertEqual(self.new(ttl_seconds=10**9).ttl, auth_module.MAX_TTL_SECONDS)

    def test_disabled_manager_never_issues_or_accepts(self):
        manager = AuthManager(None, None)
        self.assertFalse(manager.verify_code(CODE))
        self.assertIsNone(manager.verify_token("anything.at-all"))


# ----------------------------------------------------------------------------- endpoints require a session


class TestProtectedEndpoints(Base):
    def test_every_data_endpoint_rejects_requests_without_a_session(self):
        client = self.make()
        for method, path, body in PROTECTED:
            with self.subTest(path):
                response = call(client, method, path, body)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json(), {"detail": "Authentication required."})

    def test_invalid_session_cookies_are_rejected(self):
        client = self.make()
        good = self.manager.issue_token()
        for bad in ("garbage", good[:-3] + "AAA", good.split(".")[0] + ".", ""):
            client.cookies.clear()
            if bad:
                client.cookies.set(COOKIE_NAME, bad)
            for method, path, body in PROTECTED:
                self.assertEqual(call(client, method, path, body).status_code, 401, (bad[:12], path))

    def test_an_expired_session_is_rejected_and_the_session_endpoint_says_so(self):
        client = self.make()
        self.assertEqual(self.login(client).status_code, 200)
        self.assertEqual(client.get("/api/portfolios").status_code, 200)
        self.clock.advance(self.manager.ttl + 1)
        self.assertEqual(client.get("/api/portfolios").status_code, 401)
        self.assertEqual(client.get("/api/auth/session").json(), {"auth_required": True, "authenticated": False})

    def test_authenticated_requests_work_on_every_endpoint(self):
        client = self.make()
        self.assertEqual(self.login(client).status_code, 200)
        for method, path, body in PROTECTED:
            with self.subTest(path):
                response = call(client, method, path, body)
                self.assertEqual(response.status_code, 200, response.text[:200])
        self.assertEqual(len(client.get("/api/portfolios").json()), 13)
        self.assertEqual(client.post("/api/exposure", json={"portfolio_name": "International Equity Fund"}).json()["verification"]["status"], "verified")

    def test_a_missing_portfolio_is_a_404_only_after_authentication(self):
        client = self.make()
        self.assertEqual(client.get("/api/portfolios/9999").status_code, 401)
        self.login(client)
        self.assertEqual(client.get("/api/portfolios/9999").status_code, 404)

    def test_health_is_minimal_without_a_session_and_full_with_one(self):
        client = self.make()
        self.assertEqual(client.get("/api/health").json(), {"status": "ok"})
        self.login(client)
        full = client.get("/api/health").json()
        self.assertEqual(full["dataset_rows"], 603)
        self.assertIn("gemini_configured", full)

    def test_session_endpoint_reports_the_state(self):
        client = self.make()
        self.assertEqual(client.get("/api/auth/session").json(), {"auth_required": True, "authenticated": False})
        self.login(client)
        self.assertEqual(client.get("/api/auth/session").json(), {"auth_required": True, "authenticated": True})

    def test_with_access_control_off_nothing_changes(self):
        client = self.make(manager=AuthManager(None, None))
        self.assertEqual(client.get("/api/auth/session").json(), {"auth_required": False, "authenticated": True})
        self.assertEqual(client.get("/api/portfolios").status_code, 200)
        self.assertEqual(client.get("/api/health").json()["dataset_rows"], 603)

    def test_a_misconfigured_app_serves_no_data_at_all(self):
        client = self.make(manager=AuthManager(None, None, required=True))
        for method, path, body in PROTECTED:
            self.assertEqual(call(client, method, path, body).status_code, 503, path)
        self.assertEqual(self.login(client).status_code, 503)
        self.assertEqual(client.get("/api/health").json(), {"status": "ok"})


# ----------------------------------------------------------------------------- login, cookie, logout


class TestLoginAndLogout(Base):
    def test_correct_code_is_accepted_and_sets_a_hardened_cookie(self):
        client = self.make()
        response = self.login(client)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"authenticated": True, "auth_required": True, "expires_in": self.manager.ttl})
        header = response.headers["set-cookie"].lower()
        for flag in ("httponly", "samesite=strict", "path=/", f"max-age={self.manager.ttl}"):
            self.assertIn(flag, header)
        self.assertNotIn("secure", header.replace("samesite", ""))   # plain http (local use)

    def test_cookie_is_secure_on_https_and_behind_a_proxy(self):
        self.assertIn("secure", self.login(self.make(base_url="https://testserver")).headers["set-cookie"].lower())
        client = self.make()
        self.assertIn("secure", self.login(client, headers={"x-forwarded-proto": "https"}).headers["set-cookie"].lower())
        self.assertIn("secure", self.login(self.make(cookie_secure=True)).headers["set-cookie"].lower())

    def test_wrong_codes_are_rejected_without_a_cookie(self):
        client = self.make()
        for wrong in ("wrong-code-entirely", CODE[:-1], CODE.upper(), "x"):
            response = self.login(client, wrong)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json(), {"detail": "Invalid access code."})
            self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(client.get("/api/portfolios").status_code, 401)

    def test_empty_missing_or_oversized_codes_are_invalid_requests(self):
        client = self.make()
        for body in ({"code": ""}, {}, {"code": "x" * 300}, {"code": 5}):
            self.assertEqual(client.post("/api/auth/login", json=body).status_code, 422)

    def test_logout_clears_the_cookie_and_ends_access(self):
        client = self.make()
        self.login(client)
        self.assertEqual(client.get("/api/portfolios").status_code, 200)
        response = client.post("/api/auth/logout")
        self.assertEqual(response.json(), {"authenticated": False})
        self.assertIn("max-age=0", response.headers["set-cookie"].lower())
        self.assertEqual(client.get("/api/portfolios").status_code, 401)

    def test_logout_invalidates_the_token_itself_not_just_the_cookie(self):
        client = self.make()
        token = self.login(client).cookies.get(COOKIE_NAME) or client.cookies.get(COOKIE_NAME)
        self.assertTrue(token)
        client.post("/api/auth/logout")
        client.cookies.set(COOKIE_NAME, token)            # an attacker replaying the copied cookie
        self.assertEqual(client.get("/api/portfolios").status_code, 401)

    def test_logout_without_a_session_is_harmless(self):
        self.assertEqual(self.make().post("/api/auth/logout").status_code, 200)

    def test_cross_site_posts_are_refused(self):
        client = self.make()
        self.assertEqual(self.login(client, headers={"origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.login(client, headers={"sec-fetch-site": "cross-site"}).status_code, 403)
        self.assertEqual(self.login(client, headers={"origin": "http://testserver"}).status_code, 200)
        self.assertEqual(client.post("/api/ask", json={"question": ASK_Q}, headers={"origin": "https://evil.example"}).status_code, 403)

    def test_origin_check_follows_the_allowed_hosts(self):
        manager = AuthManager(CODE, SECRET, clock=self.clock, allowed_origin_hosts=["demo.example.com", "*.vercel.app"], revocations=MemoryStore(clock=self.clock))
        client = self.make(manager=manager)
        self.assertEqual(self.login(client, headers={"origin": "https://demo.example.com"}).status_code, 200)
        self.assertEqual(self.login(client, headers={"origin": "https://x.vercel.app"}).status_code, 200)
        self.assertEqual(self.login(client, headers={"origin": "https://vercel.app.evil.example"}).status_code, 403)


# ----------------------------------------------------------------------------- limits


class TestLimits(Base):
    def test_repeated_wrong_codes_are_throttled_with_retry_after(self):
        client = self.make(limits=RequestLimits(login_attempts=3))
        for _ in range(3):
            self.assertEqual(self.login(client, "wrong-code-entirely").status_code, 401)
        blocked = self.login(client, "wrong-code-entirely")
        self.assertEqual(blocked.status_code, 429)
        self.assertGreater(int(blocked.headers["retry-after"]), 0)
        self.assertEqual(self.login(client).status_code, 429)             # even the right code waits until the window ends
        self.clock.advance(RequestLimits().login_window + 1)
        self.assertEqual(self.login(client).status_code, 200)

    def test_a_successful_login_resets_that_clients_counter(self):
        client = self.make(limits=RequestLimits(login_attempts=3))
        for _ in range(2):
            self.login(client, "wrong-code-entirely")
        self.assertEqual(self.login(client).status_code, 200)
        for _ in range(2):
            self.assertEqual(self.login(client, "wrong-code-entirely").status_code, 401)

    def test_a_global_cap_applies_across_clients(self):
        client = self.make(limits=RequestLimits(login_attempts=100, login_global=4))
        for i in range(4):
            self.assertEqual(self.login(client, "wrong-code-entirely", headers={"x-forwarded-for": f"10.0.0.{i}"}).status_code, 401)
        self.assertEqual(self.login(client, "wrong-code-entirely", headers={"x-forwarded-for": "10.0.0.99"}).status_code, 429)

    def test_ask_is_limited_per_session_and_other_endpoints_are_separate(self):
        client = self.make(limits=RequestLimits(ask_per_minute=3))
        self.login(client)
        for _ in range(3):
            self.assertEqual(client.post("/api/ask", json={"question": ASK_Q}).status_code, 200)
        limited = client.post("/api/ask", json={"question": ASK_Q})
        self.assertEqual(limited.status_code, 429)
        self.assertIn("retry-after", limited.headers)
        self.assertEqual(client.get("/api/portfolios").status_code, 200)
        self.clock.advance(61)
        self.assertEqual(client.post("/api/ask", json={"question": ASK_Q}).status_code, 200)

    def test_a_second_session_has_its_own_ask_budget(self):
        first, second = self.make(limits=RequestLimits(ask_per_minute=1)), None
        self.login(first)
        first.post("/api/ask", json={"question": ASK_Q})
        self.assertEqual(first.post("/api/ask", json={"question": ASK_Q}).status_code, 429)
        second = TestClient(first.app)
        self.login(second)
        self.assertEqual(second.post("/api/ask", json={"question": ASK_Q}).status_code, 200)

    def test_the_optional_daily_cap(self):
        client = self.make(limits=RequestLimits(ask_per_day=2, ask_per_minute=50))
        self.login(client)
        self.assertEqual([client.post("/api/ask", json={"question": ASK_Q}).status_code for _ in range(3)], [200, 200, 429])

    def test_other_endpoints_have_a_generous_session_limit(self):
        client = self.make(limits=RequestLimits(api_per_minute=2))
        self.login(client)
        self.assertEqual([client.get("/api/portfolios").status_code for _ in range(3)], [200, 200, 429])

    def test_without_access_control_there_are_no_limits(self):
        client = self.make(manager=AuthManager(None, None), limits=RequestLimits(ask_per_minute=1))
        self.assertEqual([client.post("/api/ask", json={"question": ASK_Q}).status_code for _ in range(4)], [200] * 4)


class TestStores(unittest.TestCase):
    def test_memory_store_windows_marks_and_reset(self):
        clock = Clock()
        store = MemoryStore(clock=clock)
        self.assertEqual([store.hit("k", 2, 60).allowed for _ in range(3)], [True, True, False])
        self.assertGreaterEqual(store.hit("k", 2, 60).retry_after, 1)
        clock.advance(61)
        self.assertTrue(store.hit("k", 2, 60).allowed)
        store.reset("k")
        self.assertEqual(store.hit("k", 1, 60).remaining, 0)
        store.mark("m", 10)
        self.assertTrue(store.is_marked("m"))
        clock.advance(11)
        self.assertFalse(store.is_marked("m"))

    def test_store_selection_from_the_environment(self):
        self.assertIsInstance(store_from_env({}), MemoryStore)
        self.assertIsInstance(store_from_env({"UPSTASH_REDIS_REST_URL": "https://x.upstash.io"}), MemoryStore)   # token missing
        self.assertIsInstance(store_from_env({"UPSTASH_REDIS_REST_URL": "https://x.upstash.io", "UPSTASH_REDIS_REST_TOKEN": "t"}), UpstashStore)
        self.assertIsInstance(store_from_env({"UPSTASH_REDIS_REST_URL": "http://insecure.example", "UPSTASH_REDIS_REST_TOKEN": "t"}), MemoryStore)

    def test_upstash_requires_https(self):
        with self.assertRaises(ValueError):
            UpstashStore("http://x.upstash.io", "token")

    def test_upstash_commands_and_results(self):
        sent = []

        def opener(request, timeout=None):
            sent.append((request.full_url, request.get_header("Authorization"), json.loads(request.data)))
            body = json.dumps([{"result": 4}, {"result": 1}, {"result": 55}] if request.full_url.endswith("/pipeline") else {"result": 1}).encode()
            return MagicMock(__enter__=lambda s: MagicMock(read=lambda: body), __exit__=lambda *a: False)

        store = UpstashStore("https://example.upstash.io/", "secret-token", opener=opener)
        decision = store.hit("login:all", 5, 600)
        self.assertEqual((decision.allowed, decision.remaining, decision.retry_after), (True, 1, 55))
        self.assertEqual(sent[0][0], "https://example.upstash.io/pipeline")
        self.assertEqual(sent[0][1], "Bearer secret-token")
        self.assertEqual(sent[0][2][0], ["INCR", "plcap:login:all"])
        store.mark("revoked:abc", 120)
        self.assertEqual(sent[-1][2][:2], ["SET", "plcap:revoked:abc"])
        self.assertTrue(store.is_marked("revoked:abc"))
        store.reset("login:all")
        self.assertEqual(sent[-1][2], ["DEL", "plcap:login:all"])

    def test_upstash_failure_falls_back_to_the_instance_store_and_never_logs_the_token(self):
        def broken(request, timeout=None):
            raise OSError("connection refused: https://example.upstash.io Bearer secret-token")

        store = UpstashStore("https://example.upstash.io", "secret-token", opener=broken)
        with self.assertLogs("portfolio_api.limits", level="WARNING") as logs:
            decisions = [store.hit("k", 2, 60).allowed for _ in range(3)]
        self.assertEqual(decisions, [True, True, False])                 # the fallback still limits
        self.assertNotIn("secret-token", "\n".join(logs.output))
        self.assertFalse(store.is_marked("never-marked"))

    def test_request_limits_from_environment_are_bounded(self):
        limits = RequestLimits.from_env({"PORTFOLIO_LOGIN_MAX_ATTEMPTS": "0", "PORTFOLIO_ASK_PER_MINUTE": "99999", "PORTFOLIO_ASK_PER_DAY": "abc"})
        self.assertEqual((limits.login_attempts, limits.ask_per_minute, limits.ask_per_day), (1, 600, 0))


# ----------------------------------------------------------------------------- Gemini on the hosted path


class TestGeminiEnabledPath(Base):
    use_llm = True

    def attach(self, client, key=GEMINI_KEY):
        generator = GeminiSQLGenerator(api_key=key, client=client, db=self.service.db)
        self.service.sql_tool.gemini_generator = generator
        return generator

    def authed(self):
        client = self.make()
        self.assertEqual(self.login(client).status_code, 200)
        return client

    def test_a_supported_free_form_question_is_answered_by_gemini_and_verified(self):
        gemini = gemini_reply("SELECT COUNT(DISTINCT portfolio_id) AS n FROM holdings", description="Portfolios with holdings")
        self.attach(gemini)
        response = self.authed().post("/api/ask", json={"question": "How many distinct portfolios currently hold something?"})
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["status"], "success")
        result = body["result"]
        self.assertEqual((result["generation_method"], result["fallback_used"]), ("gemini", False))
        self.assertTrue(result["model_name"])
        self.assertEqual(result["generated_sql"], "SELECT COUNT(DISTINCT portfolio_id) AS n FROM holdings")
        self.assertEqual(result["rows"], self.service.db.execute_read_only("SELECT COUNT(DISTINCT portfolio_id) AS n FROM holdings")["rows"])
        self.assertEqual(result["answer_verification"]["status"], "verified")
        self.assertEqual(gemini.models.generate_content.call_count, 1)

    def test_gemini_is_not_called_for_unauthenticated_requests(self):
        gemini = gemini_reply("SELECT 1")
        self.attach(gemini)
        client = self.make()
        self.assertEqual(client.post("/api/ask", json={"question": ASK_Q}).status_code, 401)
        gemini.models.generate_content.assert_not_called()

    def test_a_gemini_failure_activates_the_deterministic_fallback(self):
        gemini = MagicMock()
        gemini.models.generate_content.side_effect = RuntimeError("503 UNAVAILABLE: model overloaded")
        self.attach(gemini)
        body = self.authed().post("/api/ask", json={"question": ASK_Q}).json()
        result = body["result"]
        self.assertEqual(body["status"], "success")
        self.assertEqual((result["generation_method"], result["fallback_used"]), ("rule_based", True))
        self.assertIn("Gemini generation failed", result["fallback_reason"])
        self.assertEqual(result["rows"], [{"COUNT(*)": 13}])
        self.assertEqual(result["answer_verification"]["status"], "verified")

    def test_unsafe_generated_sql_is_rejected_and_nothing_is_modified(self):
        for unsafe in (
            "DROP TABLE portfolios",
            "DELETE FROM holdings",
            "SELECT 1; DROP TABLE portfolios",
            "SELECT * FROM sqlite_master",
            "SELECT * FROM pragma_table_info('portfolios')",
            "WITH x AS (SELECT 1) UPDATE portfolios SET status = 'x'",
            "SELECT load_extension('evil')",
        ):
            with self.subTest(unsafe):
                self.attach(gemini_reply(unsafe))
                body = self.authed().post("/api/ask", json={"question": ASK_Q}).json()
                result = body["result"]
                self.assertEqual((result["generation_method"], result["fallback_used"]), ("rule_based", True))
                self.assertEqual(result["rows"], [{"COUNT(*)": 13}])
                self.assertNotIn("DROP", (result["generated_sql"] or "").upper())
        self.assertEqual(self.service.db.execute_read_only("SELECT COUNT(*) AS n FROM portfolios")["rows"], [{"n": 13}])
        self.assertEqual(self.service.db.execute_read_only("SELECT COUNT(*) AS n FROM holdings")["rows"], [{"n": 95}])

    def test_an_invalid_schema_reference_is_rejected_by_the_database_layer(self):
        self.attach(gemini_reply("SELECT secret_column FROM no_such_table"))
        result = self.authed().post("/api/ask", json={"question": ASK_Q}).json()["result"]
        self.assertEqual((result["generation_method"], result["fallback_used"]), ("rule_based", True))
        self.assertIn("rejected", result["fallback_reason"].lower())

    def test_the_gemini_key_never_appears_in_responses_even_when_it_is_in_the_error(self):
        gemini = MagicMock()
        gemini.models.generate_content.side_effect = RuntimeError(f"401 API key not valid: {GEMINI_KEY}")
        self.attach(gemini)
        client = self.authed()
        responses = [client.post("/api/ask", json={"question": ASK_Q}), client.get("/api/health"), client.get("/api/evaluation")]
        for response in responses:
            self.assertNotIn(GEMINI_KEY, response.text)
            self.assertNotIn(GEMINI_KEY, str(dict(response.headers)))


class TestNoSecretsInResponses(Base):
    def test_no_response_or_header_contains_a_secret_or_a_local_path(self):
        client = self.make()
        everything = [client.get("/api/auth/session"), client.get("/api/health"), self.login(client, "wrong-code-entirely")]
        everything += [call(client, m, p, b) for m, p, b in PROTECTED]          # all 401 here
        self.login(client)
        everything += [call(client, m, p, b) for m, p, b in PROTECTED] + [client.get("/api/health"), client.get("/api/auth/session"), client.post("/api/auth/logout")]
        for response in everything:
            blob = response.text + str(dict(response.headers))
            for secret in (CODE, SECRET, GEMINI_KEY, "REVIEWER_ACCESS_CODE", "SESSION_SECRET", "GEMINI_API_KEY"):
                self.assertNotIn(secret, blob)
            for local in ("C:\\", "/Users/", "/home/", "AppData", ".env"):
                self.assertNotIn(local, blob)

    def test_the_cookie_value_does_not_leak_the_code_or_the_secret(self):
        client = self.make()
        self.login(client)
        cookie = client.cookies.get(COOKIE_NAME)
        self.assertNotIn(CODE, cookie)
        self.assertNotIn(SECRET, cookie)

    def test_auth_logging_never_includes_secret_values(self):
        with self.assertLogs("portfolio_api.auth", level="ERROR") as logs:
            AuthManager("short", SECRET)
            AuthManager(CODE, "short")
        text = "\n".join(logs.output)
        self.assertNotIn(CODE, text)
        self.assertNotIn(SECRET, text)


# ----------------------------------------------------------------------------- the Vercel entry point


@unittest.skipUnless(HAVE_FASTAPI, "FastAPI is not installed")
class TestVercelEntryPoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_database()
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.db_path = str(Path(cls.tmp.name) / "portfolio.db")
        cls.built = []
        with patch.dict(os.environ, {"PORTFOLIO_DB_PATH": cls.db_path}, clear=False):
            sys.modules.pop("vercel_app", None)
            cls.vercel_app = importlib.import_module("vercel_app")   # importing builds the app from the real environment: unconfigured

    @classmethod
    def tearDownClass(cls):
        for app in cls.built + [cls.vercel_app.app]:
            app.state.service.close()   # release the SQLite files (Windows cannot delete open files)
        sys.modules.pop("vercel_app", None)
        cls.tmp.cleanup()

    def build(self, env):
        app = self.vercel_app.build_app(env)
        self.built.append(app)
        return app

    def env(self, **extra):
        base = {"PORTFOLIO_DB_PATH": self.db_path, "REVIEWER_ACCESS_CODE": CODE, "SESSION_SECRET": SECRET, "VERCEL": "1",
                "VERCEL_URL": "pl-demo-abc123.vercel.app", "VERCEL_PROJECT_PRODUCTION_URL": "pl-demo.vercel.app"}
        base.update(extra)
        return base

    def test_importing_without_access_configuration_serves_nothing(self):
        client = TestClient(self.vercel_app.app)
        self.assertEqual(client.get("/api/portfolios").status_code, 503)
        self.assertEqual(client.post("/api/ask", json={"question": ASK_Q}).status_code, 503)
        self.assertEqual(client.get("/api/health").json(), {"status": "ok"})

    def test_gemini_is_forced_off_when_access_control_is_not_active(self):
        with self.assertLogs("portfolio_api.vercel", level="ERROR"):
            app = self.build({"PORTFOLIO_DB_PATH": self.db_path, "PORTFOLIO_USE_LLM": "1", "GEMINI_API_KEY": GEMINI_KEY})
        self.assertFalse(app.state.service.use_llm)
        self.assertEqual(TestClient(app).get("/api/portfolios").status_code, 503)

    def test_gemini_is_off_by_default_even_with_access_control(self):
        app = self.build(self.env())
        self.assertFalse(app.state.service.use_llm)

    def test_gemini_is_enabled_only_with_access_control_and_the_flag(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": GEMINI_KEY, "GEMINI_MODEL": "gemini-3.5-flash-lite"}):   # the SDK wrapper reads the process environment
            app = self.build(self.env(PORTFOLIO_USE_LLM="1"))
        self.assertTrue(app.state.service.use_llm)
        generator = app.state.service.sql_tool.gemini_generator
        self.assertTrue(generator.is_configured)
        self.assertEqual(generator.model_name, "gemini-3.5-flash-lite")
        self.assertNotIn(GEMINI_KEY, repr(generator))

    def test_hosted_login_works_on_the_deployment_hostnames_only(self):
        app = self.build(self.env())
        with patch("portfolio_api.app.time.sleep"):
            for host in ("pl-demo.vercel.app", "pl-demo-abc123.vercel.app"):
                client = TestClient(app, base_url=f"https://{host}")
                response = client.post("/api/auth/login", json={"code": CODE}, headers={"origin": f"https://{host}"})
                self.assertEqual(response.status_code, 200, host)
                self.assertIn("secure", response.headers["set-cookie"].lower())
                self.assertEqual(client.get("/api/portfolios").status_code, 200)
            self.assertEqual(TestClient(app, base_url="https://evil.example").get("/api/auth/session").status_code, 400)

    def test_the_database_is_built_in_the_configured_location_and_is_read_only_at_query_time(self):
        self.build(self.env())
        self.assertTrue(Path(self.db_path).is_file())
        db = PortfolioDatabase(self.db_path)
        self.assertEqual(db.execute_read_only("SELECT COUNT(*) AS n FROM portfolios")["rows"], [{"n": 13}])
        with self.assertRaises(Exception):
            db.execute_read_only("DELETE FROM portfolios")
        db.close()

    def test_custom_domains_are_added_through_the_environment(self):
        app = self.build(self.env(PORTFOLIO_ALLOWED_HOSTS="demo.example.com, other.example.org"))
        self.assertEqual(TestClient(app, base_url="https://demo.example.com").get("/api/auth/session").status_code, 200)


if __name__ == "__main__":
    unittest.main()
