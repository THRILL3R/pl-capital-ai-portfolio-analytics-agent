"""
Tests for the independent financial verifier (portfolio_agent/financial_verifier.py).

Groups:
  1. Independence: the verifier shares no calculation code or SQL aggregation with ExposureCalculator.
  2. Correct results on the real assessment database (incl. Q9 / Q10) and on crafted edge-case databases.
  3. Deliberately corrupted calculator results are detected, with specific mismatch codes.
  4. Failure handling: database/SQL failures, fail-closed SQLGlot, response policy, agent and API integration.
No Gemini or network call is made anywhere.
"""

from __future__ import annotations

import ast
import copy
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import evaluate
from portfolio_agent import financial_verifier as fv
from portfolio_agent import sql_ast
from portfolio_agent.agent import PortfolioAgent
from portfolio_agent.database import PortfolioDatabase, ReadOnlyQueryError
from portfolio_agent.financial_verifier import (
    FAILED,
    NOT_APPLICABLE,
    UNVERIFIED,
    VERIFIED,
    FinancialVerifier,
    apply_verification,
)
from portfolio_agent.tools import ExposureCalculator, SQLQueryTool

SCHEMA = (PROJECT_ROOT / "database_schema.sql").read_text(encoding="utf-8")


def codes(v) -> set:
    return {m.code for m in v.mismatches}


class RealDbCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = PortfolioDatabase()
        if not cls.db.is_initialized():
            cls.db.initialize_database()
        cls.calc = ExposureCalculator(db=cls.db)
        cls.verifier = FinancialVerifier(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def result(self, name):
        return self.calc.calculate(name)


# ======================================================================================
# 1. Independence
# ======================================================================================
class TestIndependence(RealDbCase):
    def test_module_never_imports_the_calculator_or_any_llm(self):
        tree = ast.parse((PROJECT_ROOT / "portfolio_agent" / "financial_verifier.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported |= {a.name for a in node.names}
        for forbidden in ("tools", "ExposureCalculator", "SQLQueryTool", "sql_generator", "GeminiSQLGenerator", "genai", "google"):
            self.assertNotIn(forbidden, imported)
        source = (PROJECT_ROOT / "portfolio_agent" / "financial_verifier.py").read_text(encoding="utf-8")
        self.assertNotIn("ExposureCalculator(", source)
        self.assertNotIn(".calculate(", source)

    def test_verifier_sql_is_plain_single_table_reads_with_no_sql_aggregation(self):
        issued = []
        real = self.db.execute_read_only
        calculated = self.result("Balanced Portfolio")  # computed first, so the spy sees the verifier's queries only

        def spy(sql, params=None):
            issued.append(sql)
            return real(sql, params)

        with patch.object(self.db, "execute_read_only", side_effect=spy):
            self.assertEqual(FinancialVerifier(self.db).verify(calculated).status, VERIFIED)
        self.assertEqual(set(issued), {fv.SQL_PORTFOLIO, fv.SQL_HOLDINGS, fv.SQL_SECURITIES, fv.SQL_SECTORS})
        for sql in issued:
            self.assertEqual(len(re.findall(r"\bFROM\b", sql, re.I)), 1, sql)  # one table per query
            self.assertIsNone(re.search(r"\b(JOIN|GROUP\s+BY|SUM|COUNT|AVG|TOTAL|HAVING|DISTINCT|CASE)\b", sql, re.I), sql)

    def test_the_calculators_sql_is_not_reused(self):
        calc_sql = []
        real = self.db.execute_read_only

        def spy(sql, params=None):
            calc_sql.append(sql)
            return real(sql, params)

        with patch.object(self.db, "execute_read_only", side_effect=spy):
            self.calc.calculate("Balanced Portfolio")
        self.assertTrue(any("GROUP BY" in s for s in calc_sql))
        self.assertTrue(set(calc_sql).isdisjoint({fv.SQL_PORTFOLIO, fv.SQL_HOLDINGS, fv.SQL_SECURITIES, fv.SQL_SECTORS}))

    def test_expected_values_do_not_depend_on_the_calculator_at_all(self):
        with patch.object(ExposureCalculator, "calculate", side_effect=AssertionError("calculator must not be used")), \
             patch.object(ExposureCalculator, "resolve_portfolio", side_effect=AssertionError("calculator must not be used")):
            exp = FinancialVerifier(self.db).compute_expected(3)
        self.assertEqual(exp.portfolio_name, "Tech Innovation Fund")
        self.assertAlmostEqual(exp.sector_pct["Technology"], 100.0, places=9)

    def test_a_calculator_with_a_sql_bug_is_caught_because_the_verifier_does_not_share_that_sql(self):
        """The same wrong SQL cannot reproduce itself in the verifier: bonds leak in through a LEFT JOIN in the calculator only."""
        real = self.db

        class BuggyDb:
            def __getattr__(self, name):
                return getattr(real, name)

            def execute_read_only(self, sql, params=None):
                if "GROUP BY sect.sector_name" in sql:  # the calculator's exposure query, made wrong
                    sql = sql.replace("AND sec.asset_type = 'Stock' ", "").replace("JOIN sectors sect", "LEFT JOIN sectors sect")
                return real.execute_read_only(sql, params)

        buggy = ExposureCalculator(db=BuggyDb()).calculate("Balanced Portfolio")
        self.assertIn(None, [e["sector"] for e in buggy["exposures"]])  # the injected bug really changed the output
        verdict = FinancialVerifier(BuggyDb()).verify(buggy)  # even sharing the (bugged) db object, the verifier's path is unaffected
        self.assertEqual(verdict.status, FAILED)
        self.assertTrue({"unexpected_sector", "total_mismatch"} <= codes(verdict))
        self.assertIn("bond_inclusion", codes(verdict))

    def test_tolerance_matches_the_evaluation_methodology(self):
        self.assertEqual(fv.EXPOSURE_TOL_PP, evaluate.EXPOSURE_TOL_PP)
        self.assertEqual(fv.FRACTION_TOL, evaluate.ABS_TOL)

    def test_agrees_with_the_evaluators_csv_based_third_path(self):
        data_dir = PROJECT_ROOT / "data"
        for row in self.db.execute_read_only("SELECT portfolio_id, portfolio_name FROM portfolios")["rows"]:
            with self.subTest(row["portfolio_name"]):
                exp = self.verifier.compute_expected(row["portfolio_id"])
                csv_exp = evaluate.compute_expected_exposure(data_dir, row["portfolio_name"])
                self.assertEqual(set(exp.sector_pct), set(csv_exp["exposures_pct"]))
                for sector, pct in csv_exp["exposures_pct"].items():
                    self.assertAlmostEqual(exp.sector_pct[sector], pct, delta=fv.EXPOSURE_TOL_PP)
                self.assertEqual(exp.bond_holdings, csv_exp["excluded_bond_holdings"])
                self.assertEqual(exp.equity_holdings, csv_exp["equity_holdings"])


# ======================================================================================
# 2. Correct calculations on the real database
# ======================================================================================
class TestCorrectResults(RealDbCase):
    def test_q9_tech_innovation_fund_is_verified(self):
        r = self.result("Tech Innovation Fund")
        v = self.verifier.verify(r)
        self.assertEqual(v.status, VERIFIED, v.summary())
        self.assertEqual([(e["sector"], e["exposure_pct"]) for e in r["exposures"]], [("Technology", 100.0)])
        self.assertGreater(v.checks_run, 10)

    def test_q10_international_equity_fund_multiple_sectors_is_verified(self):
        r = self.result("International Equity Fund")
        v = self.verifier.verify(r)
        self.assertEqual(v.status, VERIFIED, v.summary())
        self.assertEqual({e["sector"]: e["exposure_pct"] for e in r["exposures"]}, {"Energy": 38.0, "Technology": 36.0, "Automotive": 15.0, "Consumer Staples": 11.0})
        exp = self.verifier.compute_expected(r["portfolio_id"])
        self.assertEqual({s: round(p, 6) for s, p in exp.sector_pct.items()}, {"Energy": 38.0, "Technology": 36.0, "Automotive": 15.0, "Consumer Staples": 11.0})

    def test_bonds_are_excluded_and_nothing_is_renormalised(self):
        r = self.result("Balanced Portfolio")
        self.assertGreater(r["excluded_bond_holdings"], 0)
        self.assertLess(r["total_equity_weight_pct"], 100.0)  # not rescaled to 100
        v = self.verifier.verify(r)
        self.assertEqual(v.status, VERIFIED, v.summary())
        exp = self.verifier.compute_expected(r["portfolio_id"])
        self.assertEqual(exp.bond_holdings, r["excluded_bond_holdings"])
        self.assertAlmostEqual(exp.total_pct, r["total_equity_weight_pct"], delta=fv.EXPOSURE_TOL_PP)
        self.assertGreater(exp.bond_weight_pct, 0)

    def test_portfolio_with_only_bonds_has_no_equity_exposure(self):
        r = self.result("Fixed Income Plus")
        self.assertEqual(r["status"], "no_equity_holdings")
        v = self.verifier.verify(r)
        self.assertEqual(v.status, VERIFIED, v.summary())

    def test_every_portfolio_in_the_assessment_database_verifies(self):
        rows = self.db.execute_read_only("SELECT portfolio_name FROM portfolios")["rows"]
        self.assertEqual(len(rows), 13)
        for row in rows:
            with self.subTest(row["portfolio_name"]):
                v = self.verifier.verify(self.result(row["portfolio_name"]))
                self.assertEqual(v.status, VERIFIED, v.summary())

    def test_verification_block_shape(self):
        block = apply_verification(self.result("International Equity Fund"), self.verifier.verify(self.result("International Equity Fund")))["verification"]
        self.assertEqual(block["status"], VERIFIED)
        self.assertTrue(block["verified"])
        self.assertEqual(block["mismatches"], [])
        self.assertEqual(block["tolerance_pp"], 1e-4)
        self.assertIn("Independent recomputation", block["method"])


# ======================================================================================
# 2b. Edge cases on crafted databases
# ======================================================================================
def make_db(sectors, securities, holdings, portfolios=((1, "Test Fund"),)):
    """A throw-away database with the project schema. Rows: sectors (id, name); securities (id, type, sector_id);
    holdings (id, portfolio_id, security_id, weight)."""
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "crafted.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    for sid, name in sectors:
        conn.execute("INSERT INTO sectors (sector_id, sector_name) VALUES (?, ?)", (sid, name))
    for sec_id, kind, sector_id in securities:
        conn.execute("INSERT INTO securities (security_id, symbol, company_name, asset_type, sector_id, current_price) VALUES (?, ?, ?, ?, ?, 1.0)", (sec_id, f"S{sec_id}", f"Co {sec_id}", kind, sector_id))
    for pid, name in portfolios:
        conn.execute("INSERT INTO portfolios (portfolio_id, portfolio_name) VALUES (?, ?)", (pid, name))
    for hid, pid, sec_id, weight in holdings:
        conn.execute("INSERT INTO holdings (holding_id, portfolio_id, security_id, quantity, current_weight) VALUES (?, ?, ?, 10, ?)", (hid, pid, sec_id, weight))
    conn.commit()
    conn.close()
    db = PortfolioDatabase(db_path=path)
    db._tmp = tmp  # keep the directory alive
    return db


class TestCraftedEdgeCases(unittest.TestCase):
    SECTORS = [(1, "Technology"), (2, "Energy"), (3, "Utilities")]

    def make(self, *args, **kwargs):
        db = make_db(*args, **kwargs)
        self.addCleanup(db._tmp.cleanup)  # cleanups run last-in-first-out: close the connection, then delete the file
        self.addCleanup(db.close)
        return db

    def check(self, db, expect_status=VERIFIED, name="Test Fund"):
        calc = ExposureCalculator(db=db).calculate(name)
        v = FinancialVerifier(db).verify(calc)
        self.assertEqual(v.status, expect_status, v.summary())
        return calc, v

    def test_zero_exposure_stock_keeps_its_sector_at_zero(self):
        db = self.make(self.SECTORS, [(1, "Stock", 1), (2, "Stock", 2)], [(1, 1, 1, 0.4), (2, 1, 2, 0.0)])
        calc, _ = self.check(db)
        self.assertEqual({e["sector"]: e["exposure_pct"] for e in calc["exposures"]}, {"Technology": 40.0, "Energy": 0.0})
        # ... and a calculator that drops the zero sector is reported as missing it
        broken = copy.deepcopy(calc)
        broken["exposures"] = [e for e in broken["exposures"] if e["sector"] != "Energy"]
        self.assertIn("missing_sector", codes(FinancialVerifier(db).verify(broken)))

    def test_unmapped_stock_is_counted_but_has_no_sector_exposure(self):
        db = self.make(self.SECTORS, [(1, "Stock", 1), (2, "Stock", None)], [(1, 1, 1, 0.5), (2, 1, 2, 0.3)])
        calc, _ = self.check(db)
        self.assertEqual((calc["equity_holdings_count"], calc["unmapped_equity_holdings"]), (2, 1))
        self.assertEqual(calc["total_equity_weight_pct"], 50.0)  # the unmapped 30% is NOT folded in or renormalised
        wrong = {**calc, "unmapped_equity_holdings": 0}
        self.assertIn("unmapped_count_mismatch", codes(FinancialVerifier(db).verify(wrong)))

    def test_sector_id_without_a_sector_record_is_dropped_and_reported(self):
        db = self.make(self.SECTORS, [(1, "Stock", 1), (2, "Stock", 99)], [(1, 1, 1, 0.5), (2, 1, 2, 0.2)])
        _, v = self.check(db)
        self.assertEqual(len(v.data_issues), 1)
        self.assertIn("unknown sector 99", v.data_issues[0])

    def test_holding_of_an_unknown_security_counts_only_toward_the_total(self):
        db = self.make(self.SECTORS, [(1, "Stock", 1)], [(1, 1, 1, 0.5), (2, 1, 77, 0.5)])
        calc, v = self.check(db)
        self.assertEqual((calc["total_holdings_count"], calc["equity_holdings_count"], calc["excluded_bond_holdings"]), (2, 1, 0))
        self.assertIn("unknown security 77", v.data_issues[0])

    def test_null_weight_adds_nothing_but_the_holding_is_counted(self):
        db = self.make(self.SECTORS, [(1, "Stock", 1), (2, "Stock", 1)], [(1, 1, 1, 0.25), (2, 1, 2, None)])
        calc, _ = self.check(db)
        self.assertEqual(calc["exposures"][0]["holding_count"], 2)
        self.assertEqual(calc["exposures"][0]["exposure_pct"], 25.0)

    def test_sector_with_only_null_weights_makes_the_calculator_error_so_nothing_is_verified(self):
        db = self.make(self.SECTORS, [(1, "Stock", 1)], [(1, 1, 1, None)])
        calc = ExposureCalculator(db=db).calculate("Test Fund")
        self.assertEqual(calc["status"], "error")
        v = FinancialVerifier(db).verify(calc)
        self.assertEqual(v.status, NOT_APPLICABLE)
        self.assertEqual(apply_verification(calc, v), calc)  # unchanged: no verification claim on an error result

    def test_weights_are_summed_per_sector_across_holdings_and_not_rescaled(self):
        db = self.make(self.SECTORS, [(1, "Stock", 1), (2, "Stock", 1), (3, "Stock", 2), (4, "Bond", None)],
                     [(1, 1, 1, 0.10), (2, 1, 2, 0.20), (3, 1, 3, 0.15), (4, 1, 4, 0.55)])
        calc, _ = self.check(db)
        self.assertEqual({e["sector"]: e["exposure_pct"] for e in calc["exposures"]}, {"Technology": 30.0, "Energy": 15.0})
        self.assertEqual(calc["total_equity_weight_pct"], 45.0)  # bonds' 55% excluded, remainder not scaled to 100
        self.assertEqual(calc["excluded_bond_holdings"], 1)

    def test_floating_point_sums_do_not_cause_false_alarms(self):
        weights = [0.1] * 7 + [0.15, 0.05]
        sec = [(i, "Stock", 1) for i in range(1, 10)]
        db = self.make(self.SECTORS, sec, [(i, 1, i, w) for i, w in enumerate(weights, 1)])
        self.check(db)

    def test_portfolio_without_holdings(self):
        db = self.make(self.SECTORS, [(1, "Stock", 1)], [])
        calc, _ = self.check(db)
        self.assertEqual((calc["status"], calc["exposures"], calc["total_holdings_count"]), ("no_equity_holdings", [], 0))

    def test_unknown_portfolio_id_in_a_result_fails(self):
        db = self.make(self.SECTORS, [(1, "Stock", 1)], [(1, 1, 1, 0.5)])
        calc = ExposureCalculator(db=db).calculate("Test Fund")
        v = FinancialVerifier(db).verify({**calc, "portfolio_id": 42})
        self.assertEqual(v.status, FAILED)
        self.assertEqual(codes(v), {"portfolio_not_found"})


# ======================================================================================
# 3. Corrupted calculator results are detected
# ======================================================================================
class TestCorruptedResultsAreDetected(RealDbCase):
    def setUp(self):
        self.good = self.result("International Equity Fund")      # 4 sectors, no bonds
        self.balanced = self.result("Balanced Portfolio")         # bonds present, total < 100

    def fails_with(self, corrupted, *expected_codes):
        v = self.verifier.verify(corrupted)
        self.assertEqual(v.status, FAILED, "corruption went undetected")
        self.assertTrue(set(expected_codes) <= codes(v), f"expected {expected_codes}, got {codes(v)}")
        return v

    def mutated(self, base=None, **top):
        r = copy.deepcopy(base or self.good)
        r.update(top)
        return r

    def test_incorrect_sector_values(self):
        for field_name, bad, code in (("exposure_pct", 99.0, "sector_pct_mismatch"), ("weight_fraction", 0.5, "sector_weight_mismatch"), ("holding_count", 99, "sector_count_mismatch")):
            with self.subTest(field_name):
                r = self.mutated()
                r["exposures"][0][field_name] = bad
                v = self.fails_with(r, code)
                bad_mismatch = next(m for m in v.mismatches if m.code == code)
                self.assertEqual(bad_mismatch.sector, r["exposures"][0]["sector"])
                self.assertEqual(bad_mismatch.actual, bad)

    def test_tolerance_boundary(self):
        within = self.mutated()
        within["exposures"][0]["exposure_pct"] += 5e-5       # below the 1e-4 pp tolerance (the calculator itself rounds to 4 dp)
        within["exposures"][0]["weight_fraction"] += 5e-7
        within["total_equity_weight_pct"] += 5e-5
        self.assertEqual(self.verifier.verify(within).status, VERIFIED)
        beyond = self.mutated()
        beyond["exposures"][0]["exposure_pct"] += 2e-4
        self.fails_with(beyond, "sector_pct_mismatch")

    def test_missing_sector(self):
        r = self.mutated()
        removed = r["exposures"].pop(1)["sector"]
        v = self.fails_with(r, "missing_sector", "total_inconsistent")  # the reported total no longer matches what is displayed
        self.assertTrue(any(m.code == "missing_sector" and m.sector == removed for m in v.mismatches))

    def test_unexpected_sector(self):
        r = self.mutated()
        r["exposures"].append({"sector": "Cryptocurrency", "weight_fraction": 0.01, "exposure_pct": 1.0, "holding_count": 1})
        v = self.fails_with(r, "unexpected_sector")
        self.assertTrue(any(m.sector == "Cryptocurrency" for m in v.mismatches))

    def test_bond_inclusion_is_diagnosed(self):
        r = copy.deepcopy(self.balanced)
        exp = self.verifier.compute_expected(r["portfolio_id"])
        r["exposures"].append({"sector": "Fixed Income", "weight_fraction": exp.bond_weight_pct / 100, "exposure_pct": exp.bond_weight_pct, "holding_count": exp.bond_holdings})
        r["total_equity_weight_pct"] = round(r["total_equity_weight_pct"] + exp.bond_weight_pct, 4)
        r["excluded_bond_holdings"] = 0
        self.fails_with(r, "unexpected_sector", "total_mismatch", "bond_inclusion", "bond_count_mismatch")

    def test_renormalisation_is_diagnosed(self):
        r = copy.deepcopy(self.balanced)
        total = r["total_equity_weight_pct"]
        self.assertLess(total, 99)
        for e in r["exposures"]:
            e["exposure_pct"] = round(e["exposure_pct"] * 100.0 / total, 4)
            e["weight_fraction"] = round(e["exposure_pct"] / 100.0, 6)
        r["total_equity_weight_pct"] = 100.0
        self.fails_with(r, "sector_pct_mismatch", "total_mismatch", "renormalised")

    def test_incorrect_totals(self):
        self.fails_with(self.mutated(total_equity_weight_pct=96.5), "total_mismatch", "total_inconsistent")
        # every sector right, total alone wrong
        self.fails_with(self.mutated(total_equity_weight_pct=100.5), "total_mismatch")
        # total equals the true value but not the sum of what is displayed
        r = self.mutated()
        r["exposures"][0]["exposure_pct"] += 1.0
        r["exposures"][0]["weight_fraction"] += 0.01
        self.fails_with(r, "sector_pct_mismatch", "total_inconsistent")

    def test_incorrect_portfolio_identity(self):
        self.fails_with(self.mutated(portfolio_name="Growth Equity Fund"), "portfolio_name_mismatch")
        other = self.mutated(portfolio_id=self.balanced["portfolio_id"])  # right name, someone else's id
        self.fails_with(other, "portfolio_name_mismatch")

    def test_incorrect_counts(self):
        for key, code in (("total_holdings_count", "holdings_count_mismatch"), ("equity_holdings_count", "equity_count_mismatch"),
                          ("excluded_bond_holdings", "bond_count_mismatch"), ("unmapped_equity_holdings", "unmapped_count_mismatch")):
            with self.subTest(key):
                self.fails_with(self.mutated(**{key: self.good[key] + 1}), code)

    def test_status_must_match_the_data(self):
        self.fails_with(self.mutated(status="no_equity_holdings"), "status_mismatch")
        empty = self.result("Fixed Income Plus")
        self.fails_with({**empty, "status": "success"}, "status_mismatch")

    def test_duplicate_sector(self):
        r = self.mutated()
        r["exposures"].append(copy.deepcopy(r["exposures"][0]))
        self.fails_with(r, "duplicate_sector")

    def test_internally_inconsistent_sector(self):
        r = self.mutated()
        r["exposures"][0]["weight_fraction"] = round(r["exposures"][0]["weight_fraction"] / 2, 6)
        self.fails_with(r, "sector_weight_mismatch", "sector_inconsistent")

    def test_malformed_results_fail_instead_of_crashing(self):
        self.assertEqual(self.verifier.verify("not a dict").status, FAILED)
        self.assertEqual(self.verifier.verify({"status": "success", "portfolio_id": "7"}).status, FAILED)
        self.fails_with(self.mutated(exposures="oops"), "malformed_result")
        r = self.mutated()
        r["exposures"][0]["exposure_pct"] = "38.0"
        self.fails_with(r, "sector_pct_mismatch")
        r = self.mutated()
        r["exposures"][0]["exposure_pct"] = float("nan")
        self.fails_with(r, "sector_pct_mismatch")
        r = self.mutated()
        r["exposures"][0] = {"sector": None}
        self.fails_with(r)

    def test_a_correct_result_is_not_flagged_by_any_of_these_checks(self):
        self.assertEqual(self.verifier.verify(self.good).status, VERIFIED)
        self.assertEqual(self.verifier.verify(self.balanced).status, VERIFIED)

    def test_mismatch_details_are_useful(self):
        r = self.mutated()
        r["exposures"][0]["exposure_pct"] = 12.5
        v = self.verifier.verify(r)
        m = next(x for x in v.mismatches if x.code == "sector_pct_mismatch").to_dict()
        for key in ("code", "message", "field", "sector", "expected", "actual"):
            self.assertIn(key, m)
        self.assertIn(r["exposures"][0]["sector"], m["message"])


# ======================================================================================
# 4. Failure handling and integration
# ======================================================================================
class TestFailureHandling(RealDbCase):
    def test_database_failure_during_verification_is_unverified_not_verified(self):
        broken = MagicMock()
        broken.execute_read_only.side_effect = ReadOnlyQueryError("database is locked")
        v = FinancialVerifier(broken).verify(self.result("Tech Innovation Fund"))
        self.assertEqual(v.status, UNVERIFIED)
        self.assertFalse(v.verified)
        self.assertEqual(v.mismatches, [])
        self.assertIn("could not be completed", v.error)

    def test_every_query_failing_in_turn_is_unverified(self):
        real = self.db.execute_read_only
        for failing in (fv.SQL_PORTFOLIO, fv.SQL_HOLDINGS, fv.SQL_SECURITIES, fv.SQL_SECTORS):
            def flaky(sql, params=None, failing=failing):
                if sql == failing:
                    raise ReadOnlyQueryError("simulated SQL failure")
                return real(sql, params)

            with self.subTest(failing[:40]), patch.object(self.db, "execute_read_only", side_effect=flaky):
                self.assertEqual(FinancialVerifier(self.db).verify(self.result("Tech Innovation Fund")).status, UNVERIFIED)

    def test_unexpected_exception_in_the_database_layer_is_contained(self):
        broken = MagicMock()
        broken.execute_read_only.side_effect = RuntimeError("boom")
        self.assertEqual(FinancialVerifier(broken).verify(self.result("Tech Innovation Fund")).status, UNVERIFIED)

    def test_sqlglot_fail_closed_applies_to_verification_queries(self):
        good = self.result("Tech Innovation Fund")
        with patch.object(sql_ast, "sqlglot", None):
            v = FinancialVerifier(self.db).verify(good)
        self.assertEqual(v.status, UNVERIFIED)  # the verifier's own SQL is also AST-validated; no query ran

    def test_verifier_cannot_write(self):
        before = self.db.execute_read_only("SELECT COUNT(*) AS n FROM holdings")["rows"][0]["n"]
        self.verifier.verify(self.result("Balanced Portfolio"))
        after = self.db.execute_read_only("SELECT COUNT(*) AS n FROM holdings")["rows"][0]["n"]
        self.assertEqual(before, after)
        for sql in (fv.SQL_PORTFOLIO, fv.SQL_HOLDINGS, fv.SQL_SECURITIES, fv.SQL_SECTORS):
            self.assertTrue(sql.lstrip().upper().startswith("SELECT"))

    def test_error_results_are_not_applicable(self):
        v = self.verifier.verify(self.calc.calculate("Nonexistent Fund"))
        self.assertEqual(v.status, NOT_APPLICABLE)

    # ---- response policy ------------------------------------------------------------

    def corrupted(self):
        r = copy.deepcopy(self.result("International Equity Fund"))
        r["exposures"][0]["exposure_pct"] += 7.0
        return r

    def test_failed_verification_is_never_presented_as_a_valid_result(self):
        bad = self.corrupted()
        out = apply_verification(bad, self.verifier.verify(bad))
        self.assertEqual(out["status"], "error")
        self.assertEqual(out["exposures"], [])
        self.assertEqual(out["total_equity_weight_pct"], 0.0)
        self.assertIn("failed independent verification", out["error"])
        self.assertEqual(out["verification"]["status"], FAILED)
        self.assertFalse(out["verification"]["verified"])
        self.assertEqual(out["verification"]["rejected_result"]["exposures"], bad["exposures"])  # kept for diagnosis only
        self.assertEqual(out["portfolio_name"], "International Equity Fund")
        self.assertNotIn("verification", bad)  # the input is not mutated

    def test_response_keys_are_preserved(self):
        good = self.result("Balanced Portfolio")
        for result in (apply_verification(good, self.verifier.verify(good)), apply_verification(self.corrupted(), FinancialVerifier(self.db).verify(self.corrupted()))):
            self.assertTrue(set(good) <= set(result))
        self.assertEqual({k: v for k, v in apply_verification(good, self.verifier.verify(good)).items() if k != "verification"}, good)

    def test_unverified_result_withholds_the_figures(self):
        good = self.result("Tech Innovation Fund")
        v = FinancialVerifier(MagicMock(execute_read_only=MagicMock(side_effect=ReadOnlyQueryError("x")))).verify(good)
        out = apply_verification(good, v)
        self.assertEqual(out["status"], "error")                       # no longer looks like a normal success
        self.assertEqual(out["exposures"], [])
        self.assertEqual(out["total_equity_weight_pct"], 0.0)
        self.assertIn("could not be completed", out["error"])
        self.assertIn("withheld", out["error"])
        self.assertEqual(out["verification"]["status"], UNVERIFIED)    # still distinct from "failed"
        self.assertFalse(out["verification"]["verified"])
        self.assertEqual(out["verification"]["mismatches"], [])        # nothing was disproved
        self.assertEqual(out["verification"]["rejected_result"]["exposures"], good["exposures"])  # kept for diagnosis only
        self.assertEqual(out["portfolio_name"], "Tech Innovation Fund")
        self.assertTrue(set(good) <= set(out))                         # response keys preserved

    def test_failed_and_unverified_are_distinguishable(self):
        good = self.result("International Equity Fund")
        bad = copy.deepcopy(good)
        bad["exposures"][0]["exposure_pct"] += 7.0
        failed = apply_verification(bad, self.verifier.verify(bad))
        unverified = apply_verification(good, FinancialVerifier(MagicMock(execute_read_only=MagicMock(side_effect=ReadOnlyQueryError("x")))).verify(good))
        self.assertEqual((failed["status"], unverified["status"]), ("error", "error"))
        self.assertEqual((failed["verification"]["status"], unverified["verification"]["status"]), (FAILED, UNVERIFIED))
        self.assertIn("failed independent verification", failed["error"])
        self.assertNotIn("failed independent verification", unverified["error"])
        self.assertIn("could not be completed", unverified["error"])
        self.assertTrue(failed["verification"]["mismatches"])
        self.assertEqual(unverified["verification"]["mismatches"], [])

    # ---- agent integration -----------------------------------------------------------

    def agent(self, calculator=None, verifier=None):
        kwargs = {} if verifier is None else {"financial_verifier": verifier}
        return PortfolioAgent(db=self.db, sql_tool=SQLQueryTool(db=self.db, use_gemini=False), exposure_tool=calculator or self.calc, intent_classifier=None, **kwargs)

    def test_agent_returns_verified_exposure_for_q9_and_q10(self):
        agent = self.agent()
        for question, portfolio in (("What are the sector exposures for the Tech Innovation Fund?", "Tech Innovation Fund"),
                                    ("Calculate the sector exposure breakdown for international equity", "International Equity Fund")):
            with self.subTest(portfolio):
                r = agent.answer_question(question)
                self.assertEqual((r["status"], r["selected_tool"]), ("success", "exposure_calculator"))
                self.assertEqual(r["result"]["verification"]["status"], VERIFIED)
                self.assertEqual({k: v for k, v in r["result"].items() if k not in ("verification", "answer_verification")}, self.calc.calculate(portfolio))
                self.assertEqual(r["result"]["answer_verification"]["status"], VERIFIED)

    def test_agent_rejects_a_corrupted_calculator_result(self):
        bad = self.corrupted()
        fake_calc = MagicMock()
        fake_calc.calculate.return_value = bad
        r = self.agent(calculator=fake_calc).answer_question("Calculate the sector exposure breakdown for international equity")
        self.assertEqual(r["status"], "error")
        self.assertIn("failed independent verification", r["error"])
        self.assertEqual(r["result"]["exposures"], [])
        self.assertEqual(r["result"]["verification"]["status"], FAILED)

    def test_agent_withholds_the_figures_when_verification_cannot_run(self):
        broken_verifier = FinancialVerifier(MagicMock(execute_read_only=MagicMock(side_effect=ReadOnlyQueryError("down"))))
        r = self.agent(verifier=broken_verifier).answer_question("What are the sector exposures for the Tech Innovation Fund?")
        self.assertEqual((r["status"], r["selected_tool"]), ("error", "exposure_calculator"))
        self.assertIn("could not be completed", r["error"])
        self.assertEqual((r["result"]["exposures"], r["result"]["total_equity_weight_pct"]), ([], 0.0))
        self.assertEqual(r["result"]["verification"]["status"], UNVERIFIED)

    def test_agent_error_responses_get_no_verification_claim(self):
        r = self.agent().answer_question("Show sector exposure for the Atlantis Fund")
        self.assertEqual(r["status"], "error")
        self.assertNotIn("verification", r["result"] or {})

    def test_with_no_verifier_the_figures_are_withheld_not_shown_unchecked(self):
        agent = PortfolioAgent(db=self.db, sql_tool=SQLQueryTool(db=self.db, use_gemini=False), intent_classifier=None, financial_verifier=None, answer_verifier=None)
        r = agent.answer_question("What are the sector exposures for the Tech Innovation Fund?")
        self.assertEqual((r["status"], r["result"]["exposures"], r["result"]["total_equity_weight_pct"]), ("error", [], 0.0))
        self.assertEqual(r["result"]["verification"]["status"], UNVERIFIED)          # "could not be checked", not "failed"
        self.assertEqual(r["result"]["verification"]["mismatches"], [])
        self.assertIn("disabled or unavailable", r["error"])
        self.assertIn("withheld", r["error"])
        # the calculator's figures survive only in the diagnostic block
        self.assertEqual([e["sector"] for e in r["result"]["verification"]["rejected_result"]["exposures"]], ["Technology"])

    def test_sql_questions_are_untouched(self):
        r = self.agent().answer_question("How many portfolios do we have in total?")
        self.assertEqual(r["selected_tool"], "sql_query")
        self.assertNotIn("verification", r["result"])

    # ---- a database fault that hits only the verifier's queries --------------------------

    def verifier_only_fault(self, db):
        """Patch ``db`` so ONLY the verifier's own queries fail (the calculator keeps working)."""
        real = db.execute_read_only

        def flaky(sql, params=None):
            if sql in (fv.SQL_PORTFOLIO, fv.SQL_HOLDINGS, fv.SQL_SECURITIES, fv.SQL_SECTORS):
                raise ReadOnlyQueryError("simulated database failure during verification")
            return real(sql, params)

        return patch.object(db, "execute_read_only", side_effect=flaky)

    def test_database_failure_only_in_verification_withholds_figures_in_the_agent(self):
        agent = self.agent()
        with self.verifier_only_fault(self.db):
            r = agent.answer_question("Calculate the sector exposure breakdown for international equity")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["result"]["exposures"], [])
        self.assertEqual(r["result"]["total_equity_weight_pct"], 0.0)
        self.assertEqual(r["result"]["verification"]["status"], UNVERIFIED)
        self.assertGreater(len(r["result"]["verification"]["rejected_result"]["exposures"]), 0)  # calculator did run

    def test_agent_and_api_apply_the_same_policy(self):
        try:
            from portfolio_api.service import PortfolioService
        except ImportError:
            self.skipTest("FastAPI adapter dependencies not installed")
        service = PortfolioService(use_llm=False, use_intent_classifier=False)
        agent = PortfolioAgent(db=service.db, sql_tool=service.sql_tool, exposure_tool=service.exposure_tool, intent_classifier=None)
        question, portfolio = "Calculate the sector exposure breakdown for international equity", "International Equity Fund"
        keys = ("status", "exposures", "total_equity_weight_pct", "error", "portfolio_name", "portfolio_id")

        # healthy: both verified, same figures
        a, b = agent.answer_question(question)["result"], service.exposure(portfolio)
        self.assertEqual(a["verification"]["status"], b["verification"]["status"], VERIFIED)
        self.assertEqual({k: a[k] for k in keys}, {k: b[k] for k in keys})

        # database fault during verification: both withhold, identically
        with self.verifier_only_fault(service.db):
            a, b = agent.answer_question(question)["result"], service.exposure(portfolio)
        self.assertEqual({k: a[k] for k in keys}, {k: b[k] for k in keys})
        self.assertEqual((b["status"], b["exposures"], b["total_equity_weight_pct"]), ("error", [], 0.0))
        self.assertEqual(b["verification"]["status"], UNVERIFIED)

        # disproved figures: both reject, identically, and as "failed" rather than "unverified"
        bad = copy.deepcopy(service.exposure_tool.calculate(portfolio))
        bad["exposures"][0]["exposure_pct"] += 7.0
        with patch.object(service.exposure_tool, "calculate", return_value=bad):
            a, b = agent.answer_question(question)["result"], service.exposure(portfolio)
        self.assertEqual({k: a[k] for k in keys}, {k: b[k] for k in keys})
        self.assertEqual((a["verification"]["status"], b["verification"]["status"]), (FAILED, FAILED))

    def test_failed_and_unverified_responses_expose_no_figures_anywhere_but_the_diagnostic_block(self):
        agent = self.agent()
        with self.verifier_only_fault(self.db):
            unverified = agent.answer_question("What are the sector exposures for the Tech Innovation Fund?")
        bad = self.corrupted()
        fake = MagicMock()
        fake.calculate.return_value = bad
        failed = self.agent(calculator=fake).answer_question("Calculate the sector exposure breakdown for international equity")
        for response in (unverified, failed):
            shown = {k: v for k, v in response["result"].items() if k != "verification"}
            self.assertEqual(shown["exposures"], [])
            self.assertEqual(shown["total_equity_weight_pct"], 0.0)
            self.assertEqual(response["status"], "error")
        self.assertNotIn("Technology", str({"error": unverified["error"], **{k: v for k, v in unverified["result"].items() if k != "verification"}}))

    # ---- CLI --pretty ------------------------------------------------------------------

    def test_pretty_cli_shows_the_verification_status_and_never_the_withheld_figures(self):
        import main as cli

        agent = self.agent()
        verified = cli._format_pretty(agent.answer_question("What are the sector exposures for the Tech Innovation Fund?"))
        self.assertIn("Verification: VERIFIED", verified)
        self.assertIn("Technology", verified)
        with self.verifier_only_fault(self.db):
            unverified = cli._format_pretty(agent.answer_question("What are the sector exposures for the Tech Innovation Fund?"))
        self.assertIn("Verification: UNVERIFIED", unverified)
        self.assertIn("withheld", unverified)
        self.assertNotIn("Technology", unverified)
        self.assertNotIn("Exposure %", unverified)
        fake = MagicMock()
        fake.calculate.return_value = self.corrupted()
        failed = cli._format_pretty(self.agent(calculator=fake).answer_question("Calculate the sector exposure breakdown for international equity"))
        self.assertIn("Verification: FAILED", failed)
        self.assertNotIn("| Energy", failed)  # no sector table row; the failure text may quote the disproved value
        self.assertNotIn("Verification: VERIFIED", failed + unverified)
        self.assertNotEqual(unverified.split("Verification:")[1].split("\n")[0], failed.split("Verification:")[1].split("\n")[0])

    # ---- API service integration ---------------------------------------------------------

    def test_api_service_verifies_and_rejects(self):
        try:
            from portfolio_api.service import PortfolioService
        except ImportError:
            self.skipTest("FastAPI adapter dependencies not installed")
        service = PortfolioService(use_llm=False, use_intent_classifier=False)
        ok = service.exposure("International Equity Fund")
        self.assertEqual(ok["verification"]["status"], VERIFIED)
        self.assertEqual({e["sector"]: e["exposure_pct"] for e in ok["exposures"]}, {"Energy": 38.0, "Technology": 36.0, "Automotive": 15.0, "Consumer Staples": 11.0})
        bad = self.corrupted()
        with patch.object(service.exposure_tool, "calculate", return_value=bad):
            out = service.exposure("International Equity Fund")
        self.assertEqual((out["status"], out["exposures"]), ("error", []))
        err = service.exposure("Nonexistent Fund")
        self.assertEqual(err["status"], "error")
        self.assertNotIn("verification", err)


if __name__ == "__main__":
    unittest.main()
