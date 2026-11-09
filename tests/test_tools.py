"""
Comprehensive unit tests for Phase 3: Core Analytical Tools.
Covers SQLQueryTool, ExposureCalculator, and RuleBasedSQLGenerator.
"""

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
import sys
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_agent.database import PortfolioDatabase, ReadOnlyQueryError
from portfolio_agent.sql_generator import RuleBasedSQLGenerator, UnsupportedQuestionError
from portfolio_agent.tools import SQLQueryTool, ExposureCalculator


class TestCoreAnalyticalTools(unittest.TestCase):
    """Test suite for Phase 3 Core Analytical Tools."""

    @classmethod
    def setUpClass(cls):
        """Set up an isolated test database from the actual schema and CSV datasets."""
        cls.temp_dir = tempfile.mkdtemp(prefix="pl_capital_tools_test_")
        cls.db_path = Path(cls.temp_dir) / "test_portfolio.db"
        cls.schema_path = PortfolioDatabase.resolve_schema_path()
        cls.data_dir = PortfolioDatabase.resolve_data_dir()

        cls.db = PortfolioDatabase(cls.db_path)
        cls.db.initialize_database(
            schema_path=cls.schema_path,
            data_dir=cls.data_dir,
            reset=True,
        )

        cls.generator = RuleBasedSQLGenerator()
        cls.sql_tool = SQLQueryTool(db=cls.db, generator=cls.generator)
        cls.exposure_tool = ExposureCalculator(db=cls.db)

        # Load ground truth questions
        gt_path = PortfolioDatabase.resolve_project_root() / "ground_truth_dataset.json"
        with open(gt_path, "r", encoding="utf-8") as f:
            cls.ground_truth = json.load(f)["questions"]

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        try:
            shutil.rmtree(cls.temp_dir)
        except Exception:
            pass

    # =========================================================================
    # PART 1: Verification of All 8 Ground-Truth text2sql Questions
    # =========================================================================

    def test_01_sql_q1_total_portfolio_count(self):
        """Q1: How many portfolios do we have in total?"""
        q_item = next(q for q in self.ground_truth if q["id"] == 1)
        question = q_item["question"]
        expected_sql = q_item["ground_truth"]["sql_query"]

        # Run direct GT query on DB
        expected_res = self.db.execute_read_only(expected_sql)

        # Run via SQLQueryTool
        tool_res = self.sql_tool.execute(question)

        self.assertEqual(tool_res["status"], "success")
        self.assertEqual(tool_res["tool"], "sql_query")
        self.assertEqual(tool_res["row_count"], 1)
        self.assertEqual(tool_res["rows"], expected_res["rows"])
        # Expected count is 13
        self.assertEqual(list(tool_res["rows"][0].values())[0], 13)

    def test_02_sql_q2_active_portfolio_names(self):
        """Q2: What are the names of all active portfolios?"""
        q_item = next(q for q in self.ground_truth if q["id"] == 2)
        question = q_item["question"]
        expected_sql = q_item["ground_truth"]["sql_query"]

        expected_res = self.db.execute_read_only(expected_sql)
        tool_res = self.sql_tool.execute(question)

        self.assertEqual(tool_res["status"], "success")
        self.assertEqual(tool_res["row_count"], 10)
        self.assertEqual(tool_res["rows"], expected_res["rows"])
        self.assertEqual(tool_res["query_parameters"], ["Active"])

    def test_03_sql_q3_technology_sector_securities(self):
        """Q3: Which securities are in the Technology sector?"""
        q_item = next(q for q in self.ground_truth if q["id"] == 3)
        question = q_item["question"]
        expected_sql = q_item["ground_truth"]["sql_query"]

        expected_res = self.db.execute_read_only(expected_sql)
        tool_res = self.sql_tool.execute(question)

        self.assertEqual(tool_res["status"], "success")
        self.assertEqual(tool_res["row_count"], 8)
        self.assertEqual(tool_res["rows"], expected_res["rows"])
        self.assertEqual(tool_res["query_parameters"], ["Technology"])

    def test_04_sql_q4_high_risk_total_aum(self):
        """Q4: What is the total Assets Under Management (AUM) for portfolios with high target risk level?"""
        q_item = next(q for q in self.ground_truth if q["id"] == 4)
        question = q_item["question"]
        expected_sql = q_item["ground_truth"]["sql_query"]

        expected_res = self.db.execute_read_only(expected_sql)
        tool_res = self.sql_tool.execute(question)

        self.assertEqual(tool_res["status"], "success")
        self.assertEqual(tool_res["row_count"], 1)
        self.assertEqual(tool_res["rows"], expected_res["rows"])
        self.assertEqual(list(tool_res["rows"][0].values())[0], 85000000.0)

    def test_05_sql_q5_top_5_holdings_growth_equity_fund(self):
        """Q5: Show me the top 5 holdings by cost basis in the Growth Equity Fund"""
        q_item = next(q for q in self.ground_truth if q["id"] == 5)
        question = q_item["question"]
        expected_sql = q_item["ground_truth"]["sql_query"]

        expected_res = self.db.execute_read_only(expected_sql)
        tool_res = self.sql_tool.execute(question)

        self.assertEqual(tool_res["status"], "success")
        self.assertEqual(tool_res["row_count"], 5)
        self.assertEqual(tool_res["rows"], expected_res["rows"])
        self.assertEqual(tool_res["query_parameters"], ["Growth Equity Fund", 5])

    def test_06_sql_q6_average_current_price_by_sector(self):
        """Q6: What is the average current price of securities in each sector?"""
        q_item = next(q for q in self.ground_truth if q["id"] == 6)
        question = q_item["question"]
        expected_sql = q_item["ground_truth"]["sql_query"]

        expected_res = self.db.execute_read_only(expected_sql)
        tool_res = self.sql_tool.execute(question)

        self.assertEqual(tool_res["status"], "success")
        self.assertEqual(tool_res["row_count"], 7)
        self.assertEqual(tool_res["rows"], expected_res["rows"])

    def test_07_sql_q7_tech_holding_value_percentage_by_portfolio(self):
        """Q7: For each portfolio, show the total value of Technology sector holdings and percentage"""
        q_item = next(q for q in self.ground_truth if q["id"] == 7)
        question = q_item["question"]
        expected_sql = q_item["ground_truth"]["sql_query"]

        expected_res = self.db.execute_read_only(expected_sql)
        tool_res = self.sql_tool.execute(question)

        self.assertEqual(tool_res["status"], "success")
        self.assertEqual(tool_res["row_count"], 8)
        self.assertEqual(tool_res["rows"], expected_res["rows"])

    def test_08_sql_q8_diversification_metrics(self):
        """Q8: Find portfolios that have holdings in more than 5 different sectors and diversification metrics"""
        q_item = next(q for q in self.ground_truth if q["id"] == 8)
        question = q_item["question"]
        expected_sql = q_item["ground_truth"]["sql_query"]

        expected_res = self.db.execute_read_only(expected_sql)
        tool_res = self.sql_tool.execute(question)

        self.assertEqual(tool_res["status"], "success")
        self.assertEqual(tool_res["row_count"], 1)
        self.assertEqual(tool_res["rows"], expected_res["rows"])
        self.assertEqual(tool_res["rows"][0]["portfolio_name"], "Total Stock Market Index Fund")
        self.assertEqual(tool_res["rows"][0]["sector_count"], 6)
        self.assertEqual(tool_res["rows"][0]["total_holdings"], 12)
        self.assertEqual(tool_res["rows"][0]["diversification_ratio"], 0.5)

    # =========================================================================
    # PART 2: Exposure Calculator Ground-Truth Verifications
    # =========================================================================

    def test_09_exposure_tech_innovation_fund(self):
        """Q9: Tech Innovation Fund exposure must match Technology = 100%."""
        result = self.exposure_tool.calculate("Tech Innovation Fund")

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["tool"], "exposure_calculator")
        self.assertEqual(result["portfolio_name"], "Tech Innovation Fund")
        self.assertEqual(result["portfolio_id"], 3)
        self.assertEqual(result["equity_holdings_count"], 8)
        self.assertEqual(result["excluded_bond_holdings"], 0)
        self.assertEqual(result["total_equity_weight_pct"], 100.0)

        # Must have exactly 1 sector: Technology
        self.assertEqual(len(result["exposures"]), 1)
        tech_exp = result["exposures"][0]
        self.assertEqual(tech_exp["sector"], "Technology")
        self.assertEqual(tech_exp["exposure_pct"], 100.0)
        self.assertEqual(tech_exp["weight_fraction"], 1.0)
        self.assertEqual(tech_exp["holding_count"], 8)

    def test_10_exposure_international_equity_fund(self):
        """
        Q10: International Equity Fund exposure must independently match:
        Energy = 38%, Technology = 36%, Automotive = 15%, Consumer Staples = 11%.
        """
        result = self.exposure_tool.calculate("International Equity Fund")

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["portfolio_name"], "International Equity Fund")
        self.assertEqual(result["portfolio_id"], 7)
        self.assertEqual(result["equity_holdings_count"], 8)
        self.assertEqual(result["excluded_bond_holdings"], 0)
        self.assertEqual(result["total_equity_weight_pct"], 100.0)

        exp_map = {e["sector"]: e["exposure_pct"] for e in result["exposures"]}
        expected_map = {
            "Energy": 38.0,
            "Technology": 36.0,
            "Automotive": 15.0,
            "Consumer Staples": 11.0,
        }
        self.assertEqual(exp_map, expected_map)

    # =========================================================================
    # PART 3: Required Edge Cases & Error Handling Tests
    # =========================================================================

    def test_11_exact_portfolio_name(self):
        """Test resolving exact portfolio name case-insensitively."""
        pid, pname = self.exposure_tool.resolve_portfolio("growth equity fund")
        self.assertEqual(pid, 1)
        self.assertEqual(pname, "Growth Equity Fund")

    def test_12_unique_partial_portfolio_name(self):
        """Test resolving unambiguous partial portfolio name."""
        # 'international equity' -> 'International Equity Fund'
        pid, pname = self.exposure_tool.resolve_portfolio("international equity")
        self.assertEqual(pid, 7)
        self.assertEqual(pname, "International Equity Fund")

        # 'tech innovation' -> 'Tech Innovation Fund'
        pid, pname = self.exposure_tool.resolve_portfolio("tech innovation")
        self.assertEqual(pid, 3)
        self.assertEqual(pname, "Tech Innovation Fund")

    def test_13_ambiguous_portfolio_name(self):
        """Test that ambiguous portfolio name is rejected with descriptive error."""
        with self.assertRaises(ValueError) as ctx:
            self.exposure_tool.resolve_portfolio("Fund")
        self.assertIn("Ambiguous portfolio name", str(ctx.exception))

    def test_14_nonexistent_portfolio(self):
        """Test handling of nonexistent portfolio."""
        with self.assertRaises(ValueError) as ctx:
            self.exposure_tool.resolve_portfolio("Mars Exploration Fund")
        self.assertIn("not found", str(ctx.exception))

        # Via calculate without raise_on_error
        res = self.exposure_tool.calculate("Mars Exploration Fund", raise_on_error=False)
        self.assertEqual(res["status"], "error")
        self.assertIn("not found", res["error"])

    def test_15_empty_question(self):
        """Test handling empty question in SQLQueryTool and generator."""
        res = self.sql_tool.execute("")
        self.assertEqual(res["status"], "error")
        self.assertIn("empty", res["error"])

        res_space = self.sql_tool.execute("   ")
        self.assertEqual(res_space["status"], "error")

        with self.assertRaises(UnsupportedQuestionError):
            self.generator.generate("")

    def test_16_unknown_sql_question(self):
        """Test handling unrecognised questions in SQLQueryTool and generator."""
        unknown_q = "What is the weather in Mumbai today?"
        res = self.sql_tool.execute(unknown_q)
        self.assertEqual(res["status"], "unsupported")
        self.assertIn("not recognized", res["error"])

        with self.assertRaises(UnsupportedQuestionError):
            self.generator.generate(unknown_q)

    def test_17_portfolio_with_no_equity_holdings(self):
        """
        Test portfolio 8 ('Fixed Income Plus') which contains only bonds.
        Calculator must report excluded bonds, 0 equity holdings, and no equity exposures.
        """
        res = self.exposure_tool.calculate("Fixed Income Plus")
        self.assertEqual(res["status"], "no_equity_holdings")
        self.assertEqual(res["portfolio_id"], 8)
        self.assertEqual(res["equity_holdings_count"], 0)
        self.assertEqual(res["excluded_bond_holdings"], 5)
        self.assertEqual(res["total_equity_weight_pct"], 0.0)
        self.assertEqual(res["exposures"], [])

    def test_18_invalid_database_query(self):
        """Test that invalid SQL execution is safely caught and returned with error status."""
        with self.assertRaises(ReadOnlyQueryError):
            self.db.execute_read_only("SELECT * FROM non_existent_table_xyz;")

    def test_19_sql_injection_attempt(self):
        """Test that SQL injection queries are rejected by the read-only layer."""
        injection_queries = [
            "SELECT * FROM portfolios; DROP TABLE portfolios;",
            "SELECT * FROM portfolios WHERE 1=1; DELETE FROM portfolios;",
            "SELECT * FROM portfolios -- comment injection",
        ]
        for inj in injection_queries:
            with self.subTest(query=inj):
                with self.assertRaises(ReadOnlyQueryError):
                    self.db.execute_read_only(inj)

    def test_20_fractional_weight_conversion(self):
        """Verify that fractional current_weight values (e.g. 0.18) are converted to 18.0%."""
        res = self.exposure_tool.calculate("Conservative Income Fund")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["portfolio_id"], 2)

        # Conservative Income Fund has:
        # stock 8 (Financials, weight 0.15 -> 15.0%)
        # stock 9 (Healthcare, weight 0.12 -> 12.0%)
        # stock 11 (Consumer Staples, weight 0.05 -> 5.0%)
        # bonds 26, 27, 28, 29 (excluded)
        self.assertEqual(res["excluded_bond_holdings"], 4)
        self.assertEqual(res["equity_holdings_count"], 3)
        self.assertEqual(res["total_equity_weight_pct"], 32.0)

        exp_map = {e["sector"]: e["exposure_pct"] for e in res["exposures"]}
        self.assertEqual(exp_map["Financials"], 15.0)
        self.assertEqual(exp_map["Healthcare"], 12.0)
        self.assertEqual(exp_map["Consumer Staples"], 5.0)


if __name__ == "__main__":
    unittest.main()
