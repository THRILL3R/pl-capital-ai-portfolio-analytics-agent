"""
Independent financial verification of sector-exposure results.

Purpose
-------
``ExposureCalculator`` computes equity sector exposure with one SQL aggregate. This module recomputes the
same figures by a *different* route and compares the two, so an error in the calculator's query, arithmetic or
filtering is caught instead of being presented as a result.

Why the verifier is independent (it deliberately shares no calculation code with the calculator)
------------------------------------------------------------------------------------------
* It never imports or calls ``ExposureCalculator`` (a test enforces this).
* It reads only **raw rows** with plain single-table SELECTs (``holdings`` of one portfolio, ``securities``,
  ``sectors``, ``portfolios``). There is no JOIN, GROUP BY, SUM or COUNT in SQL (a test enforces this).
* All joining, filtering and summation happen in Python: dictionary look-ups instead of SQL joins and
  ``math.fsum`` instead of SQL ``SUM``, so a SQL join/filter bug or SQLite aggregation rule cannot be
  reproduced by accident.
* It resolves the portfolio from the *id* in the result, not by re-running name resolution.
* Queries go through ``PortfolioDatabase.execute_read_only``: SQLGlot AST validation (mandatory), the
  ``mode=ro`` connection, ``query_only`` and the authorizer all still apply. Nothing here can write.
* It never uses Gemini or any network service.

Conventions verified (identical to the project's definitions; nothing is redefined here)
----------------------------------------------------------------------------------------
* Only ``asset_type = 'Stock'`` holdings contribute; Bonds are excluded.
* exposure_pct(sector) = SUM(current_weight of that sector's stock holdings) * 100. **No renormalisation.**
* A stock whose security has no sector (NULL ``sector_id``) is not in any sector's exposure but still counts as
  an equity holding and as "unmapped". A sector id that matches no ``sectors`` row is dropped like the
  calculator's inner join drops it, and is reported under ``data_issues``.
* A NULL ``current_weight`` adds nothing to a sum but the holding is still counted (SQL SUM / COUNT semantics).
* A stock with weight 0 keeps its sector in the result with 0.0% exposure.

Outcome of a verification (see ``apply_verification`` for how each is presented):
``verified``; ``failed`` (the figures were disproved); ``unverified`` (they could not be checked, e.g. a database
error: the figures are withheld, never shown as a normal result); ``not_applicable`` (the calculation itself errored).

Tolerance is the evaluator's: ``EXPOSURE_TOL_PP`` (1e-4 percentage points) per sector; the total allows that
amount per sector because the calculator rounds each sector before adding them; fractions use 1e-6.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Same values as evaluate.py (EXPOSURE_TOL_PP and ABS_TOL); tests assert they have not drifted apart.
EXPOSURE_TOL_PP = 1e-4
FRACTION_TOL = 1e-6

VERIFIED = "verified"
FAILED = "failed"
UNVERIFIED = "unverified"
NOT_APPLICABLE = "not_applicable"

METHOD_DESCRIPTION = (
    "Independent recomputation in Python from raw holdings, securities, sectors and portfolios rows read with "
    "single-table read-only queries; no calculator code and no SQL aggregation is shared."
)
MAX_MISMATCHES = 25

# The only SQL the verifier issues: plain single-table reads (no JOIN / GROUP BY / aggregate functions).
SQL_PORTFOLIO = "SELECT portfolio_id, portfolio_name FROM portfolios WHERE portfolio_id = ?"
SQL_HOLDINGS = "SELECT holding_id, security_id, current_weight FROM holdings WHERE portfolio_id = ?"
SQL_SECURITIES = "SELECT security_id, asset_type, sector_id FROM securities"
SQL_SECTORS = "SELECT sector_id, sector_name FROM sectors"


@dataclass
class Mismatch:
    code: str
    message: str
    field: Optional[str] = None
    sector: Optional[str] = None
    expected: Any = None
    actual: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class ExpectedExposure:
    """What the data says the calculator should have returned."""

    portfolio_id: int
    portfolio_name: str
    sector_weights: Dict[str, float]      # fraction, exact (fsum)
    sector_counts: Dict[str, int]
    total_holdings: int
    equity_holdings: int
    bond_holdings: int
    unmapped_equity: int
    bond_weight_pct: float
    data_issues: List[str] = field(default_factory=list)

    @property
    def sector_pct(self) -> Dict[str, float]:
        return {s: w * 100.0 for s, w in self.sector_weights.items()}

    @property
    def total_pct(self) -> float:
        return math.fsum(self.sector_pct.values())

    @property
    def status(self) -> str:
        return "no_equity_holdings" if self.equity_holdings == 0 else "success"


@dataclass
class VerificationResult:
    status: str
    checks_run: int = 0
    mismatches: List[Mismatch] = field(default_factory=list)
    error: Optional[str] = None
    data_issues: List[str] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return self.status == VERIFIED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "verified": self.verified,
            "method": METHOD_DESCRIPTION,
            "tolerance_pp": EXPOSURE_TOL_PP,
            "checks_run": self.checks_run,
            "mismatches": [m.to_dict() for m in self.mismatches[:MAX_MISMATCHES]],
            "mismatch_count": len(self.mismatches),
            "data_issues": list(self.data_issues),
            "error": self.error,
        }

    def summary(self) -> str:
        if self.status == FAILED:
            first = "; ".join(m.message for m in self.mismatches[:3])
            more = f" (+{len(self.mismatches) - 3} more)" if len(self.mismatches) > 3 else ""
            return f"{first}{more}"
        return self.error or self.status


def figures_digest(result: Dict[str, Any]) -> str:
    """
    SHA-256 of exactly the figures a verification covers (identity, counts, sectors, totals), in canonical form.

    ``apply_verification`` stores it in a *verified* block; the answer verifier recomputes it over what is about to be
    displayed, so any change between verification and display is detected. It is a fingerprint only: no arithmetic.
    """
    exposures = result.get("exposures")
    canonical = {
        "portfolio_id": result.get("portfolio_id"),
        "portfolio_name": result.get("portfolio_name"),
        "status": result.get("status"),
        "total_equity_weight_pct": result.get("total_equity_weight_pct"),
        "counts": [result.get(k) for k in ("total_holdings_count", "equity_holdings_count", "excluded_bond_holdings", "unmapped_equity_holdings")],
        "exposures": [
            [e.get("sector"), e.get("exposure_pct"), e.get("weight_fraction"), e.get("holding_count")]
            for e in (exposures if isinstance(exposures, list) else []) if isinstance(e, dict)
        ],
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


class FinancialVerifier:
    """Recomputes sector exposure independently and compares it with an ExposureCalculator result."""

    def __init__(self, db: Any) -> None:
        self.db = db

    # ------------------------------------------------------------------ independent calculation

    def compute_expected(self, portfolio_id: int) -> ExpectedExposure:
        """Raw rows -> Python. Raises LookupError for an unknown portfolio; DB errors propagate to the caller."""
        portfolio_rows = self.db.execute_read_only(SQL_PORTFOLIO, (int(portfolio_id),))["rows"]
        if not portfolio_rows:
            raise LookupError(f"Portfolio id {portfolio_id} does not exist.")
        holdings = self.db.execute_read_only(SQL_HOLDINGS, (int(portfolio_id),))["rows"]
        securities = {r["security_id"]: r for r in self.db.execute_read_only(SQL_SECURITIES)["rows"]}
        sectors = {r["sector_id"]: r["sector_name"] for r in self.db.execute_read_only(SQL_SECTORS)["rows"]}

        weights: Dict[str, List[float]] = {}
        counts: Dict[str, int] = {}
        equity = bonds = unmapped = 0
        bond_weights: List[float] = []
        issues: List[str] = []

        for h in holdings:
            sec = securities.get(h["security_id"])
            if sec is None:
                issues.append(f"holding {h['holding_id']} references unknown security {h['security_id']}")
                continue  # neither stock nor bond, exactly like an inner join on securities
            weight = h["current_weight"]
            if sec["asset_type"] == "Bond":
                bonds += 1
                if _is_number(weight):
                    bond_weights.append(float(weight))
                continue
            if sec["asset_type"] != "Stock":
                issues.append(f"security {h['security_id']} has unexpected asset_type {sec['asset_type']!r}")
                continue
            equity += 1
            if sec["sector_id"] is None:
                unmapped += 1
                continue
            sector_name = sectors.get(sec["sector_id"])
            if sector_name is None:
                issues.append(f"security {h['security_id']} references unknown sector {sec['sector_id']}")
                continue  # dropped, like the calculator's inner join on sectors
            counts[sector_name] = counts.get(sector_name, 0) + 1
            bucket = weights.setdefault(sector_name, [])
            if weight is not None:
                bucket.append(float(weight))  # NULL adds nothing to a SUM but the holding is still counted

        return ExpectedExposure(
            portfolio_id=int(portfolio_id),
            portfolio_name=portfolio_rows[0]["portfolio_name"],
            sector_weights={s: math.fsum(v) for s, v in weights.items()},
            sector_counts=counts,
            total_holdings=len(holdings),
            equity_holdings=equity,
            bond_holdings=bonds,
            unmapped_equity=unmapped,
            bond_weight_pct=math.fsum(bond_weights) * 100.0,
            data_issues=issues,
        )

    # ------------------------------------------------------------------ comparison

    def verify(self, result: Any) -> VerificationResult:
        """
        Verify a calculator result. Never raises: database/SQL problems give status ``unverified``; a result that
        is not a successful calculation (status 'error', unknown portfolio) is ``not_applicable``.
        """
        if not isinstance(result, dict):
            return VerificationResult(FAILED, 1, [Mismatch("malformed_result", "The calculator result is not a dictionary.")])
        if result.get("status") not in ("success", "no_equity_holdings"):
            return VerificationResult(NOT_APPLICABLE, error="Nothing to verify: the calculation did not produce a result.")
        pid = result.get("portfolio_id")
        if isinstance(pid, bool) or not isinstance(pid, int):
            return VerificationResult(FAILED, 1, [Mismatch("malformed_result", "The result has no integer portfolio_id.", field="portfolio_id", actual=pid)])
        try:
            expected = self.compute_expected(pid)
        except LookupError as exc:
            return VerificationResult(FAILED, 1, [Mismatch("portfolio_not_found", str(exc), field="portfolio_id", actual=pid)])
        except Exception as exc:  # SQL / database failure: cannot confirm, must not claim "verified"
            logger.error("Financial verification could not read the database: %s: %s", type(exc).__name__, exc)
            return VerificationResult(UNVERIFIED, 0, error=f"Verification could not be completed ({type(exc).__name__}).")

        mismatches: List[Mismatch] = []
        checks = self._compare(result, expected, mismatches)
        status = FAILED if mismatches else VERIFIED
        return VerificationResult(status, checks, mismatches, data_issues=expected.data_issues)

    def _compare(self, result: Dict[str, Any], exp: ExpectedExposure, out: List[Mismatch]) -> int:
        checks = 0

        def check(ok: bool, code: str, message: str, **kw: Any) -> None:
            nonlocal checks
            checks += 1
            if not ok:
                out.append(Mismatch(code, message, **kw))

        check(result.get("portfolio_name") == exp.portfolio_name, "portfolio_name_mismatch",
              f"Portfolio name is {result.get('portfolio_name')!r} but portfolio {exp.portfolio_id} is {exp.portfolio_name!r}.",
              field="portfolio_name", expected=exp.portfolio_name, actual=result.get("portfolio_name"))
        check(result.get("status") == exp.status, "status_mismatch",
              f"Status is {result.get('status')!r} but the data implies {exp.status!r}.", field="status", expected=exp.status, actual=result.get("status"))

        for key, expected_value, code in (
            ("total_holdings_count", exp.total_holdings, "holdings_count_mismatch"),
            ("equity_holdings_count", exp.equity_holdings, "equity_count_mismatch"),
            ("excluded_bond_holdings", exp.bond_holdings, "bond_count_mismatch"),
            ("unmapped_equity_holdings", exp.unmapped_equity, "unmapped_count_mismatch"),
        ):
            actual = result.get(key)
            check(actual == expected_value and not isinstance(actual, bool), code,
                  f"{key} is {actual!r}, expected {expected_value}.", field=key, expected=expected_value, actual=actual)

        raw = result.get("exposures")
        if not isinstance(raw, list) or not all(isinstance(e, dict) for e in raw):
            check(False, "malformed_result", "exposures must be a list of objects.", field="exposures", actual=type(raw).__name__)
            return checks
        actual_by_sector: Dict[str, Dict[str, Any]] = {}
        for entry in raw:
            sector = entry.get("sector")
            check(sector not in actual_by_sector, "duplicate_sector", f"Sector {sector!r} appears more than once.", sector=str(sector))
            actual_by_sector.setdefault(sector, entry)

        expected_pct = exp.sector_pct
        for sector in sorted(set(expected_pct) - set(actual_by_sector)):
            check(False, "missing_sector", f"Sector {sector!r} is missing (expected {expected_pct[sector]:.4f}%).",
                  sector=sector, expected=round(expected_pct[sector], 6))
        for sector in sorted(set(actual_by_sector) - set(expected_pct), key=str):
            check(False, "unexpected_sector",
                  f"Unexpected sector {sector!r} (a bond, unmapped holding or invented sector must not appear).",
                  sector=str(sector), actual=actual_by_sector[sector].get("exposure_pct"))
        checks += 2  # the two set comparisons above

        for sector in sorted(set(expected_pct) & set(actual_by_sector)):
            entry = actual_by_sector[sector]
            pct, frac, n = entry.get("exposure_pct"), entry.get("weight_fraction"), entry.get("holding_count")
            check(_is_number(pct) and abs(float(pct) - expected_pct[sector]) <= EXPOSURE_TOL_PP, "sector_pct_mismatch",
                  f"{sector}: exposure {pct!r}% but the data gives {expected_pct[sector]:.4f}%.",
                  field="exposure_pct", sector=sector, expected=round(expected_pct[sector], 6), actual=pct)
            check(_is_number(frac) and abs(float(frac) - exp.sector_weights[sector]) <= FRACTION_TOL, "sector_weight_mismatch",
                  f"{sector}: weight_fraction {frac!r} but the data gives {exp.sector_weights[sector]:.6f}.",
                  field="weight_fraction", sector=sector, expected=round(exp.sector_weights[sector], 6), actual=frac)
            check(n == exp.sector_counts[sector] and not isinstance(n, bool), "sector_count_mismatch",
                  f"{sector}: holding_count {n!r}, expected {exp.sector_counts[sector]}.",
                  field="holding_count", sector=sector, expected=exp.sector_counts[sector], actual=n)
            if _is_number(pct) and _is_number(frac):  # the two representations must agree with each other
                check(abs(float(pct) - float(frac) * 100.0) <= EXPOSURE_TOL_PP, "sector_inconsistent",
                      f"{sector}: exposure_pct {pct} is not weight_fraction {frac} x 100.", field="exposure_pct", sector=sector)

        total = result.get("total_equity_weight_pct")
        tol_total = EXPOSURE_TOL_PP * max(1, len(expected_pct))
        total_ok = _is_number(total) and abs(float(total) - exp.total_pct) <= tol_total
        check(total_ok, "total_mismatch", f"total_equity_weight_pct is {total!r} but the data gives {exp.total_pct:.4f}%.",
              field="total_equity_weight_pct", expected=round(exp.total_pct, 6), actual=total)
        reported_sum = math.fsum(float(e["exposure_pct"]) for e in raw if _is_number(e.get("exposure_pct")))
        check(_is_number(total) and abs(float(total) - reported_sum) <= tol_total, "total_inconsistent",
              f"total_equity_weight_pct {total!r} does not equal the sum of the reported sectors ({reported_sum:.4f}).",
              field="total_equity_weight_pct", expected=round(reported_sum, 6), actual=total)

        # Diagnoses of the two classic mistakes, added when the numbers already disagree. Both can fit at once (equity
        # plus bonds summing to 100% looks identical to a rescaling), so each is reported independently.
        if not total_ok and _is_number(total):
            if exp.bond_weight_pct > 0 and abs(float(total) - (exp.total_pct + exp.bond_weight_pct)) <= tol_total:
                out.append(Mismatch("bond_inclusion", "The total equals the equity total plus the bond weights: bonds were included.",
                                    field="total_equity_weight_pct", expected=round(exp.total_pct, 6), actual=total))
            if exp.total_pct > 0 and abs(exp.total_pct - 100.0) > tol_total and abs(float(total) - 100.0) <= tol_total:
                out.append(Mismatch("renormalised", "The weights were rescaled to 100% (the project never renormalises).",
                                    field="total_equity_weight_pct", expected=round(exp.total_pct, 6), actual=total))
        return checks


UNAVAILABLE_MESSAGE = "Independent financial verification is disabled or unavailable."


def unavailable_verification(result: Any) -> VerificationResult:
    """
    The outcome to use when no FinancialVerifier is configured. Exposure figures must never be shown without an
    independent check, so a real calculation result is ``unverified`` (and ``apply_verification`` then withholds its
    figures exactly as for any other unverified result); a result that is already an error needs no verification.
    """
    if isinstance(result, dict) and result.get("status") in ("success", "no_equity_holdings"):
        return VerificationResult(UNVERIFIED, 0, error=UNAVAILABLE_MESSAGE)
    return VerificationResult(NOT_APPLICABLE, error="Nothing to verify: the calculation did not produce a result.")


def apply_verification(result: Dict[str, Any], verification: VerificationResult) -> Dict[str, Any]:
    """
    Attach the verification to a calculator result and enforce the response policy. The same function is used by
    the agent and by ``POST /api/exposure``, so both behave identically. Response keys are preserved.

    * verified: returned unchanged plus a ``verification`` block (``status: "verified"``).
    * failed (the figures were *disproved*): not presented as valid. ``status`` becomes ``"error"``,
      ``exposures`` and the total are cleared, ``error`` says the result failed verification.
    * unverified (the figures could *not be checked*, e.g. a database error): treated the same way, because
      unchecked figures must not look like a normal result. ``error`` says verification could not be completed and
      the figures were withheld. ``verification.status`` stays ``"unverified"`` so the two cases remain distinct.
    * not applicable (the calculation itself already returned an error): returned untouched.

    For failed and unverified results the calculator's figures are kept only inside
    ``verification['rejected_result']`` for diagnosis.
    """
    if verification.status == NOT_APPLICABLE:
        return result
    block = verification.to_dict()
    if verification.status == VERIFIED:
        block["figures_digest"] = figures_digest(result)  # fingerprint of the exact figures that were verified
        return {**result, "verification": block}
    block["rejected_result"] = {
        "exposures": result.get("exposures"),
        "total_equity_weight_pct": result.get("total_equity_weight_pct"),
        "status": result.get("status"),
    }
    if verification.status == FAILED:
        message = "The exposure result failed independent verification and was not accepted: " + verification.summary()
    else:
        message = (
            "Independent verification could not be completed, so the exposure figures have been withheld. "
            + (verification.error or "")
        ).strip()
    return {
        **result,
        "status": "error",
        "exposures": [],
        "total_equity_weight_pct": 0.0,
        "error": message,
        "verification": block,
    }
