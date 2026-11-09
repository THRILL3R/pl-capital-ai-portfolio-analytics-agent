"""
Tests for final-answer verification (portfolio_agent/answer_verifier.py).

The verifier checks that a response is consistent with its structured tool results. It performs no financial
calculation, touches no database and calls no model; these tests prove that as well as exercising every check.
Gemini is mocked wherever it appears.
"""

from __future__ import annotations

import ast
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import main as cli
from portfolio_agent import answer_verifier as av
from portfolio_agent import financial_verifier as fv
from portfolio_agent.agent import PortfolioAgent
from portfolio_agent.answer_verifier import FAILED, NOT_APPLICABLE, UNVERIFIED, VERIFIED, AnswerVerifier
from portfolio_agent.database import PortfolioDatabase, ReadOnlyQueryError
from portfolio_agent.scenario_verifier import ScenarioVerifier, scenario_digest
from portfolio_agent.sql_generator import GeminiSQLGenerator
from portfolio_agent.tools import ExposureCalculator, SQLQueryTool

HYBRID_Q = "Show the holdings and sector exposure breakdown for the International Equity Fund"
EXPOSURE_Q = "Calculate the sector exposure breakdown for international equity"
STD_KEYS = {"status", "question", "selected_tool", "route_reason", "result", "error"}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = PortfolioDatabase()
        if not cls.db.is_initialized():
            cls.db.initialize_database()
        cls.calc = ExposureCalculator(db=cls.db)
        cls.verifier = AnswerVerifier()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def agent(self, answer_verifier=None, **kw):
        kw.setdefault("sql_tool", SQLQueryTool(db=self.db, use_gemini=False))
        kw.setdefault("exposure_tool", self.calc)
        return PortfolioAgent(db=self.db, intent_classifier=None, answer_verifier=answer_verifier, **kw)

    def raw(self, question, **kw):
        """The agent's response BEFORE answer verification (answer_verifier disabled)."""
        return self.agent(**kw).answer_question(question)

    def block(self, response):
        return response["result"]["answer_verification"]

    def codes(self, response):
        return {m["code"] for m in self.block(response)["mismatches"]}

    def verifier_only_fault(self):
        real = self.db.execute_read_only

        def flaky(sql, params=None):
            if sql in (fv.SQL_PORTFOLIO, fv.SQL_HOLDINGS, fv.SQL_SECURITIES, fv.SQL_SECTORS):
                raise ReadOnlyQueryError("simulated database failure during verification")
            return real(sql, params)

        return patch.object(self.db, "execute_read_only", side_effect=flaky)


# ======================================================================================
# SQL responses
# ======================================================================================
class TestSqlResponses(Base):
    def test_valid_sql_responses_verify_and_carry_a_deterministic_summary(self):
        gt = json.loads((PROJECT_ROOT / "ground_truth_dataset.json").read_text(encoding="utf-8"))["questions"]
        for q in (x for x in gt if x["type"] == "text2sql"):
            with self.subTest(q["id"]):
                out = self.verifier.verify_response(self.raw(q["question"]))
                b = self.block(out)
                self.assertEqual((b["status"], b["verified"], b["mismatch_count"]), (VERIFIED, True, 0))
                self.assertGreater(b["checks_run"], 5)
                self.assertRegex(b["summary"], r"^\d+ rows? returned")
                self.assertEqual(out["status"], "success")

    def test_the_block_contract(self):
        b = self.block(self.verifier.verify_response(self.raw("How many portfolios do we have in total?")))
        for key in ("status", "verified", "method", "scope", "checks_run", "mismatch_count", "mismatches", "checks_skipped", "unverified_claims", "summary", "error"):
            self.assertIn(key, b)
        self.assertIn("No financial calculation", b["method"])
        self.assertEqual(b["checks_skipped"], [])

    def test_incorrect_row_count_fails_and_the_data_is_withheld(self):
        r = self.raw("What are the names of all active portfolios?")
        self.assertEqual(r["result"]["row_count"], 10)
        r["result"]["row_count"] = 3
        out = self.verifier.verify_response(r)
        self.assertEqual(self.block(out)["status"], FAILED)
        self.assertIn("row_count_mismatch", self.codes(out))
        res = out["result"]
        self.assertEqual((out["status"], res["status"], res["rows"], res["row_count"], res["columns"]), ("error", "error", [], 0, []))
        self.assertIn("withheld", out["error"])
        rejected = self.block(out)["rejected_result"]["result"]
        self.assertEqual((rejected["row_count_reported"], rejected["rows_returned"]), (3, 10))  # counts kept for diagnosis, not the rows
        self.assertNotIn("rows", rejected)

    def test_inconsistent_columns_and_rows_fail(self):
        base = self.raw("What are the names of all active portfolios?")
        mutations = {
            "row lacks a declared column": lambda r: r["rows"][0].pop("portfolio_name"),
            "row has an undeclared column": lambda r: r["rows"][0].update(extra=1),
            "row is not an object": lambda r: r["rows"].__setitem__(0, ["Growth Equity Fund"]),
            "columns renamed": lambda r: r.__setitem__("columns", ["name"]),
        }
        for label, mutate in mutations.items():
            with self.subTest(label):
                r = copy.deepcopy(base)
                mutate(r["result"])
                out = self.verifier.verify_response(r)
                self.assertEqual(self.block(out)["status"], FAILED)
                self.assertIn("row_structure_mismatch", self.codes(out))
                self.assertEqual(out["result"]["rows"], [])
        for bad in ("oops", None):
            with self.subTest(f"columns={bad!r}"):
                r = copy.deepcopy(base)
                r["result"]["columns"] = bad
                self.assertEqual(self.block(self.verifier.verify_response(r))["status"], FAILED)

    def test_duplicate_column_names_do_not_cause_false_alarms(self):
        r = self.raw("How many portfolios do we have in total?")
        r["result"].update(columns=["id", "id"], rows=[{"id": 1}], row_count=1)
        self.assertEqual(self.block(self.verifier.verify_response(r))["status"], VERIFIED)

    def test_other_sql_inconsistencies(self):
        base = self.raw("How many portfolios do we have in total?")
        cases = {
            "status_mismatch": lambda x: x.__setitem__("status", "error"),
            "missing_sql": lambda x: x["result"].__setitem__("generated_sql", ""),
            "parameters_invalid": lambda x: x["result"].__setitem__("query_parameters", "bad"),
            "fallback_unexplained": lambda x: x["result"].update(fallback_used=True, fallback_reason=None),
            "fallback_label_mismatch": lambda x: x["result"].update(generation_method="gemini", fallback_used=True, fallback_reason="x"),
            "sql_status_invalid": lambda x: x["result"].__setitem__("status", "weird"),
            "tool_mismatch": lambda x: x.__setitem__("selected_tool", "exposure_calculator"),
        }
        for code, mutate in cases.items():
            with self.subTest(code):
                r = copy.deepcopy(base)
                mutate(r)
                out = self.verifier.verify_response(r)
                self.assertEqual(self.block(out)["status"], FAILED)
                self.assertIn(code, self.codes(out))

    def test_a_failed_sql_result_must_not_carry_rows(self):
        r = self.raw("What is the meaning of life?")
        self.assertEqual(r["status"], "unsupported")
        out = self.verifier.verify_response(copy.deepcopy(r))
        self.assertEqual(self.block(out)["status"], NOT_APPLICABLE)  # an unsupported answer presents no analytical claim
        self.assertEqual(out["status"], "unsupported")
        leaking = copy.deepcopy(r)
        leaking["result"].update(rows=[{"x": 1}], columns=["x"], row_count=1)
        out = self.verifier.verify_response(leaking)
        self.assertEqual(self.block(out)["status"], FAILED)
        self.assertEqual((out["result"]["rows"], out["status"]), ([], "unsupported"))  # cleared; never turned into success

    def test_an_error_is_never_silently_turned_into_success(self):
        r = self.raw("How many portfolios do we have in total?")
        r["result"]["status"] = "error"
        r["status"] = "error"
        out = self.verifier.verify_response(r)
        self.assertEqual(out["status"], "error")
        self.assertEqual(out["result"]["rows"], [])

    def test_model_written_description_is_reported_as_an_unverified_claim(self):
        r = self.raw("How many portfolios do we have in total?")
        r["result"].update(generation_method="gemini", model_name="gemini-test", query_description="Total AUM is exactly $999 billion")
        out = self.verifier.verify_response(r)
        b = self.block(out)
        self.assertEqual(b["status"], VERIFIED)                 # the structured data is consistent ...
        self.assertEqual(b["unverified_claims"], ["query_description"])   # ... the sentence is not vouched for
        self.assertNotIn("999", b["summary"])                   # the summary never repeats model text
        self.assertEqual(out["result"]["query_description"], "Total AUM is exactly $999 billion")  # untouched, labelled by the UI
        none = self.verifier.verify_response(self.raw("How many portfolios do we have in total?"))
        self.assertEqual(self.block(none)["unverified_claims"], [])

    def test_responses_without_an_analytical_result_are_left_alone(self):
        agent = self.agent(answer_verifier=AnswerVerifier())
        for q in ("hello", "what can you do", "Show sector exposure for the Atlantis Fund"):
            r = agent.answer_question(q)
            self.assertFalse(isinstance((r.get("result") or {}).get("answer_verification"), dict) and r["result"].get("tool") == "conversation")
        conv = agent.answer_question("hello")
        self.assertNotIn("answer_verification", conv["result"])
        for odd in (None, {}, {"result": None}, {"result": {"tool": "other"}}, {"result": "text"}):
            self.assertEqual(self.verifier.verify_response(odd), odd)


# ======================================================================================
# Exposure responses
# ======================================================================================
class TestExposureResponses(Base):
    def test_verified_exposure_response(self):
        out = self.agent(answer_verifier=AnswerVerifier()).answer_question(EXPOSURE_Q)
        b = self.block(out)
        self.assertEqual((b["status"], b["mismatch_count"], out["status"]), (VERIFIED, 0, "success"))
        self.assertEqual(b["summary"], "Equity sector exposure for International Equity Fund: 4 sectors, bonds excluded.")
        self.assertEqual(out["result"]["verification"]["status"], "verified")
        self.assertEqual(out["result"]["verification"]["figures_digest"], fv.figures_digest(out["result"]))
        self.assertEqual(b["checks_skipped"], [])

    def test_no_equity_holdings_is_verified_and_described(self):
        out = self.agent(answer_verifier=AnswerVerifier()).answer_question("Show sector exposure for Fixed Income Plus")
        self.assertEqual((out["status"], self.block(out)["status"]), ("no_equity_holdings", VERIFIED))
        self.assertIn("no equity holdings", self.block(out)["summary"])

    def test_figures_that_changed_after_verification_are_detected_and_withheld(self):
        r = self.raw(EXPOSURE_Q)
        r["result"]["exposures"][0]["exposure_pct"] = 99.0   # tampered after the financial verification
        out = self.verifier.verify_response(r)
        self.assertEqual(self.block(out)["status"], FAILED)
        self.assertIn("figures_changed_after_verification", self.codes(out))
        self.assertEqual((out["status"], out["result"]["exposures"], out["result"]["total_equity_weight_pct"]), ("error", [], 0.0))
        self.assertEqual(self.block(out)["rejected_result"]["result"]["exposures"][0]["exposure_pct"], 99.0)  # diagnosis only

    def test_unverified_financial_status_with_figures_shown_fails(self):
        r = self.raw(EXPOSURE_Q)
        r["result"]["verification"]["status"] = "unverified"
        r["result"]["verification"]["verified"] = False
        out = self.verifier.verify_response(r)
        self.assertEqual(self.block(out)["status"], FAILED)
        self.assertIn("exposure_not_verified", self.codes(out))
        self.assertEqual(out["result"]["exposures"], [])

    def test_failed_financial_verification_stays_withheld_and_is_reported_as_failed(self):
        bad = copy.deepcopy(self.calc.calculate("International Equity Fund"))
        bad["exposures"][0]["exposure_pct"] += 7.0
        fake = MagicMock()
        fake.calculate.return_value = bad
        out = self.agent(answer_verifier=AnswerVerifier(), exposure_tool=fake).answer_question(EXPOSURE_Q)
        self.assertEqual((out["status"], out["result"]["exposures"], out["result"]["total_equity_weight_pct"]), ("error", [], 0.0))
        self.assertEqual(self.block(out)["status"], FAILED)
        self.assertIn("financial_verification_failed", self.codes(out))
        self.assertIn("failed independent verification", out["error"])  # the Phase 4.1 message is preserved

    def test_unverified_financial_status_stays_withheld_and_is_reported_as_unverified(self):
        agent = self.agent(answer_verifier=AnswerVerifier())
        with self.verifier_only_fault():
            out = agent.answer_question(EXPOSURE_Q)
        self.assertEqual((out["status"], out["result"]["exposures"]), ("error", []))
        self.assertEqual(self.block(out)["status"], UNVERIFIED)
        self.assertEqual(self.block(out)["mismatch_count"], 0)  # nothing was disproved
        self.assertIn("could not be completed", out["error"])

    def test_leaked_figures_on_a_withheld_result_are_removed_again(self):
        r = self.raw(EXPOSURE_Q)
        good_exposures = copy.deepcopy(r["result"]["exposures"])
        r["status"] = r["result"]["status"] = "error"
        r["result"]["verification"] = {"status": "failed", "verified": False, "mismatch_count": 2}
        out = self.verifier.verify_response(r)
        self.assertIn("withheld_figures_shown", self.codes(out))
        self.assertEqual((out["result"]["exposures"], out["result"]["total_equity_weight_pct"]), ([], 0.0))
        self.assertEqual(self.block(out)["rejected_result"]["result"]["exposures"], good_exposures)

    def test_disabled_financial_verification_withholds_the_figures(self):
        out = self.agent(answer_verifier=AnswerVerifier(), financial_verifier=None).answer_question(EXPOSURE_Q)
        b = self.block(out)
        self.assertEqual(b["status"], UNVERIFIED)               # distinct from "failed": nothing was disproved
        self.assertEqual(b["mismatch_count"], 0)
        self.assertFalse(b["verified"])
        self.assertEqual((out["status"], out["result"]["exposures"], out["result"]["total_equity_weight_pct"]), ("error", [], 0.0))
        self.assertIn("disabled or unavailable", out["error"])
        self.assertEqual(out["result"]["verification"]["status"], UNVERIFIED)

    def test_the_answer_verifier_alone_withholds_figures_that_carry_no_verification_block(self):
        r = self.raw(EXPOSURE_Q)
        del r["result"]["verification"]
        out = self.verifier.verify_response(r)
        b = self.block(out)
        self.assertEqual(b["status"], UNVERIFIED)
        self.assertTrue(b["checks_skipped"])
        self.assertEqual((out["status"], out["result"]["exposures"], out["result"]["total_equity_weight_pct"]), ("error", [], 0.0))
        self.assertEqual(len(b["rejected_result"]["result"]["exposures"]), 4)   # diagnostics only, never the normal result

    def test_disabled_verification_keeps_failed_and_unverified_distinct(self):
        disabled = self.agent(answer_verifier=AnswerVerifier(), financial_verifier=None).answer_question(EXPOSURE_Q)
        bad = copy.deepcopy(self.calc.calculate("International Equity Fund"))
        bad["exposures"][0]["exposure_pct"] += 7.0
        fake = MagicMock()
        fake.calculate.return_value = bad
        failed = self.agent(answer_verifier=AnswerVerifier(), exposure_tool=fake).answer_question(EXPOSURE_Q)
        self.assertEqual((self.block(disabled)["status"], disabled["result"]["verification"]["status"]), (UNVERIFIED, UNVERIFIED))
        self.assertEqual((self.block(failed)["status"], failed["result"]["verification"]["status"]), (FAILED, "failed"))
        self.assertIn("could not be completed", disabled["error"])
        self.assertIn("failed independent verification", failed["error"])
        for out in (disabled, failed):
            self.assertEqual(out["result"]["exposures"], [])

    def test_disabled_verification_in_the_cli_and_api_follows_the_same_policy(self):
        agent = self.agent(answer_verifier=AnswerVerifier(), financial_verifier=None)
        out = agent.answer_question(EXPOSURE_Q)
        text = cli._format_pretty(out)
        self.assertIn("Verification: UNVERIFIED", text)
        self.assertIn("Answer verification: UNVERIFIED", text)
        self.assertIn("withheld", text)
        self.assertNotIn("| Energy", text)
        self.assertNotIn("Exposure %", text)
        try:
            from portfolio_api.service import PortfolioService
        except ImportError:
            return
        service = PortfolioService(use_llm=False, use_intent_classifier=False)
        service_agent = PortfolioAgent(db=service.db, sql_tool=service.sql_tool, exposure_tool=service.exposure_tool, intent_classifier=None, financial_verifier=None)
        a = service_agent.answer_question(EXPOSURE_Q)["result"]
        self.assertEqual((a["status"], a["exposures"], a["verification"]["status"]), ("error", [], UNVERIFIED))
        with patch.object(service.exposure_tool, "calculate", return_value={**service.exposure_tool.calculate("International Equity Fund")}):
            ok = service.exposure("International Equity Fund")          # the API endpoint always runs the real verifier
        self.assertEqual(ok["verification"]["status"], VERIFIED)

    def test_hybrid_cannot_mark_exposure_verified_when_the_verifier_is_disabled(self):
        out = self.agent(answer_verifier=AnswerVerifier(), financial_verifier=None).answer_question(HYBRID_Q)
        exposure = out["result"]["components"]["exposure"]
        self.assertEqual((out["status"], out["result"]["complete"]), ("partial", False))
        self.assertEqual(exposure["verification"]["status"], UNVERIFIED)
        self.assertNotIn("figures_digest", exposure["verification"])             # a digest is issued only for verified figures
        self.assertEqual((exposure["status"], exposure["exposures"], exposure["total_equity_weight_pct"]), ("error", [], 0.0))
        self.assertIn("could not be independently verified", out["error"])
        self.assertEqual(out["result"]["components"]["sql"]["row_count"], 8)      # the SQL part is unaffected
        self.assertNotEqual(self.block(out)["status"], VERIFIED)
        # the same with the answer verifier switched off: the hybrid tool itself withholds
        raw = self.raw(HYBRID_Q, financial_verifier=None)
        self.assertEqual(raw["result"]["components"]["exposure"]["exposures"], [])
        self.assertEqual(raw["result"]["components"]["exposure"]["verification"]["status"], UNVERIFIED)
        self.assertEqual(raw["status"], "partial")

    def test_a_calculation_error_needs_no_verification_even_when_disabled(self):
        out = self.agent(answer_verifier=AnswerVerifier(), financial_verifier=None).answer_question("Show sector exposure for the Atlantis Fund")
        self.assertEqual(out["status"], "error")
        self.assertNotIn("verification", out["result"] or {})

    def test_identity_count_and_structure_checks(self):
        base = self.raw(EXPOSURE_Q)
        cases = {
            "identity_missing": lambda r: r.__setitem__("portfolio_name", ""),
            "counts_invalid": lambda r: r.__setitem__("equity_holdings_count", -1),
            "exposures_malformed": lambda r: r["exposures"][0].pop("holding_count"),
            "sector_count_exceeds_holdings": lambda r: r.__setitem__("equity_holdings_count", 2),
            "status_mismatch": lambda r: r.__setitem__("status", "no_equity_holdings"),
        }
        for code, mutate in cases.items():
            with self.subTest(code):
                r = copy.deepcopy(base)
                mutate(r["result"])
                if code == "status_mismatch":
                    r["status"] = "no_equity_holdings"
                out = self.verifier.verify_response(r)
                self.assertEqual(self.block(out)["status"], FAILED)
                self.assertEqual(out["result"]["exposures"], [])


# ======================================================================================
# Hybrid responses
# ======================================================================================
class TestHybridResponses(Base):
    def test_a_complete_hybrid_verifies(self):
        out = self.agent(answer_verifier=AnswerVerifier()).answer_question(HYBRID_Q)
        b = self.block(out)
        self.assertEqual((b["status"], b["mismatch_count"], out["status"], out["result"]["complete"]), (VERIFIED, 0, "success", True))
        self.assertTrue(out["result"]["summary"].startswith("Both parts completed"))

    def raw_hybrid(self, **kw):
        return self.raw(HYBRID_Q, **kw)

    def failing_sql(self):
        tool = MagicMock()
        tool.use_gemini = False
        tool.execute.return_value = {"status": "error", "tool": "sql_query", "rows": [], "columns": [], "row_count": 0, "error": "boom", "generation_method": "rule_based"}
        return tool

    def test_portfolio_identity_mismatches_fail_and_withhold_both_parts(self):
        base = self.raw_hybrid()
        cases = {
            "portfolio id differs": lambda r: r["portfolio"].__setitem__("portfolio_id", 3),
            "portfolio name differs": lambda r: r["portfolio"].__setitem__("portfolio_name", "Growth Equity Fund"),
            "sql asked about another portfolio": lambda r: r["components"]["sql"].__setitem__("question", "Show the holdings in the Growth Equity Fund"),
            "sql rows from another portfolio": lambda r: r["components"]["sql"]["rows"][0].update(portfolio_name="Balanced Portfolio") or r["components"]["sql"]["columns"].append("portfolio_name") or [row.update(portfolio_name="Balanced Portfolio") for row in r["components"]["sql"]["rows"]],
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                r = copy.deepcopy(base)
                mutate(r["result"])
                out = self.verifier.verify_response(r)
                self.assertEqual(self.block(out)["status"], FAILED)
                self.assertTrue(self.codes(out) & {"portfolio_identity_mismatch", "sql_portfolio_mismatch", "sql_rows_portfolio_mismatch"})
                self.assertEqual((out["status"], out["result"]["complete"]), ("error", False))
                comps = out["result"]["components"]
                self.assertEqual((comps["sql"]["rows"], comps["exposure"]["exposures"]), ([], []))

    def test_component_status_mismatch(self):
        r = self.raw_hybrid()
        r["result"]["component_status"]["sql"] = "error"
        out = self.verifier.verify_response(r)
        self.assertEqual(self.block(out)["status"], FAILED)
        self.assertIn("component_status_mismatch", self.codes(out))
        self.assertEqual(out["result"]["component_status"], {"sql": "success", "exposure": "success"})  # recomputed from the components

    def test_complete_flag_can_only_be_true_when_both_components_succeed(self):
        partial = self.raw_hybrid(sql_tool=self.failing_sql())
        self.assertEqual((partial["status"], partial["result"]["complete"]), ("partial", False))
        lying = copy.deepcopy(partial)
        lying["result"]["complete"] = True
        out = self.verifier.verify_response(lying)
        self.assertIn("complete_flag_mismatch", self.codes(out))
        self.assertIs(out["result"]["complete"], False)
        self.assertEqual(out["status"], "partial")
        # the reverse (complete=false on a success) is also reported, and is never "upgraded" to true
        success = self.raw_hybrid()
        success["result"]["complete"] = False
        out = self.verifier.verify_response(success)
        self.assertIn("complete_flag_mismatch", self.codes(out))
        self.assertIs(out["result"]["complete"], False)

    def test_overall_status_must_match_the_components(self):
        r = self.raw_hybrid(sql_tool=self.failing_sql())
        r["status"] = r["result"]["status"] = "success"
        r["result"]["complete"] = True
        out = self.verifier.verify_response(r)
        self.assertIn("overall_status_mismatch", self.codes(out))
        self.assertEqual((out["status"], out["result"]["complete"]), ("partial", False))  # never more successful than the evidence
        under = self.raw_hybrid()
        under["status"] = under["result"]["status"] = "error"
        out = self.verifier.verify_response(under)
        self.assertEqual(out["status"], "error")                 # never upgraded either

    def test_summary_and_issues_must_describe_the_outcome(self):
        r = self.raw_hybrid(sql_tool=self.failing_sql())
        r["result"]["summary"] = "Both parts completed: everything is fine."
        out = self.verifier.verify_response(r)
        self.assertIn("summary_mismatch", self.codes(out))
        self.assertTrue(out["result"]["summary"].startswith("Partial result"))
        r2 = self.raw_hybrid(sql_tool=self.failing_sql())
        r2["result"]["issues"] = ["Exposure component error: something unrelated"]
        self.assertIn("summary_omits_failure", self.codes(self.verifier.verify_response(r2)))
        r3 = self.raw_hybrid(sql_tool=self.failing_sql())
        r3["result"]["error"] = None
        self.assertIn("error_field_mismatch", self.codes(self.verifier.verify_response(r3)))

    def test_sql_success_with_exposure_failure_stays_partial_and_verifies_what_it_shows(self):
        calc = MagicMock(wraps=self.calc)
        calc.calculate.return_value = {"status": "error", "tool": "exposure_calculator", "portfolio_id": 7, "portfolio_name": "International Equity Fund",
                                       "exposures": [], "total_equity_weight_pct": 0.0, "error": "Calculation failed: boom"}
        out = self.agent(answer_verifier=AnswerVerifier(), exposure_tool=calc).answer_question(HYBRID_Q)
        self.assertEqual((out["status"], out["result"]["complete"]), ("partial", False))
        self.assertEqual(out["result"]["components"]["sql"]["row_count"], 8)
        self.assertEqual(self.block(out)["status"], VERIFIED)    # what is displayed (the SQL table) is consistent
        self.assertIn("Partial result", out["result"]["summary"])

    def test_exposure_success_with_sql_failure_stays_partial(self):
        out = self.agent(answer_verifier=AnswerVerifier(), sql_tool=self.failing_sql()).answer_question(HYBRID_Q)
        self.assertEqual((out["status"], out["result"]["complete"]), ("partial", False))
        self.assertEqual(out["result"]["components"]["exposure"]["verification"]["status"], "verified")
        self.assertEqual(self.block(out)["status"], VERIFIED)
        self.assertNotEqual(out["result"]["summary"].split(" ")[0], "Both")

    def test_a_bad_sql_component_is_withheld_but_the_verified_exposure_is_kept(self):
        r = self.raw_hybrid()
        r["result"]["components"]["sql"]["row_count"] = 99
        out = self.verifier.verify_response(r)
        res = out["result"]
        self.assertEqual(self.block(out)["status"], FAILED)
        self.assertEqual((out["status"], res["complete"], res["component_status"]), ("partial", False, {"sql": "error", "exposure": "success"}))
        self.assertEqual(res["components"]["sql"]["rows"], [])
        self.assertEqual(len(res["components"]["exposure"]["exposures"]), 4)
        self.assertIn("SQL component failed answer verification", res["error"])

    def test_a_bad_exposure_component_is_withheld_but_the_sql_table_is_kept(self):
        r = self.raw_hybrid()
        r["result"]["components"]["exposure"]["exposures"][0]["exposure_pct"] = 1.0
        out = self.verifier.verify_response(r)
        res = out["result"]
        self.assertEqual((out["status"], res["complete"]), ("partial", False))
        self.assertEqual(res["components"]["exposure"]["exposures"], [])
        self.assertEqual(res["components"]["sql"]["row_count"], 8)

    def test_both_components_bad_is_an_error(self):
        r = self.raw_hybrid()
        r["result"]["components"]["sql"]["row_count"] = 99
        r["result"]["components"]["exposure"]["exposures"][0]["exposure_pct"] = 1.0
        out = self.verifier.verify_response(r)
        self.assertEqual((out["status"], out["result"]["complete"]), ("error", False))
        self.assertTrue(out["result"]["summary"].startswith("Neither part"))

    def test_database_failure_during_financial_verification_gives_a_labelled_partial(self):
        agent = self.agent(answer_verifier=AnswerVerifier())
        with self.verifier_only_fault():
            out = agent.answer_question(HYBRID_Q)
        self.assertEqual((out["status"], out["result"]["complete"]), ("partial", False))
        self.assertEqual(out["result"]["components"]["exposure"]["exposures"], [])
        self.assertEqual(self.block(out)["status"], UNVERIFIED)
        self.assertEqual(out["result"]["components"]["sql"]["row_count"], 8)

    def test_unresolved_portfolio_hybrid_is_not_flagged(self):
        out = self.agent(answer_verifier=AnswerVerifier()).answer_question("Show the holdings and sector exposure for the Atlantis Fund")
        self.assertEqual(out["status"], "error")
        self.assertEqual(self.block(out)["status"], NOT_APPLICABLE)


# ======================================================================================
# Robustness, independence, interfaces
# ======================================================================================
class TestScenarioResponses(Base):
    """Scenario answers: the arithmetic is verified independently by ScenarioVerifier; AnswerVerifier checks structure and provenance."""

    Q = "Stress test the International Equity Fund under a 20% market decline."

    def good(self, question=None):
        return self.raw(question or self.Q)          # response before answer verification (carries ScenarioVerifier's verdict)

    def mutated(self, mutate, question=None):
        r = copy.deepcopy(self.good(question))
        mutate(r["result"])
        return r

    def assert_withheld(self, out, code=None, status=FAILED):
        res = out["result"]
        self.assertEqual(self.block(out)["status"], status)
        if code:
            self.assertIn(code, self.codes(out))
        self.assertEqual(out["status"], "error")
        self.assertEqual((res["sector_impacts"], res["starting_exposure"], res["estimated_portfolio_impact_pct"], res["estimated_value_impact"], res["coverage"]),
                         ([], [], None, None, None))
        self.assertNotIn(out["status"], ("success", "no_equity_holdings"))

    # ---- accepted ---------------------------------------------------------------------------------------------

    def test_verified_scenario_is_accepted(self):
        out = self.verifier.verify_response(self.good())
        b = self.block(out)
        self.assertEqual((b["status"], b["verified"], b["mismatch_count"], out["status"]), (VERIFIED, True, 0, "success"))
        self.assertGreater(b["checks_run"], 20)
        self.assertEqual(b["checks_skipped"], [])
        self.assertIn("Hypothetical scenario", b["summary"])
        self.assertIn("International Equity Fund", b["summary"])
        self.assertEqual(out["result"]["verification"]["figures_digest"], scenario_digest(out["result"]))
        self.assertEqual(len(out["result"]["sector_impacts"]), 4)

    def test_every_portfolio_and_scenario_is_accepted(self):
        rows = self.db.execute_read_only("SELECT portfolio_name FROM portfolios")["rows"]
        for p in rows:
            for q in (f"Run a severe market stress scenario for the {p['portfolio_name']}.", f"Stress test the {p['portfolio_name']} under a 10% market decline."):
                with self.subTest(portfolio=p["portfolio_name"], question=q[:25]):
                    out = self.verifier.verify_response(self.raw(q))
                    self.assertIn(out["result"]["tool"], ("scenario_analysis",))
                    self.assertEqual(self.block(out)["status"], VERIFIED, self.block(out)["mismatches"])

    def test_bond_only_portfolio_and_missing_aum_are_accepted(self):
        out = self.verifier.verify_response(self.raw("Run a severe market stress scenario for the Fixed Income Plus."))
        self.assertEqual((out["status"], self.block(out)["status"]), ("no_equity_holdings", VERIFIED))
        self.assertIn("nothing to shock", self.block(out)["summary"])

    def test_the_agent_attaches_the_block_by_default_and_errors_are_not_applicable(self):
        agent = self.agent(answer_verifier=AnswerVerifier())
        out = agent.answer_question(self.Q)
        self.assertEqual((out["selected_tool"], out["status"], self.block(out)["status"]), ("scenario_analysis", "success", VERIFIED))
        invalid = agent.answer_question("Stress test the International Equity Fund under a 250% market decline")
        self.assertEqual((invalid["status"], self.block(invalid)["status"]), ("error", NOT_APPLICABLE))
        self.assertIn("between -100%", invalid["error"])
        unresolved = agent.answer_question("Run a stress test for the Atlantis Fund under a 20% market decline")
        self.assertEqual((unresolved["status"], self.block(unresolved)["status"]), ("error", NOT_APPLICABLE))
        none = agent.answer_question("What happens to the Technology exposure if that sector falls 25%?")
        self.assertNotIn("answer_verification", none["result"] or {})   # no result object at all: a request for the portfolio

    # ---- inconsistent results are rejected and withheld ----------------------------------------------------------

    def test_structural_inconsistencies_are_rejected_and_withheld(self):
        cases = {
            "contribution_sign_mismatch": lambda r: r["sector_impacts"][0].__setitem__("contribution_pp", 5.0),
            "shocked_flag_mismatch": lambda r: r["sector_impacts"][0].__setitem__("shocked", False),
            "sector_duplicate": lambda r: r["sector_impacts"].append(copy.deepcopy(r["sector_impacts"][0])),
            "sector_set_mismatch": lambda r: r["starting_exposure"].pop(),
            "total_missing": lambda r: r.__setitem__("estimated_portfolio_impact_pct", None),
            "total_sign_mismatch": lambda r: r.__setitem__("estimated_portfolio_impact_pct", 7.0),
            "coverage_invalid": lambda r: r["coverage"].__setitem__("renormalised", True),
            "value_impact_invalid": lambda r: r["estimated_value_impact"].__setitem__("amount", 1000.0),
            "shock_assumptions_invalid": lambda r: r["scenario"].__setitem__("market_shock_pct", 500),
            "identity_missing": lambda r: r.__setitem__("portfolio_name", ""),
            "scenario_identity_invalid": lambda r: r["scenario"].__setitem__("type", "predefined"),
            "sector_structure_invalid": lambda r: r["sector_impacts"][0].pop("contribution_pp"),
            "unheld_shocks_invalid": lambda r: r.__setitem__("unheld_sector_shocks", [{"sector": "Energy", "shock_pct": -5}]),
            "sector_count_exceeds_holdings": lambda r: r["coverage"].__setitem__("equity_holdings_count", 2),
        }
        for code, mutate in cases.items():
            with self.subTest(code):
                out = self.verifier.verify_response(self.mutated(mutate))
                self.assert_withheld(out, code)
                self.assertGreater(self.block(out)["mismatch_count"], 0)
                self.assertIn("withheld", out["error"])

    def test_hypothetical_and_not_forecast_flags_are_enforced(self):
        for mutate in (lambda r: r["scenario"].__setitem__("is_forecast", True), lambda r: r["scenario"].__setitem__("is_hypothetical", False),
                       lambda r: r.__setitem__("disclaimer", "This will happen."), lambda r: r.__setitem__("disclaimer", None)):
            out = self.verifier.verify_response(self.mutated(mutate))
            self.assert_withheld(out)
            self.assertTrue(self.codes(out) & {"hypothetical_flags", "disclaimer_missing"})

    def test_contribution_and_total_tampering_is_caught_by_provenance_even_when_signs_agree(self):
        """ScenarioVerifier already recomputed the numbers; changing them afterwards (in a sign-consistent way) breaks the digest."""
        out = self.verifier.verify_response(self.mutated(lambda r: (r["sector_impacts"][0].__setitem__("contribution_pp", -0.5), r.__setitem__("estimated_portfolio_impact_pct", -12.0))))
        self.assert_withheld(out, "figures_changed_after_verification")
        rejected = self.block(out)["rejected_result"]["result"]
        self.assertEqual(rejected["estimated_portfolio_impact_pct"], -12.0)           # diagnostics only
        self.assertNotIn(-12.0, [out["result"].get("estimated_portfolio_impact_pct")])

    def test_portfolio_mismatch(self):
        for mutate in (lambda r: r.__setitem__("portfolio_id", 3), lambda r: r.__setitem__("portfolio_name", "Growth Equity Fund")):
            out = self.verifier.verify_response(self.mutated(mutate))
            self.assert_withheld(out, "figures_changed_after_verification")

    def test_status_mismatches(self):
        r = self.good()
        r["status"] = "no_equity_holdings"
        self.assert_withheld(self.verifier.verify_response(r), "status_mismatch")
        r = self.good()
        r["result"]["status"] = "bogus"
        self.assertIn("scenario_status_invalid", self.codes(self.verifier.verify_response(r)))
        r = self.good()
        r["selected_tool"] = "exposure_calculator"
        self.assertIn("tool_mismatch", self.codes(self.verifier.verify_response(r)))
        r = self.good(); r["result"]["verification"]["mismatch_count"] = 2
        self.assertIn("verification_mismatches", self.codes(self.verifier.verify_response(r)))

    # ---- failed / unverified scenario verification -------------------------------------------------------------------

    def test_failed_scenario_verification_keeps_the_figures_withheld_and_is_reported_as_failed(self):
        bad = copy.deepcopy(self.good()["result"]); bad.pop("verification"); bad["sector_impacts"][0]["contribution_pp"] += 1.0
        broken = MagicMock()
        broken.verify.return_value = ScenarioVerifier(self.db).verify(bad)
        out = self.agent(answer_verifier=AnswerVerifier(), scenario_verifier=broken).answer_question(self.Q)
        self.assert_withheld(out, "scenario_verification_failed")
        self.assertEqual(out["result"]["verification"]["status"], "failed")
        self.assertIn("failed independent verification", out["error"])               # the scenario verifier's own message survives

    def test_unverified_scenario_is_withheld_and_distinct_from_failed(self):
        agent = self.agent(answer_verifier=AnswerVerifier())
        with self.verifier_only_fault_scenario():
            out = agent.answer_question(self.Q)
        self.assert_withheld(out, status=UNVERIFIED)
        self.assertEqual(self.block(out)["mismatch_count"], 0)
        self.assertEqual(out["result"]["verification"]["status"], UNVERIFIED)
        self.assertIn("could not be completed", out["error"])
        none = self.agent(answer_verifier=AnswerVerifier(), scenario_verifier=None).answer_question(self.Q)
        self.assert_withheld(none, status=UNVERIFIED)
        self.assertIn("disabled or unavailable", none["error"])

    def test_figures_without_any_verification_block_are_withheld(self):
        r = self.good()
        del r["result"]["verification"]
        out = self.verifier.verify_response(r)
        self.assert_withheld(out, status=UNVERIFIED)
        self.assertTrue(self.block(out)["checks_skipped"])
        self.assertEqual(len(self.block(out)["rejected_result"]["result"]["sector_impacts"]), 4)

    def test_figures_on_a_withheld_result_are_removed_again(self):
        r = self.good()
        r["status"] = r["result"]["status"] = "error"
        r["result"]["verification"] = {"status": "failed", "verified": False, "mismatch_count": 1}
        out = self.verifier.verify_response(r)
        self.assertIn("withheld_figures_shown", self.codes(out))
        self.assert_withheld(out)
        plain = self.good(); plain["status"] = plain["result"]["status"] = "error"; plain["result"].pop("verification")
        self.assertIn("data_on_failed_result", self.codes(self.verifier.verify_response(plain)))

    def test_no_unverified_scenario_ever_reaches_a_normal_success(self):
        scenarios = [
            self.verifier.verify_response(self.mutated(lambda r: r.__setitem__("estimated_portfolio_impact_pct", 3.0))),
            self.verifier.verify_response(self.mutated(lambda r: r.pop("verification"))),
            self.agent(answer_verifier=AnswerVerifier(), scenario_verifier=None).answer_question(self.Q),
        ]
        with self.verifier_only_fault_scenario():
            scenarios.append(self.agent(answer_verifier=AnswerVerifier()).answer_question(self.Q))
        for out in scenarios:
            self.assertEqual(out["status"], "error")
            self.assertEqual(out["result"]["sector_impacts"], [])
            self.assertIsNone(out["result"]["estimated_portfolio_impact_pct"])
            self.assertFalse(self.block(out)["verified"])

    def test_verifier_exception_withholds_the_scenario(self):
        with patch.object(AnswerVerifier, "_scenario", side_effect=RuntimeError("boom")):
            out = AnswerVerifier().verify_response(self.good())
        self.assert_withheld(out, status=UNVERIFIED)
        self.assertIn("could not be completed", self.block(out)["error"])

    def test_cli_and_api_follow_the_same_policy(self):
        text = cli._format_pretty(self.agent(answer_verifier=AnswerVerifier()).answer_question(self.Q))
        self.assertIn("Answer verification: VERIFIED", text)
        self.assertIn("HYPOTHETICAL", text)
        failed = cli._format_pretty(self.verifier.verify_response(self.mutated(lambda r: r["scenario"].__setitem__("is_forecast", True))))
        self.assertIn("Answer verification: FAILED", failed)
        self.assertNotIn("-7.6000", failed)
        try:
            from portfolio_api.service import PortfolioService
        except ImportError:
            return
        service = PortfolioService(use_llm=False, use_intent_classifier=False)
        self.assertEqual(service.ask(self.Q)["result"]["answer_verification"]["status"], VERIFIED)

    def verifier_only_fault_scenario(self):
        from portfolio_agent import scenario_verifier as sv

        real = self.db.execute_read_only

        def flaky(sql, params=None):
            if sql in (sv.SQL_PORTFOLIO, sv.SQL_HOLDINGS, sv.SQL_SECURITIES, sv.SQL_SECTORS):
                raise ReadOnlyQueryError("simulated database failure during scenario verification")
            return real(sql, params)

        return patch.object(self.db, "execute_read_only", side_effect=flaky)


class TestRobustnessAndIndependence(Base):
    def test_a_verifier_exception_withholds_the_data_and_never_propagates(self):
        r = self.raw("What are the names of all active portfolios?")
        with patch.object(AnswerVerifier, "_sql", side_effect=RuntimeError("boom")):
            out = AnswerVerifier().verify_response(copy.deepcopy(r))
        b = self.block(out)
        self.assertEqual(b["status"], UNVERIFIED)
        self.assertIn("could not be completed", b["error"])
        self.assertEqual((out["status"], out["result"]["rows"]), ("error", []))
        self.assertEqual(b["rejected_result"]["result"]["rows_returned"], 10)
        h = self.raw(HYBRID_Q)
        with patch.object(AnswerVerifier, "_hybrid", side_effect=RuntimeError("boom")):
            out = AnswerVerifier().verify_response(h)
        self.assertEqual((out["status"], out["result"]["complete"]), ("error", False))
        self.assertEqual(out["result"]["components"]["exposure"]["exposures"], [])

    def test_even_a_failing_policy_step_degrades_to_a_plain_error(self):
        r = self.raw("How many portfolios do we have in total?")
        with patch.object(AnswerVerifier, "_run", side_effect=RuntimeError("a")), patch.object(AnswerVerifier, "_block", side_effect=RuntimeError("b")):
            out = AnswerVerifier().verify_response(r)
        self.assertEqual((out["status"], out["result"]), ("error", None))

    def test_database_failures_cannot_affect_the_answer_verifier_it_never_queries(self):
        r = self.raw(HYBRID_Q)
        with patch.object(PortfolioDatabase, "execute_read_only", side_effect=ReadOnlyQueryError("database down")):
            out = AnswerVerifier().verify_response(r)
        self.assertEqual(self.block(out)["status"], VERIFIED)

    def test_the_module_performs_no_financial_calculation_and_has_no_model_or_database_access(self):
        tree = ast.parse((PROJECT_ROOT / "portfolio_agent" / "answer_verifier.py").read_text(encoding="utf-8"))
        imported = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    imported.setdefault(a.name, set())
            elif isinstance(node, ast.ImportFrom):
                imported.setdefault(node.module or "", set()).update(a.name for a in node.names)
        for forbidden in ("sqlite3", "database", "tools", "sql_generator", "google", "genai", "numpy", "decimal", "statistics", "math"):
            self.assertNotIn(forbidden, {m.split(".")[-1] for m in imported}, forbidden)
        self.assertEqual(imported.get("financial_verifier"), {"figures_digest"})   # a hash function only
        self.assertEqual(imported.get("scenario_verifier"), {"scenario_digest"})   # a hash function only (the arithmetic stays in ScenarioVerifier)
        arithmetic = (ast.Mult, ast.Div, ast.Sub, ast.Pow, ast.Mod, ast.FloorDiv)
        self.assertEqual([n for n in ast.walk(tree) if isinstance(n, ast.BinOp) and isinstance(n.op, arithmetic)], [])
        calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertFalse(calls & {"sum", "round", "abs", "fsum", "mean", "min", "max", "float", "Decimal"}, calls)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertFalse(names & {"execute_read_only", "generate_content", "ExposureCalculator", "FinancialVerifier", "SQLQueryTool", "PortfolioDatabase", "GeminiSQLGenerator"}, names)
        strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        self.assertFalse([s for s in strings if s.lstrip().upper().startswith(("SELECT ", "WITH ", "INSERT ", "UPDATE ", "DELETE "))])  # no SQL text at all

    def test_no_second_gemini_call_is_made_for_answer_verification(self):
        def run(answer_verifier):
            client = MagicMock()
            client.models.generate_content.return_value = MagicMock(text=json.dumps({
                "sql": "SELECT COUNT(*) FROM portfolios", "parameters": [], "description": "Count of portfolios"}))
            tool = SQLQueryTool(db=self.db, gemini_generator=GeminiSQLGenerator(api_key="unit-test-placeholder", client=client, db=self.db), use_gemini=True)
            with patch.object(GeminiSQLGenerator, "is_configured", True):
                out = self.agent(answer_verifier=answer_verifier, sql_tool=tool).answer_question("How many portfolios do we have in total?")
            return out, client.models.generate_content.call_count

        plain, calls_without = run(None)
        checked, calls_with = run(AnswerVerifier())
        self.assertEqual((calls_without, calls_with), (1, 1))
        self.assertEqual(self.block(checked)["unverified_claims"], ["query_description"])
        self.assertEqual(checked["result"]["generation_method"], "gemini")
        for key in plain["result"]:
            self.assertEqual(checked["result"][key], plain["result"][key])  # nothing but the added block differs

    def test_response_contracts_remain_compatible(self):
        checked = self.agent(answer_verifier=AnswerVerifier())
        sql = checked.answer_question("How many portfolios do we have in total?")
        plain = self.raw("How many portfolios do we have in total?")
        self.assertEqual(set(sql), STD_KEYS)
        self.assertEqual(set(sql["result"]) - set(plain["result"]), {"answer_verification"})
        self.assertEqual({k: v for k, v in sql["result"].items() if k != "answer_verification"}, plain["result"])
        exposure = checked.answer_question(EXPOSURE_Q)
        self.assertEqual(set(exposure["result"]) - set(self.raw(EXPOSURE_Q)["result"]), {"answer_verification"})
        self.assertEqual(sql["route_reason"], "Question routed to SQL query tool for database analysis.")
        self.assertIsNone(self.agent(answer_verifier=None).answer_verifier)

    def test_json_serialisable(self):
        json.dumps(self.agent(answer_verifier=AnswerVerifier()).answer_question(HYBRID_Q))


class TestInterfaces(Base):
    def test_api_service_and_endpoint_carry_the_block(self):
        try:
            from portfolio_api.service import PortfolioService
        except ImportError:
            self.skipTest("FastAPI adapter dependencies not installed")
        service = PortfolioService(use_llm=False, use_intent_classifier=False)
        for q in ("How many portfolios do we have in total?", EXPOSURE_Q, HYBRID_Q):
            self.assertEqual(service.ask(q)["result"]["answer_verification"]["status"], VERIFIED)
        try:
            from fastapi.testclient import TestClient
            from portfolio_api.app import create_app

            body = TestClient(create_app(service), base_url="http://127.0.0.1:8000").post("/api/ask", json={"question": HYBRID_Q}).json()
            self.assertEqual(body["result"]["answer_verification"]["status"], VERIFIED)
            greeting = TestClient(create_app(service), base_url="http://127.0.0.1:8000").post("/api/ask", json={"question": "hello"}).json()
            self.assertNotIn("answer_verification", greeting["result"])
        except ImportError:
            pass

    def test_agent_and_api_agree_on_failures(self):
        try:
            from portfolio_api.service import PortfolioService
        except ImportError:
            self.skipTest("FastAPI adapter dependencies not installed")
        service = PortfolioService(use_llm=False, use_intent_classifier=False)
        agent = PortfolioAgent(db=service.db, sql_tool=service.sql_tool, exposure_tool=service.exposure_tool, intent_classifier=None)
        with patch.object(service.sql_tool, "execute", wraps=service.sql_tool.execute) as spy:
            spy.side_effect = lambda q, **kw: {**SQLQueryTool.execute(service.sql_tool, q, **kw), "row_count": 99}
            a, b = agent.answer_question("What are the names of all active portfolios?"), service.ask("What are the names of all active portfolios?")
        for out in (a, b):
            self.assertEqual((out["status"], out["result"]["rows"], out["result"]["answer_verification"]["status"]), ("error", [], FAILED))
        self.assertEqual(a["error"], b["error"])

    def test_pretty_cli_shows_each_status_and_labels_model_text(self):
        agent = self.agent(answer_verifier=AnswerVerifier())
        ok = cli._format_pretty(agent.answer_question("How many portfolios do we have in total?"))
        self.assertIn("Answer verification: VERIFIED", ok)
        r = self.raw("How many portfolios do we have in total?")
        r["result"].update(generation_method="gemini", model_name="m", query_description="Number of portfolios")
        text = cli._format_pretty(self.verifier.verify_response(r))
        self.assertIn("Description (model-generated, NOT verified): Number of portfolios", text)
        self.assertIn("model-written text is NOT verified", text)
        bad = self.raw("What are the names of all active portfolios?")
        bad["result"]["row_count"] = 1
        failed = cli._format_pretty(self.verifier.verify_response(bad))
        self.assertIn("Answer verification: FAILED", failed)
        self.assertIn("row_count is 1 but 10 rows", failed)
        self.assertNotIn("Growth Equity Fund", failed)       # withheld rows are not printed
        with self.verifier_only_fault():
            unverified = cli._format_pretty(agent.answer_question(EXPOSURE_Q))
        self.assertIn("Answer verification: UNVERIFIED", unverified)
        self.assertNotIn("Energy", unverified.split("Answer verification")[1].split("Error")[0] if "Error" in unverified else unverified)
        self.assertNotIn("VERIFIED (response", failed + unverified)


if __name__ == "__main__":
    unittest.main()
