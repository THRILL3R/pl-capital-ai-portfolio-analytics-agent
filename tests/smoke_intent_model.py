"""
OPTIONAL smoke test of the REAL local embedding model (not part of the normal suite).

It is skipped unless RUN_INTENT_MODEL_SMOKE=1 and the model is already cached
(run `python scripts/download_intent_model.py` once). It never calls Gemini or any network API.

    $env:RUN_INTENT_MODEL_SMOKE = "1"
    .\\.venv\\Scripts\\python.exe -B -m unittest tests.smoke_intent_model -v

The accuracy floors below are deliberately below the calibrated results (see
docs/INTENT_CLASSIFIER_CALIBRATION.md) so ordinary model-runtime differences do not make it flaky,
while a broken model, threshold or prototype set still fails it.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from intent_validation_set import HOLDOUT, VALIDATION
from portfolio_agent.agent import PortfolioAgent
from portfolio_agent.intent_classifier import ANALYTICAL, CONVERSATIONAL, Intent, IntentClassifierUnavailable, build_default_classifier

ENABLED = os.environ.get("RUN_INTENT_MODEL_SMOKE") == "1"


@unittest.skipUnless(ENABLED, "set RUN_INTENT_MODEL_SMOKE=1 to run the real-model smoke test")
class TestRealModelSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("PORTFOLIO_INTENT_CLASSIFIER", None)  # tests/__init__ turns it off for the normal suite
        cls.clf = build_default_classifier()
        try:
            cls.clf.warm_up()
        except IntentClassifierUnavailable as exc:
            raise unittest.SkipTest(str(exc))

    @staticmethod
    def correct(result_intent: Intent, expected: str) -> bool:
        exp = Intent(expected)
        return result_intent in ANALYTICAL if exp in ANALYTICAL else result_intent == exp

    def accuracy(self, dataset, categories=None):
        rows = [(t, e, c) for t, e, c in dataset if categories is None or c in categories]
        good = sum(self.correct(self.clf.classify(t).intent, e) for t, e, _c in rows)
        return good / len(rows)

    def test_accuracy_floors(self):
        self.assertGreaterEqual(self.accuracy(VALIDATION), 0.90)
        self.assertGreaterEqual(self.accuracy(HOLDOUT), 0.80)

    def test_official_questions_are_always_analytical(self):
        gt = json.loads((PROJECT_ROOT / "ground_truth_dataset.json").read_text(encoding="utf-8"))["questions"]
        for q in gt:
            with self.subTest(q=q["id"]):
                self.assertIn(self.clf.classify(q["question"]).intent, ANALYTICAL)

    def test_spec_examples(self):
        c = self.clf.classify
        for text in ("Who are you?", "What is your name?", "Introduce yourself.", "What assistant am I speaking to?"):
            self.assertEqual(c(text).intent, Intent.IDENTITY, text)
        for text in ("What can you do?", "What things can you do?", "How can you help me?", "What questions can I ask?",
                     "Tell me about your features.", "What kind of portfolio analysis is available?", "How should I use this assistant?"):
            self.assertEqual(c(text).intent, Intent.CAPABILITIES, text)
        for text in ("Hi, how many portfolios do we have?", "What can you tell me about Growth Equity Fund holdings?",
                     "Can you help me calculate Balanced Portfolio exposure?", "Show the top holdings in Growth Equity Fund.",
                     "Compare portfolios by risk.", "What is the total AUM?"):
            self.assertIn(c(text).intent, ANALYTICAL, text)

    def test_no_analytical_question_is_swallowed_by_a_conversational_or_rejecting_intent(self):
        blocked = []
        for t, e, cat in VALIDATION + HOLDOUT:
            if Intent(e) in ANALYTICAL:
                got = self.clf.classify(t).intent
                if got in CONVERSATIONAL or got == Intent.UNSUPPORTED:
                    blocked.append((t, got.value))
        self.assertEqual(blocked, [])

    def test_agent_end_to_end_without_gemini(self):
        tool = MagicMock()
        tool.use_gemini = False
        tool.execute.return_value = {"status": "success"}
        agent = PortfolioAgent(db=MagicMock(), sql_tool=tool, exposure_tool=MagicMock(), intent_classifier=self.clf)
        for q, intent in (("Who are you?", "identity"), ("What can you do?", "capabilities"), ("tell me more", "clarification")):
            r = agent.answer_question(q)
            self.assertEqual((r["status"], r["result"]["intent"]), ("conversation", intent), q)
        self.assertEqual(agent.answer_question("What's the weather today?")["status"], "unsupported")
        tool.execute.assert_not_called()

    def test_historical_risk_questions_reach_sql_with_the_real_classifier(self):
        """The exact query that used to be misrouted to the exposure calculator, plus related historical-risk questions."""
        questions = [
            "What was the maximum drawdown of the Growth Equity Fund last year?",
            "What is the Sharpe ratio of the Balanced Portfolio?",
            "Show the VaR 95 for the Growth Equity Fund",
            "What is the volatility of the Tech Innovation Fund?",
            "What were the historical returns of the International Equity Fund?",
            "What is the tracking error of the Growth Equity Fund?",
            "What was the NAV of the Growth Equity Fund in 2023?",
        ]
        for q in questions:
            sql, exposure = MagicMock(), MagicMock()
            sql.use_gemini = False
            sql.execute.return_value = {"status": "success", "tool": "sql_query", "rows": [], "columns": [], "row_count": 0}
            agent = PortfolioAgent(db=MagicMock(), sql_tool=sql, exposure_tool=exposure, intent_classifier=self.clf, financial_verifier=None, answer_verifier=None)
            r = agent.answer_question(q)
            with self.subTest(q):
                self.assertEqual(r["selected_tool"], "sql_query")
                sql.execute.assert_called_once()
                exposure.calculate.assert_not_called()

    def test_exposure_hybrid_and_scenario_routes_are_unchanged_with_the_real_classifier(self):
        from portfolio_agent.database import PortfolioDatabase
        from portfolio_agent.tools import ExposureCalculator, SQLQueryTool

        db = PortfolioDatabase()
        agent = PortfolioAgent(db=db, sql_tool=SQLQueryTool(db=db, use_gemini=False), exposure_tool=ExposureCalculator(db=db), intent_classifier=self.clf)
        cases = [
            ("What are the sector exposures for the Tech Innovation Fund?", "exposure_calculator"),
            ("Calculate the sector exposure breakdown for international equity", "exposure_calculator"),
            ("Show the holdings and sector exposure breakdown for the International Equity Fund", "hybrid_analysis"),
            ("Stress test the International Equity Fund under a 20% market decline.", "scenario_analysis"),
            ("Show me the top 5 holdings by cost basis in the Growth Equity Fund", "sql_query"),
        ]
        for q, tool in cases:
            with self.subTest(q):
                self.assertEqual(agent.answer_question(q)["selected_tool"], tool)
        gt = json.loads((PROJECT_ROOT / "ground_truth_dataset.json").read_text(encoding="utf-8"))["questions"]
        for q in gt:
            with self.subTest(official=q["id"]):
                self.assertEqual(agent.answer_question(q["question"])["selected_tool"], "sql_query" if q["type"] == "text2sql" else "exposure_calculator")
        db.close()



if __name__ == "__main__":
    unittest.main()
