"""
Unit tests for evaluate.py (Phase 6 ground-truth evaluator).

No test makes a live Gemini API call: tests/__init__.py removes GEMINI_API_KEY and disables
.env loading, and every Gemini-enabled test injects a mocked client. Mocked results are
never evidence of live Gemini performance.
"""

from __future__ import annotations

import copy
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import evaluate  # noqa: E402
from portfolio_agent.agent import PortfolioAgent  # noqa: E402
from portfolio_agent.database import PortfolioDatabase  # noqa: E402
from portfolio_agent.sql_generator import GeminiSQLGenerator  # noqa: E402
from portfolio_agent.tools import SQLQueryTool  # noqa: E402

GROUND_TRUTH = PROJECT_ROOT / "ground_truth_dataset.json"
DATA_DIR = PROJECT_ROOT / "data"


class TestCompareTables(unittest.TestCase):
    """The comparison must be strict about values/order but tolerant of cosmetic differences."""

    def test_01_exact_match(self):
        ok, failures, _ = evaluate.compare_tables(["a"], [(13,)], ["COUNT(*)"], [(13,)], ordered=False)
        self.assertTrue(ok, failures)

    def test_02_numeric_tolerance(self):
        ok, _, _ = evaluate.compare_tables(["v"], [(85000000.0,)], ["v"], [(85000000.0000001,)], ordered=False)
        self.assertTrue(ok)
        ok, failures, _ = evaluate.compare_tables(["v"], [(85000000.0,)], ["v"], [(85000001.0,)], ordered=False)
        self.assertFalse(ok)
        self.assertIn("do not match", failures[0])

    def test_03_row_count_mismatch(self):
        ok, failures, _ = evaluate.compare_tables(["n"], [("A",), ("B",)], ["n"], [("A",)], ordered=False)
        self.assertFalse(ok)
        self.assertIn("Row count mismatch", failures[0])

    def test_04_order_enforced_only_when_ordered(self):
        exp = [("A", 3.0), ("B", 2.0)]
        act = [("B", 2.0), ("A", 3.0)]
        ok, _, _ = evaluate.compare_tables(["n", "v"], exp, ["n", "v"], act, ordered=False)
        self.assertTrue(ok)
        ok, failures, _ = evaluate.compare_tables(["n", "v"], exp, ["n", "v"], act, ordered=True)
        self.assertFalse(ok)
        self.assertIn("order", failures[0])

    def test_05_reordered_and_extra_columns(self):
        exp = [("A", 1.5), ("B", 2.5)]
        act = [(99, 1.5, "A"), (98, 2.5, "B")]
        ok, failures, notes = evaluate.compare_tables(["name", "val"], exp, ["id", "value", "label"], act, ordered=True)
        self.assertTrue(ok, failures)
        self.assertTrue(any("extra column" in n for n in notes))

    def test_06_missing_columns_fail(self):
        ok, failures, _ = evaluate.compare_tables(["a", "b"], [(1, 2)], ["a"], [(1,)], ordered=False)
        self.assertFalse(ok)
        self.assertIn("Column count mismatch", failures[0])

    def test_07_strings_compare_exactly(self):
        ok, _, _ = evaluate.compare_tables(["n"], [("Growth Equity Fund",)], ["n"], [("growth equity fund",)], ordered=False)
        self.assertFalse(ok)


class TestIndependentExpectations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = PortfolioDatabase()
        if not cls.db.is_initialized():
            cls.db.initialize_database()
        cls.db.close()
        cls.db_path = cls.db.db_path

    def test_08_q9_tech_innovation_from_csv(self):
        exp = evaluate.compute_expected_exposure(DATA_DIR, "Tech Innovation Fund")
        self.assertEqual({k: round(v, 6) for k, v in exp["exposures_pct"].items()}, {"Technology": 100.0})

    def test_09_q10_international_equity_from_csv(self):
        exp = evaluate.compute_expected_exposure(DATA_DIR, "International Equity Fund")
        self.assertEqual(
            {k: round(v, 6) for k, v in exp["exposures_pct"].items()},
            {"Energy": 38.0, "Technology": 36.0, "Automotive": 15.0, "Consumer Staples": 11.0},
        )

    def test_10_csv_exposure_matches_sqlite_for_every_portfolio(self):
        """Cross-check: CSV-based expectation == equity-only weight sums from SQLite, all 13 portfolios."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            names = [r[0] for r in conn.execute("SELECT portfolio_name FROM portfolios")]
            for name in names:
                with self.subTest(portfolio=name):
                    sql_rows = conn.execute(
                        "SELECT sec.sector_name, SUM(h.current_weight) * 100, "
                        "(SELECT COUNT(*) FROM holdings h2 JOIN securities s2 ON s2.security_id = h2.security_id "
                        " JOIN portfolios p2 ON p2.portfolio_id = h2.portfolio_id "
                        " WHERE p2.portfolio_name = ? AND s2.asset_type = 'Bond') "
                        "FROM holdings h JOIN securities s ON s.security_id = h.security_id "
                        "JOIN sectors sec ON sec.sector_id = s.sector_id "
                        "JOIN portfolios p ON p.portfolio_id = h.portfolio_id "
                        "WHERE p.portfolio_name = ? AND s.asset_type = 'Stock' GROUP BY sec.sector_name",
                        (name, name),
                    ).fetchall()
                    exp = evaluate.compute_expected_exposure(DATA_DIR, name)
                    self.assertEqual(set(exp["exposures_pct"]), {r[0] for r in sql_rows})
                    for sector, pct, _ in sql_rows:
                        self.assertAlmostEqual(exp["exposures_pct"][sector], pct, places=4)
        finally:
            conn.close()

    def test_11_bond_holdings_excluded_not_renormalised(self):
        """
        Synthetic CSVs (the supplied dataset has no mixed stock/bond portfolio):
        Tech 0.30 + Energy 0.20 stocks + 0.50 bond -> 30% / 20%, NOT renormalised to 60% / 40%.
        """
        tmp = Path(tempfile.mkdtemp(prefix="pl_eval_csv_"))
        try:
            files = {
                "portfolios": "portfolio_id,portfolio_name\n1,Mixed Fund\n",
                "sectors": "sector_id,sector_name\n1,Technology\n2,Energy\n",
                "securities": "security_id,asset_type,sector_id\n1,Stock,1\n2,Stock,2\n3,Bond,\n",
                "holdings": "holding_id,portfolio_id,security_id,current_weight\n1,1,1,0.30\n2,1,2,0.20\n3,1,3,0.50\n",
            }
            for name, content in files.items():
                (tmp / f"{name}.csv").write_text(content, encoding="utf-8")
            exp = evaluate.compute_expected_exposure(tmp, "Mixed Fund")
            self.assertAlmostEqual(exp["exposures_pct"]["Technology"], 30.0, places=6)
            self.assertAlmostEqual(exp["exposures_pct"]["Energy"], 20.0, places=6)
            self.assertEqual(set(exp["exposures_pct"]), {"Technology", "Energy"})
            self.assertEqual(exp["excluded_bond_holdings"], 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_12_official_sql_runs_independently(self):
        questions = evaluate.load_ground_truth(GROUND_TRUTH)
        self.assertEqual(len(questions), 10)
        q1 = next(q for q in questions if q["id"] == 1)
        cols, rows = evaluate.run_expected_sql(self.db_path, q1["ground_truth"]["sql_query"])
        self.assertEqual(rows, [(13,)])

    def test_13_integrity_check(self):
        integ = evaluate.check_integrity(self.db_path, DATA_DIR)
        self.assertTrue(integ["matches"])
        self.assertEqual(integ["db_total"], 603)
        self.assertEqual(integ["fk_violations"], 0)


class _PerturbingAgent:
    """Wraps a real agent and corrupts selected answers to prove the evaluator detects failures."""

    def __init__(self, inner: PortfolioAgent, mutate):
        self.inner = inner
        self.mutate = mutate

    def answer_question(self, question):
        resp = copy.deepcopy(self.inner.answer_question(question))
        return self.mutate(question, resp)


class TestEvaluationRuns(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = PortfolioDatabase()
        if not cls.db.is_initialized():
            cls.db.initialize_database()
        cls.questions = evaluate.load_ground_truth(GROUND_TRUTH)
        cls.tmp = tempfile.mkdtemp(prefix="pl_eval_")

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _deterministic_agent(self) -> PortfolioAgent:
        return PortfolioAgent(db=self.db, sql_tool=SQLQueryTool(db=self.db, use_gemini=False))

    def _run(self, agent, mode="deterministic", client="none", model=None):
        return evaluate.run_evaluation(agent, self.questions, self.db.db_path, DATA_DIR, mode, client, model)

    def test_14_deterministic_run_scores_10_of_10(self):
        run = self._run(self._deterministic_agent())
        self.assertEqual((run.passed, run.total), (10, 10), [r.failures for r in run.results if not r.passed])
        for r in run.results:
            if r.qtype == "text2sql":
                self.assertEqual(r.generation_method, "rule_based")
                self.assertFalse(r.fallback_used)
            else:
                self.assertEqual(r.selected_tool, "exposure_calculator")

    def test_15_detects_wrong_value_wrong_route_and_wrong_exposure(self):
        def mutate(question, resp):
            if "Assets Under Management" in question:
                row = resp["result"]["rows"][0]
                key = next(iter(row))
                row[key] = row[key] + 1.0
            if "Tech Innovation Fund" in question:
                resp["selected_tool"] = "sql_query"
            if "international equity" in question:
                resp["result"]["exposures"][0]["exposure_pct"] += 0.5
            if "active portfolios" in question:
                resp["result"]["rows"].pop()
            return resp

        run = self._run(_PerturbingAgent(self._deterministic_agent(), mutate))
        by_id = {r.qid: r for r in run.results}
        self.assertEqual(run.passed, 6)
        self.assertIn("do not match", " ".join(by_id[4].failures))
        self.assertIn("Routed to 'sql_query'", " ".join(by_id[9].failures))
        self.assertIn("expected", " ".join(by_id[10].failures))
        self.assertIn("Row count mismatch", " ".join(by_id[2].failures))

    def test_16_detects_bond_sector_and_ordering_errors(self):
        def mutate(question, resp):
            if "international equity" in question:
                resp["result"]["exposures"].append({"sector": "Fixed Income", "exposure_pct": 10.0})
            if "average current price" in question:
                resp["result"]["rows"].reverse()
            return resp

        run = self._run(_PerturbingAgent(self._deterministic_agent(), mutate))
        by_id = {r.qid: r for r in run.results}
        self.assertFalse(by_id[10].passed)
        self.assertIn("Unexpected sectors", " ".join(by_id[10].failures))
        self.assertFalse(by_id[6].passed)
        self.assertIn("ORDER BY", " ".join(by_id[6].failures))

    def test_17_agent_exception_is_reported_not_raised(self):
        broken = MagicMock()
        broken.answer_question.side_effect = RuntimeError("boom")
        run = self._run(broken)
        self.assertEqual(run.passed, 0)
        self.assertIn("boom", run.results[0].failures[0])

    def test_18_llm_mode_with_mocked_gemini_is_labelled_not_live(self):
        """Mocked Gemini returns the official SQL; evaluator records 'gemini' but flags non-live."""
        sql_by_question = {
            q["question"]: q["ground_truth"]["sql_query"] for q in self.questions if q["type"] == "text2sql"
        }

        def fake_generate_content(model, contents, config):
            question = contents.split("Question:", 1)[1].strip()
            resp = MagicMock()
            resp.text = json.dumps({"sql": sql_by_question[question], "parameters": [], "description": "mock"})
            return resp

        client = MagicMock()
        client.models.generate_content.side_effect = fake_generate_content
        gen = GeminiSQLGenerator(client=client, db=self.db, model="mock-model")
        agent = PortfolioAgent(db=self.db, sql_tool=SQLQueryTool(db=self.db, gemini_generator=gen, use_gemini=True))

        run = self._run(agent, mode="llm", client="mock", model="mock-model")
        self.assertEqual(run.passed, 10, [r.failures for r in run.results if not r.passed])
        sql_results = [r for r in run.results if r.qtype == "text2sql"]
        self.assertTrue(all(r.generation_method == "gemini" for r in sql_results))
        self.assertEqual(client.models.generate_content.call_count, 8)  # never called for exposure questions
        report = evaluate.render_report(run)
        self.assertIn("injected/mocked client", report)
        self.assertIn("not** evidence", report)

    def test_19_llm_mode_with_failing_gemini_reports_fallback(self):
        client = MagicMock()
        client.models.generate_content.side_effect = TimeoutError("deadline exceeded")
        gen = GeminiSQLGenerator(client=client, db=self.db, model="mock-model")
        agent = PortfolioAgent(db=self.db, sql_tool=SQLQueryTool(db=self.db, gemini_generator=gen, use_gemini=True))

        run = self._run(agent, mode="llm", client="mock", model="mock-model")
        self.assertEqual(run.passed, 10)
        for r in run.results:
            if r.qtype == "text2sql":
                self.assertEqual(r.generation_method, "rule_based")
                self.assertTrue(r.fallback_used)
                self.assertIn("timeout", r.fallback_reason)
        self.assertIn("(fallback)", evaluate.render_report(run))

    def test_20_report_contents(self):
        run = self._run(self._deterministic_agent())
        report = evaluate.render_report(run)
        for expected in (
            "Overall score: 10/10",
            "Deterministic (rule-based SQL generation",
            "## Results Summary",
            "## Failed Questions",
            "None. All questions passed.",
            "## Verification Status",
            "says nothing about live Gemini performance",
            "## Limitations",
            "603 DB rows vs 603 CSV rows",
        ):
            self.assertIn(expected, report)
        self.assertEqual(report.count("### Q"), 10)

    def test_21_failed_report_explains_failures(self):
        def mutate(question, resp):
            if "Growth Equity Fund" in question:
                resp["result"]["rows"] = resp["result"]["rows"][:3]
            return resp

        report = evaluate.render_report(self._run(_PerturbingAgent(self._deterministic_agent(), mutate)))
        self.assertIn("Overall score: 9/10", report)
        self.assertIn("### Q5: Show me the top 5 holdings", report)
        self.assertIn("Row count mismatch: expected 5, got 3", report)

    def test_22_cli_deterministic_writes_report(self):
        report_path = Path(self.tmp) / "report.md"
        code = evaluate.main(["--report", str(report_path)])
        self.assertEqual(code, 0)
        self.assertIn("Overall score: 10/10", report_path.read_text(encoding="utf-8"))

    def test_23_cli_use_llm_without_key_makes_no_live_call(self):
        """With no key (tests clear it), --use-llm falls back for every SQL question and is not 'live'."""
        report_path = Path(self.tmp) / "report_llm.md"
        code = evaluate.main(["--use-llm", "--report", str(report_path)])
        self.assertEqual(code, 0)
        text = report_path.read_text(encoding="utf-8")
        self.assertIn("not configured - rule-based fallback only", text)
        self.assertIn("No API requests were made", text)
        self.assertIn("Gemini not configured", text)  # per-question fallback reason
        self.assertNotIn("live API requests were attempted", text)

    def test_24_cli_question_subset_is_labelled_and_isolated(self):
        """--questions evaluates only the chosen IDs and labels the report as a subset run."""
        report_path = Path(self.tmp) / "report_subset.md"
        code = evaluate.main(["--questions", "7", "--report", str(report_path)])
        self.assertEqual(code, 0)
        text = report_path.read_text(encoding="utf-8")
        self.assertIn("Targeted subset run: Q7 only", text)
        self.assertIn("Subset score: 1/1", text)
        self.assertNotIn("Overall score", text)
        self.assertEqual(text.count("### Q"), 1)

    def test_25_question_subset_validation_and_default_report_path(self):
        questions = evaluate.load_ground_truth(evaluate.DEFAULT_GROUND_TRUTH)
        selected, subset = evaluate.select_questions(questions, "7, 9")
        self.assertEqual(subset, [7, 9])
        self.assertEqual([int(q["id"]) for q in selected], [7, 9])
        self.assertEqual(evaluate.select_questions(questions, None), (questions, None))
        for bad in ("11", "abc", ","):
            with self.assertRaises(ValueError):
                evaluate.select_questions(questions, bad)
        self.assertEqual(evaluate.main(["--questions", "99", "--no-report"]), 2)
        # A subset run never overwrites the full evaluation_report.md by default.
        original_root = evaluate.PROJECT_ROOT
        evaluate.PROJECT_ROOT = Path(self.tmp)
        try:
            self.assertEqual(evaluate.main(["--questions", "1"]), 0)
        finally:
            evaluate.PROJECT_ROOT = original_root
        self.assertTrue((Path(self.tmp) / "evaluation_report_subset.md").is_file())
        self.assertFalse((Path(self.tmp) / "evaluation_report.md").exists())


if __name__ == "__main__":
    unittest.main()
