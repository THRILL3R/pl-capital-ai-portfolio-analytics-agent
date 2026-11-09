"""
Unit tests for GeminiSQLGenerator, hybrid fallback, routing regression, error handling,
and exposure-calculation semantics.

All Gemini interactions are MOCKED. These tests are not evidence of a live API call;
live verification is performed separately by scripts/gemini_smoke_test.py.
"""

from __future__ import annotations

import inspect
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Resolve paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_agent import sql_generator as sql_generator_module
from portfolio_agent.database import PortfolioDatabase, ReadOnlyQueryError
from portfolio_agent.sql_generator import (
    DEFAULT_GEMINI_MODEL,
    RuleBasedSQLGenerator,
    GeminiSQLGenerator,
    GeminiGenerationError,
    UnsupportedQuestionError,
)
from portfolio_agent.tools import SQLQueryTool, ExposureCalculator
from portfolio_agent.agent import PortfolioAgent

try:
    from google.genai import errors as genai_errors  # type: ignore

    HAVE_GENAI_SDK = True
except ImportError:  # pragma: no cover - depends on environment
    genai_errors = None
    HAVE_GENAI_SDK = False

FAKE_KEY = "unit-test-placeholder"


def _mock_client_returning(payload) -> MagicMock:
    """Build a mock google-genai client whose generate_content returns ``payload``."""
    client = MagicMock()
    response = MagicMock()
    response.text = payload if isinstance(payload, (str, type(None))) else json.dumps(payload)
    client.models.generate_content.return_value = response
    return client


def _mock_client_raising(exc: BaseException) -> MagicMock:
    client = MagicMock()
    client.models.generate_content.side_effect = exc
    return client


class _CodedError(Exception):
    """Duck-typed stand-in for google.genai.errors.APIError (has integer .code)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class TestGeminiSQLGenerator(unittest.TestCase):
    """Test suite for GeminiSQLGenerator with mocks."""

    @classmethod
    def setUpClass(cls):
        cls.db = PortfolioDatabase()
        if not cls.db.is_initialized():
            cls.db.initialize_database()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def test_01_successful_gemini_response_parsing(self):
        """Test parsing valid structured JSON response from Gemini."""
        generator = GeminiSQLGenerator(api_key="fake-test-key", db=self.db)
        raw_json = json.dumps({
            "sql": "SELECT COUNT(*) FROM portfolios;",
            "parameters": [],
            "description": "Count total portfolios"
        })
        parsed = generator.parse_gemini_response(raw_json)
        self.assertEqual(parsed["sql"], "SELECT COUNT(*) FROM portfolios;")
        self.assertEqual(parsed["parameters"], [])
        self.assertEqual(parsed["description"], "Count total portfolios")

    def test_02_parsing_with_markdown_fences(self):
        """Test that markdown fences (```json ... ```) are cleanly stripped."""
        generator = GeminiSQLGenerator(api_key="fake-test-key", db=self.db)
        raw_with_fences = "```json\n" + json.dumps({
            "sql": "SELECT portfolio_name FROM portfolios WHERE status = ?;",
            "parameters": ["Active"],
            "description": "Active portfolios"
        }) + "\n```"
        parsed = generator.parse_gemini_response(raw_with_fences)
        self.assertEqual(parsed["sql"], "SELECT portfolio_name FROM portfolios WHERE status = ?;")
        self.assertEqual(parsed["parameters"], ["Active"])

    def test_03_parameter_placeholder_count_mismatch(self):
        """Test rejection when '?' placeholders do not match parameters list length."""
        generator = GeminiSQLGenerator(api_key="fake-test-key", db=self.db)
        # 2 placeholders but 1 parameter
        raw_json = json.dumps({
            "sql": "SELECT * FROM portfolios WHERE status = ? AND target_risk_level = ?;",
            "parameters": ["Active"],
            "description": "Mismatch"
        })
        with self.assertRaises(GeminiGenerationError) as ctx:
            generator.parse_gemini_response(raw_json)
        self.assertIn("Parameter count mismatch", str(ctx.exception))
        self.assertEqual(ctx.exception.category, "invalid_parameters")

    def test_04_invalid_json_rejection(self):
        """Test rejection of malformed JSON."""
        generator = GeminiSQLGenerator(api_key="fake-test-key", db=self.db)
        with self.assertRaises(GeminiGenerationError) as ctx:
            generator.parse_gemini_response("NOT VALID JSON at all {sql: ...")
        self.assertIn("Failed to parse Gemini response as JSON", str(ctx.exception))
        self.assertEqual(ctx.exception.category, "malformed_json")

    def test_05_empty_output_rejection(self):
        """Test rejection of empty model output."""
        generator = GeminiSQLGenerator(api_key="fake-test-key", db=self.db)
        with self.assertRaises(GeminiGenerationError) as ctx:
            generator.parse_gemini_response("")
        self.assertIn("empty response", str(ctx.exception))
        self.assertEqual(ctx.exception.category, "empty_response")

    def test_06_missing_sql_field(self):
        """Test rejection when 'sql' key is missing in JSON."""
        generator = GeminiSQLGenerator(api_key="fake-test-key", db=self.db)
        raw_json = json.dumps({"parameters": [], "description": "No sql"})
        with self.assertRaises(GeminiGenerationError) as ctx:
            generator.parse_gemini_response(raw_json)
        self.assertIn("Missing required key 'sql'", str(ctx.exception))

    def test_07_missing_api_key_configuration(self):
        """Test is_configured is False and generation raises error when no key provided."""
        with patch.dict(os.environ, {}, clear=True):
            generator = GeminiSQLGenerator(api_key=None, db=self.db)
            self.assertFalse(generator.is_configured)
            with self.assertRaises(GeminiGenerationError) as ctx:
                generator.generate("How many portfolios?")
            self.assertIn("GEMINI_API_KEY is not configured", str(ctx.exception))
            self.assertEqual(ctx.exception.category, "not_configured")

    def test_08_mocked_gemini_generate_success(self):
        """Test end-to-end generate() using mocked Google GenAI client."""
        mock_client = _mock_client_returning({
            "sql": "SELECT COUNT(*) FROM portfolios;",
            "parameters": [],
            "description": "Total portfolios"
        })
        generator = GeminiSQLGenerator(client=mock_client, db=self.db)
        sql, params = generator.generate("How many portfolios do we have?")
        self.assertEqual(sql, "SELECT COUNT(*) FROM portfolios;")
        self.assertEqual(params, ())
        mock_client.models.generate_content.assert_called_once()

    # ── Phase 5 hardening ──────────────────────────────────────────

    def test_19_schema_grounding_is_dynamic_and_accurate(self):
        """System instruction uses the live schema and real categorical values only."""
        generator = GeminiSQLGenerator(client=MagicMock(), db=self.db)
        instruction = generator.build_system_instruction()
        # Real columns from database_schema.sql
        for real in ("benchmark_index", "cost_basis", "current_weight", "sector_name", "asset_type"):
            self.assertIn(real, instruction)
        # Columns that do NOT exist and were previously in a static prompt block
        for bogus in ("unrealized_gain_loss", "ytd_return", "value_at_risk", "benchmark_id ->"):
            self.assertNotIn(bogus, instruction)
        # Value hints read from the database
        self.assertIn("'International Equity Fund'", instruction)
        self.assertIn("'High'", instruction)
        self.assertIn("'Technology'", instruction)

    def test_20_no_legacy_sdk_fallback(self):
        """The deprecated google-generativeai package is not referenced."""
        source = inspect.getsource(sql_generator_module)
        self.assertNotIn("generativeai", source)
        self.assertNotIn("GenerativeModel", source)

    def test_21_model_configuration(self):
        """Default model comes from one constant and is overridable via GEMINI_MODEL."""
        self.assertEqual(DEFAULT_GEMINI_MODEL, "gemini-3.8-flash")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(GeminiSQLGenerator(db=self.db).model_name, DEFAULT_GEMINI_MODEL)
        with patch.dict(os.environ, {"GEMINI_MODEL": "custom-model-id"}, clear=True):
            self.assertEqual(GeminiSQLGenerator(db=self.db).model_name, "custom-model-id")

    def test_22_request_uses_structured_output_config(self):
        """generate_content is called with model, JSON mime type, JSON schema and system instruction."""
        mock_client = _mock_client_returning(
            {"sql": "SELECT COUNT(*) FROM portfolios", "parameters": [], "description": "Count"}
        )
        generator = GeminiSQLGenerator(client=mock_client, db=self.db, model="test-model")
        result = generator.generate_structured("How many portfolios?")
        kwargs = mock_client.models.generate_content.call_args.kwargs
        self.assertEqual(kwargs["model"], "test-model")
        self.assertIn("How many portfolios?", kwargs["contents"])
        config = kwargs["config"]
        self.assertEqual(config["response_mime_type"], "application/json")
        self.assertEqual(config["response_json_schema"]["required"], ["sql", "parameters", "description"])
        self.assertIn("read-only", config["system_instruction"])
        self.assertEqual(result["description"], "Count")
        self.assertEqual(result["model_name"], "test-model")

    def test_23_empty_model_response_via_client(self):
        """A response with no text (e.g. blocked candidate) raises empty_response."""
        generator = GeminiSQLGenerator(client=_mock_client_returning(None), db=self.db)
        with self.assertRaises(GeminiGenerationError) as ctx:
            generator.generate_structured("How many portfolios?")
        self.assertEqual(ctx.exception.category, "empty_response")

    def test_24_malformed_json_via_client(self):
        generator = GeminiSQLGenerator(client=_mock_client_returning("{'sql': SELECT"), db=self.db)
        with self.assertRaises(GeminiGenerationError) as ctx:
            generator.generate_structured("How many portfolios?")
        self.assertEqual(ctx.exception.category, "malformed_json")

    def test_25_unsafe_sql_rejected_before_execution(self):
        """Multi-statement / write SQL is rejected by the generator itself."""
        generator = GeminiSQLGenerator(api_key="k", db=self.db)
        for unsafe in (
            "SELECT 1; DROP TABLE portfolios",
            "UPDATE portfolios SET status = 'Closed'",
            "SELECT * FROM portfolios -- comment",
            "WITH x AS (SELECT 1) DELETE FROM holdings",
        ):
            with self.subTest(sql=unsafe):
                with self.assertRaises(GeminiGenerationError) as ctx:
                    generator.parse_gemini_response(json.dumps({"sql": unsafe, "parameters": [], "description": ""}))
                self.assertEqual(ctx.exception.category, "unsafe_sql")

    def test_26_invalid_parameter_types_rejected(self):
        generator = GeminiSQLGenerator(api_key="k", db=self.db)
        for bad_params in ([{"x": 1}], [[1, 2]], "Active"):
            with self.subTest(params=bad_params):
                payload = {"sql": "SELECT * FROM portfolios WHERE status = ?", "parameters": bad_params}
                with self.assertRaises(GeminiGenerationError) as ctx:
                    generator.parse_gemini_response(json.dumps(payload))
                self.assertEqual(ctx.exception.category, "invalid_parameters")

    def test_27_named_placeholders_rejected(self):
        generator = GeminiSQLGenerator(api_key="k", db=self.db)
        payload = {"sql": "SELECT * FROM portfolios WHERE status = :status", "parameters": []}
        with self.assertRaises(GeminiGenerationError) as ctx:
            generator.parse_gemini_response(json.dumps(payload))
        self.assertEqual(ctx.exception.category, "invalid_parameters")

    def test_28_question_mark_inside_literal_not_counted(self):
        generator = GeminiSQLGenerator(api_key="k", db=self.db)
        payload = {"sql": "SELECT 'why?' AS q, portfolio_name FROM portfolios WHERE status = ?", "parameters": ["Active"]}
        parsed = generator.parse_gemini_response(json.dumps(payload))
        self.assertEqual(parsed["parameters"], ["Active"])

    def test_29_unanswerable_question(self):
        generator = GeminiSQLGenerator(api_key="k", db=self.db)
        payload = {"sql": "", "parameters": [], "description": "No weather data in schema."}
        with self.assertRaises(GeminiGenerationError) as ctx:
            generator.parse_gemini_response(json.dumps(payload))
        self.assertEqual(ctx.exception.category, "unanswerable")
        self.assertIn("weather", str(ctx.exception))

    def test_30_quota_error_classification(self):
        """HTTP 429 (rate limit / quota) is classified as 'quota'."""
        errors_to_test = [_CodedError(429, "Resource has been exhausted")]
        if HAVE_GENAI_SDK:
            errors_to_test.append(
                genai_errors.ClientError(
                    429, {"error": {"code": 429, "message": "Quota exceeded", "status": "RESOURCE_EXHAUSTED"}}
                )
            )
        for exc in errors_to_test:
            with self.subTest(exc=type(exc).__name__):
                generator = GeminiSQLGenerator(client=_mock_client_raising(exc), db=self.db)
                with self.assertRaises(GeminiGenerationError) as ctx:
                    generator.generate_structured("How many portfolios?")
                self.assertEqual(ctx.exception.category, "quota")

    def test_31_timeout_classification(self):
        timeouts = [TimeoutError("timed out")]
        try:
            import httpx  # installed as a google-genai dependency

            timeouts.append(httpx.ReadTimeout("read timed out"))
        except ImportError:
            pass
        for exc in timeouts:
            with self.subTest(exc=type(exc).__name__):
                generator = GeminiSQLGenerator(client=_mock_client_raising(exc), db=self.db)
                with self.assertRaises(GeminiGenerationError) as ctx:
                    generator.generate_structured("How many portfolios?")
                self.assertEqual(ctx.exception.category, "timeout")

    def test_32_network_error_classification(self):
        generator = GeminiSQLGenerator(client=_mock_client_raising(ConnectionError("unreachable")), db=self.db)
        with self.assertRaises(GeminiGenerationError) as ctx:
            generator.generate_structured("How many portfolios?")
        self.assertEqual(ctx.exception.category, "network")

    def test_33_auth_error_and_secret_redaction(self):
        """Auth failures are classified and the API key never appears in error text."""
        exc = _CodedError(403, f"API key not valid: {FAKE_KEY} (url ...?key={FAKE_KEY})")
        generator = GeminiSQLGenerator(api_key=FAKE_KEY, client=_mock_client_raising(exc), db=self.db)
        with self.assertRaises(GeminiGenerationError) as ctx:
            generator.generate_structured("How many portfolios?")
        self.assertEqual(ctx.exception.category, "auth")
        self.assertNotIn(FAKE_KEY, str(ctx.exception))
        self.assertNotIn(FAKE_KEY, repr(generator))

    def test_34_missing_schema_fails_without_calling_api(self):
        mock_client = _mock_client_returning({"sql": "SELECT 1", "parameters": []})
        generator = GeminiSQLGenerator(client=mock_client, db=None)
        with self.assertRaises(GeminiGenerationError) as ctx:
            generator.generate_structured("How many portfolios?")
        self.assertEqual(ctx.exception.category, "schema_unavailable")
        mock_client.models.generate_content.assert_not_called()

    @unittest.skipUnless(HAVE_GENAI_SDK, "google-genai SDK not installed")
    def test_35_real_sdk_client_construction_without_network(self):
        """A real genai.Client is constructed (no request sent) with the configured timeout."""
        from google import genai

        generator = GeminiSQLGenerator(api_key=FAKE_KEY, db=self.db, timeout_seconds=12)
        client = generator._get_client()
        self.assertIsInstance(client, genai.Client)
        self.assertEqual(generator.timeout_seconds, 12.0)


class TestHybridFallback(unittest.TestCase):
    """Test suite for SQLQueryTool hybrid Gemini + Rule-based fallback."""

    @classmethod
    def setUpClass(cls):
        cls.db = PortfolioDatabase()
        if not cls.db.is_initialized():
            cls.db.initialize_database()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def _tool(self, client, generator=None) -> SQLQueryTool:
        gemini_gen = GeminiSQLGenerator(client=client, db=self.db)
        return SQLQueryTool(db=self.db, generator=generator, gemini_generator=gemini_gen, use_gemini=True)

    def test_09_gemini_success_execution(self):
        """When Gemini returns valid SQL, it executes and reports generation_method='gemini'."""
        tool = self._tool(_mock_client_returning({
            "sql": "SELECT COUNT(*) FROM portfolios;",
            "parameters": [],
            "description": "Total portfolios"
        }))
        res = tool.execute("How many portfolios do we have in total?")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["generation_method"], "gemini")
        self.assertFalse(res["fallback_used"])
        self.assertIsNone(res["fallback_reason"])
        self.assertEqual(res["rows"][0]["COUNT(*)"], 13)

    def test_10_gemini_api_failure_triggers_fallback(self):
        """When Gemini raises an API exception, fallback to rule-based generator succeeds."""
        tool = self._tool(_mock_client_raising(RuntimeError("Quota exceeded or connection error")))
        res = tool.execute("What are the names of all active portfolios?")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["generation_method"], "rule_based")
        self.assertTrue(res["fallback_used"])
        self.assertIn("Quota exceeded", res["fallback_reason"])
        self.assertEqual(res["row_count"], 10)

    def test_11_gemini_invalid_sql_triggers_fallback(self):
        """When Gemini generates SQL that fails read-only validation, fallback succeeds."""
        tool = self._tool(_mock_client_returning({
            "sql": "DELETE FROM portfolios;",
            "parameters": [],
            "description": "Unsafe query"
        }))
        res = tool.execute("How many portfolios do we have in total?")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["generation_method"], "rule_based")
        self.assertTrue(res["fallback_used"])
        self.assertIn("unsafe_sql", res["fallback_reason"])
        self.assertEqual(res["rows"][0]["COUNT(*)"], 13)

    def test_12_gemini_disabled_by_flag(self):
        """When use_gemini=False, rule-based generator is used directly without calling Gemini."""
        mock_client = MagicMock()
        gemini_gen = GeminiSQLGenerator(client=mock_client, db=self.db)
        tool = SQLQueryTool(db=self.db, gemini_generator=gemini_gen, use_gemini=False)

        res = tool.execute("How many portfolios do we have in total?")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["generation_method"], "rule_based")
        self.assertFalse(res["fallback_used"])
        self.assertIsNone(res["fallback_reason"])
        mock_client.models.generate_content.assert_not_called()

    def test_13_gemini_unconfigured_uses_rule_based(self):
        """
        When Gemini is enabled but not configured, the rule-based generator is used and the
        response reports this as a fallback with an explicit reason (Phase 5 contract).
        """
        with patch.dict(os.environ, {}, clear=True):
            gemini_gen = GeminiSQLGenerator(api_key=None, db=self.db)
        tool = SQLQueryTool(db=self.db, gemini_generator=gemini_gen, use_gemini=True)

        res = tool.execute("Which securities are in the Technology sector?")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["generation_method"], "rule_based")
        self.assertTrue(res["fallback_used"])
        self.assertIn("not configured", res["fallback_reason"])
        self.assertEqual(res["row_count"], 8)

    def test_14_unsupported_question_both_fail(self):
        """When Gemini fails and rule-based does not support question, clear unsupported error is returned."""
        tool = self._tool(_mock_client_raising(RuntimeError("Network down")))
        res = tool.execute("What is the meaning of life?")
        self.assertEqual(res["status"], "unsupported")
        self.assertTrue(res["fallback_used"])
        self.assertIn("Network down", res["fallback_reason"])
        self.assertIn("not recognized", res["error"])

    # ── Phase 5 hardening ──────────────────────────────────────────

    def test_36_unknown_column_rejected_by_db_then_fallback(self):
        """Syntactically safe SQL referencing a non-existent column fails in the DB layer → fallback."""
        tool = self._tool(_mock_client_returning(
            {"sql": "SELECT unrealized_gain_loss FROM holdings", "parameters": [], "description": "x"}
        ))
        res = tool.execute("How many portfolios do we have in total?")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["generation_method"], "rule_based")
        self.assertTrue(res["fallback_used"])
        self.assertIn("rejected by database layer", res["fallback_reason"])
        self.assertEqual(res["rows"][0]["COUNT(*)"], 13)

    def test_37_valid_gemini_query_with_zero_rows_does_not_fall_back(self):
        """An empty result set from valid Gemini SQL is a legitimate answer."""
        rule_gen = MagicMock(spec=RuleBasedSQLGenerator)
        tool = self._tool(
            _mock_client_returning({
                "sql": "SELECT portfolio_name FROM portfolios WHERE status = ?",
                "parameters": ["Liquidated"],
                "description": "Liquidated portfolios",
            }),
            generator=rule_gen,
        )
        res = tool.execute("Which portfolios are liquidated?")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["generation_method"], "gemini")
        self.assertEqual(res["row_count"], 0)
        self.assertEqual(res["rows"], [])
        self.assertFalse(res["fallback_used"])
        self.assertIsNone(res["fallback_reason"])
        rule_gen.generate.assert_not_called()

    def test_38_quota_failure_falls_back(self):
        tool = self._tool(_mock_client_raising(_CodedError(429, "Resource exhausted")))
        res = tool.execute("How many portfolios do we have in total?")
        self.assertEqual(res["generation_method"], "rule_based")
        self.assertTrue(res["fallback_used"])
        self.assertIn("(quota)", res["fallback_reason"])
        self.assertEqual(res["rows"][0]["COUNT(*)"], 13)

    def test_39_timeout_falls_back(self):
        tool = self._tool(_mock_client_raising(TimeoutError("deadline exceeded")))
        res = tool.execute("How many portfolios do we have in total?")
        self.assertTrue(res["fallback_used"])
        self.assertIn("(timeout)", res["fallback_reason"])
        self.assertEqual(res["status"], "success")

    def test_40_malformed_and_empty_responses_fall_back(self):
        for payload, category in (("not json", "malformed_json"), ("", "empty_response"), (None, "empty_response")):
            with self.subTest(payload=payload):
                tool = self._tool(_mock_client_returning(payload))
                res = tool.execute("How many portfolios do we have in total?")
                self.assertEqual(res["status"], "success")
                self.assertEqual(res["generation_method"], "rule_based")
                self.assertTrue(res["fallback_used"])
                self.assertIn(category, res["fallback_reason"])

    def test_41_parameterized_gemini_query_and_metadata(self):
        """Parameterised Gemini SQL executes with bound params; metadata fields are populated."""
        tool = self._tool(_mock_client_returning({
            "sql": "SELECT SUM(total_aum) AS total_aum FROM portfolios WHERE target_risk_level = ?",
            "parameters": ["High"],
            "description": "Total AUM of high-risk portfolios",
        }))
        res = tool.execute("What is the total AUM for portfolios with high target risk level?")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["generation_method"], "gemini")
        self.assertEqual(res["model_name"], DEFAULT_GEMINI_MODEL)
        self.assertEqual(res["query_parameters"], ["High"])
        self.assertEqual(res["query_description"], "Total AUM of high-risk portfolios")
        self.assertAlmostEqual(res["rows"][0]["total_aum"], 85_000_000.0)
        for key in ("generation_method", "model_name", "fallback_used", "fallback_reason"):
            self.assertIn(key, res)


class TestRoutingRegressionAndSafety(unittest.TestCase):
    """Test suite for Phase 5 routing regression requirements."""

    @classmethod
    def setUpClass(cls):
        cls.db = PortfolioDatabase()
        if not cls.db.is_initialized():
            cls.db.initialize_database()
        cls.agent = PortfolioAgent(db=cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def test_15_regression_q7_tech_percentage_routes_to_sql(self):
        """Q7 technology percentage by portfolio routes to SQLQueryTool."""
        q = (
            "For each portfolio, show the total value of Technology sector holdings "
            "and what percentage it represents of the total portfolio value"
        )
        resp = self.agent.answer_question(q)
        self.assertEqual(resp["status"], "success")
        self.assertEqual(resp["selected_tool"], "sql_query")

    def test_16_regression_percentage_invested_routes_to_exposure(self):
        """'What percentage is invested in Technology in Tech Innovation Fund?' routes to ExposureCalculator."""
        q = "What percentage is invested in Technology in Tech Innovation Fund?"
        resp = self.agent.answer_question(q)
        self.assertEqual(resp["status"], "success")
        self.assertEqual(resp["selected_tool"], "exposure_calculator")
        self.assertEqual(resp["result"]["portfolio_name"], "Tech Innovation Fund")
        self.assertEqual(resp["result"]["exposures"][0]["sector"], "Technology")
        self.assertAlmostEqual(resp["result"]["exposures"][0]["exposure_pct"], 100.0, places=1)

    def test_17_regression_sector_allocation_routes_to_exposure(self):
        """'Show sector allocation for International Equity' routes to ExposureCalculator."""
        q = "Show sector allocation for International Equity"
        resp = self.agent.answer_question(q)
        self.assertEqual(resp["status"], "success")
        self.assertEqual(resp["selected_tool"], "exposure_calculator")
        self.assertEqual(resp["result"]["portfolio_name"], "International Equity Fund")
        self.assertEqual(len(resp["result"]["exposures"]), 4)

    def test_18_exposure_calculator_never_calls_gemini(self):
        """ExposureCalculator remains completely deterministic and has no reference to Gemini."""
        calc = ExposureCalculator(db=self.db)
        self.assertFalse(hasattr(calc, "gemini_generator"))
        self.assertFalse(hasattr(calc, "generator"))
        res = calc.calculate("Tech Innovation Fund")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["tool"], "exposure_calculator")

    def test_42_holding_value_percentage_by_portfolio_routes_to_sql(self):
        q = "Show Technology holding value and percentage by portfolio."
        resp = self.agent.answer_question(q)
        self.assertEqual(resp["selected_tool"], "sql_query")
        self.assertEqual(resp["status"], "success")
        self.assertIn("tech_percentage", resp["result"]["columns"])

    def test_43_technology_market_value_each_portfolio_routes_to_sql(self):
        q = "What is the Technology market value for each portfolio?"
        resp = self.agent.answer_question(q)
        self.assertEqual(resp["selected_tool"], "sql_query")
        self.assertEqual(resp["status"], "success")
        self.assertIn("tech_value", resp["result"]["columns"])

    def test_44_q7_uses_market_value_not_weights(self):
        """Q7 tech_value equals SUM(quantity * current_price) computed independently."""
        resp = self.agent.answer_question("What is the Technology market value for each portfolio?")
        conn = sqlite3.connect(str(self.db.db_path))
        try:
            expected = dict(conn.execute(
                "SELECT p.portfolio_name, SUM(h.quantity * s.current_price) FROM holdings h "
                "JOIN securities s ON s.security_id = h.security_id "
                "JOIN sectors sec ON sec.sector_id = s.sector_id "
                "JOIN portfolios p ON p.portfolio_id = h.portfolio_id "
                "WHERE sec.sector_name = 'Technology' GROUP BY p.portfolio_name"
            ).fetchall())
        finally:
            conn.close()
        rows = resp["result"]["rows"]
        self.assertEqual(len(rows), len(expected))
        for row in rows:
            self.assertAlmostEqual(row["tech_value"], expected[row["portfolio_name"]], places=4)


class TestExposureNoRenormalization(unittest.TestCase):
    """
    Exposure = SUM(current_weight) per sector for Stock holdings, x100.
    It must NOT be divided by total equity weight (no renormalisation to 100%).
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp_dir = tempfile.mkdtemp(prefix="pl_exposure_")
        cls.db_path = Path(cls.tmp_dir) / "synthetic.db"
        schema = (PROJECT_ROOT / "database_schema.sql").read_text(encoding="utf-8")
        conn = sqlite3.connect(str(cls.db_path))
        try:
            conn.executescript(schema)
            conn.executemany(
                "INSERT INTO sectors (sector_id, sector_name) VALUES (?, ?)",
                [(1, "Technology"), (2, "Energy")],
            )
            conn.executemany(
                "INSERT INTO securities (security_id, symbol, company_name, asset_type, sector_id, current_price) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (1, "TECH", "Tech Co", "Stock", 1, 100.0),
                    (2, "ENER", "Energy Co", "Stock", 2, 50.0),
                    (3, "BOND1", "Govt Bond", "Bond", None, 1000.0),
                ],
            )
            conn.executemany(
                "INSERT INTO portfolios (portfolio_id, portfolio_name, status) VALUES (?, ?, ?)",
                [(1, "Mixed Synthetic Fund", "Active"), (2, "Bond Only Synthetic Fund", "Active")],
            )
            conn.executemany(
                "INSERT INTO holdings (holding_id, portfolio_id, security_id, quantity, current_weight, cost_basis) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (1, 1, 1, 10, 0.30, 1000.0),
                    (2, 1, 2, 10, 0.20, 500.0),
                    (3, 1, 3, 1, 0.50, 1000.0),
                    (4, 2, 3, 5, 1.00, 5000.0),
                ],
            )
            conn.commit()
        finally:
            conn.close()
        cls.synthetic_db = PortfolioDatabase(cls.db_path)
        cls.synthetic_calc = ExposureCalculator(db=cls.synthetic_db)

        cls.real_db = PortfolioDatabase()
        if not cls.real_db.is_initialized():
            cls.real_db.initialize_database()
        cls.real_calc = ExposureCalculator(db=cls.real_db)

    @classmethod
    def tearDownClass(cls):
        cls.synthetic_db.close()
        cls.real_db.close()
        shutil.rmtree(cls.tmp_dir, ignore_errors=True)

    def test_45_mixed_portfolio_weights_not_renormalized(self):
        res = self.synthetic_calc.calculate("Mixed Synthetic Fund")
        self.assertEqual(res["status"], "success")
        by_sector = {e["sector"]: e for e in res["exposures"]}
        # Renormalised values would be 60% / 40%. Correct values are the raw weights.
        self.assertAlmostEqual(by_sector["Technology"]["exposure_pct"], 30.0, places=6)
        self.assertAlmostEqual(by_sector["Energy"]["exposure_pct"], 20.0, places=6)
        self.assertAlmostEqual(by_sector["Technology"]["weight_fraction"], 0.30, places=6)
        self.assertAlmostEqual(res["total_equity_weight_pct"], 50.0, places=6)
        self.assertEqual(res["excluded_bond_holdings"], 1)
        self.assertEqual(res["equity_holdings_count"], 2)
        self.assertEqual(res["total_holdings_count"], 3)

    def test_46_bond_only_portfolio(self):
        res = self.synthetic_calc.calculate("Bond Only Synthetic Fund")
        self.assertEqual(res["status"], "no_equity_holdings")
        self.assertEqual(res["exposures"], [])
        self.assertEqual(res["excluded_bond_holdings"], 1)
        self.assertEqual(res["equity_holdings_count"], 0)
        self.assertIsNone(res["error"])

    def test_47_ground_truth_q9_tech_innovation(self):
        res = self.real_calc.calculate("Tech Innovation Fund")
        self.assertEqual([(e["sector"], round(e["exposure_pct"], 4)) for e in res["exposures"]], [("Technology", 100.0)])

    def test_48_ground_truth_q10_international_equity(self):
        res = self.real_calc.calculate("international equity")
        self.assertEqual(res["portfolio_name"], "International Equity Fund")
        got = {e["sector"]: round(e["exposure_pct"], 4) for e in res["exposures"]}
        self.assertEqual(
            got, {"Energy": 38.0, "Technology": 36.0, "Automotive": 15.0, "Consumer Staples": 11.0}
        )


class TestMalformedCteRegression(unittest.TestCase):
    """
    Live failure (gemini-3.5-flash-lite, Q7): the model emitted the CTE list and then dropped the final
    SELECT keyword, so SQLite reported ``near "t": syntax error`` and the answer came from the rule-based
    fallback. These tests pin that exact statement, the valid counterpart, and the repair behaviour.
    All Gemini interactions are mocked; none of this is live evidence.
    """

    # Exact shape of the malformed statement returned by Gemini (no SELECT before "t.portfolio_name").
    MALFORMED_Q7 = (
        "WITH portfolio_totals AS (SELECT p.portfolio_id, p.portfolio_name, "
        "SUM(h.quantity * s.current_price) AS total_portfolio_value "
        "FROM portfolios p JOIN holdings h ON p.portfolio_id = h.portfolio_id "
        "JOIN securities s ON h.security_id = s.security_id GROUP BY p.portfolio_id), "
        "tech_holdings AS (SELECT p.portfolio_id, SUM(h.quantity * s.current_price) AS tech_market_value "
        "FROM portfolios p JOIN holdings h ON p.portfolio_id = h.portfolio_id "
        "JOIN securities s ON h.security_id = s.security_id "
        "JOIN sectors sec ON s.sector_id = sec.sector_id "
        "WHERE sec.sector_name = ? GROUP BY p.portfolio_id) "
        "t.portfolio_name, t.tech_market_value, pt.total_portfolio_value, "
        "ROUND((t.tech_market_value * 100.0) / pt.total_portfolio_value, 2) AS percentage "
        "FROM tech_holdings t JOIN portfolio_totals pt ON t.portfolio_id = pt.portfolio_id "
        "ORDER BY percentage DESC"
    )
    # The same statement as a correct Gemini-style answer: only the missing keyword is added back and
    # the join is written properly (portfolio_name comes from the totals CTE).
    VALID_Q7 = MALFORMED_Q7.replace(") t.portfolio_name, t.tech_market_value,", ") SELECT pt.portfolio_name, t.tech_market_value,")
    QUESTION = (
        "For each portfolio, show the total value of Technology sector holdings and what percentage it "
        "represents of the total portfolio value"
    )

    @classmethod
    def setUpClass(cls):
        cls.db = PortfolioDatabase()
        if not cls.db.is_initialized():
            cls.db.initialize_database()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def _generator(self, client) -> GeminiSQLGenerator:
        return GeminiSQLGenerator(api_key=FAKE_KEY, client=client, db=self.db)

    def _payload(self, sql):
        return {"sql": sql, "parameters": ["Technology"], "description": "Technology share per portfolio"}

    def _responses(self, *payloads):
        client = MagicMock()
        outs = []
        for p in payloads:
            r = MagicMock()
            r.text = json.dumps(p)
            outs.append(r)
        client.models.generate_content.side_effect = outs
        return client

    # ---- the exact failure ------------------------------------------------------------

    def test_sqlite_itself_rejects_the_exact_malformed_statement(self):
        """Documents the original failure (SQLite's message) and that it is now stopped before execution."""
        conn = self.db.get_read_only_connection()  # the raw engine, bypassing every validation layer
        with self.assertRaises(sqlite3.Error) as raw:
            conn.execute(self.MALFORMED_Q7, ("Technology",))
        self.assertIn('near "t"', str(raw.exception))
        # Through the public path the AST layer rejects it first, without touching the database.
        with self.assertRaises(ReadOnlyQueryError) as ctx:
            self.db.execute_read_only(self.MALFORMED_Q7, ("Technology",))
        self.assertIn("AST validation", str(ctx.exception))

    def test_structure_check_flags_the_exact_malformed_statement(self):
        problem = sql_generator_module.check_cte_structure(self.MALFORMED_Q7)
        self.assertIsNotNone(problem)
        self.assertIn("SELECT must follow", problem)
        self.assertIn("'t.portfolio_name,'", problem)

    def test_parser_rejects_it_before_execution_with_a_specific_category(self):
        gen = self._generator(MagicMock())
        with self.assertRaises(GeminiGenerationError) as ctx:
            gen.parse_gemini_response(json.dumps(self._payload(self.MALFORMED_Q7)))
        self.assertEqual(ctx.exception.category, "malformed_sql")
        self.assertEqual(ctx.exception.sql, self.MALFORMED_Q7)

    # ---- the valid counterpart --------------------------------------------------------

    def test_valid_q7_style_cte_passes_validation_and_matches_the_official_query(self):
        gen = self._generator(MagicMock())
        parsed = gen.parse_gemini_response(json.dumps(self._payload(self.VALID_Q7)))
        self.assertIsNone(sql_generator_module.check_cte_structure(parsed["sql"]))
        got = self.db.execute_read_only(parsed["sql"], tuple(parsed["parameters"]))
        # Independent expectation: the ground-truth query from the assessment, run on the same database.
        truth = json.loads((PROJECT_ROOT / "ground_truth_dataset.json").read_text(encoding="utf-8"))["questions"][6]
        # The official query inlines 'Technology' (it has no '?' placeholder), so it takes no parameters;
        # the Gemini-style statement above uses a '?' placeholder and keeps its single binding.
        self.assertNotIn("?", truth["ground_truth"]["sql_query"])
        self.assertEqual(parsed["parameters"], ["Technology"])
        expected = self.db.execute_read_only(truth["ground_truth"]["sql_query"])
        self.assertEqual(got["row_count"], expected["row_count"])
        by_name = {r["portfolio_name"]: r for r in got["rows"]}
        for row in expected["rows"]:
            mine = by_name[row["portfolio_name"]]
            self.assertAlmostEqual(mine["percentage"], row["tech_percentage"], places=2)

    def test_structure_check_accepts_well_formed_cte_variants(self):
        ok = [
            "SELECT 1",
            "WITH a AS (SELECT 1) SELECT * FROM a",
            "WITH a AS (SELECT 1), b AS (SELECT 2 FROM (SELECT 3)) SELECT * FROM a, b",
            "WITH a(x, y) AS (SELECT 1, 2) SELECT x FROM a",
            "WITH a AS MATERIALIZED (SELECT 1) SELECT * FROM a",
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 3) SELECT i FROM n",
            'WITH "tech totals" AS (SELECT 1) SELECT * FROM "tech totals"',
            "WITH a AS (SELECT ')' AS paren) SELECT * FROM a",          # parenthesis inside a string literal
            "with a as (select 1)\n\n  select * from a",
        ]
        for sql in ok:
            with self.subTest(sql=sql):
                self.assertIsNone(sql_generator_module.check_cte_structure(sql))
        gen = RuleBasedSQLGenerator()
        for q in (self.QUESTION, "Find portfolios that have holdings in more than 5 different sectors and show their diversification metrics"):
            self.assertIsNone(sql_generator_module.check_cte_structure(gen.generate(q)[0]))

    def test_structure_check_flags_other_malformed_ctes(self):
        bad = {
            "WITH a AS (SELECT 1) FROM a": "FROM",
            "WITH a AS (SELECT 1), b AS (SELECT 2) JOIN b ON 1 = 1 SELECT 1": "JOIN",
            "WITH a AS (SELECT 1) a.x FROM a": "a.x",
            "WITH a AS (SELECT 1)": "nothing",
            "WITH a (SELECT 1) SELECT * FROM a": "name AS",
            "WITH a AS SELECT 1 SELECT * FROM a": "wrapped in parentheses",
            "WITH a AS (SELECT 1, b AS (SELECT 2) SELECT 1": "unbalanced",
        }
        for sql, needle in bad.items():
            with self.subTest(sql=sql):
                problem = sql_generator_module.check_cte_structure(sql)
                self.assertIsNotNone(problem)
                self.assertIn(needle, problem)

    # ---- prompt constraint ------------------------------------------------------------

    def test_system_instruction_states_the_cte_structure_rules(self):
        text = self._generator(MagicMock()).build_system_instruction()
        for needle in (
            "SQL structure rules",
            "WITH first_cte AS",
            "SELECT <result columns>",
            "NOTHING else may sit between the last CTE's closing ')' and that final",
            "the final SELECT keyword is",
        ):
            self.assertIn(needle, text)
        # Generic guidance only: no question-specific SQL or answer is embedded.
        self.assertNotIn("tech_percentage", text)
        self.assertNotIn("Technology", self.__class__.__dict__.get("_unused", "") + sql_generator_module.GeminiSQLGenerator.SQL_STRUCTURE_RULES)

    # ---- repair behaviour -------------------------------------------------------------

    def test_one_repair_attempt_recovers_from_malformed_sql(self):
        client = self._responses(self._payload(self.MALFORMED_Q7), self._payload(self.VALID_Q7))
        result = self._generator(client).generate_structured(self.QUESTION)
        self.assertEqual(client.models.generate_content.call_count, 2)
        self.assertEqual(result["sql"], self.VALID_Q7)
        second = client.models.generate_content.call_args_list[1].kwargs["contents"]
        self.assertIn("rejected before execution", second)
        self.assertIn("SELECT must follow", second)
        self.assertIn("t.portfolio_name, t.tech_market_value", second)  # the rejected SQL is shown to the model
        self.assertTrue(second.startswith(f"Question: {self.QUESTION}"))

    def test_repair_is_limited_to_one_extra_call(self):
        client = self._responses(self._payload(self.MALFORMED_Q7), self._payload(self.MALFORMED_Q7), self._payload(self.VALID_Q7))
        with self.assertRaises(GeminiGenerationError) as ctx:
            self._generator(client).generate_structured(self.QUESTION)
        self.assertEqual(ctx.exception.category, "malformed_sql")
        self.assertEqual(client.models.generate_content.call_count, 2)

    def test_other_failures_are_not_retried(self):
        for payload in (
            self._payload("DELETE FROM holdings"),
            {"sql": "SELECT ?", "parameters": [], "description": "x"},
            {"sql": "", "parameters": [], "description": "cannot answer"},
        ):
            with self.subTest(sql=payload["sql"]):
                client = self._responses(payload, payload)
                with self.assertRaises(GeminiGenerationError):
                    self._generator(client).generate_structured(self.QUESTION)
                self.assertEqual(client.models.generate_content.call_count, 1)

    def test_valid_sql_needs_a_single_call(self):
        client = self._responses(self._payload(self.VALID_Q7))
        self._generator(client).generate_structured(self.QUESTION)
        self.assertEqual(client.models.generate_content.call_count, 1)

    def test_safety_checks_still_run_before_the_structure_check(self):
        gen = self._generator(MagicMock())
        unsafe = "WITH a AS (SELECT 1) DELETE FROM holdings"
        with self.assertRaises(GeminiGenerationError) as ctx:
            gen.parse_gemini_response(json.dumps({"sql": unsafe, "parameters": [], "description": ""}))
        self.assertEqual(ctx.exception.category, "unsafe_sql")
        with self.assertRaises(GeminiGenerationError) as ctx:  # parameters are still enforced on valid structure
            gen.parse_gemini_response(json.dumps({"sql": self.VALID_Q7, "parameters": [], "description": ""}))
        self.assertEqual(ctx.exception.category, "invalid_parameters")

    # ---- tool level: Gemini success is reported separately from fallback ----------------

    def _tool(self, client) -> SQLQueryTool:
        return SQLQueryTool(db=self.db, gemini_generator=self._generator(client), use_gemini=True)

    def test_repaired_gemini_sql_is_reported_as_gemini_not_fallback(self):
        client = self._responses(self._payload(self.MALFORMED_Q7), self._payload(self.VALID_Q7))
        res = self._tool(client).execute(self.QUESTION)
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["generation_method"], "gemini")
        self.assertFalse(res["fallback_used"])
        self.assertEqual(res["generated_sql"], self.VALID_Q7)

    def test_persistently_malformed_gemini_sql_falls_back_and_is_labelled_as_fallback(self):
        client = self._responses(self._payload(self.MALFORMED_Q7), self._payload(self.MALFORMED_Q7))
        res = self._tool(client).execute(self.QUESTION)
        self.assertEqual(res["status"], "success")                 # the rule-based answer is correct ...
        self.assertEqual(res["generation_method"], "rule_based")   # ... but it is never counted as Gemini
        self.assertTrue(res["fallback_used"])
        self.assertIn("malformed_sql", res["fallback_reason"])
        self.assertEqual(res["row_count"], 8)


if __name__ == "__main__":
    unittest.main()
