"""
Independent verification of scenario / stress-test results.

``ScenarioTool`` computes a scenario from the exposure calculator's output with ``decimal.Decimal``. This module
recomputes the same result by a different route and compares the two, so an error in the engine (a wrong formula,
a dropped sector, a mis-applied shock, renormalisation, a wrong unit) is caught rather than shown.

Independence
------------
* It never imports or calls ``ScenarioTool``, ``ExposureCalculator`` or any model (a test enforces this).
* It reads raw rows with plain single-table SELECTs (no JOIN / GROUP BY / aggregate) through
  ``PortfolioDatabase.execute_read_only`` (SQLGlot validation, read-only connection, authorizer). It cannot write.
* Joining, filtering and arithmetic happen in Python with exact ``fractions.Fraction`` (the engine uses ``Decimal``).
* The four predefined scenarios are defined again here, from the specification, so a wrong value in the engine's
  table is detected instead of being trusted.

What is checked: portfolio identity; the shock definition (predefined scenarios against the independent definition,
all shocks within [-100, +100]); the sector set; each sector's starting weight and exposure; each applied shock; each
contribution (weight x shock); the total estimated impact; coverage (equity, shocked, unshocked, unmapped, bond,
accounted and unaccounted weight, counts); shocks on unheld sectors; the AUM and the currency impact; internal
arithmetic consistency of the reported numbers; and the hypothetical / not-a-forecast / not-renormalised flags.

Tolerance: 1e-4 percentage points per contribution (the project's exposure tolerance); the total allows that per
sector; the currency amount allows the equivalent of the total tolerance plus one cent.

Outcomes: ``verified`` | ``failed`` (disproved: figures withheld) | ``unverified`` (could not be checked, or no verifier:
figures withheld) | ``not_applicable`` (the scenario itself errored).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from fractions import Fraction
from typing import Any, Dict, List, Optional

from .financial_verifier import FAILED, NOT_APPLICABLE, UNVERIFIED, VERIFIED, Mismatch, VerificationResult

logger = logging.getLogger(__name__)

SCENARIO_TOL_PP = 1e-4
MAX_MISMATCHES = 25
SHOCK_LIMIT = Fraction(100)
METHOD = (
    "Independent recomputation in Python (exact fractions) from raw holdings, securities, sectors and portfolios rows "
    "read with single-table read-only queries; no scenario-engine or calculator code is shared."
)
UNAVAILABLE_MESSAGE = "Independent scenario verification is disabled or unavailable."

SQL_PORTFOLIO = "SELECT portfolio_id, portfolio_name, total_aum FROM portfolios WHERE portfolio_id = ?"
SQL_HOLDINGS = "SELECT holding_id, security_id, current_weight FROM holdings WHERE portfolio_id = ?"
SQL_SECURITIES = "SELECT security_id, asset_type, sector_id FROM securities"
SQL_SECTORS = "SELECT sector_id, sector_name FROM sectors"

# The specification's scenarios, restated independently of the engine: id -> (market shock %, {sector: shock %}).
INDEPENDENT_SCENARIOS = {
    "broad_market_downturn": (-10, {}),
    "severe_market_stress": (-20, {}),
    "technology_sell_off": (0, {"Technology": -25}),
    "energy_shock": (0, {"Energy": -20}),
}


def _frac(x: Any) -> Fraction:
    return Fraction(str(x))


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def scenario_digest(result: Dict[str, Any]) -> str:
    """Fingerprint of exactly the figures a scenario verification covers (no arithmetic)."""
    canonical = {k: result.get(k) for k in ("portfolio_id", "portfolio_name", "status", "estimated_portfolio_impact_pct", "estimated_value_impact")}
    canonical["scenario"] = result.get("scenario")
    canonical["sector_impacts"] = result.get("sector_impacts")
    canonical["starting_exposure"] = result.get("starting_exposure")
    canonical["unheld"] = result.get("unheld_sector_shocks")
    canonical["coverage"] = result.get("coverage")
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, default=str).encode("utf-8")).hexdigest()


class ScenarioVerifier:
    """Recomputes a scenario result from raw rows and compares it with the engine's output."""

    def __init__(self, db: Any) -> None:
        self.db = db

    # ------------------------------------------------------------------ independent recomputation

    def _read(self, portfolio_id: int) -> Dict[str, Any]:
        q = self.db.execute_read_only
        rows = q(SQL_PORTFOLIO, (int(portfolio_id),))["rows"]
        if not rows:
            raise LookupError(f"Portfolio id {portfolio_id} does not exist.")
        holdings = q(SQL_HOLDINGS, (int(portfolio_id),))["rows"]
        securities = {r["security_id"]: r for r in q(SQL_SECURITIES)["rows"]}
        sectors = {r["sector_id"]: r["sector_name"] for r in q(SQL_SECTORS)["rows"]}

        by_sector: Dict[str, List[Fraction]] = {}
        counts: Dict[str, int] = {}
        equity = unmapped = bonds = 0
        unmapped_w: List[Fraction] = []
        bond_w: List[Fraction] = []
        for h in holdings:
            sec = securities.get(h["security_id"])
            if sec is None:
                continue
            w = _frac(h["current_weight"]) if h["current_weight"] is not None else None
            if sec["asset_type"] == "Bond":
                bonds += 1
                if w is not None:
                    bond_w.append(w)
            elif sec["asset_type"] == "Stock":
                equity += 1
                if sec["sector_id"] is None:
                    unmapped += 1
                    if w is not None:
                        unmapped_w.append(w)
                elif sec["sector_id"] in sectors:
                    name = sectors[sec["sector_id"]]
                    counts[name] = counts.get(name, 0) + 1
                    by_sector.setdefault(name, [])
                    if w is not None:
                        by_sector[name].append(w)
        return {
            "name": rows[0]["portfolio_name"], "aum": rows[0]["total_aum"], "sector_names": set(sectors.values()),
            "weights": {s: sum(v, Fraction(0)) for s, v in by_sector.items()}, "counts": counts,
            "equity": equity, "unmapped": unmapped, "bonds": bonds,
            "unmapped_w": sum(unmapped_w, Fraction(0)), "bond_w": sum(bond_w, Fraction(0)), "total_holdings": len(holdings),
        }

    # ------------------------------------------------------------------ public

    def verify(self, result: Any) -> VerificationResult:
        """Never raises. Database problems give ``unverified``; a result that is already an error is ``not_applicable``."""
        if not isinstance(result, dict):
            return VerificationResult(FAILED, 1, [Mismatch("malformed_result", "The scenario result is not a dictionary.")])
        if result.get("status") not in ("success", "no_equity_holdings"):
            return VerificationResult(NOT_APPLICABLE, error="Nothing to verify: the scenario did not produce a result.")
        pid = result.get("portfolio_id")
        if isinstance(pid, bool) or not isinstance(pid, int):
            return VerificationResult(FAILED, 1, [Mismatch("malformed_result", "The result has no integer portfolio_id.", field="portfolio_id", actual=pid)])
        try:
            truth = self._read(pid)
        except LookupError as exc:
            return VerificationResult(FAILED, 1, [Mismatch("portfolio_not_found", str(exc), field="portfolio_id", actual=pid)])
        except Exception as exc:
            logger.error("Scenario verification could not read the database: %s: %s", type(exc).__name__, exc)
            return VerificationResult(UNVERIFIED, 0, error=f"Verification could not be completed ({type(exc).__name__}).")
        try:
            mismatches: List[Mismatch] = []
            checks = self._compare(result, truth, mismatches)
        except Exception as exc:  # a malformed result must fail cleanly, not crash
            return VerificationResult(FAILED, 1, [Mismatch("malformed_result", f"The result could not be checked ({type(exc).__name__}).")])
        return VerificationResult(FAILED if mismatches else VERIFIED, checks, mismatches)

    # ------------------------------------------------------------------ comparison

    def _compare(self, r: Dict[str, Any], t: Dict[str, Any], out: List[Mismatch]) -> int:
        checks = 0

        def check(ok: bool, code: str, message: str, **kw: Any) -> None:
            nonlocal checks
            checks += 1
            if not ok:
                out.append(Mismatch(code, message, **kw))

        def near(actual: Any, expected: Fraction, tol: float = SCENARIO_TOL_PP) -> bool:
            return _num(actual) and abs(float(_frac(actual) - expected)) <= tol

        check(r.get("portfolio_name") == t["name"], "portfolio_name_mismatch", f"Portfolio name is {r.get('portfolio_name')!r} but portfolio {r.get('portfolio_id')} is {t['name']!r}.",
              field="portfolio_name", expected=t["name"], actual=r.get("portfolio_name"))
        expected_status = "no_equity_holdings" if t["equity"] == 0 else "success"
        check(r.get("status") == expected_status, "status_mismatch", f"Status is {r.get('status')!r} but the data implies {expected_status!r}.", field="status", expected=expected_status, actual=r.get("status"))

        sc = r.get("scenario") if isinstance(r.get("scenario"), dict) else {}
        check(sc.get("is_hypothetical") is True and sc.get("is_forecast") is False, "hypothetical_flags", "The scenario must be flagged hypothetical and not a forecast.")
        check(isinstance(r.get("disclaimer"), str) and "hypothetical" in r["disclaimer"].lower() and "not a forecast" in r["disclaimer"].lower(), "disclaimer_missing",
              "The hypothetical-scenario disclaimer is missing.")
        cov = r.get("coverage") if isinstance(r.get("coverage"), dict) else {}
        check(cov.get("renormalised") is False, "renormalised_flag", "Weights must not be renormalised.")

        # --- shock definition
        market_ok = _num(sc.get("market_shock_pct")) and abs(_frac(sc["market_shock_pct"])) <= SHOCK_LIMIT
        shocks_in = sc.get("sector_shocks") if isinstance(sc.get("sector_shocks"), dict) else None
        check(market_ok and shocks_in is not None and all(_num(v) and abs(_frac(v)) <= SHOCK_LIMIT for v in shocks_in.values()), "shock_out_of_range",
              "Shocks must be finite numbers within -100% and +100%.")
        if not (market_ok and shocks_in is not None):
            return checks
        market = _frac(sc["market_shock_pct"])
        shocks = {k: _frac(v) for k, v in shocks_in.items() if _num(v)}
        check(all(k in t["sector_names"] for k in shocks), "unknown_shock_sector", "A shock names a sector that does not exist.")
        scenario_id = sc.get("id")
        if scenario_id in INDEPENDENT_SCENARIOS:
            want_market, want_sectors = INDEPENDENT_SCENARIOS[scenario_id]
            check(market == want_market and shocks == {k: Fraction(v) for k, v in want_sectors.items()}, "scenario_definition_mismatch",
                  f"Scenario {scenario_id!r} should apply market {want_market}% and sector shocks {want_sectors}.", field="scenario",
                  expected={"market_shock_pct": want_market, "sector_shocks": want_sectors}, actual={"market_shock_pct": sc.get("market_shock_pct"), "sector_shocks": shocks_in})
            market, shocks = Fraction(want_market), {k: Fraction(v) for k, v in want_sectors.items()}   # recompute with the trusted definition
        else:
            check(scenario_id == "custom", "scenario_id_unknown", f"Scenario id {scenario_id!r} is neither predefined nor custom.")

        # --- sectors, starting exposure, contributions
        impacts, starting = r.get("sector_impacts"), r.get("starting_exposure")
        if not (isinstance(impacts, list) and isinstance(starting, list) and all(isinstance(x, dict) for x in impacts + starting)):
            check(False, "malformed_result", "sector_impacts and starting_exposure must be lists of objects.")
            return checks
        weights = t["weights"]
        for name, rows in (("sector_impacts", impacts), ("starting_exposure", starting)):
            seen = [x.get("sector") for x in rows]
            check(len(seen) == len(set(seen)), "sector_duplicate", f"A sector appears more than once in {name}.", field=name)
            check(set(seen) == set(weights), "sector_set_mismatch", f"{name} covers {sorted(map(str, set(seen)))} but the portfolio holds {sorted(weights)}.",
                  field=name, expected=sorted(weights), actual=sorted(map(str, set(seen))))
        by_impact = {x.get("sector"): x for x in impacts}
        by_start = {x.get("sector"): x for x in starting}
        expected_total = Fraction(0)
        expected_shocked = Fraction(0)
        for sector, w in weights.items():
            shock = shocks.get(sector, market)
            contribution = w * shock
            expected_total += contribution
            if shock != 0:
                expected_shocked += w
            row, start = by_impact.get(sector, {}), by_start.get(sector, {})
            check(near(start.get("exposure_pct"), w * 100), "exposure_mismatch", f"{sector}: starting exposure {start.get('exposure_pct')!r}% but the data gives {float(w * 100):.4f}%.",
                  field="exposure_pct", sector=sector, expected=float(w * 100), actual=start.get("exposure_pct"))
            check(near(row.get("exposure_pct"), w * 100), "exposure_mismatch", f"{sector}: exposure in the impact row is {row.get('exposure_pct')!r}% but the data gives {float(w * 100):.4f}%.",
                  field="exposure_pct", sector=sector, expected=float(w * 100), actual=row.get("exposure_pct"))
            check(near(row.get("shock_pct"), shock), "shock_mismatch", f"{sector}: applied shock {row.get('shock_pct')!r}% but the scenario implies {float(shock):g}%.",
                  field="shock_pct", sector=sector, expected=float(shock), actual=row.get("shock_pct"))
            check(near(row.get("contribution_pp"), contribution), "contribution_mismatch",
                  f"{sector}: contribution {row.get('contribution_pp')!r} pp but weight x shock gives {float(contribution):.6f} pp.",
                  field="contribution_pp", sector=sector, expected=float(contribution), actual=row.get("contribution_pp"))
            if _num(row.get("exposure_pct")) and _num(row.get("shock_pct")) and _num(row.get("contribution_pp")):
                check(abs(float(_frac(row["exposure_pct"]) * _frac(row["shock_pct"]) / 100 - _frac(row["contribution_pp"]))) <= SCENARIO_TOL_PP, "contribution_inconsistent",
                      f"{sector}: the reported contribution is not exposure x shock / 100.", field="contribution_pp", sector=sector)
            check(row.get("shocked") == (shock != 0), "shocked_flag_mismatch", f"{sector}: the 'shocked' flag disagrees with the applied shock.", sector=sector)

        # --- total
        tol_total = SCENARIO_TOL_PP * max(1, len(weights))
        total = r.get("estimated_portfolio_impact_pct")
        check(near(total, expected_total, tol_total), "total_mismatch", f"Estimated impact is {total!r} pp but the data gives {float(expected_total):.6f} pp.",
              field="estimated_portfolio_impact_pct", expected=float(expected_total), actual=total)
        reported_sum = sum((_frac(x["contribution_pp"]) for x in impacts if _num(x.get("contribution_pp"))), Fraction(0))
        check(near(total, reported_sum, tol_total), "total_inconsistent", f"Estimated impact {total!r} pp is not the sum of the reported contributions ({float(reported_sum):.6f}).",
              field="estimated_portfolio_impact_pct", expected=float(reported_sum), actual=total)

        # --- unheld shocks
        unheld = {x.get("sector"): x.get("shock_pct") for x in (r.get("unheld_sector_shocks") or []) if isinstance(x, dict)}
        want_unheld = {k: v for k, v in shocks.items() if k not in weights}
        check(set(unheld) == set(want_unheld) and all(near(unheld[k], v) for k, v in want_unheld.items()), "unheld_shocks_mismatch",
              f"Shocks on sectors the portfolio does not hold should be {sorted(want_unheld)}.", field="unheld_sector_shocks")

        # --- coverage
        equity_w = sum(weights.values(), Fraction(0))
        accounted = equity_w + t["unmapped_w"] + t["bond_w"]
        for key, expected in (("equity_exposure_pct", equity_w * 100), ("shocked_weight_pct", expected_shocked * 100),
                              ("unshocked_mapped_weight_pct", (equity_w - expected_shocked) * 100), ("unmapped_equity_weight_pct", t["unmapped_w"] * 100),
                              ("excluded_bond_weight_pct", t["bond_w"] * 100), ("recorded_weight_accounted_pct", accounted * 100),
                              ("recorded_weight_unaccounted_pct", 100 - accounted * 100)):
            check(near(cov.get(key), expected, tol_total), "coverage_mismatch", f"coverage.{key} is {cov.get(key)!r} but the data gives {float(expected):.4f}.",
                  field=key, expected=float(expected), actual=cov.get(key))
        for key, expected_n in (("equity_holdings_count", t["equity"]), ("unmapped_equity_holdings", t["unmapped"]), ("excluded_bond_holdings", t["bonds"])):
            actual = cov.get(key)
            check(isinstance(actual, int) and not isinstance(actual, bool) and actual == expected_n, "count_mismatch", f"coverage.{key} is {actual!r}, expected {expected_n}.",
                  field=key, expected=expected_n, actual=actual)

        # --- currency impact
        aum = t["aum"]
        value = r.get("estimated_value_impact")
        if _num(aum) and aum > 0:
            want = _frac(aum) * expected_total / 100
            tol_amount = float(abs(_frac(aum))) * tol_total / 100 + 0.01
            ok = isinstance(value, dict) and near(value.get("aum"), _frac(aum), 1e-6 * float(aum)) and near(value.get("amount"), want, tol_amount) and value.get("currency") == "USD"
            check(ok, "value_impact_mismatch", f"Currency impact {value!r} but AUM {aum} x impact gives {float(want):.2f} USD.", field="estimated_value_impact",
                  expected=float(want), actual=value)
        else:
            check(value is None, "value_impact_unsupported", "A currency impact is shown although no positive AUM is recorded.", field="estimated_value_impact", actual=value)
        return checks


# ======================================================================================
# Response policy
# ======================================================================================
def unavailable_scenario_verification(result: Any) -> VerificationResult:
    if isinstance(result, dict) and result.get("status") in ("success", "no_equity_holdings"):
        return VerificationResult(UNVERIFIED, 0, error=UNAVAILABLE_MESSAGE)
    return VerificationResult(NOT_APPLICABLE, error="Nothing to verify: the scenario did not produce a result.")


def verification_block(v: VerificationResult) -> Dict[str, Any]:
    return {
        "status": v.status, "verified": v.status == VERIFIED, "method": METHOD, "tolerance_pp": SCENARIO_TOL_PP, "checks_run": v.checks_run,
        "mismatches": [m.to_dict() for m in v.mismatches[:MAX_MISMATCHES]], "mismatch_count": len(v.mismatches), "error": v.error,
    }


def apply_scenario_verification(result: Dict[str, Any], v: VerificationResult) -> Dict[str, Any]:
    """
    verified: result plus a ``verification`` block with a digest of the verified figures.
    failed (disproved) / unverified (could not be checked, or no verifier): the figures are withheld: status ``error``,
    tables and impacts cleared, the rejected figures kept only inside ``verification.rejected_result``.
    not applicable (the scenario already errored): returned untouched.
    """
    if v.status == NOT_APPLICABLE:
        return result
    block = verification_block(v)
    if v.status == VERIFIED:
        block["figures_digest"] = scenario_digest(result)
        return {**result, "verification": block}
    block["rejected_result"] = {k: result.get(k) for k in ("sector_impacts", "starting_exposure", "estimated_portfolio_impact_pct", "estimated_value_impact", "coverage")}
    if v.status == FAILED:
        message = "The scenario result failed independent verification and was not accepted: " + "; ".join(m.message for m in v.mismatches[:3])
    else:
        message = ("Independent verification could not be completed, so the scenario figures have been withheld. " + (v.error or "")).strip()
    return {
        **result, "status": "error", "starting_exposure": [], "sector_impacts": [], "unheld_sector_shocks": [],
        "estimated_portfolio_impact_pct": None, "estimated_value_impact": None, "coverage": None, "error": message, "verification": block,
    }
