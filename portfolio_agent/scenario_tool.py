"""
Deterministic scenario / stress-testing engine.

It answers one question: *if sector X moved by s%, what would that do to the portfolio's recorded equity weights?*
Every result is a **hypothetical illustration, not a forecast and not actual performance**.

Formulas (percentages; weights are recorded ``holdings.current_weight`` fractions of the portfolio, never rescaled)
-----------------------------------------------------------------------------------------------------------------
    sector contribution (percentage points of portfolio value) = w_sector x shock_sector_pct
    estimated portfolio impact (percentage points)             = SUM over sectors of (w_sector x shock_sector_pct)
    estimated value impact (currency)                          = total_aum x estimated_portfolio_impact_pct / 100
        (only when the portfolio has a positive recorded AUM; weights are then read as fractions of that AUM)

Example: Technology weight 0.36 and a -25% Technology shock contribute 0.36 x -25 = -9.0 percentage points.

Rules
-----
* Only Stock holdings with a mapped sector are shocked. Bonds are excluded (not modelled). Stock holdings with no
  sector are reported (count and weight) and contribute zero. Nothing is renormalised and nothing is forced to
  100%: the result states how much of the portfolio's recorded weight is covered and how much is not.
* Sectors without a shock contribute exactly zero. A shock on a sector the portfolio does not hold is reported, with
  zero impact, instead of being silently ignored.
* Arithmetic uses ``decimal.Decimal`` (28 digits) from the decimal text of the recorded weights; results are rounded
  to 6 decimal places only for display.
* Inputs must be finite real numbers within [-100, +100] percent per sector (a sector cannot lose more than 100%);
  NaN, infinity, booleans, text and unknown sector names are rejected.
* All data access is read-only through ``PortfolioDatabase.execute_read_only`` (SQLGlot AST validation, read-only
  connection, authorizer). Gemini is never involved.

The result is verified by ``ScenarioVerifier`` (scenario_verifier.py), which recomputes it independently from raw rows.
Without a verifier, or if verification fails or cannot complete, the figures are withheld (same policy as exposure).
"""

from __future__ import annotations

import logging
import math
import re
from decimal import Decimal, ROUND_HALF_EVEN, getcontext
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)
getcontext().prec = 28

TOOL_NAME = "scenario_analysis"
SHOCK_MIN = Decimal(-100)
SHOCK_MAX = Decimal(100)
DISPLAY_PLACES = Decimal("0.000001")
DISCLAIMER = (
    "Hypothetical scenario for illustration only. It is not a forecast or prediction and not actual portfolio "
    "performance. It applies the stated shocks to the recorded sector weights of a static portfolio."
)
ASSUMPTIONS = [
    "Impact is expressed as percentage points of total portfolio value: the sum of recorded current_weight x sector shock.",
    "Only Stock holdings mapped to a sector are shocked; bonds and unmapped stocks are not modelled and contribute zero.",
    "Weights are used as recorded: they are not renormalised and are not forced to total 100%.",
    "Instantaneous, static shock: no correlations, beta, rebalancing, cash flows or second-order effects.",
    "Value impact (when shown) = total AUM x impact / 100, reading recorded weights as fractions of total AUM; "
    "the sample dataset is denominated in USD.",
]

# Named, illustrative scenarios (shock values are percentages; sector_shocks override the market shock).
SCENARIOS: Dict[str, Dict[str, Any]] = {
    "broad_market_downturn": {
        "name": "Broad Market Downturn",
        "description": "Illustrative: every mapped stock sector falls 10%.",
        "market_shock_pct": -10, "sector_shocks": {},
    },
    "severe_market_stress": {
        "name": "Severe Market Stress",
        "description": "Illustrative: every mapped stock sector falls 20%.",
        "market_shock_pct": -20, "sector_shocks": {},
    },
    "technology_sell_off": {
        "name": "Technology Sell-off",
        "description": "Illustrative: Technology falls 25%; all other sectors are unchanged.",
        "market_shock_pct": 0, "sector_shocks": {"Technology": -25},
    },
    "energy_shock": {
        "name": "Energy Shock",
        "description": "Illustrative: Energy falls 20%; all other sectors are unchanged.",
        "market_shock_pct": 0, "sector_shocks": {"Energy": -20},
    },
}


class ScenarioError(ValueError):
    """Invalid scenario input (message is safe to show to the user)."""


def to_decimal(value: Any) -> Decimal:
    """Exact Decimal from the *decimal text* of a recorded number (0.36 -> Decimal('0.36'), not its binary expansion)."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def validate_shock(value: Any, label: str = "shock") -> Decimal:
    """A finite real number of percent within [-100, +100]. Booleans, text, NaN and infinity are rejected."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ScenarioError(f"{label} must be a number (percent), got {type(value).__name__}.")
    if isinstance(value, float) and not math.isfinite(value):
        raise ScenarioError(f"{label} must be a finite number, got {value}.")
    d = to_decimal(value)
    if not d.is_finite():
        raise ScenarioError(f"{label} must be a finite number, got {value}.")
    if d < SHOCK_MIN or d > SHOCK_MAX:
        raise ScenarioError(f"{label} must be between -100% and +100% (a sector cannot lose more than 100%), got {d.normalize():f}%.")
    return d


def display(d: Decimal) -> float:
    return float(d.quantize(DISPLAY_PLACES, rounding=ROUND_HALF_EVEN))


def resolve_scenario(
    sector_names: List[str],
    scenario_id: Optional[str] = None,
    market_shock_pct: Any = None,
    sector_shocks: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Validate a predefined or custom scenario and return its normalised definition (Decimal shocks)."""
    custom = market_shock_pct is not None or bool(sector_shocks)
    if scenario_id is not None and custom:
        raise ScenarioError("Choose either a predefined scenario or custom shocks, not both.")
    if scenario_id is not None:
        if scenario_id not in SCENARIOS:
            raise ScenarioError(f"Unknown scenario '{scenario_id}'. Available: {', '.join(SCENARIOS)}.")
        spec = SCENARIOS[scenario_id]
        return {
            "id": scenario_id, "name": spec["name"], "description": spec["description"], "type": "predefined",
            "market_shock_pct": validate_shock(spec["market_shock_pct"], "market shock"),
            "sector_shocks": {k: validate_shock(v, f"{k} shock") for k, v in spec["sector_shocks"].items()},
        }
    if not custom:
        raise ScenarioError("Specify a predefined scenario or at least one shock (market-wide or for a sector).")
    market = validate_shock(market_shock_pct, "market shock") if market_shock_pct is not None else Decimal(0)
    canonical = {n.lower(): n for n in sector_names}
    shocks: Dict[str, Decimal] = {}
    if sector_shocks is not None and not isinstance(sector_shocks, dict):
        raise ScenarioError("sector_shocks must be an object mapping sector names to percent shocks.")
    for raw_name, raw_value in (sector_shocks or {}).items():
        if not isinstance(raw_name, str) or raw_name.strip().lower() not in canonical:
            raise ScenarioError(f"Unknown sector {raw_name!r}. Valid sectors: {', '.join(sorted(sector_names))}.")
        name = canonical[raw_name.strip().lower()]
        if name in shocks:
            raise ScenarioError(f"Sector {name!r} was given more than once.")
        shocks[name] = validate_shock(raw_value, f"{name} shock")
    parts = ([f"all mapped sectors {market:+f}%".replace("+-", "-")] if market_shock_pct is not None else []) + [f"{k} {v:+f}%" for k, v in shocks.items()]
    return {
        "id": "custom", "name": "Custom scenario", "type": "custom",
        "description": "Illustrative custom scenario: " + "; ".join(parts) + ". Sectors without a shock are unchanged.",
        "market_shock_pct": market, "sector_shocks": shocks,
    }


class ScenarioTool:
    """Runs a scenario for one portfolio and returns a (verified) structured result."""

    def __init__(self, db: Any, exposure_tool: Any, verifier: Optional[Any] = None) -> None:
        self.db = db
        self.exposure_tool = exposure_tool  # starting sector exposures come from the existing calculator
        self.verifier = verifier

    @staticmethod
    def list_scenarios() -> List[Dict[str, Any]]:
        return [{"id": k, "name": v["name"], "description": v["description"], "market_shock_pct": v["market_shock_pct"],
                 "sector_shocks": dict(v["sector_shocks"])} for k, v in SCENARIOS.items()]

    # ------------------------------------------------------------------ public

    def run(
        self,
        portfolio_identifier: Any,
        scenario_id: Optional[str] = None,
        market_shock_pct: Any = None,
        sector_shocks: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        try:
            sector_names = [r["sector_name"] for r in self.db.execute_read_only("SELECT sector_name FROM sectors")["rows"]]
            scenario = resolve_scenario(sector_names, scenario_id, market_shock_pct, sector_shocks)
        except ScenarioError as exc:
            return self._error(str(exc), error_type="invalid_scenario")
        except Exception as exc:
            return self._error(f"Scenario could not be prepared: {exc}", error_type="database_error")
        try:
            portfolio_id, portfolio_name = self.exposure_tool.resolve_portfolio(portfolio_identifier)
        except Exception as exc:  # unknown / ambiguous portfolio, or a database error
            return self._error(str(exc), error_type="invalid_portfolio")
        try:
            result = self._compute(portfolio_id, portfolio_name, scenario)
        except Exception as exc:
            logger.error("Scenario calculation failed: %s: %s", type(exc).__name__, exc)
            return self._error(f"Scenario calculation failed: {exc}", error_type="calculation_error", portfolio=(portfolio_id, portfolio_name))
        if result.get("status") not in ("success", "no_equity_holdings"):
            return result
        from .scenario_verifier import apply_scenario_verification, unavailable_scenario_verification

        verification = self.verifier.verify(result) if self.verifier is not None else unavailable_scenario_verification(result)
        return apply_scenario_verification(result, verification)

    # ------------------------------------------------------------------ calculation

    def _compute(self, portfolio_id: int, portfolio_name: str, scenario: Dict[str, Any]) -> Dict[str, Any]:
        exposure = self.exposure_tool.calculate(portfolio_id)
        if exposure.get("status") not in ("success", "no_equity_holdings"):
            return self._error(exposure.get("error") or "The starting exposure could not be calculated.", error_type="exposure_error",
                               portfolio=(portfolio_id, portfolio_name))
        q = self.db.execute_read_only
        bond = q("SELECT COUNT(*) AS n, SUM(h.current_weight) AS w FROM holdings h JOIN securities s ON h.security_id = s.security_id "
                 "WHERE h.portfolio_id = ? AND s.asset_type = 'Bond'", (portfolio_id,))["rows"][0]
        unmapped = q("SELECT COUNT(*) AS n, SUM(h.current_weight) AS w FROM holdings h JOIN securities s ON h.security_id = s.security_id "
                     "WHERE h.portfolio_id = ? AND s.asset_type = 'Stock' AND s.sector_id IS NULL", (portfolio_id,))["rows"][0]
        aum_rows = q("SELECT total_aum FROM portfolios WHERE portfolio_id = ?", (portfolio_id,))["rows"]
        aum = aum_rows[0]["total_aum"] if aum_rows else None

        market = scenario["market_shock_pct"]
        sector_shocks: Dict[str, Decimal] = scenario["sector_shocks"]
        starting: List[Dict[str, Any]] = []
        impacts: List[Dict[str, Any]] = []
        total = Decimal(0)
        equity_weight = Decimal(0)
        shocked_weight = Decimal(0)
        held = set()
        for e in exposure.get("exposures", []):
            w = to_decimal(e["weight_fraction"])
            shock = sector_shocks.get(e["sector"], market)
            contribution = w * shock                      # fraction x percent = percentage points of the portfolio
            total += contribution
            equity_weight += w
            if shock != 0:
                shocked_weight += w
            held.add(e["sector"])
            starting.append({"sector": e["sector"], "weight_fraction": float(w), "exposure_pct": display(w * 100), "holding_count": e["holding_count"]})
            impacts.append({"sector": e["sector"], "exposure_pct": display(w * 100), "shock_pct": display(shock),
                            "contribution_pp": display(contribution), "shocked": shock != 0})
        unheld = [{"sector": k, "shock_pct": display(v)} for k, v in sector_shocks.items() if k not in held]
        bond_w = to_decimal(bond["w"]) if bond["w"] is not None else Decimal(0)
        unmapped_w = to_decimal(unmapped["w"]) if unmapped["w"] is not None else Decimal(0)
        accounted = equity_weight + unmapped_w + bond_w
        value_impact: Optional[Dict[str, Any]] = None
        value_note: Optional[str] = None
        if isinstance(aum, (int, float)) and not isinstance(aum, bool) and math.isfinite(aum) and aum > 0:
            amount = (to_decimal(aum) * total / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
            value_impact = {"amount": float(amount), "currency": "USD", "aum": float(to_decimal(aum)),
                            "basis": "total_aum x estimated_portfolio_impact_pct / 100"}
        else:
            value_note = "No positive total AUM is recorded for this portfolio, so no currency impact is shown."
        no_equity = not exposure.get("exposures")
        return {
            "tool": TOOL_NAME,
            "status": "no_equity_holdings" if no_equity else "success",
            "portfolio_id": portfolio_id,
            "portfolio_name": portfolio_name,
            "scenario": {
                "id": scenario["id"], "name": scenario["name"], "description": scenario["description"], "type": scenario["type"],
                "market_shock_pct": display(market),
                "sector_shocks": {k: display(v) for k, v in sector_shocks.items()},
                "is_hypothetical": True, "is_forecast": False,
            },
            "starting_exposure": starting,
            "sector_impacts": impacts,
            "unheld_sector_shocks": unheld,
            "estimated_portfolio_impact_pct": display(total),
            "impact_basis": "percentage points of total portfolio value (hypothetical, not actual performance)",
            "estimated_value_impact": value_impact,
            "value_impact_note": value_note,
            "coverage": {
                "equity_exposure_pct": display(equity_weight * 100),
                "shocked_weight_pct": display(shocked_weight * 100),
                "unshocked_mapped_weight_pct": display((equity_weight - shocked_weight) * 100),
                "equity_holdings_count": exposure.get("equity_holdings_count", 0),
                "unmapped_equity_holdings": int(unmapped["n"] or 0),
                "unmapped_equity_weight_pct": display(unmapped_w * 100),
                "excluded_bond_holdings": int(bond["n"] or 0),
                "excluded_bond_weight_pct": display(bond_w * 100),
                "recorded_weight_accounted_pct": display(accounted * 100),
                "recorded_weight_unaccounted_pct": display(100 - accounted * 100),
                "renormalised": False,
            },
            "assumptions": list(ASSUMPTIONS),
            "disclaimer": DISCLAIMER,
            "error": None,
        }

    @staticmethod
    def _error(message: str, error_type: str, portfolio: Optional[tuple] = None) -> Dict[str, Any]:
        return {
            "tool": TOOL_NAME, "status": "error", "error_type": error_type,
            "portfolio_id": portfolio[0] if portfolio else None, "portfolio_name": portfolio[1] if portfolio else None,
            "scenario": None, "starting_exposure": [], "sector_impacts": [], "unheld_sector_shocks": [],
            "estimated_portfolio_impact_pct": None, "estimated_value_impact": None, "coverage": None,
            "disclaimer": DISCLAIMER, "error": message,
        }


# ======================================================================================
# Deterministic question parsing (no model involved)
# ======================================================================================
_CUE = re.compile(
    r"stress[\s-]?test|stress scenario|market stress|\bscenario\b|what[\s-]?if|what happens|sell[\s-]?off|\bdownturn\b|"
    r"market (?:crash|decline|drop|fall)|\bshock\b|simulate", re.IGNORECASE)
_HISTORICAL = re.compile(
    r"\b(historical(?:ly)?|history|last (?:year|month|quarter|week)|past|previous(?:ly)?|actual(?:ly)?|realised|realized|so far|"
    r"drawdown|volatility|sharpe|beta|tracking error|performance|returns?)\b|\bvar\b", re.IGNORECASE)
_NUM = r"(\d+(?:\.\d+)?)\s*(?:%|percent|per ?cent|pct)"
_NEG = r"(?:fall|falls|fell|drop|drops|dropped|decline|declines|declined|lose|loses|lost|down|sell(?:s|ing)?[\s-]?off|crash(?:es)?|plunge[sd]?|slump(?:s|ed)?|loss|correction)"
_POS = r"(?:rise|rises|rose|rally|rallies|gain|gains|up|increase[sd]?|jump(?:s|ed)?|surge[sd]?|climb(?:s|ed)?)"
_NAMED = [
    (re.compile(r"severe (?:market )?stress|severe stress", re.I), "severe_market_stress"),
    (re.compile(r"(?:tech(?:nology)?)[\s-]*sell[\s-]?off", re.I), "technology_sell_off"),
    (re.compile(r"energy shock", re.I), "energy_shock"),
    (re.compile(r"broad market (?:downturn|decline|sell[\s-]?off)|market downturn", re.I), "broad_market_downturn"),
]
_ALIASES = {"tech": "Technology", "oil": "Energy", "healthcare": "Healthcare", "health care": "Healthcare"}


def is_scenario_question(question: str) -> bool:
    """True for hypothetical stress / what-if questions with a stated shock or a named scenario. Historical and
    descriptive questions (performance, drawdown, returns, past tense) are never scenario questions."""
    if not _CUE.search(question) or _HISTORICAL.search(question):
        return False
    return bool(re.search(_NUM, question, re.I)) or any(p.search(question) for p, _ in _NAMED) or bool(re.search(r"stress[\s-]?test|stress scenario", question, re.I))


def parse_scenario_request(question: str, sector_names: List[str]) -> Dict[str, Any]:
    """
    Extract the scenario from the wording: ``{"scenario_id": ..., "market_shock_pct": ..., "sector_shocks": {...}}``
    (keys absent when not stated). Values are plain numbers; validation happens in ``resolve_scenario``.
    """
    out: Dict[str, Any] = {}
    sector_shocks: Dict[str, float] = {}
    names = {n.lower(): n for n in sector_names}
    names.update({k: v for k, v in _ALIASES.items() if v in sector_names})
    for key in sorted(names, key=len, reverse=True):
        sector = names[key]
        if sector in sector_shocks:
            continue
        k = re.escape(key)
        for sign, verbs in ((-1.0, _NEG), (1.0, _POS)):
            m = (re.search(rf"\b{k}\b[^.?!]{{0,45}}?\b{verbs}\b\s*(?:by\s+|about\s+|roughly\s+)?{_NUM}", question, re.I)
                 or re.search(rf"{_NUM}\s*(?:\w+\s+){{0,2}}?{verbs}\s+(?:in|for|of)\s+(?:the\s+)?{k}\b", question, re.I))
            if m:
                sector_shocks[sector] = sign * float(m.group(1))
                break
    if sector_shocks:
        out["sector_shocks"] = sector_shocks
    for sign, verbs in ((-1.0, _NEG), (1.0, _POS)):
        m = (re.search(rf"{_NUM}\s*(?:market\s+)?{verbs}\b", question, re.I)
             or re.search(rf"\b(?:market|markets|equities|stocks|all sectors)\b\s*{verbs}\b\s*(?:by\s+)?{_NUM}", question, re.I))
        if m and not out.get("sector_shocks"):
            out["market_shock_pct"] = sign * float(m.group(1))
            break
    if not out:
        for pattern, scenario_id in _NAMED:
            if pattern.search(question):
                out["scenario_id"] = scenario_id
                break
    return out
