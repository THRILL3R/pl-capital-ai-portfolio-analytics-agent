"""
Hybrid analysis: one question that needs BOTH database information and deterministic sector exposure.

Example: "Show the holdings and sector exposure breakdown for the International Equity Fund".

This module only *coordinates* existing components; it contains no calculation and no SQL of its own:

* ``SQLQueryTool``      answers the data part (Gemini-first with the existing deterministic fallback, every
                        statement still goes through lexical + SQLGlot AST validation and the read-only database);
* ``ExposureCalculator`` computes the exposure, deterministically (Stock holdings only, bonds excluded, no
                        renormalisation). Gemini never sees or computes it;
* ``FinancialVerifier``  independently recomputes the exposure; the established fail-safe policy
                        (``apply_verification``) withholds the figures when verification fails or cannot be completed.

Only the SQL component can reach Gemini, and it is asked only for the data part: the exposure wording is removed from
the sub-question first, so the model is never asked to compute exposure.

Outcome rules (a partial result is never presented as a complete one):
    both components succeed                 -> status ``success``, ``complete`` true
    exactly one succeeds                    -> status ``partial``, ``complete`` false, the failure is explained
    neither succeeds / portfolio not found  -> status ``error``
An exposure component that failed verification or could not be verified counts as *not succeeded*: its figures are
already cleared (Phase 4.1 policy) and only the diagnostic block remains.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from .financial_verifier import FinancialVerifier, apply_verification, unavailable_verification

logger = logging.getLogger(__name__)

TOOL_NAME = "hybrid_analysis"

# A data request that sits next to the exposure request ("... holdings and sector exposure ...").
_DATA_CUE_RE = re.compile(
    r"\b(holdings?|positions?|securities|constituents|cost basis|market value|largest|biggest|top\s+\w+|list of)\b", re.IGNORECASE
)
# Plain cues the deterministic generator can answer with a simple holdings list.
_PLAIN_CUE_RE = re.compile(r"\b(holdings?|positions?|securities|constituents)\b", re.IGNORECASE)
_RANKING_RE = re.compile(r"\b(cost basis|market value|largest|biggest|smallest|top\s+\w+|bottom|average|total|most|least|highest|lowest)\b", re.IGNORECASE)
_CONJUNCTION_RE = re.compile(r"(\band\b|\bplus\b|\balong with\b|\bas well as\b|\btogether with\b|\balso\b|,)", re.IGNORECASE)

# The question itself must name exposure explicitly; a classifier hint alone is not enough (so a question such as the
# official Q7 / Q8 can never become hybrid merely because the classifier leaned towards "exposure").
_EXPOSURE_TERM_RE = re.compile(r"\bexposures?\b|\b(?:equity\s+)?sector\s+(?:allocations?|distribution|mix|weights?|breakdown)\b", re.IGNORECASE)

# The exposure wording, removed before the data part is sent to the SQL tool (and so to Gemini).
_EXPOSURE_CLAUSE_RE = re.compile(
    r"(?:,?\s*(?:and|plus|along with|as well as|together with|also)\s+)?"
    r"(?:the\s+)?(?:(?:equity\s+)?sector\s+)?(?:exposures?|allocations?|distribution|mix|weights?)"
    r"(?:\s+breakdown|\s+analysis)?",
    re.IGNORECASE,
)


def is_hybrid_question(question: str, exposure_signal: bool) -> bool:
    """
    True when the question asks for exposure (``exposure_signal``: the agent's keyword rule or the classifier) AND
    for portfolio data, joined by a conjunction. Purely lexical: no model and no Gemini call.
    """
    if not exposure_signal or not _EXPOSURE_TERM_RE.search(question):
        return False
    without_exposure = _EXPOSURE_CLAUSE_RE.sub(" ", question)
    return bool(_DATA_CUE_RE.search(without_exposure)) and bool(_CONJUNCTION_RE.search(question))


def build_sql_question(question: str, portfolio_text: Optional[str], canonical_name: str) -> str:
    """
    The data-only sub-question for the SQL tool: the exposure wording is removed and the portfolio is named
    canonically, so the SQL tool resolves exactly the portfolio the exposure component uses.
    """
    text = _EXPOSURE_CLAUSE_RE.sub(" ", question)
    text = re.sub(r"\bthe\s+(?:and|plus|also)\s+the\b", "the", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(show|list|give|display)(\s+me)?\s+(?:and|plus|also)\b", r"\1\2", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*(?:,|\band\b|\bplus\b|\balso\b)\s*(?=(?:for|of|in)\b|[?.!]*$)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" ,.?!")
    if portfolio_text:
        text = re.sub(re.escape(portfolio_text), canonical_name, text, flags=re.IGNORECASE)
    if canonical_name.lower() not in text.lower():
        text = f"{text} for the {canonical_name}".strip()
    # A plain "holdings" request gets the canonical template, which the deterministic generator answers directly.
    if _PLAIN_CUE_RE.search(text) and not _RANKING_RE.search(text):
        return f"Show the holdings in the {canonical_name}"
    return text


class HybridAnalysisTool:
    """Coordinates SQLQueryTool, ExposureCalculator and FinancialVerifier for one portfolio."""

    def __init__(self, sql_tool: Any, exposure_tool: Any, verifier: Optional[FinancialVerifier] = None) -> None:
        self.sql_tool = sql_tool
        self.exposure_tool = exposure_tool
        self.verifier = verifier

    # ------------------------------------------------------------------ components

    def _exposure_component(self, portfolio_id: int) -> Dict[str, Any]:
        try:
            result = self.exposure_tool.calculate(portfolio_id)
            # No verifier configured is treated as "could not be verified": the figures are withheld, never shown unchecked.
            verification = self.verifier.verify(result) if self.verifier is not None else unavailable_verification(result)
            return apply_verification(result, verification)
        except Exception as exc:  # contained: the SQL component can still be reported
            logger.error("Hybrid exposure component failed: %s: %s", type(exc).__name__, exc)
            return {
                "status": "error", "tool": "exposure_calculator", "portfolio_id": portfolio_id, "exposures": [],
                "total_equity_weight_pct": 0.0, "error": f"Exposure calculation failed: {exc}",
            }

    def _sql_component(self, sql_question: str, allow_gemini: bool, skip_reason: Optional[str]) -> Dict[str, Any]:
        try:
            if allow_gemini:
                result = self.sql_tool.execute(sql_question)
            else:
                result = self.sql_tool.execute(sql_question, allow_gemini=False, gemini_skip_reason=skip_reason)
            return {**result, "question": sql_question}
        except Exception as exc:
            logger.error("Hybrid SQL component failed: %s: %s", type(exc).__name__, exc)
            return {"status": "error", "tool": "sql_query", "question": sql_question, "rows": [], "columns": [],
                    "row_count": 0, "error": f"SQL query tool failed: {exc}"}

    # ------------------------------------------------------------------ orchestration

    def execute(
        self,
        question: str,
        portfolio_identifier: str,
        allow_gemini: bool = True,
        gemini_skip_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            portfolio_id, canonical = self.exposure_tool.resolve_portfolio(portfolio_identifier)
        except Exception as exc:  # ValueError (unknown / ambiguous) or a database error
            # Without one unambiguous portfolio neither component is run: they could describe different portfolios.
            return self._result("error", None, {}, [f"Portfolio could not be resolved: {exc}"], error=f"Portfolio could not be resolved: {exc}")

        exposure = self._exposure_component(portfolio_id)
        sql_question = build_sql_question(question, portfolio_identifier, canonical)
        sql = self._sql_component(sql_question, allow_gemini, gemini_skip_reason)

        exposure_ok = exposure.get("status") in ("success", "no_equity_holdings")
        sql_ok = sql.get("status") == "success"
        issues: List[str] = []
        if not sql_ok:
            issues.append(f"SQL component {sql.get('status', 'error')}: {sql.get('error') or 'no result was produced'}")
        if not exposure_ok:
            verification = exposure.get("verification") or {}
            if verification.get("status") == "failed":
                issues.append("Exposure component failed independent verification; its figures were withheld.")
            elif verification.get("status") == "unverified":
                issues.append("Exposure component could not be independently verified; its figures were withheld.")
            else:
                issues.append(f"Exposure component error: {exposure.get('error') or 'no result was produced'}")

        status = "success" if (sql_ok and exposure_ok) else "partial" if (sql_ok or exposure_ok) else "error"
        return self._result(
            status, {"portfolio_id": portfolio_id, "portfolio_name": canonical}, {"sql": sql, "exposure": exposure}, issues,
            error=None if status == "success" else " ".join(issues),
        )

    @staticmethod
    def _result(status: str, portfolio: Optional[Dict[str, Any]], components: Dict[str, Dict[str, Any]],
                issues: List[str], error: Optional[str]) -> Dict[str, Any]:
        sql, exposure = components.get("sql"), components.get("exposure")
        component_status = {
            "sql": sql.get("status") if sql else "not_run",
            "exposure": exposure.get("status") if exposure else "not_run",
        }
        if status == "success":
            summary = "Both parts completed: the database result and the independently verified sector exposure."
        elif status == "partial":
            summary = "Partial result: only part of the request could be completed. " + " ".join(issues)
        else:
            summary = "Neither part of the request could be completed. " + " ".join(issues)
        return {
            "tool": TOOL_NAME,
            "status": status,
            "complete": status == "success",
            "portfolio": portfolio,
            "component_status": component_status,
            "components": components,
            "issues": issues,
            "summary": summary.strip(),
            "error": error,
        }
