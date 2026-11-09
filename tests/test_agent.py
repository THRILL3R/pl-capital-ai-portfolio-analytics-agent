"""
Unit tests for PortfolioAgent and CLI (main.py).
Tests routing, structured response format, edge cases, and CLI invocation.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Resolve paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_agent.database import PortfolioDatabase
from portfolio_agent.agent import PortfolioAgent
from portfolio_agent.tools import SQLQueryTool, ExposureCalculator


class TestPortfolioAgent(unittest.TestCase):
    """Tests for the PortfolioAgent orchestration layer."""

    @classmethod
    def setUpClass(cls):
        """Set up a shared database and agent for the test suite."""
        cls.db = PortfolioDatabase()
        if not cls.db.is_initialized():
            cls.db.initialize_database()
        cls.agent = PortfolioAgent(db=cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    # ── Routing tests ──────────────────────────────────────────────

    def test_01_sql_total_portfolios(self):
        """Q1: 'How many portfolios do we have in total?' → SQLQueryTool."""
        resp = self.agent.answer_question("How many portfolios do we have in total?")
        self.assertEqual(resp["status"], "success")
        self.assertEqual(resp["selected_tool"], "sql_query")
        rows = resp["result"]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["COUNT(*)"], 13)

    def test_02_sql_active_portfolios(self):
        """Q2: 'What are the names of all active portfolios?' → SQLQueryTool."""
        resp = self.agent.answer_question("What are the names of all active portfolios?")
        self.assertEqual(resp["status"], "success")
        self.assertEqual(resp["selected_tool"], "sql_query")
        self.assertEqual(resp["result"]["row_count"], 10)

    def test_03_sql_technology_securities(self):
        """Q3: 'Which securities are in the Technology sector?' → SQLQueryTool."""
        resp = self.agent.answer_question("Which securities are in the Technology sector?")
        self.assertEqual(resp["status"], "success")
        self.assertEqual(resp["selected_tool"], "sql_query")
        self.assertEqual(resp["result"]["row_count"], 8)

    def test_04_sql_high_risk_aum(self):
        """Q4: AUM for high-risk portfolios → SQLQueryTool."""
        resp = self.agent.answer_question(
            "What is the total Assets Under Management (AUM) for portfolios with high target risk level?"
        )
        self.assertEqual(resp["status"], "success")
        self.assertEqual(resp["selected_tool"], "sql_query")

    def test_05_exposure_routes_to_calculator(self):
        """Sector exposure question → ExposureCalculator."""
        resp = self.agent.answer_question("What are the sector exposures for the Tech Innovation Fund?")
        self.assertEqual(resp["status"], "success")
        self.assertEqual(resp["selected_tool"], "exposure_calculator")
        self.assertEqual(resp["result"]["portfolio_name"], "Tech Innovation Fund")

    def test_06_exposure_tech_innovation_fund(self):
        """Tech Innovation Fund → Technology 100%."""
        resp = self.agent.answer_question("Show sector exposure for Tech Innovation Fund")
        self.assertEqual(resp["status"], "success")
        self.assertEqual(resp["selected_tool"], "exposure_calculator")
        exposures = resp["result"]["exposures"]
        self.assertEqual(len(exposures), 1)
        self.assertEqual(exposures[0]["sector"], "Technology")
        self.assertAlmostEqual(exposures[0]["exposure_pct"], 100.0, places=1)

    def test_07_exposure_international_equity_partial_name(self):
        """'international equity' partial name → International Equity Fund."""
        resp = self.agent.answer_question(
            "Calculate the sector exposure breakdown for international equity"
        )
        self.assertEqual(resp["status"], "success")
        self.assertEqual(resp["selected_tool"], "exposure_calculator")
        self.assertEqual(resp["result"]["portfolio_name"], "International Equity Fund")
        # Should have 4 sectors: Energy, Technology, Automotive, Consumer Staples
        self.assertEqual(len(resp["result"]["exposures"]), 4)

    def test_08_ambiguous_portfolio_name(self):
        """Ambiguous portfolio name → error with helpful message."""
        resp = self.agent.answer_question("Show sector exposure for Fund")
        self.assertEqual(resp["status"], "error")
        self.assertEqual(resp["selected_tool"], "exposure_calculator")
        self.assertIn("error", resp)
        self.assertIsNotNone(resp["error"])

    def test_09_unknown_portfolio_name(self):
        """Unknown portfolio name → error."""
        resp = self.agent.answer_question("Show sector exposure for Nonexistent Fund XYZ")
        self.assertEqual(resp["status"], "error")
        self.assertEqual(resp["selected_tool"], "exposure_calculator")
        self.assertIsNotNone(resp["error"])

    def test_10_empty_question(self):
        """Empty or whitespace question → error."""
        for empty_q in ["", "   ", None]:
            resp = self.agent.answer_question(empty_q)
            self.assertEqual(resp["status"], "error")
            self.assertIsNotNone(resp["error"])

    def test_11_unsupported_question(self):
        """Unrecognised question → unsupported status via SQLQueryTool."""
        resp = self.agent.answer_question("What is the meaning of life?")
        self.assertEqual(resp["status"], "unsupported")
        self.assertEqual(resp["selected_tool"], "sql_query")
        self.assertIsNotNone(resp["error"])

    def test_12_structured_response_format(self):
        """Every response must contain required keys."""
        required_keys = {"status", "question", "selected_tool", "route_reason", "result", "error"}
        resp = self.agent.answer_question("How many portfolios do we have in total?")
        self.assertTrue(required_keys.issubset(resp.keys()))

        resp2 = self.agent.answer_question("Show sector exposure for Tech Innovation Fund")
        self.assertTrue(required_keys.issubset(resp2.keys()))

    def test_13_tool_exception_handling(self):
        """Agent handles tool exceptions gracefully without stack traces."""
        # Mock SQLQueryTool.execute to raise an exception
        broken_sql_tool = MagicMock(spec=SQLQueryTool)
        broken_sql_tool.execute.side_effect = RuntimeError("Mock database crash")

        agent = PortfolioAgent(db=self.db, sql_tool=broken_sql_tool)
        resp = agent.answer_question("How many portfolios do we have in total?")
        self.assertEqual(resp["status"], "error")
        self.assertIn("Mock database crash", resp["error"])
        self.assertIsNone(resp["result"])

    def test_14_q7_not_routed_to_exposure(self):
        """Q7 (Tech percentage for each portfolio) must NOT route to ExposureCalculator."""
        resp = self.agent.answer_question(
            "For each portfolio, show the total value of Technology sector holdings "
            "and what percentage it represents of the total portfolio value"
        )
        self.assertEqual(resp["status"], "success")
        self.assertEqual(resp["selected_tool"], "sql_query")

    def test_15_exposure_missing_portfolio_name(self):
        """Exposure question without a portfolio name → error with guidance."""
        resp = self.agent.answer_question("Show sector exposure")
        self.assertEqual(resp["status"], "error")
        self.assertEqual(resp["selected_tool"], "exposure_calculator")
        self.assertIn("specify", resp["error"].lower())

    def test_16_exposure_by_portfolio_id(self):
        """Exposure question using portfolio ID → correct routing."""
        resp = self.agent.answer_question("Show sector exposure for portfolio 1")
        self.assertEqual(resp["status"], "success")
        self.assertEqual(resp["selected_tool"], "exposure_calculator")
        self.assertIsNotNone(resp["result"]["portfolio_name"])

    def test_17_response_is_json_serializable(self):
        """All agent responses must be JSON-serializable."""
        questions = [
            "How many portfolios do we have in total?",
            "Show sector exposure for Tech Innovation Fund",
            "What is the meaning of life?",
            "",
        ]
        for q in questions:
            resp = self.agent.answer_question(q)
            try:
                json.dumps(resp, default=str)
            except (TypeError, ValueError) as exc:
                self.fail(f"Response for '{q}' is not JSON-serializable: {exc}")


class TestCLI(unittest.TestCase):
    """Tests for the main.py CLI application."""

    MAIN_PY = str(PROJECT_ROOT / "main.py")
    PYTHON = sys.executable

    @classmethod
    def setUpClass(cls):
        """Ensure database is initialized."""
        db = PortfolioDatabase()
        if not db.is_initialized():
            db.initialize_database()
        db.close()

    def _run_cli(self, *args, stdin_text: str | None = None, timeout: int = 30) -> subprocess.CompletedProcess:
        """Helper to run main.py as a subprocess."""
        cmd = [self.PYTHON, self.MAIN_PY] + list(args)
        return subprocess.run(
            cmd,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(PROJECT_ROOT),
        )

    def test_18_cli_one_shot(self):
        """CLI one-shot mode returns valid JSON."""
        result = self._run_cli("--question", "How many portfolios do we have in total?")
        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")
        resp = json.loads(result.stdout)
        self.assertEqual(resp["status"], "success")
        self.assertEqual(resp["selected_tool"], "sql_query")

    def test_19_cli_one_shot_pretty(self):
        """CLI one-shot with --pretty flag produces human-readable output."""
        result = self._run_cli(
            "--pretty",
            "--question",
            "Show sector exposure for Tech Innovation Fund",
        )
        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")
        output = result.stdout
        # Pretty output should contain readable labels, not raw JSON
        self.assertIn("Status", output)
        self.assertIn("Tool", output)
        self.assertIn("Technology", output)

    def test_20_cli_interactive_exit(self):
        """CLI interactive mode exits cleanly on 'exit' command."""
        result = self._run_cli(stdin_text="exit\n")
        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")
        self.assertIn("Goodbye", result.stdout)

    def test_21_cli_interactive_quit(self):
        """CLI interactive mode exits cleanly on 'quit' command."""
        result = self._run_cli(stdin_text="quit\n")
        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")
        self.assertIn("Goodbye", result.stdout)

    def test_22_cli_interactive_q(self):
        """CLI interactive mode exits cleanly on 'q' command."""
        result = self._run_cli(stdin_text="q\n")
        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")
        self.assertIn("Goodbye", result.stdout)

    def test_23_cli_interactive_question_then_exit(self):
        """CLI interactive mode answers a question then exits."""
        stdin_text = "How many portfolios do we have in total?\nexit\n"
        result = self._run_cli(stdin_text=stdin_text)
        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")
        # Should contain the answer
        self.assertIn("success", result.stdout)
        self.assertIn("Goodbye", result.stdout)

    def test_24_cli_missing_database(self):
        """CLI with a non-existent database path returns exit code 1."""
        result = self._run_cli("--db", "nonexistent_path/fake.db", "--question", "test")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not found", result.stderr.lower())

    def test_25_cli_help(self):
        """CLI --help flag works and displays usage information."""
        result = self._run_cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("usage", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
