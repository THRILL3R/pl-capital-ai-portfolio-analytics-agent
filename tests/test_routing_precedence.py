"""
Routing precedence: historical / descriptive questions about STORED performance and risk data (maximum drawdown, returns,
volatility, Sharpe, VaR/CVaR, tracking error, past periods) must reach the SQL Query Tool, even when the local intent
classifier suggests "sector exposure" (or "unsupported" / "unclear"). Existing exposure, hybrid, scenario and SQL routes,
and the official Q7 / Q8 routing, must not change.

The classifier is exercised both ways: disabled (legacy routing) and enabled via a scripted stand-in that reproduces the
misrouting verdict. The real model is covered by tests/smoke_intent_model.py. Gemini is mocked.
"""

from __future__ import annotations

import json
import sys
import unittest
from typing import Dict, Optional
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_agent.agent import PortfolioAgent
from portfolio_agent.database import PortfolioDatabase
from portfolio_agent.intent_classifier import Intent, IntentResult
from portfolio_agent.sql_generator import GeminiSQLGenerator
from portfolio_agent.tools import ExposureCalculator, SQLQueryTool

DRAWDOWN_Q = "What was the maximum drawdown of the Growth Equity Fund last year?"
HISTORICAL_RISK_QUESTIONS = [
    DRAWDOWN_Q,
    "What is the maximum drawdown of the Balanced Portfolio?",
    "Show the historical returns of the International Equity Fund",
    "What was the past performance of the Conservative Income Fund?",
    "What is the volatility of the Tech Innovation Fund?",
    "What is the Sharpe ratio of the Growth Equity Fund?",
    "What is the Sortino ratio of the Balanced Portfolio?",
    "Show the VaR 95 for the Growth Equity Fund",
    "What is the CVaR of the Tech Innovation Fund?",
    "What is the value at risk of the Balanced Portfolio?",
    "What is the tracking error of the Growth Equity Fund?",
    "What was the NAV of the Growth Equity Fund in 2023?",
    "How did the Balanced Portfolio perform in the last quarter?",
    "What were the returns of the Growth Equity Fund year to date?",
    "What was the beta of the Tech Innovation Fund over the past 3 years?",
]


class StubClassifier:
    """
    Stands in for the local model with scripted verdicts: ``intent`` for every question, or a per-question override from
    ``script`` (so a test can give each question the verdict a *correct* classifier would, or a misleading one).
    """

    def __init__(self, intent: Intent, script: Optional[Dict[str, Intent]] = None) -> None:
        self.intent = intent
        self.script = script or {}

    def classify(self, text: str) -> IntentResult:
        verdict = self.script.get(text, self.intent)
        return IntentResult(verdict, 0.9, 0.5, "stub", verdict, {})

    def warm_up(self) -> None:
        return None


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = PortfolioDatabase()
        if not cls.db.is_initialized():
            cls.db.initialize_database()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def agent(self, classifier=None, sql_tool=None):
        return PortfolioAgent(db=self.db, sql_tool=sql_tool or SQLQueryTool(db=self.db, use_gemini=False), exposure_tool=ExposureCalculator(db=self.db), intent_classifier=classifier)

    def spying_agent(self, intent):
        sql, exposure = MagicMock(), MagicMock()
        sql.use_gemini = False
        sql.execute.return_value = {"status": "success", "tool": "sql_query", "rows": [], "columns": [], "row_count": 0}
        agent = PortfolioAgent(db=self.db, sql_tool=sql, exposure_tool=exposure, intent_classifier=StubClassifier(intent), financial_verifier=None, answer_verifier=None)
        return agent, sql, exposure


class TestHistoricalRiskRouting(Base):
    def test_the_exact_maximum_drawdown_query_reaches_sql_with_the_classifier_disabled(self):
        r = self.agent(classifier=None).answer_question(DRAWDOWN_Q)
        self.assertEqual(r["selected_tool"], "sql_query")

    def test_the_exact_maximum_drawdown_query_reaches_sql_even_when_the_classifier_says_sector_exposure(self):
        agent, sql, exposure = self.spying_agent(Intent.SECTOR_EXPOSURE)
        r = agent.answer_question(DRAWDOWN_Q)
        self.assertEqual(r["selected_tool"], "sql_query")
        sql.execute.assert_called_once_with(DRAWDOWN_Q)          # the original question, unchanged
        exposure.calculate.assert_not_called()

    def test_every_historical_risk_phrasing_reaches_sql_whatever_the_classifier_suggests(self):
        for intent in (Intent.SECTOR_EXPOSURE, Intent.PORTFOLIO_ANALYTICS, Intent.UNSUPPORTED, Intent.UNKNOWN):
            for q in HISTORICAL_RISK_QUESTIONS:
                with self.subTest(intent=intent.value, question=q):
                    agent, sql, exposure = self.spying_agent(intent)
                    r = agent.answer_question(q)
                    self.assertEqual(r["selected_tool"], "sql_query")
                    sql.execute.assert_called_once()
                    exposure.calculate.assert_not_called()

    def test_every_historical_risk_phrasing_reaches_sql_with_the_classifier_disabled(self):
        agent = self.agent(classifier=None)
        for q in HISTORICAL_RISK_QUESTIONS:
            with self.subTest(q):
                self.assertEqual(agent.answer_question(q)["selected_tool"], "sql_query")

    def test_gemini_enabled_the_drawdown_question_returns_stored_data_not_exposure(self):
        sql = ("SELECT p.portfolio_name, MIN(pp.max_drawdown) AS max_drawdown FROM portfolio_performance pp JOIN portfolios p ON p.portfolio_id = pp.portfolio_id "
               "WHERE p.portfolio_name = ? GROUP BY p.portfolio_name")
        client = MagicMock()
        client.models.generate_content.return_value = MagicMock(text=json.dumps({"sql": sql, "parameters": ["Growth Equity Fund"], "description": "Worst recorded drawdown"}))
        tool = SQLQueryTool(db=self.db, gemini_generator=GeminiSQLGenerator(api_key="unit-test-placeholder", client=client, db=self.db), use_gemini=True)
        with patch.object(GeminiSQLGenerator, "is_configured", True):
            r = self.agent(classifier=StubClassifier(Intent.SECTOR_EXPOSURE), sql_tool=tool).answer_question(DRAWDOWN_Q)
        self.assertEqual((r["selected_tool"], r["status"], r["result"]["generation_method"]), ("sql_query", "success", "gemini"))
        self.assertEqual(r["result"]["columns"], ["portfolio_name", "max_drawdown"])
        stored = self.db.execute_read_only(sql, ("Growth Equity Fund",))["rows"]
        self.assertEqual(r["result"]["rows"], stored)
        self.assertEqual(client.models.generate_content.call_count, 1)
        self.assertEqual(r["result"]["answer_verification"]["status"], "verified")
        self.assertNotIn("exposures", r["result"])

    def test_the_historical_question_needs_a_portfolio_or_fund_to_override_a_rejection(self):
        """General-knowledge questions that happen to contain 'returns' or 'last year' are still rejected locally."""
        for q in ("What are the returns on a savings account?", "What was the weather last year?", "Who won the match in the last year?"):
            with self.subTest(q):
                agent, sql, _ = self.spying_agent(Intent.UNSUPPORTED)
                r = agent.answer_question(q)
                self.assertEqual((r["status"], r["selected_tool"]), ("unsupported", None))
                sql.execute.assert_not_called()

    def test_conversational_verdicts_are_not_overridden(self):
        agent, sql, _ = self.spying_agent(Intent.CAPABILITIES)
        r = agent.answer_question("What can you tell me about the funds' performance?")
        self.assertEqual((r["status"], r["selected_tool"]), ("conversation", None))
        sql.execute.assert_not_called()

    def test_historical_wording_is_recognised(self):
        for q in HISTORICAL_RISK_QUESTIONS:
            self.assertTrue(PortfolioAgent._is_historical_risk_question(q), q)
        for q in ("What are the sector exposures for the Tech Innovation Fund?", "How many portfolios do we have in total?", "Show the holdings of the Balanced Portfolio",
                  "Which securities are in the Technology sector?", "Stress test the International Equity Fund under a 20% market decline."):
            self.assertFalse(PortfolioAgent._is_historical_risk_question(q), q)


class TestExistingRoutesUnchanged(Base):
    @staticmethod
    def correct_verdicts(questions):
        """What a sensible classifier says: exposure-flavoured questions -> SECTOR_EXPOSURE, everything else -> PORTFOLIO_ANALYTICS."""
        exposure_words = ("exposure", "stress test", "stress scenario", "if technology falls")
        return {q: (Intent.SECTOR_EXPOSURE if any(w in q.lower() for w in exposure_words) else Intent.PORTFOLIO_ANALYTICS) for q in questions}

    def test_official_questions_are_never_historical_and_keep_their_routes(self):
        gt = json.loads((PROJECT_ROOT / "ground_truth_dataset.json").read_text(encoding="utf-8"))["questions"]
        expected = {"text2sql": "sql_query", "exposure_calculator": "exposure_calculator"}
        script = {q["question"]: (Intent.SECTOR_EXPOSURE if q["type"] == "exposure_calculator" else Intent.PORTFOLIO_ANALYTICS) for q in gt}
        for label, classifier in (("disabled", None), ("enabled", StubClassifier(Intent.PORTFOLIO_ANALYTICS, script))):
            agent = self.agent(classifier=classifier)
            for q in gt:
                with self.subTest(q=q["id"], classifier=label):
                    self.assertFalse(PortfolioAgent._is_historical_risk_question(q["question"]))
                    self.assertEqual(agent.answer_question(q["question"])["selected_tool"], expected[q["type"]])

    def test_q7_keeps_routing_to_sql_even_when_the_classifier_leans_towards_exposure(self):
        q7 = next(q["question"] for q in json.loads((PROJECT_ROOT / "ground_truth_dataset.json").read_text(encoding="utf-8"))["questions"] if q["id"] == 7)
        self.assertEqual(self.agent(classifier=StubClassifier(Intent.SECTOR_EXPOSURE)).answer_question(q7)["selected_tool"], "sql_query")   # exposure exclusions apply

    def test_exposure_hybrid_scenario_and_sql_routes_are_unchanged_with_and_without_a_classifier(self):
        cases = [
            ("What are the sector exposures for the Tech Innovation Fund?", "exposure_calculator"),
            ("What are the sector exposures of the International Equity Fund?", "exposure_calculator"),
            ("Show the holdings and sector exposure breakdown for the International Equity Fund", "hybrid_analysis"),
            ("Stress test the International Equity Fund under a 20% market decline.", "scenario_analysis"),
            ("Run a severe market stress scenario for the Growth Equity Fund.", "scenario_analysis"),
            ("Show me the top 5 holdings by cost basis in the Growth Equity Fund", "sql_query"),
            ("How many portfolios do we have in total?", "sql_query"),
        ]
        script = self.correct_verdicts([q for q, _ in cases])
        for label, classifier in (("disabled", None), ("enabled", StubClassifier(Intent.PORTFOLIO_ANALYTICS, script))):
            agent = self.agent(classifier=classifier)
            for q, tool in cases:
                with self.subTest(q=q, classifier=label):
                    self.assertEqual(agent.answer_question(q)["selected_tool"], tool)

    def test_a_historical_question_among_exposure_questions_is_routed_independently(self):
        """The same enabled classifier: exposure stays exposure while the drawdown question (verdict: exposure) goes to SQL."""
        script = {DRAWDOWN_Q: Intent.SECTOR_EXPOSURE, "What are the sector exposures for the Tech Innovation Fund?": Intent.SECTOR_EXPOSURE}
        agent = self.agent(classifier=StubClassifier(Intent.PORTFOLIO_ANALYTICS, script))
        self.assertEqual(agent.answer_question("What are the sector exposures for the Tech Innovation Fund?")["selected_tool"], "exposure_calculator")
        self.assertEqual(agent.answer_question(DRAWDOWN_Q)["selected_tool"], "sql_query")

    def test_scenario_questions_are_not_captured_by_the_historical_rule(self):
        agent, sql, _ = self.spying_agent(Intent.SECTOR_EXPOSURE)
        r = agent.answer_question("What happens to the Tech Innovation Fund if Technology falls 25%?")
        self.assertEqual(r["selected_tool"], "scenario_analysis")
        sql.execute.assert_not_called()

    def test_a_pure_exposure_keyword_question_with_historical_wording_still_goes_to_exposure(self):
        """Explicit exposure wording keeps its meaning: the exposure calculator only has current weights, and says so."""
        agent = self.agent(classifier=None)
        self.assertEqual(agent.answer_question("What are the sector exposures for the Tech Innovation Fund?")["selected_tool"], "exposure_calculator")


if __name__ == "__main__":
    unittest.main()
