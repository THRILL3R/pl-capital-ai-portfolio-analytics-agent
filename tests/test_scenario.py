"""
Tests for deterministic scenario / stress testing (scenario_tool.py) and its independent verification
(scenario_verifier.py), plus agent routing, API, CLI and the withholding policy.

Gemini is mocked wherever it could appear (it must never be called). Crafted databases use the project schema.
"""

from __future__ import annotations

import ast
import copy
import json
import math
import sqlite3
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import main as cli
from portfolio_agent import scenario_verifier as sv
from portfolio_agent import sql_ast
from portfolio_agent.agent import PortfolioAgent
from portfolio_agent.database import PortfolioDatabase, ReadOnlyQueryError
from portfolio_agent.scenario_tool import (
    SCENARIOS, SHOCK_MAX, SHOCK_MIN, ScenarioError, ScenarioTool, is_scenario_question, parse_scenario_request, resolve_scenario, validate_shock,
)
from portfolio_agent.scenario_verifier import (
    FAILED, NOT_APPLICABLE, UNVERIFIED, VERIFIED, ScenarioVerifier, apply_scenario_verification, scenario_digest, unavailable_scenario_verification,
)
from portfolio_agent.sql_generator import GeminiSQLGenerator
from portfolio_agent.tools import ExposureCalculator, SQLQueryTool

SCHEMA = (PROJECT_ROOT / "database_schema.sql").read_text(encoding="utf-8")
INTL = {"Energy": 0.38, "Technology": 0.36, "Automotive": 0.15, "Consumer Staples": 0.11}


def make_db(sectors, securities, holdings, aum=1_000_000.0, portfolios=((1, "Test Fund"),)):
    """Crafted database. securities: (id, type, sector_id); holdings: (id, portfolio_id, security_id, weight)."""
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "crafted.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    for sid, name in sectors:
        conn.execute("INSERT INTO sectors (sector_id, sector_name) VALUES (?, ?)", (sid, name))
    for sec_id, kind, sector_id in securities:
        conn.execute("INSERT INTO securities (security_id, symbol, company_name, asset_type, sector_id, current_price) VALUES (?, ?, ?, ?, ?, 1.0)", (sec_id, f"S{sec_id}", f"Co {sec_id}", kind, sector_id))
    for pid, name in portfolios:
        conn.execute("INSERT INTO portfolios (portfolio_id, portfolio_name, total_aum) VALUES (?, ?, ?)", (pid, name, aum))
    for hid, pid, sec_id, weight in holdings:
        conn.execute("INSERT INTO holdings (holding_id, portfolio_id, security_id, quantity, current_weight) VALUES (?, ?, ?, 10, ?)", (hid, pid, sec_id, weight))
    conn.commit()
    conn.close()
    db = PortfolioDatabase(db_path=path)
    db._tmp = tmp
    return db


SECTORS = [(1, "Technology"), (2, "Energy"), (3, "Utilities")]


class RealDbCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = PortfolioDatabase()
        if not cls.db.is_initialized():
            cls.db.initialize_database()
        cls.calc = ExposureCalculator(db=cls.db)
        cls.tool = ScenarioTool(cls.db, cls.calc, ScenarioVerifier(cls.db))
        cls.verifier = ScenarioVerifier(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def contributions(self, result):
        return {i["sector"]: i["contribution_pp"] for i in result["sector_impacts"]}


# ======================================================================================
# Calculations
# ======================================================================================
class TestScenarioCalculations(RealDbCase):
    def test_the_specification_example_technology_36_percent_times_minus_25(self):
        r = self.tool.run("International Equity Fund", "technology_sell_off")
        self.assertEqual(r["status"], "success")
        self.assertEqual(self.contributions(r), {"Energy": 0.0, "Technology": -9.0, "Automotive": 0.0, "Consumer Staples": 0.0})
        self.assertEqual(r["estimated_portfolio_impact_pct"], -9.0)
        self.assertEqual(r["verification"]["status"], VERIFIED)

    def test_broad_market_downturn_is_minus_10_on_every_mapped_sector(self):
        r = self.tool.run("International Equity Fund", "broad_market_downturn")
        self.assertEqual({i["shock_pct"] for i in r["sector_impacts"]}, {-10.0})
        self.assertEqual(self.contributions(r), {"Energy": -3.8, "Technology": -3.6, "Automotive": -1.5, "Consumer Staples": -1.1})
        self.assertEqual(r["estimated_portfolio_impact_pct"], -10.0)

    def test_severe_market_stress_is_minus_20_and_is_not_rescaled_for_partly_invested_portfolios(self):
        full = self.tool.run("International Equity Fund", "severe_market_stress")
        self.assertEqual(full["estimated_portfolio_impact_pct"], -20.0)
        balanced = self.tool.run("Balanced Portfolio", "severe_market_stress")
        self.assertEqual(balanced["coverage"]["equity_exposure_pct"], 36.0)
        self.assertEqual(balanced["estimated_portfolio_impact_pct"], -7.2)   # 0.36 x -20, NOT -20
        self.assertEqual(balanced["verification"]["status"], VERIFIED)

    def test_energy_shock(self):
        r = self.tool.run("International Equity Fund", "energy_shock")
        self.assertEqual(self.contributions(r)["Energy"], -7.6)
        self.assertEqual(r["estimated_portfolio_impact_pct"], -7.6)

    def test_predefined_scenarios_match_the_specification(self):
        self.assertEqual({k: (v["market_shock_pct"], v["sector_shocks"]) for k, v in SCENARIOS.items()},
                         {"broad_market_downturn": (-10, {}), "severe_market_stress": (-20, {}), "technology_sell_off": (0, {"Technology": -25}), "energy_shock": (0, {"Energy": -20})})
        self.assertEqual(SCENARIOS["broad_market_downturn"]["name"], "Broad Market Downturn")
        self.assertEqual(SCENARIOS["technology_sell_off"]["name"], "Technology Sell-off")
        self.assertEqual(sv.INDEPENDENT_SCENARIOS, {k: (v["market_shock_pct"], v["sector_shocks"]) for k, v in SCENARIOS.items()})  # the two tables agree

    def test_positive_shock(self):
        r = self.tool.run("International Equity Fund", market_shock_pct=None, sector_shocks={"Energy": 10})
        self.assertEqual((r["estimated_portfolio_impact_pct"], self.contributions(r)["Energy"]), (3.8, 3.8))
        self.assertEqual(r["estimated_value_impact"]["amount"], 1_064_000.0)
        up = self.tool.run("International Equity Fund", market_shock_pct=5)
        self.assertEqual(up["estimated_portfolio_impact_pct"], 5.0)
        self.assertEqual(up["verification"]["status"], VERIFIED)

    def test_zero_shock_changes_nothing(self):
        r = self.tool.run("International Equity Fund", market_shock_pct=0)
        self.assertEqual(r["estimated_portfolio_impact_pct"], 0.0)
        self.assertEqual({i["contribution_pp"] for i in r["sector_impacts"]}, {0.0})
        self.assertFalse(any(i["shocked"] for i in r["sector_impacts"]))
        self.assertEqual((r["coverage"]["shocked_weight_pct"], r["coverage"]["unshocked_mapped_weight_pct"]), (0.0, 100.0))
        self.assertEqual(r["estimated_value_impact"]["amount"], 0.0)
        self.assertEqual(r["verification"]["status"], VERIFIED)

    def test_multiple_sector_shocks_and_a_market_shock_with_overrides(self):
        r = self.tool.run("International Equity Fund", sector_shocks={"Technology": -25, "Energy": -20, "Automotive": 5})
        self.assertEqual(self.contributions(r), {"Energy": -7.6, "Technology": -9.0, "Automotive": 0.75, "Consumer Staples": 0.0})
        self.assertEqual(r["estimated_portfolio_impact_pct"], -15.85)
        mixed = self.tool.run("International Equity Fund", market_shock_pct=-5, sector_shocks={"Technology": -25})
        self.assertEqual({i["sector"]: i["shock_pct"] for i in mixed["sector_impacts"]}, {"Energy": -5.0, "Technology": -25.0, "Automotive": -5.0, "Consumer Staples": -5.0})
        self.assertAlmostEqual(mixed["estimated_portfolio_impact_pct"], -9.0 + (-5 * 0.64), places=9)
        self.assertEqual(mixed["verification"]["status"], VERIFIED)

    def test_unshocked_sectors_contribute_exactly_zero_and_unheld_shocks_are_reported(self):
        r = self.tool.run("International Equity Fund", sector_shocks={"Technology": -25, "Utilities": -50})
        self.assertEqual(r["unheld_sector_shocks"], [{"sector": "Utilities", "shock_pct": -50.0}])
        self.assertEqual(r["estimated_portfolio_impact_pct"], -9.0)
        self.assertEqual(r["verification"]["status"], VERIFIED)

    def test_bonds_are_excluded_and_reported(self):
        r = self.tool.run("Balanced Portfolio", market_shock_pct=-50)
        c = r["coverage"]
        self.assertEqual((c["excluded_bond_holdings"], c["excluded_bond_weight_pct"]), (4, 64.0))
        self.assertEqual(r["estimated_portfolio_impact_pct"], -18.0)       # only the 36% equity is shocked
        self.assertEqual(c["recorded_weight_accounted_pct"], 100.0)
        self.assertNotIn("Fixed Income", {i["sector"] for i in r["sector_impacts"]})

    def test_no_renormalisation_ever(self):
        r = self.tool.run("Balanced Portfolio", market_shock_pct=-10)
        self.assertFalse(r["coverage"]["renormalised"])
        self.assertEqual(r["estimated_portfolio_impact_pct"], -3.6)
        self.assertEqual(sum(i["exposure_pct"] for i in r["sector_impacts"]), 36.0)

    def test_bond_only_portfolio_has_nothing_to_shock(self):
        r = self.tool.run("Fixed Income Plus", "broad_market_downturn")
        self.assertEqual((r["status"], r["sector_impacts"], r["estimated_portfolio_impact_pct"]), ("no_equity_holdings", [], 0.0))
        self.assertEqual(r["verification"]["status"], VERIFIED)
        self.assertGreater(r["coverage"]["excluded_bond_holdings"], 0)

    def test_every_portfolio_and_every_scenario_verifies(self):
        for p in self.db.execute_read_only("SELECT portfolio_name FROM portfolios")["rows"]:
            for sid in SCENARIOS:
                with self.subTest(portfolio=p["portfolio_name"], scenario=sid):
                    r = self.tool.run(p["portfolio_name"], sid)
                    self.assertEqual(r["verification"]["status"], VERIFIED, r["verification"]["mismatches"])

    def test_value_impact_uses_recorded_aum_and_is_labelled(self):
        r = self.tool.run("International Equity Fund", "severe_market_stress")
        self.assertEqual(r["estimated_value_impact"], {"amount": -5_600_000.0, "currency": "USD", "aum": 28_000_000.0, "basis": "total_aum x estimated_portfolio_impact_pct / 100"})
        self.assertIn("percentage points", r["impact_basis"])

    def test_result_contract_and_language(self):
        r = self.tool.run("International Equity Fund", "technology_sell_off")
        for key in ("tool", "status", "portfolio_id", "portfolio_name", "scenario", "starting_exposure", "sector_impacts", "unheld_sector_shocks",
                    "estimated_portfolio_impact_pct", "estimated_value_impact", "coverage", "assumptions", "disclaimer", "verification", "error"):
            self.assertIn(key, r)
        self.assertEqual((r["tool"], r["portfolio_id"], r["portfolio_name"]), ("scenario_analysis", 7, "International Equity Fund"))
        self.assertTrue(r["scenario"]["is_hypothetical"])
        self.assertFalse(r["scenario"]["is_forecast"])
        text = json.dumps(r).lower()
        self.assertIn("not a forecast", text)
        self.assertIn("not actual portfolio performance", text)
        self.assertNotIn("will lose", text)
        self.assertNotIn("predicted", text)
        json.dumps(r)

    def test_decimal_arithmetic_has_no_binary_float_noise(self):
        db = make_db(SECTORS, [(1, "Stock", 1)], [(1, 1, 1, 0.1)])
        r = ScenarioTool(db, ExposureCalculator(db=db), ScenarioVerifier(db)).run("Test Fund", market_shock_pct=-30)
        self.assertEqual(r["sector_impacts"][0]["contribution_pp"], -3.0)    # 0.1 x -30 is exactly -3, not -3.0000000000000004
        self.assertEqual(Decimal("0.1") * Decimal(-30), Decimal("-3.0"))
        db.close()


# ======================================================================================
# Coverage edge cases on crafted databases
# ======================================================================================
class TestCraftedCoverage(unittest.TestCase):
    def run_scenario(self, db, **kw):
        self.addCleanup(db._tmp.cleanup)
        self.addCleanup(db.close)
        return ScenarioTool(db, ExposureCalculator(db=db), ScenarioVerifier(db)).run("Test Fund", **kw)

    def test_partial_coverage_is_reported_not_hidden(self):
        db = make_db(SECTORS, [(1, "Stock", 1), (2, "Stock", 2)], [(1, 1, 1, 0.25), (2, 1, 2, 0.15)])
        r = self.run_scenario(db, sector_shocks={"Technology": -20})
        c = r["coverage"]
        self.assertEqual((c["equity_exposure_pct"], c["shocked_weight_pct"], c["unshocked_mapped_weight_pct"]), (40.0, 25.0, 15.0))
        self.assertEqual((c["recorded_weight_accounted_pct"], c["recorded_weight_unaccounted_pct"]), (40.0, 60.0))   # 60% of the portfolio is not described
        self.assertEqual(r["estimated_portfolio_impact_pct"], -5.0)
        self.assertEqual(r["verification"]["status"], VERIFIED)

    def test_unmapped_stock_is_identified_and_contributes_zero(self):
        db = make_db(SECTORS, [(1, "Stock", 1), (2, "Stock", None)], [(1, 1, 1, 0.5), (2, 1, 2, 0.3)])
        r = self.run_scenario(db, market_shock_pct=-10)
        c = r["coverage"]
        self.assertEqual((c["unmapped_equity_holdings"], c["unmapped_equity_weight_pct"], c["equity_holdings_count"]), (1, 30.0, 2))
        self.assertEqual(r["estimated_portfolio_impact_pct"], -5.0)          # only the mapped 50% is shocked
        self.assertEqual(c["equity_exposure_pct"], 50.0)
        self.assertEqual(r["verification"]["status"], VERIFIED)

    def test_bond_weight_is_excluded_even_under_a_market_wide_shock(self):
        db = make_db(SECTORS, [(1, "Stock", 1), (2, "Bond", None)], [(1, 1, 1, 0.4), (2, 1, 2, 0.6)])
        r = self.run_scenario(db, market_shock_pct=-100)
        self.assertEqual(r["estimated_portfolio_impact_pct"], -40.0)         # a bond is never shocked, even by -100%
        self.assertEqual((r["coverage"]["excluded_bond_holdings"], r["coverage"]["excluded_bond_weight_pct"]), (1, 60.0))
        self.assertEqual(r["estimated_value_impact"]["amount"], -400_000.0)

    def test_weights_not_summing_to_100_are_never_forced_to(self):
        db = make_db(SECTORS, [(1, "Stock", 1), (2, "Stock", 2)], [(1, 1, 1, 0.3), (2, 1, 2, 0.2)])
        r = self.run_scenario(db, market_shock_pct=-10)
        self.assertEqual(r["estimated_portfolio_impact_pct"], -5.0)
        self.assertEqual(r["coverage"]["recorded_weight_unaccounted_pct"], 50.0)
        self.assertFalse(r["coverage"]["renormalised"])

    def test_empty_portfolio(self):
        db = make_db(SECTORS, [(1, "Stock", 1)], [])
        r = self.run_scenario(db, market_shock_pct=-10)
        self.assertEqual((r["status"], r["sector_impacts"], r["estimated_portfolio_impact_pct"]), ("no_equity_holdings", [], 0.0))
        self.assertEqual(r["verification"]["status"], VERIFIED)

    def test_missing_aum_means_no_currency_impact(self):
        db = make_db(SECTORS, [(1, "Stock", 1)], [(1, 1, 1, 0.5)], aum=None)
        r = self.run_scenario(db, market_shock_pct=-10)
        self.assertIsNone(r["estimated_value_impact"])
        self.assertIn("No positive total AUM", r["value_impact_note"])
        self.assertEqual(r["verification"]["status"], VERIFIED)
        db2 = make_db(SECTORS, [(1, "Stock", 1)], [(1, 1, 1, 0.5)], aum=0)
        self.assertIsNone(self.run_scenario(db2, market_shock_pct=-10)["estimated_value_impact"])

    def test_null_weights_add_nothing(self):
        db = make_db(SECTORS, [(1, "Stock", 1), (2, "Stock", 1)], [(1, 1, 1, 0.2), (2, 1, 2, None)])
        r = self.run_scenario(db, market_shock_pct=-10)
        self.assertEqual(r["estimated_portfolio_impact_pct"], -2.0)
        self.assertEqual(r["verification"]["status"], VERIFIED)


# ======================================================================================
# Input validation
# ======================================================================================
class TestValidation(RealDbCase):
    def test_invalid_portfolio(self):
        r = self.tool.run("Atlantis Fund", "broad_market_downturn")
        self.assertEqual((r["status"], r["error_type"], r["sector_impacts"]), ("error", "invalid_portfolio", []))
        self.assertIn("not found", r["error"])
        self.assertNotIn("verification", r)
        ambiguous = self.tool.run("Fund", "broad_market_downturn")
        self.assertEqual(ambiguous["status"], "error")

    def test_nan_infinity_and_malformed_shocks_are_rejected(self):
        bad = [float("nan"), float("inf"), float("-inf"), Decimal("NaN"), Decimal("Infinity"), "10", "abc", True, False, None, [5], {"a": 1}, object()]
        for value in bad:
            with self.subTest(repr(value)):
                if value is None:
                    continue  # None means "not given": covered below
                r = self.tool.run("International Equity Fund", market_shock_pct=value)
                self.assertEqual((r["status"], r["error_type"]), ("error", "invalid_scenario"), repr(value))
                self.assertEqual(r["sector_impacts"], [])
        for value in (float("nan"), float("inf"), "5", True):
            r = self.tool.run("International Equity Fund", sector_shocks={"Technology": value})
            self.assertEqual(r["error_type"], "invalid_scenario")
        self.assertEqual(self.tool.run("International Equity Fund", sector_shocks=["Technology"])["error_type"], "invalid_scenario")

    def test_shock_boundaries(self):
        self.assertEqual((SHOCK_MIN, SHOCK_MAX), (Decimal(-100), Decimal(100)))
        for ok_value in (-100, 100, -100.0, 99.999999, 0, -0.0):
            validate_shock(ok_value)
        for bad_value in (-100.000001, 100.000001, -101, 1e9, -1e9):
            with self.subTest(bad_value), self.assertRaises(ScenarioError):
                validate_shock(bad_value)
        r = self.tool.run("International Equity Fund", market_shock_pct=-100)
        self.assertEqual(r["estimated_portfolio_impact_pct"], -100.0)
        self.assertEqual(r["verification"]["status"], VERIFIED)
        self.assertEqual(r["estimated_value_impact"]["amount"], -28_000_000.0)       # a total wipe-out of the equity, never worse
        up = self.tool.run("International Equity Fund", market_shock_pct=100)
        self.assertEqual(up["estimated_portfolio_impact_pct"], 100.0)
        tech = self.tool.run("International Equity Fund", sector_shocks={"Technology": -100})
        self.assertEqual(tech["estimated_portfolio_impact_pct"], -36.0)
        for value in (-100.01, 100.01):
            self.assertEqual(self.tool.run("International Equity Fund", market_shock_pct=value)["error_type"], "invalid_scenario")
            self.assertEqual(self.tool.run("International Equity Fund", sector_shocks={"Technology": value})["error_type"], "invalid_scenario")

    def test_sector_and_scenario_names(self):
        ok = self.tool.run("International Equity Fund", sector_shocks={"technology": -10})
        self.assertEqual(ok["scenario"]["sector_shocks"], {"Technology": -10.0})        # case-insensitive, canonical name kept
        for kwargs in ({"sector_shocks": {"Crypto": -10}}, {"sector_shocks": {"Technology": -10, "TECHNOLOGY": -20}}, {"scenario_id": "unknown"},
                       {"scenario_id": "energy_shock", "market_shock_pct": -5}, {}, {"sector_shocks": {}}, {"sector_shocks": {5: -1}}):
            with self.subTest(kwargs):
                r = self.tool.run("International Equity Fund", **kwargs)
                self.assertEqual((r["status"], r["error_type"]), ("error", "invalid_scenario"))

    def test_resolve_scenario_normalises_custom_definitions(self):
        s = resolve_scenario(["Technology", "Energy"], None, -15, {"energy": 5})
        self.assertEqual((s["id"], s["market_shock_pct"], s["sector_shocks"]), ("custom", Decimal(-15), {"Energy": Decimal(5)}))
        self.assertIn("Energy +5", s["description"])


# ======================================================================================
# Independent verification
# ======================================================================================
class TestIndependentVerification(RealDbCase):
    def setUp(self):
        self.good = copy.deepcopy(self.tool.run("International Equity Fund", sector_shocks={"Technology": -25, "Energy": -20}))
        self.good.pop("verification")
        self.balanced = copy.deepcopy(self.tool.run("Balanced Portfolio", "technology_sell_off"))
        self.balanced.pop("verification")

    def fails_with(self, result, *codes):
        v = self.verifier.verify(result)
        self.assertEqual(v.status, FAILED, "corruption went undetected")
        got = {m.code for m in v.mismatches}
        self.assertTrue(set(codes) <= got, f"expected {codes}, got {got}")
        return v

    def mutated(self, base=None):
        return copy.deepcopy(base or self.good)

    def test_correct_results_verify(self):
        self.assertEqual(self.verifier.verify(self.good).status, VERIFIED)
        self.assertEqual(self.verifier.verify(self.balanced).status, VERIFIED)

    def test_wrong_contributions_totals_and_shocks_are_detected(self):
        r = self.mutated(); r["sector_impacts"][0]["contribution_pp"] += 1.0
        v = self.fails_with(r, "contribution_mismatch", "total_inconsistent")
        self.assertEqual(next(m for m in v.mismatches if m.code == "contribution_mismatch").sector, r["sector_impacts"][0]["sector"])
        self.fails_with({**self.mutated(), "estimated_portfolio_impact_pct": -1.0}, "total_mismatch", "total_inconsistent")
        r = self.mutated(); r["sector_impacts"][1]["shock_pct"] = -1.0
        self.fails_with(r, "shock_mismatch")
        r = self.mutated(); r["sector_impacts"][0]["exposure_pct"] = 10.0
        self.fails_with(r, "exposure_mismatch")
        r = self.mutated(); r["starting_exposure"][0]["exposure_pct"] = 10.0
        self.fails_with(r, "exposure_mismatch")

    def test_the_predefined_definition_is_checked_against_the_independent_copy(self):
        r = self.mutated(self.balanced)   # technology_sell_off recomputed consistently but with -2.5 instead of -25
        r["scenario"]["sector_shocks"]["Technology"] = -2.5
        for row in r["sector_impacts"]:
            if row["sector"] == "Technology":
                row["shock_pct"], row["contribution_pp"] = -2.5, round(row["exposure_pct"] * -2.5 / 100, 6)
        r["estimated_portfolio_impact_pct"] = round(sum(x["contribution_pp"] for x in r["sector_impacts"]), 6)
        self.fails_with(r, "scenario_definition_mismatch")

    def test_sector_set_errors(self):
        r = self.mutated(); removed = r["sector_impacts"].pop()["sector"]
        v = self.fails_with(r, "sector_set_mismatch")
        self.assertIn(removed, str(v.mismatches[0].expected))
        r = self.mutated(); r["sector_impacts"].append({"sector": "Crypto", "exposure_pct": 1.0, "shock_pct": 0.0, "contribution_pp": 0.0, "shocked": False})
        self.fails_with(r, "sector_set_mismatch")
        r = self.mutated(); r["sector_impacts"].append(copy.deepcopy(r["sector_impacts"][0]))
        self.fails_with(r, "sector_duplicate")
        r = self.mutated(); r["scenario"]["sector_shocks"]["Atlantis"] = -5
        self.fails_with(r, "unknown_shock_sector")

    def test_coverage_exclusions_and_flags(self):
        for key, bad, code in (("excluded_bond_weight_pct", 1.0, "coverage_mismatch"), ("unmapped_equity_weight_pct", 5.0, "coverage_mismatch"),
                               ("shocked_weight_pct", 100.0, "coverage_mismatch"), ("recorded_weight_unaccounted_pct", 40.0, "coverage_mismatch"),
                               ("excluded_bond_holdings", 0, "count_mismatch"), ("unmapped_equity_holdings", 3, "count_mismatch"), ("equity_holdings_count", 99, "count_mismatch")):
            with self.subTest(key):
                r = self.mutated(self.balanced); r["coverage"][key] = bad
                self.fails_with(r, code)
        r = self.mutated(); r["coverage"]["renormalised"] = True
        self.fails_with(r, "renormalised_flag")
        r = self.mutated(); r["scenario"]["is_forecast"] = True
        self.fails_with(r, "hypothetical_flags")
        r = self.mutated(); r["disclaimer"] = "Looks great."
        self.fails_with(r, "disclaimer_missing")

    def test_renormalised_figures_are_detected(self):
        r = self.mutated(self.balanced)
        scale = 100.0 / r["coverage"]["equity_exposure_pct"]
        for row in r["sector_impacts"]:
            row["exposure_pct"] = round(row["exposure_pct"] * scale, 6)
            row["contribution_pp"] = round(row["contribution_pp"] * scale, 6)
        r["estimated_portfolio_impact_pct"] = round(r["estimated_portfolio_impact_pct"] * scale, 6)
        self.fails_with(r, "exposure_mismatch", "total_mismatch")

    def test_currency_impact_and_identity_checks(self):
        r = self.mutated(); r["estimated_value_impact"]["amount"] = -1.0
        self.fails_with(r, "value_impact_mismatch")
        r = self.mutated(); r["estimated_value_impact"]["aum"] = 1.0
        self.fails_with(r, "value_impact_mismatch")
        r = self.mutated(); r["estimated_value_impact"] = None
        self.fails_with(r, "value_impact_mismatch")
        self.fails_with({**self.mutated(), "portfolio_name": "Growth Equity Fund"}, "portfolio_name_mismatch")
        self.fails_with({**self.mutated(), "status": "no_equity_holdings"}, "status_mismatch")
        v = self.verifier.verify({**self.mutated(), "portfolio_id": 9999})
        self.assertEqual((v.status, v.mismatches[0].code), (FAILED, "portfolio_not_found"))

    def test_tolerance_boundaries(self):
        r = self.mutated()
        r["sector_impacts"][0]["contribution_pp"] += 5e-5
        r["estimated_portfolio_impact_pct"] += 5e-5
        self.assertEqual(self.verifier.verify(r).status, VERIFIED)
        r = self.mutated(); r["sector_impacts"][0]["contribution_pp"] += 5e-4
        self.fails_with(r, "contribution_mismatch")

    def test_malformed_results_fail_cleanly(self):
        self.assertEqual(self.verifier.verify("x").status, FAILED)
        self.assertEqual(self.verifier.verify({"status": "success", "portfolio_id": "7"}).status, FAILED)
        for mutate in (lambda r: r.__setitem__("sector_impacts", "oops"), lambda r: r.__setitem__("scenario", None), lambda r: r.__setitem__("coverage", None),
                       lambda r: r["sector_impacts"][0].__setitem__("shock_pct", float("nan")), lambda r: r["scenario"].__setitem__("market_shock_pct", 500)):
            r = self.mutated(); mutate(r)
            self.assertEqual(self.verifier.verify(r).status, FAILED)

    def test_an_engine_with_a_wrong_unit_is_caught_because_nothing_is_shared(self):
        class BuggyTool(ScenarioTool):
            def _compute(self, *args, **kwargs):
                r = super()._compute(*args, **kwargs)
                for row in r["sector_impacts"]:
                    row["contribution_pp"] = round(row["contribution_pp"] * 100, 6)   # percent-vs-fraction unit bug
                r["estimated_portfolio_impact_pct"] = round(r["estimated_portfolio_impact_pct"] * 100, 6)
                return r

        out = BuggyTool(self.db, self.calc, ScenarioVerifier(self.db)).run("International Equity Fund", "technology_sell_off")
        self.assertEqual(out["status"], "error")
        self.assertEqual(out["verification"]["status"], FAILED)
        self.assertEqual(out["sector_impacts"], [])

    def test_the_verifier_shares_no_code_with_the_engine_and_uses_plain_reads(self):
        tree = ast.parse((PROJECT_ROOT / "portfolio_agent" / "scenario_verifier.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported |= {node.module or ""} | {a.name for a in node.names}
            elif isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
        for forbidden in ("scenario_tool", "ScenarioTool", "tools", "ExposureCalculator", "genai", "google", "sql_generator", "decimal", "Decimal"):
            self.assertNotIn(forbidden, imported)
        self.assertIn("fractions", imported)
        issued = []
        real = self.db.execute_read_only
        with patch.object(self.db, "execute_read_only", side_effect=lambda s, p=None: (issued.append(s), real(s, p))[1]):
            ScenarioVerifier(self.db).verify(self.good)
        self.assertEqual(set(issued), {sv.SQL_PORTFOLIO, sv.SQL_HOLDINGS, sv.SQL_SECURITIES, sv.SQL_SECTORS})
        import re
        for sql in issued:
            self.assertIsNone(re.search(r"\b(JOIN|GROUP\s+BY|SUM|COUNT|AVG)\b", sql, re.I), sql)

    def test_no_model_and_no_calculation_in_the_engine_either(self):
        source = (PROJECT_ROOT / "portfolio_agent" / "scenario_tool.py").read_text(encoding="utf-8")
        for token in ("generate_content", "genai", "GeminiSQLGenerator"):
            self.assertNotIn(token, source)


# ======================================================================================
# Safety and withholding
# ======================================================================================
class TestSafetyAndWithholding(RealDbCase):
    def verifier_only_fault(self):
        real = self.db.execute_read_only

        def flaky(sql, params=None):
            if sql in (sv.SQL_PORTFOLIO, sv.SQL_HOLDINGS, sv.SQL_SECURITIES, sv.SQL_SECTORS):
                raise ReadOnlyQueryError("simulated database failure during verification")
            return real(sql, params)

        return patch.object(self.db, "execute_read_only", side_effect=flaky)

    def test_failed_verification_withholds_the_figures(self):
        good = self.tool.run("International Equity Fund", "technology_sell_off")
        bad = copy.deepcopy(good); bad.pop("verification"); bad["sector_impacts"][1]["contribution_pp"] = -1.0
        out = apply_scenario_verification(bad, self.verifier.verify(bad))
        self.assertEqual((out["status"], out["sector_impacts"], out["starting_exposure"], out["estimated_portfolio_impact_pct"], out["estimated_value_impact"], out["coverage"]),
                         ("error", [], [], None, None, None))
        self.assertIn("failed independent verification", out["error"])
        self.assertEqual(out["verification"]["status"], FAILED)
        self.assertEqual(out["verification"]["rejected_result"]["sector_impacts"][1]["contribution_pp"], -1.0)
        self.assertNotIn("figures_digest", out["verification"])

    def test_unverified_when_the_database_fails_during_verification(self):
        with self.verifier_only_fault():
            r = self.tool.run("International Equity Fund", "technology_sell_off")
        self.assertEqual((r["status"], r["sector_impacts"], r["estimated_portfolio_impact_pct"]), ("error", [], None))
        self.assertEqual(r["verification"]["status"], UNVERIFIED)
        self.assertEqual(r["verification"]["mismatches"], [])
        self.assertIn("could not be completed", r["error"])
        self.assertNotIn("failed independent verification", r["error"])
        self.assertEqual(len(r["verification"]["rejected_result"]["sector_impacts"]), 4)

    def test_no_verifier_means_withheld_not_unchecked(self):
        r = ScenarioTool(self.db, self.calc, None).run("International Equity Fund", "technology_sell_off")
        self.assertEqual((r["status"], r["sector_impacts"]), ("error", []))
        self.assertEqual(r["verification"]["status"], UNVERIFIED)
        self.assertIn("disabled or unavailable", r["error"])
        self.assertEqual(unavailable_scenario_verification({"status": "error"}).status, NOT_APPLICABLE)

    def test_verified_results_carry_a_digest_of_exactly_the_verified_figures(self):
        r = self.tool.run("International Equity Fund", "technology_sell_off")
        self.assertEqual(r["verification"]["figures_digest"], scenario_digest(r))
        changed = copy.deepcopy(r); changed["sector_impacts"][0]["contribution_pp"] = 0.5
        self.assertNotEqual(scenario_digest(changed), r["verification"]["figures_digest"])

    def test_sqlglot_and_read_only_protections_still_apply(self):
        before = self.db.execute_read_only("SELECT COUNT(*) AS n FROM holdings")["rows"][0]["n"]
        with patch.object(sql_ast, "sqlglot", None):
            r = self.tool.run("International Equity Fund", "technology_sell_off")
        self.assertEqual((r["status"], r["error_type"], r["sector_impacts"]), ("error", "database_error", []))   # fail closed: nothing ran
        issued = []
        real = self.db.execute_read_only
        with patch.object(self.db, "execute_read_only", side_effect=lambda s, p=None: (issued.append(s), real(s, p))[1]):
            self.tool.run("International Equity Fund", "technology_sell_off")
        self.assertTrue(issued)
        self.assertTrue(all(s.lstrip().upper().startswith("SELECT") for s in issued))
        self.assertEqual(self.db.execute_read_only("SELECT COUNT(*) AS n FROM holdings")["rows"][0]["n"], before)
        for attack in ("DROP TABLE holdings", "SELECT * FROM sqlite_master"):
            with self.assertRaises(ReadOnlyQueryError):
                self.db.execute_read_only(attack)

    def test_injection_text_in_names_cannot_reach_sql(self):
        before = self.db.execute_read_only("SELECT COUNT(*) AS n FROM portfolios")["rows"][0]["n"]
        for name in ("International Equity Fund'; DROP TABLE portfolios; --", "x' OR '1'='1"):
            r = self.tool.run(name, "broad_market_downturn")
            self.assertEqual(r["status"], "error")
        r = self.tool.run("International Equity Fund", sector_shocks={"Technology'; DROP TABLE holdings; --": -5})
        self.assertEqual(r["error_type"], "invalid_scenario")
        self.assertEqual(self.db.execute_read_only("SELECT COUNT(*) AS n FROM portfolios")["rows"][0]["n"], before)


# ======================================================================================
# Agent routing, CLI, API
# ======================================================================================
class TestAgentRouting(RealDbCase):
    def agent(self, **kw):
        kw.setdefault("sql_tool", SQLQueryTool(db=self.db, use_gemini=False))
        return PortfolioAgent(db=self.db, exposure_tool=self.calc, intent_classifier=None, **kw)

    def test_the_specification_questions_route_to_the_scenario_tool(self):
        agent = self.agent()
        r = agent.answer_question("Stress test the International Equity Fund under a 20% market decline.")
        self.assertEqual((r["selected_tool"], r["status"]), ("scenario_analysis", "success"))
        self.assertEqual(r["result"]["scenario"]["market_shock_pct"], -20.0)
        self.assertEqual(r["result"]["estimated_portfolio_impact_pct"], -20.0)
        self.assertEqual(r["result"]["verification"]["status"], VERIFIED)
        self.assertEqual(r["error"], None)
        r = agent.answer_question("Run a severe market stress scenario for the Growth Equity Fund.")
        self.assertEqual((r["selected_tool"], r["result"]["scenario"]["id"], r["result"]["portfolio_name"]), ("scenario_analysis", "severe_market_stress", "Growth Equity Fund"))
        r = agent.answer_question("What happens to the Tech Innovation Fund if Technology falls 25%?")
        self.assertEqual((r["selected_tool"], r["result"]["estimated_portfolio_impact_pct"]), ("scenario_analysis", -25.0))
        self.assertEqual(r["result"]["scenario"]["sector_shocks"], {"Technology": -25.0})

    def test_the_sector_question_without_a_portfolio_asks_for_one(self):
        r = self.agent().answer_question("What happens to the Technology exposure if that sector falls 25%?")
        self.assertEqual((r["status"], r["selected_tool"], r["result"]), ("error", "scenario_analysis", None))
        self.assertIn("name the portfolio", r["error"])

    def test_a_scenario_without_a_shock_asks_for_one(self):
        r = self.agent().answer_question("Stress test the International Equity Fund")
        self.assertEqual(r["status"], "error")
        self.assertIn("state the scenario", r["error"])

    def test_wording_variants_and_signs(self):
        agent = self.agent()
        cases = [
            ("What if energy rises 15% for the International Equity Fund?", {"Energy": 15.0}, None),
            ("What happens to the Balanced Portfolio if the market falls 12.5%?", {}, -12.5),
            ("Simulate a 30% drop in Technology for the Tech Innovation Fund", {"Technology": -30.0}, None),
        ]
        for q, sectors, market in cases:
            with self.subTest(q):
                r = agent.answer_question(q)
                self.assertEqual(r["selected_tool"], "scenario_analysis")
                self.assertEqual(r["result"]["scenario"]["sector_shocks"], sectors)
                if market is not None:
                    self.assertEqual(r["result"]["scenario"]["market_shock_pct"], market)

    def test_invalid_shocks_in_a_question_are_rejected_not_clamped(self):
        r = self.agent().answer_question("Stress test the International Equity Fund under a 250% market decline")
        self.assertEqual((r["status"], r["result"]["error_type"]), ("error", "invalid_scenario"))
        self.assertIn("between -100%", r["error"])

    def test_descriptive_and_historical_questions_are_not_routed_to_scenarios(self):
        for q in ("What was the maximum drawdown of the Growth Equity Fund last year?", "Show the performance of the Balanced Portfolio",
                  "What is the volatility of the Growth Equity Fund?", "What was the market downturn return in 2023?", "How did Technology perform historically?",
                  "Show me the top 5 holdings by cost basis in the Growth Equity Fund", "How many portfolios do we have in total?", "Which securities are in the Technology sector?",
                  "What are the sector exposures for the Tech Innovation Fund?", "Show the holdings and sector exposure breakdown for the International Equity Fund"):
            with self.subTest(q):
                self.assertFalse(is_scenario_question(q), q)
        agent = self.agent()
        self.assertEqual(agent.answer_question("How many portfolios do we have in total?")["selected_tool"], "sql_query")
        self.assertEqual(agent.answer_question("What are the sector exposures for the Tech Innovation Fund?")["selected_tool"], "exposure_calculator")
        self.assertEqual(agent.answer_question("Show the holdings and sector exposure breakdown for the International Equity Fund")["selected_tool"], "hybrid_analysis")

    def test_official_questions_are_never_scenario_questions(self):
        for q in json.loads((PROJECT_ROOT / "ground_truth_dataset.json").read_text(encoding="utf-8"))["questions"]:
            self.assertFalse(is_scenario_question(q["question"]), q["question"])

    def test_parsing(self):
        names = [r["sector_name"] for r in self.db.execute_read_only("SELECT sector_name FROM sectors")["rows"]]
        self.assertEqual(parse_scenario_request("Run an energy shock for X", names), {"scenario_id": "energy_shock"})
        self.assertEqual(parse_scenario_request("technology sell-off please", names), {"scenario_id": "technology_sell_off"})
        self.assertEqual(parse_scenario_request("a 20% market decline", names), {"market_shock_pct": -20.0})
        self.assertEqual(parse_scenario_request("if Healthcare drops by 8%", names), {"sector_shocks": {"Healthcare": -8.0}})
        self.assertEqual(parse_scenario_request("if tech rallies 10%", names), {"sector_shocks": {"Technology": 10.0}})
        self.assertEqual(parse_scenario_request("hello", names), {})

    def test_no_gemini_call_is_ever_made(self):
        client = MagicMock()
        tool = SQLQueryTool(db=self.db, gemini_generator=GeminiSQLGenerator(api_key="unit-test-placeholder", client=client, db=self.db), use_gemini=True)
        with patch.object(GeminiSQLGenerator, "is_configured", True):
            r = self.agent(sql_tool=tool).answer_question("Stress test the International Equity Fund under a 20% market decline.")
        self.assertEqual(r["selected_tool"], "scenario_analysis")
        client.models.generate_content.assert_not_called()

    def test_failed_and_unverified_verification_withhold_via_the_agent(self):
        r = self.agent(scenario_verifier=None).answer_question("Stress test the International Equity Fund under a 20% market decline.")
        self.assertEqual((r["status"], r["result"]["sector_impacts"], r["result"]["verification"]["status"]), ("error", [], UNVERIFIED))
        self.assertIn("withheld", r["error"])
        broken = MagicMock()
        bad = copy.deepcopy(self.tool.run("International Equity Fund", market_shock_pct=-20)); bad.pop("verification")
        broken.verify.return_value = self.verifier.verify({**bad, "estimated_portfolio_impact_pct": -1.0})
        r = self.agent(scenario_verifier=broken).answer_question("Stress test the International Equity Fund under a 20% market decline.")
        self.assertEqual((r["status"], r["result"]["verification"]["status"], r["result"]["sector_impacts"]), ("error", FAILED, []))

    def test_pretty_cli(self):
        out = cli._format_pretty(self.agent().answer_question("Stress test the International Equity Fund under a 20% market decline."))
        for needle in ("Tool   : scenario_analysis", "HYPOTHETICAL, not a forecast", "Verification: VERIFIED", "-7.6000", "Estimated portfolio impact: -20.0000", "-5,600,000.00 USD", "not renormalised", "not a forecast or prediction"):
            self.assertIn(needle, out)
        withheld = cli._format_pretty(self.agent(scenario_verifier=None).answer_question("Stress test the International Equity Fund under a 20% market decline."))
        self.assertIn("Verification: UNVERIFIED", withheld)
        self.assertNotIn("-7.6000", withheld)
        self.assertNotIn("Estimated portfolio impact", withheld)

    def test_existing_response_keys_are_unchanged(self):
        r = self.agent().answer_question("Stress test the International Equity Fund under a 20% market decline.")
        self.assertEqual(set(r), {"status", "question", "selected_tool", "route_reason", "result", "error"})


class TestApi(RealDbCase):
    def setUp(self):
        try:
            from fastapi.testclient import TestClient
            from portfolio_api.app import create_app
            from portfolio_api.service import PortfolioService
        except ImportError:
            self.skipTest("FastAPI adapter dependencies not installed")
        self.service = PortfolioService(use_llm=False, use_intent_classifier=False)
        self.client = TestClient(create_app(self.service), base_url="http://127.0.0.1:8000")

    def test_options_endpoint(self):
        body = self.client.get("/api/scenarios").json()
        self.assertEqual([s["id"] for s in body["scenarios"]], list(SCENARIOS))
        self.assertIn("Technology", body["sectors"])
        self.assertEqual(body["shock_limits_pct"], {"min": -100.0, "max": 100.0})

    def test_scenario_endpoint_contract(self):
        body = self.client.post("/api/scenario", json={"portfolio_name": "International Equity Fund", "scenario_id": "technology_sell_off"}).json()
        self.assertEqual((body["status"], body["tool"], body["estimated_portfolio_impact_pct"], body["verification"]["status"]), ("success", "scenario_analysis", -9.0, "verified"))
        custom = self.client.post("/api/scenario", json={"portfolio_name": "Balanced Portfolio", "market_shock_pct": -10, "sector_shocks": {"Technology": -25}}).json()
        self.assertEqual(custom["verification"]["status"], "verified")
        self.assertEqual(custom["scenario"]["type"], "custom")
        invalid = self.client.post("/api/scenario", json={"portfolio_name": "International Equity Fund", "market_shock_pct": -150})
        self.assertEqual(invalid.status_code, 200)
        self.assertEqual((invalid.json()["status"], invalid.json()["error_type"]), ("error", "invalid_scenario"))
        unknown = self.client.post("/api/scenario", json={"portfolio_name": "Atlantis", "scenario_id": "energy_shock"}).json()
        self.assertEqual(unknown["error_type"], "invalid_portfolio")

    def test_malformed_requests_are_rejected(self):
        for body in ({}, {"portfolio_name": ""}, {"portfolio_name": "x", "market_shock_pct": "abc"}, {"portfolio_name": "x", "sector_shocks": [1]},
                     {"portfolio_name": "x", "sector_shocks": {f"s{i}": 1 for i in range(30)}}):
            self.assertEqual(self.client.post("/api/scenario", json=body).status_code, 422, body)
        nan = self.client.post("/api/scenario", content='{"portfolio_name": "International Equity Fund", "market_shock_pct": NaN}', headers={"Content-Type": "application/json"})
        self.assertEqual(nan.status_code, 422)

    def test_security_headers_and_no_public_access_are_unchanged(self):
        r = self.client.post("/api/scenario", json={"portfolio_name": "International Equity Fund", "scenario_id": "energy_shock"})
        self.assertEqual(r.headers.get("cache-control"), "no-store")
        self.assertNotIn("access-control-allow-origin", {k.lower() for k in r.headers})
        from fastapi.testclient import TestClient
        from portfolio_api.app import create_app

        foreign = TestClient(create_app(self.service), base_url="http://evil.example")
        self.assertEqual(foreign.post("/api/scenario", json={"portfolio_name": "x", "scenario_id": "energy_shock"}).status_code, 400)

    def test_ask_endpoint_routes_scenarios_and_leaves_other_questions_alone(self):
        body = self.client.post("/api/ask", json={"question": "Stress test the International Equity Fund under a 20% market decline."}).json()
        self.assertEqual((body["selected_tool"], body["result"]["verification"]["status"]), ("scenario_analysis", "verified"))
        self.assertEqual(self.client.post("/api/ask", json={"question": "How many portfolios do we have in total?"}).json()["selected_tool"], "sql_query")

    def test_service_withholds_when_verification_fails(self):
        bad = copy.deepcopy(self.tool.run("International Equity Fund", market_shock_pct=-20)); bad.pop("verification"); bad["sector_impacts"][0]["contribution_pp"] = 1.0
        with patch.object(ScenarioTool, "_compute", return_value=bad):
            out = self.service.scenario("International Equity Fund", None, -20, None)
        self.assertEqual((out["status"], out["sector_impacts"], out["verification"]["status"]), ("error", [], FAILED))

    def test_nothing_is_written(self):
        before = self.db.execute_read_only("SELECT COUNT(*) AS n FROM holdings")["rows"][0]["n"]
        self.client.post("/api/scenario", json={"portfolio_name": "International Equity Fund", "scenario_id": "severe_market_stress"})
        self.assertEqual(self.db.execute_read_only("SELECT COUNT(*) AS n FROM holdings")["rows"][0]["n"], before)


if __name__ == "__main__":
    unittest.main()
