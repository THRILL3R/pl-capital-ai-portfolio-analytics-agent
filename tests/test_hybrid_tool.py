"""
Tests for hybrid analysis (portfolio_agent/hybrid_tool.py): one question that needs both the SQL tool and the
independently verified, deterministic exposure calculation.

Gemini is always mocked. Nothing here is live evidence.
"""

from __future__ import annotations

import copy
import json
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import main as cli
from portfolio_agent import financial_verifier as fv
from portfolio_agent import sql_ast
from portfolio_agent.agent import PortfolioAgent
from portfolio_agent.database import PortfolioDatabase, ReadOnlyQueryError
from portfolio_agent.financial_verifier import FinancialVerifier
from portfolio_agent.hybrid_tool import HybridAnalysisTool, build_sql_question, is_hybrid_question
from portfolio_agent.sql_generator import GeminiSQLGenerator, RuleBasedSQLGenerator, UnsupportedQuestionError
from portfolio_agent.tools import ExposureCalculator, SQLQueryTool

HYBRID_Q = "Show the holdings and sector exposure breakdown for the International Equity Fund"
INTL = {"Energy": 38.0, "Technology": 36.0, "Automotive": 15.0, "Consumer Staples": 11.0}
STD_KEYS = {"status", "question", "selected_tool", "route_reason", "result", "error"}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = PortfolioDatabase()
        if not cls.db.is_initialized():
            cls.db.initialize_database()
        cls.calc = ExposureCalculator(db=cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def agent(self, sql_tool=None, exposure_tool=None, **kw):
        return PortfolioAgent(
            db=self.db,
            sql_tool=sql_tool or SQLQueryTool(db=self.db, use_gemini=False),
            exposure_tool=exposure_tool or self.calc,
            intent_classifier=None,
            **kw,
        )

    def holdings_count(self):
        return self.db.execute_read_only("SELECT COUNT(*) AS n FROM holdings")["rows"][0]["n"]

    def verifier_only_fault(self):
        real = self.db.execute_read_only

        def flaky(sql, params=None):
            if sql in (fv.SQL_PORTFOLIO, fv.SQL_HOLDINGS, fv.SQL_SECURITIES, fv.SQL_SECTORS):
                raise ReadOnlyQueryError("simulated database failure during verification")
            return real(sql, params)

        return patch.object(self.db, "execute_read_only", side_effect=flaky)


# ======================================================================================
# Routing
# ======================================================================================
class TestRouting(Base):
    def test_sql_only_questions_are_unchanged(self):
        agent = self.agent()
        for q in ("Show me the top 5 holdings by cost basis in the Growth Equity Fund", "How many portfolios do we have in total?",
                  "Which securities are in the Technology sector?", "What are the names of all active portfolios?"):
            with self.subTest(q):
                r = agent.answer_question(q)
                self.assertEqual((r["selected_tool"], r["status"]), ("sql_query", "success"))
                self.assertNotIn("components", r["result"])

    def test_exposure_only_questions_are_unchanged(self):
        agent = self.agent()
        for q, portfolio in (("What are the sector exposures for the Tech Innovation Fund?", "Tech Innovation Fund"),
                             ("Calculate the sector exposure breakdown for international equity", "International Equity Fund"),
                             ("What are the sector exposures of the International Equity Fund?", "International Equity Fund")):
            with self.subTest(q):
                r = agent.answer_question(q)
                self.assertEqual((r["selected_tool"], r["status"], r["result"]["portfolio_name"]), ("exposure_calculator", "success", portfolio))
                self.assertEqual(r["result"]["verification"]["status"], "verified")

    def test_none_of_the_official_questions_is_hybrid(self):
        gt = json.loads((PROJECT_ROOT / "ground_truth_dataset.json").read_text(encoding="utf-8"))["questions"]
        for q in gt:
            with self.subTest(q["id"]):
                self.assertFalse(is_hybrid_question(q["question"], exposure_signal=True))
                self.assertFalse(is_hybrid_question(q["question"], exposure_signal=False))

    def test_hybrid_phrasings(self):
        for q in (HYBRID_Q, "Show me the sector exposure and the holdings of Growth Equity Fund",
                  "What are the holdings and sector exposures for international equity?",
                  "List the securities, plus sector allocation, of the Balanced Portfolio",
                  "Give me the top 5 holdings by cost basis and the sector exposure for Growth Equity Fund"):
            with self.subTest(q):
                self.assertTrue(is_hybrid_question(q, True))
                self.assertFalse(is_hybrid_question(q, False))  # needs an exposure signal as well

    def test_a_data_word_alone_or_an_exposure_word_alone_is_not_hybrid(self):
        for q in ("Show the holdings of the Growth Equity Fund", "Show the sector exposure for the Balanced Portfolio",
                  "What is the sector exposure of the portfolio?", "Which portfolios hold the most stocks?"):
            self.assertFalse(is_hybrid_question(q, True), q)

    def test_hybrid_question_selects_the_hybrid_tool_with_both_components(self):
        r = self.agent().answer_question(HYBRID_Q)
        self.assertEqual(set(r), STD_KEYS)
        self.assertEqual((r["status"], r["selected_tool"]), ("success", "hybrid_analysis"))
        res = r["result"]
        self.assertEqual((res["tool"], res["complete"], res["status"]), ("hybrid_analysis", True, "success"))
        self.assertEqual(set(res["components"]), {"sql", "exposure"})
        self.assertEqual(res["component_status"], {"sql": "success", "exposure": "success"})
        self.assertEqual(res["portfolio"], {"portfolio_id": 7, "portfolio_name": "International Equity Fund"})
        self.assertIsNone(r["error"])

    def test_the_local_classifier_hint_also_reaches_the_hybrid_tool(self):
        from portfolio_agent.intent_classifier import Intent

        agent = self.agent()
        self.assertFalse(agent._is_exposure_question("Please show the positions plus the equity exposure by sector for the International Equity Fund"))
        q = "Please show the positions plus the equity exposure by sector for the International Equity Fund"  # no keyword rule matches
        self.assertEqual(agent._route_analytical(q, hint=Intent.SECTOR_EXPOSURE)["selected_tool"], "hybrid_analysis")
        self.assertNotEqual(agent._route_analytical(q, hint=Intent.PORTFOLIO_ANALYTICS)["selected_tool"], "hybrid_analysis")

    def test_hybrid_without_a_portfolio_asks_for_one(self):
        r = self.agent().answer_question("Show the holdings and sector exposure")
        self.assertEqual((r["status"], r["selected_tool"], r["result"]), ("error", "hybrid_analysis", None))
        self.assertIn("name one", r["error"])


# ======================================================================================
# Components and portfolio resolution
# ======================================================================================
class TestComponents(Base):
    def test_loosely_named_portfolio_resolves_to_one_canonical_portfolio_for_both_parts(self):
        r = self.agent().answer_question("Show the holdings and sector exposure breakdown for international equity")
        res = r["result"]
        self.assertEqual(res["portfolio"]["portfolio_name"], "International Equity Fund")
        self.assertEqual(res["components"]["exposure"]["portfolio_id"], 7)
        self.assertEqual(res["components"]["sql"]["query_parameters"], ["international equity fund"])
        self.assertTrue(all(row["symbol"] for row in res["components"]["sql"]["rows"]))
        self.assertIn("International Equity Fund", res["components"]["sql"]["question"])

    def test_unknown_or_ambiguous_portfolio_runs_neither_component(self):
        sql, calc = MagicMock(), MagicMock()
        calc.resolve_portfolio.side_effect = ValueError("Portfolio 'Atlantis Fund' not found.")
        r = PortfolioAgent(db=self.db, sql_tool=sql, exposure_tool=calc, intent_classifier=None).answer_question("Show the holdings and sector exposure for the Atlantis Fund")
        self.assertEqual((r["status"], r["selected_tool"]), ("error", "hybrid_analysis"))
        self.assertEqual(r["result"]["components"], {})
        self.assertEqual(r["result"]["component_status"], {"sql": "not_run", "exposure": "not_run"})
        sql.execute.assert_not_called()
        calc.calculate.assert_not_called()
        real = self.agent().answer_question("Show the holdings and sector exposure for the Atlantis Fund")
        self.assertEqual(real["status"], "error")
        self.assertIn("not found", real["error"])

    def test_sql_result_is_preserved_verbatim(self):
        r = self.agent().answer_question(HYBRID_Q)
        sql = r["result"]["components"]["sql"]
        direct = SQLQueryTool(db=self.db, use_gemini=False).execute("Show the holdings in the International Equity Fund")
        for key in ("status", "tool", "generated_sql", "query_parameters", "row_count", "rows", "columns", "generation_method", "model_name", "fallback_used", "fallback_reason"):
            self.assertEqual(sql[key], direct[key], key)
        self.assertEqual(sql["row_count"], 8)
        self.assertIn("SELECT", sql["generated_sql"])
        self.assertEqual(sql["query_parameters"], ["international equity fund"])  # bound, never interpolated
        self.assertEqual(sql["question"], "Show the holdings in the International Equity Fund")

    def test_exposure_is_the_deterministic_result_and_independently_verified(self):
        res = self.agent().answer_question(HYBRID_Q)["result"]["components"]["exposure"]
        calculated = self.calc.calculate("International Equity Fund")
        self.assertEqual({k: v for k, v in res.items() if k != "verification"}, calculated)
        self.assertEqual({e["sector"]: e["exposure_pct"] for e in res["exposures"]}, INTL)
        self.assertEqual((res["total_equity_weight_pct"], res["excluded_bond_holdings"]), (100.0, 0))
        self.assertEqual(res["verification"]["status"], "verified")

    def test_bonds_excluded_and_no_renormalisation_in_a_hybrid_answer(self):
        r = self.agent().answer_question("Show the holdings and sector exposure for the Balanced Portfolio")
        res = r["result"]["components"]
        self.assertEqual(r["status"], "success")
        self.assertGreater(res["exposure"]["excluded_bond_holdings"], 0)
        self.assertLess(res["exposure"]["total_equity_weight_pct"], 100.0)
        asset_types = {row["asset_type"] for row in res["sql"]["rows"]}
        self.assertEqual(asset_types, {"Stock", "Bond"})  # the data part lists bonds; the exposure part excludes them

    def test_the_verifier_is_applied_to_the_exposure_result(self):
        verifier = FinancialVerifier(self.db)
        with patch.object(verifier, "verify", wraps=verifier.verify) as spy:
            self.agent(financial_verifier=verifier).answer_question(HYBRID_Q)
        spy.assert_called_once()
        self.assertEqual(spy.call_args.args[0]["portfolio_id"], 7)

    def test_sql_sub_question_never_contains_exposure_wording(self):
        for q, portfolio in ((HYBRID_Q, "International Equity Fund"), ("Show me the sector exposure and the holdings of Growth Equity Fund", "Growth Equity Fund"),
                             ("List the securities, plus sector allocation, of the Balanced Portfolio", "Balanced Portfolio"),
                             ("Give me the top 5 holdings by cost basis and the sector exposure for Growth Equity Fund", "Growth Equity Fund")):
            sub = build_sql_question(q, portfolio, portfolio)
            with self.subTest(sub):
                self.assertIsNone(__import__("re").search(r"exposure|allocation|distribution|\bmix\b", sub, __import__("re").I))
                self.assertIn(portfolio, sub)
        self.assertEqual(build_sql_question("Show the holdings and sector exposure for international equity", "international equity", "International Equity Fund"),
                         "Show the holdings in the International Equity Fund")
        self.assertIn("top 5 holdings by cost basis", build_sql_question("Give me the top 5 holdings by cost basis and the sector exposure for Growth Equity Fund", "Growth Equity Fund", "Growth Equity Fund"))


# ======================================================================================
# Partial results and failures
# ======================================================================================
class TestPartialAndFailedResults(Base):
    def failing_sql(self, status="error", error="SQL generation failed (simulated)"):
        tool = MagicMock()
        tool.use_gemini = False
        tool.execute.return_value = {"status": status, "tool": "sql_query", "generated_sql": None, "query_parameters": None, "row_count": 0, "rows": [], "columns": [], "generation_method": "rule_based", "error": error}
        return tool

    def test_sql_fails_but_exposure_succeeds_is_partial_and_keeps_only_the_verified_exposure(self):
        r = self.agent(sql_tool=self.failing_sql()).answer_question(HYBRID_Q)
        res = r["result"]
        self.assertEqual((r["status"], res["status"], res["complete"]), ("partial", "partial", False))
        self.assertEqual(res["component_status"], {"sql": "error", "exposure": "success"})
        self.assertEqual(res["components"]["exposure"]["verification"]["status"], "verified")
        self.assertEqual({e["sector"]: e["exposure_pct"] for e in res["components"]["exposure"]["exposures"]}, INTL)
        self.assertEqual(res["components"]["sql"]["rows"], [])
        self.assertIn("SQL component error", r["error"])
        self.assertIn("simulated", r["error"])
        self.assertIn("Partial result", res["summary"])

    def test_sql_unsupported_is_reported_as_unsupported_not_success(self):
        r = self.agent(sql_tool=self.failing_sql("unsupported", "Question not recognized")).answer_question(HYBRID_Q)
        self.assertEqual(r["status"], "partial")
        self.assertEqual(r["result"]["component_status"]["sql"], "unsupported")

    def test_exposure_fails_but_sql_succeeds_is_partial(self):
        calc = MagicMock(wraps=self.calc)
        calc.calculate.return_value = {"status": "error", "tool": "exposure_calculator", "portfolio_id": 7, "portfolio_name": "International Equity Fund",
                                       "exposures": [], "total_equity_weight_pct": 0.0, "error": "Calculation failed: boom"}
        r = self.agent(exposure_tool=calc).answer_question(HYBRID_Q)
        res = r["result"]
        self.assertEqual((r["status"], res["complete"]), ("partial", False))
        self.assertEqual(res["component_status"], {"sql": "success", "exposure": "error"})
        self.assertEqual(res["components"]["sql"]["row_count"], 8)
        self.assertEqual(res["components"]["exposure"]["exposures"], [])
        self.assertIn("Exposure component error", r["error"])
        self.assertNotIn("verification", res["components"]["exposure"])  # an error result carries no verification claim

    def test_failed_verification_withholds_the_exposure_and_makes_the_answer_partial(self):
        calc = MagicMock(wraps=self.calc)
        bad = copy.deepcopy(self.calc.calculate("International Equity Fund"))
        bad["exposures"][0]["exposure_pct"] += 7.0
        calc.calculate.return_value = bad
        r = self.agent(exposure_tool=calc).answer_question(HYBRID_Q)
        res = r["result"]
        exposure = res["components"]["exposure"]
        self.assertEqual((r["status"], res["complete"]), ("partial", False))
        self.assertEqual((exposure["status"], exposure["exposures"], exposure["total_equity_weight_pct"]), ("error", [], 0.0))
        self.assertEqual(exposure["verification"]["status"], "failed")
        self.assertIn("failed independent verification", r["error"])
        self.assertEqual(res["components"]["sql"]["status"], "success")

    def test_unverified_exposure_is_withheld_and_distinct_from_failed(self):
        with self.verifier_only_fault():
            r = self.agent().answer_question(HYBRID_Q)
        exposure = r["result"]["components"]["exposure"]
        self.assertEqual((r["status"], exposure["status"], exposure["exposures"]), ("partial", "error", []))
        self.assertEqual(exposure["verification"]["status"], "unverified")
        self.assertIn("could not be independently verified", r["error"])
        self.assertNotIn("failed independent verification", r["error"])

    def test_nothing_succeeds_is_an_error(self):
        with self.verifier_only_fault():
            r = self.agent(sql_tool=self.failing_sql()).answer_question(HYBRID_Q)
        self.assertEqual((r["status"], r["result"]["complete"]), ("error", False))
        self.assertIn("Neither part", r["result"]["summary"])

    def test_a_partial_result_is_never_presented_as_complete(self):
        scenarios = []
        scenarios.append(self.agent(sql_tool=self.failing_sql()).answer_question(HYBRID_Q))
        with self.verifier_only_fault():
            scenarios.append(self.agent().answer_question(HYBRID_Q))
        for r in scenarios:
            self.assertNotEqual(r["status"], "success")
            self.assertFalse(r["result"]["complete"])
            self.assertIsNotNone(r["error"])
            self.assertTrue(r["result"]["issues"])

    def test_component_exceptions_are_contained(self):
        sql = MagicMock()
        sql.use_gemini = False
        sql.execute.side_effect = RuntimeError("sql tool exploded")
        r = self.agent(sql_tool=sql).answer_question(HYBRID_Q)
        self.assertEqual(r["status"], "partial")
        self.assertIn("sql tool exploded", r["error"])
        calc = MagicMock()
        calc.resolve_portfolio.return_value = (7, "International Equity Fund")
        calc.calculate.side_effect = RuntimeError("calculator exploded")
        r = self.agent(exposure_tool=calc).answer_question(HYBRID_Q)
        self.assertEqual(r["status"], "partial")
        self.assertIn("calculator exploded", r["error"])


# ======================================================================================
# Safety: AST validation, injection, Gemini boundaries
# ======================================================================================
class TestSafety(Base):
    def gemini_tool(self, sql_text, params=()):
        client = MagicMock()
        client.models.generate_content.return_value = MagicMock(text=json.dumps({"sql": sql_text, "parameters": list(params), "description": "x"}))
        gen = GeminiSQLGenerator(api_key="unit-test-placeholder", client=client, db=self.db)
        return SQLQueryTool(db=self.db, gemini_generator=gen, use_gemini=True), client

    def test_unsafe_gemini_sql_in_the_data_part_is_rejected_and_never_executed(self):
        before = self.holdings_count()
        for unsafe in ("DELETE FROM holdings", "SELECT * FROM sqlite_master", "SELECT 1; DROP TABLE holdings",
                       "SELECT * FROM pragma_table_info('holdings')", "SELECT load_extension('x')", "WITH a AS (SELECT 1) t.x FROM a"):
            with self.subTest(unsafe):
                tool, client = self.gemini_tool(unsafe)
                with patch.object(GeminiSQLGenerator, "is_configured", True):
                    r = self.agent(sql_tool=tool).answer_question(HYBRID_Q)
                sql = r["result"]["components"]["sql"]
                self.assertEqual(sql["generation_method"], "rule_based")  # fell back: the unsafe statement was not run
                self.assertTrue(sql["fallback_used"])
                self.assertRegex(sql["fallback_reason"], r"unsafe_sql|malformed_sql")
                self.assertEqual(r["result"]["components"]["exposure"]["verification"]["status"], "verified")
        self.assertEqual(self.holdings_count(), before)

    def test_injection_text_in_the_question_cannot_alter_the_database(self):
        before = self.holdings_count()
        for q in ("Show the holdings and sector exposure for International Equity Fund'; DROP TABLE holdings; --",
                  "Show the holdings and sector exposure for the International Equity Fund; DELETE FROM holdings",
                  "Show the holdings and sector exposure for \"International Equity Fund\" UNION SELECT * FROM sqlite_master"):
            with self.subTest(q):
                r = self.agent().answer_question(q)
                self.assertIn(r["selected_tool"], ("hybrid_analysis", "sql_query", "exposure_calculator"))
        self.assertEqual(self.holdings_count(), before)
        self.assertEqual(self.db.execute_read_only("SELECT COUNT(*) AS n FROM portfolios")["rows"][0]["n"], 13)

    def test_sqlglot_missing_fails_closed_for_both_components(self):
        before = self.holdings_count()
        with patch.object(sql_ast, "sqlglot", None):
            r = self.agent().answer_question(HYBRID_Q)
        # Even resolving the portfolio needs a validated query, so nothing at all ran without AST validation.
        self.assertEqual((r["status"], r["result"]["components"]), ("error", {}))
        self.assertEqual(r["result"]["component_status"], {"sql": "not_run", "exposure": "not_run"})
        self.assertIn("SQLGlot is required", r["error"])
        self.assertEqual(self.holdings_count(), before)

    def test_gemini_is_asked_only_for_the_data_part_and_never_for_exposure(self):
        tool, client = self.gemini_tool("SELECT s.symbol FROM holdings h JOIN securities s ON h.security_id = s.security_id JOIN portfolios p ON h.portfolio_id = p.portfolio_id WHERE p.portfolio_name = ?", ["International Equity Fund"])
        with patch.object(GeminiSQLGenerator, "is_configured", True):
            r = self.agent(sql_tool=tool).answer_question(HYBRID_Q)
        self.assertEqual(client.models.generate_content.call_count, 1)
        prompt = client.models.generate_content.call_args.kwargs["contents"].lower()
        for word in ("exposure", "allocation", "renormal"):
            self.assertNotIn(word, prompt)
        self.assertIn("international equity fund", prompt)
        res = r["result"]["components"]
        self.assertEqual(res["sql"]["generation_method"], "gemini")
        # the exposure numbers come from the calculator, whatever Gemini wrote
        self.assertEqual({e["sector"]: e["exposure_pct"] for e in res["exposure"]["exposures"]}, INTL)
        self.assertEqual(r["status"], "success")

    def test_exposure_only_questions_never_call_gemini(self):
        tool, client = self.gemini_tool("SELECT 1")
        with patch.object(GeminiSQLGenerator, "is_configured", True):
            r = self.agent(sql_tool=tool).answer_question("What are the sector exposures for the Tech Innovation Fund?")
        self.assertEqual(r["selected_tool"], "exposure_calculator")
        client.models.generate_content.assert_not_called()

    def test_no_gemini_when_the_classifier_is_unavailable(self):
        from portfolio_agent.intent_classifier import IntentClassifierUnavailable, LocalIntentClassifier

        tool, client = self.gemini_tool("SELECT 1")
        clf = LocalIntentClassifier(embedder=MagicMock(side_effect=RuntimeError("no model")), examples={"GREETING": ["hi"]})
        agent = PortfolioAgent(db=self.db, sql_tool=tool, exposure_tool=self.calc, intent_classifier=clf)
        with patch.object(GeminiSQLGenerator, "is_configured", True):
            r = agent.answer_question(HYBRID_Q)
        client.models.generate_content.assert_not_called()
        self.assertEqual(r["status"], "success")
        self.assertIn("classifier is unavailable", r["result"]["components"]["sql"]["fallback_reason"])


# ======================================================================================
# Contracts, API, CLI, generator rule
# ======================================================================================
class TestContractsAndInterfaces(Base):
    def test_simple_response_contracts_are_untouched(self):
        agent = self.agent()
        sql = agent.answer_question("How many portfolios do we have in total?")
        exposure = agent.answer_question("What are the sector exposures for the Tech Innovation Fund?")
        self.assertEqual(set(sql), STD_KEYS)
        self.assertEqual(set(exposure), STD_KEYS)
        self.assertEqual(sql["result"]["tool"], "sql_query")
        self.assertEqual(exposure["result"]["tool"], "exposure_calculator")
        self.assertEqual(sql["route_reason"], "Question routed to SQL query tool for database analysis.")
        self.assertNotIn("components", exposure["result"])

    def test_hybrid_response_is_json_serialisable(self):
        json.dumps(self.agent().answer_question(HYBRID_Q))

    def test_api_service_and_endpoint_return_the_hybrid_contract(self):
        try:
            from portfolio_api.service import PortfolioService
        except ImportError:
            self.skipTest("FastAPI adapter dependencies not installed")
        service = PortfolioService(use_llm=False, use_intent_classifier=False)
        r = service.ask(HYBRID_Q)
        self.assertEqual((r["status"], r["selected_tool"], r["result"]["complete"]), ("success", "hybrid_analysis", True))
        try:
            from fastapi.testclient import TestClient
            from portfolio_api.app import create_app

            client = TestClient(create_app(service), base_url="http://127.0.0.1:8000")
            body = client.post("/api/ask", json={"question": HYBRID_Q}).json()
            self.assertEqual(body["selected_tool"], "hybrid_analysis")
            self.assertEqual(body["result"]["components"]["exposure"]["verification"]["status"], "verified")
            plain = client.post("/api/ask", json={"question": "How many portfolios do we have in total?"}).json()
            self.assertEqual(plain["selected_tool"], "sql_query")
        except ImportError:
            pass

    def test_pretty_cli_shows_both_parts_and_flags_partial_results(self):
        full = cli._format_pretty(self.agent().answer_question(HYBRID_Q))
        for needle in ("Tool   : hybrid_analysis", "Complete : yes", "[SQL] status: success", "[Exposure] status: success | verification: VERIFIED", "Energy", "CVX"):
            self.assertIn(needle, full)
        failing = MagicMock()
        failing.use_gemini = False
        failing.execute.return_value = {"status": "error", "tool": "sql_query", "rows": [], "columns": [], "error": "boom"}
        partial = cli._format_pretty(self.agent(sql_tool=failing).answer_question(HYBRID_Q))
        self.assertIn("Complete : NO - PARTIAL", partial)
        self.assertIn("NOT a complete result", partial)
        self.assertIn("Energy", partial)  # the verified exposure is still shown

    def test_one_shot_cli_exit_code_for_partial_is_nonzero(self):
        # 'partial' is not in the zero-exit set (success / unsupported / no_equity_holdings / conversation).
        import inspect

        self.assertNotIn("partial", inspect.getsource(cli.main).split("return 0 if status in")[1].split("\n")[0])

    def test_holdings_rule_of_the_rule_based_generator_is_narrow(self):
        gen = RuleBasedSQLGenerator()
        sql, params = gen.generate("Show the holdings in the International Equity Fund")
        self.assertIn("WHERE LOWER(p.portfolio_name) = LOWER(?)", sql)
        self.assertEqual(params, ("international equity fund",))
        self.assertEqual(self.db.execute_read_only(sql, params)["row_count"], 8)
        # still unsupported, exactly as before: ranking, aggregates, exposure wording
        for q in ("Show the top holdings in the Growth Equity Fund", "What is the total value of holdings in the Balanced Portfolio", "What are the holdings by sector in the Balanced Portfolio"):
            with self.subTest(q), self.assertRaises(UnsupportedQuestionError):
                gen.generate(q)
        # and it never takes over a ground-truth pattern
        self.assertIn("cost_basis", gen.generate("Show me the top 5 holdings by cost basis in the Growth Equity Fund")[0])
        self.assertIn("ORDER BY h.cost_basis DESC", gen.generate("Show me the top 5 holdings by cost basis in the Growth Equity Fund")[0])

    def test_portfolio_name_extraction_prefers_the_longer_keyword(self):
        agent = self.agent()
        self.assertEqual(agent._extract_portfolio_name("What are the sector exposures of the International Equity Fund?"), "International Equity Fund")
        self.assertEqual(agent._extract_portfolio_name("What are the sector exposures for the Tech Innovation Fund?"), "Tech Innovation Fund")

    def test_hybrid_tool_can_be_used_directly(self):
        tool = HybridAnalysisTool(SQLQueryTool(db=self.db, use_gemini=False), self.calc, FinancialVerifier(self.db))
        res = tool.execute(HYBRID_Q, "International Equity Fund")
        self.assertEqual((res["status"], res["portfolio"]["portfolio_id"]), ("success", 7))
        self.assertEqual(res["error"], None)


if __name__ == "__main__":
    unittest.main()
