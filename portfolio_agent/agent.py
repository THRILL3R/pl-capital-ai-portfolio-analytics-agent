"""
Portfolio Agent orchestration layer for PL Capital AI Portfolio Analytics Agent.
Routes natural-language questions to the appropriate analytical tool
(SQLQueryTool or ExposureCalculator) and returns structured responses.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Union

from .database import PortfolioDatabase
from .tools import SQLQueryTool, ExposureCalculator

logger = logging.getLogger(__name__)

# Keywords that signal a sector-exposure question
_EXPOSURE_KEYWORDS: List[str] = [
    "sector exposure",
    "exposure breakdown",
    "sector allocation",
    "sector distribution",
    "sector mix",
    "sector weight",
    "exposure for",
    "show exposure",
    "calculate exposure",
    "calculate the sector exposure",
    "sector exposures",
    "percentage is invested in",
    "percentage invested in",
]

# Phrases that should NOT be routed to exposure. These signal the Q7-style
# cross-portfolio market-value calculation (quantity * current_price), which is a
# different metric from weight-based equity sector exposure and belongs to the SQL tool.
_EXPOSURE_EXCLUSIONS: List[str] = [
    "for each portfolio",
    "each portfolio",
    "by portfolio",
    "per portfolio",
    "across portfolios",
    "across all portfolios",
    "total portfolio value",
    "market value",
    "tech_percentage",
]


class PortfolioAgent:
    """
    Orchestration layer for the PL Capital AI Portfolio Analytics Agent.

    Responsibilities:
    1. Accept natural-language questions.
    2. Validate user input.
    3. Detect sector-exposure intent.
    4. Extract or resolve portfolio identifiers/names.
    5. Route exposure questions to ExposureCalculator.
    6. Route general portfolio questions to SQLQueryTool.
    7. Return a consistent structured response.
    8. Handle tool failures gracefully.
    9. Handle unsupported questions.
    10. Preserve tool execution metadata.
    """

    def __init__(
        self,
        db: Optional[PortfolioDatabase] = None,
        sql_tool: Optional[SQLQueryTool] = None,
        exposure_tool: Optional[ExposureCalculator] = None,
    ) -> None:
        self.db = db or PortfolioDatabase()
        self.sql_tool = sql_tool or SQLQueryTool(db=self.db)
        self.exposure_tool = exposure_tool or ExposureCalculator(db=self.db)

    def answer_question(self, question: str) -> Dict[str, Any]:
        """
        Process a natural-language question and return a structured response.

        Parameters:
            question: The user's natural-language question.

        Returns:
            A JSON-compatible dictionary:
            {
                "status": "success" | "unsupported" | "error" | "no_equity_holdings",
                "question": str,
                "selected_tool": "sql_query" | "exposure_calculator" | None,
                "route_reason": str,
                "result": Dict[str, Any] | None,
                "error": Optional[str]
            }
        """
        # --- Input validation ---
        if not question or not isinstance(question, str) or not question.strip():
            return self._make_response(
                status="error",
                question=question if isinstance(question, str) else str(question),
                selected_tool=None,
                route_reason="Input validation failed.",
                result=None,
                error="Question cannot be empty or whitespace.",
            )

        question = question.strip()

        # --- Routing decision ---
        if self._is_exposure_question(question):
            return self._handle_exposure(question)
        else:
            return self._handle_sql(question)

    def _is_exposure_question(self, question: str) -> bool:
        """
        Determine whether the question is asking for sector exposure.

        Uses keyword matching against exposure-specific phrases,
        while excluding questions that reference per-portfolio
        percentage breakdowns handled by the SQL tool (Q7).
        """
        norm = question.lower()

        # Check exclusions first — Q7 talks about "percentage" and "each portfolio"
        for excl in _EXPOSURE_EXCLUSIONS:
            if excl in norm:
                return False

        # Check for exposure keywords
        for kw in _EXPOSURE_KEYWORDS:
            if kw in norm:
                return True

        return False

    def _extract_portfolio_name(self, question: str) -> Optional[str]:
        """
        Extract a portfolio identifier from a sector-exposure question.

        Strategy:
        1. Match 'for <name>' pattern after exposure keywords.
        2. Match 'for the <name>' pattern.
        3. Match quoted portfolio names.
        4. Match 'portfolio <id>' pattern.
        5. Fallback: extract the noun phrase following the last exposure keyword.
        """
        norm = question.strip()

        # 1. Quoted portfolio name: "Tech Innovation Fund" or 'Tech Innovation Fund'
        quoted_match = re.search(r"""["']([^"']+)["']""", norm)
        if quoted_match:
            return quoted_match.group(1).strip()

        # 2. Pattern: "portfolio <id>" (numeric) — check before generic "for <name>"
        pid_match = re.search(r"\bportfolio\s+(\d+)\b", norm, re.IGNORECASE)
        if pid_match:
            return pid_match.group(1)

        # 3. Pattern: "for the <name>" or "for <name>" — capture until end or punctuation
        for_match = re.search(
            r"\bfor\s+(?:the\s+)?(.+?)(?:\?|$)",
            norm,
            re.IGNORECASE,
        )
        if for_match:
            candidate = for_match.group(1).strip().rstrip("?. ")
            # Filter out generic phrases that aren't portfolio names
            if candidate and candidate.lower() not in (
                "portfolios",
                "portfolio",
                "each portfolio",
                "all portfolios",
                "the portfolio",
            ):
                return candidate

        # 4. Pattern: "... in <Sector> in <Portfolio>" (e.g., "in Technology in Tech Innovation Fund")
        double_in = re.search(
            r"\bin\s+[a-zA-Z]+\s+in\s+(?:the\s+)?(.+?)(?:\?|$)",
            norm,
            re.IGNORECASE,
        )
        if double_in:
            candidate = double_in.group(1).strip().rstrip("?. ")
            if candidate and candidate.lower() not in (
                "portfolios",
                "portfolio",
                "each portfolio",
                "all portfolios",
                "the portfolio",
            ):
                return candidate

        # 5. Pattern: "in <name>" (e.g., "in Tech Innovation Fund")
        in_match = re.search(
            r"\bin\s+(?:the\s+)?(.+?)(?:\?|$)",
            norm,
            re.IGNORECASE,
        )
        if in_match:
            candidate = in_match.group(1).strip().rstrip("?. ")
            if candidate and candidate.lower() not in (
                "portfolios",
                "portfolio",
                "each portfolio",
                "all portfolios",
                "the portfolio",
            ):
                return candidate

        # 6. Fallback: text after last exposure keyword occurrence
        lower = norm.lower()
        last_pos = -1
        matched_kw = ""
        for kw in _EXPOSURE_KEYWORDS:
            pos = lower.rfind(kw)
            if pos > last_pos:
                last_pos = pos
                matched_kw = kw

        if last_pos >= 0:
            remainder = norm[last_pos + len(matched_kw) :].strip().rstrip("?. ")
            # Remove leading prepositions
            remainder = re.sub(r"^(?:for|of|in)\s+(?:the\s+)?", "", remainder, flags=re.IGNORECASE).strip()
            if remainder:
                return remainder

        return None

    def _handle_exposure(self, question: str) -> Dict[str, Any]:
        """Route a sector-exposure question to ExposureCalculator."""
        portfolio_name = self._extract_portfolio_name(question)

        if not portfolio_name:
            return self._make_response(
                status="error",
                question=question,
                selected_tool="exposure_calculator",
                route_reason="Detected sector-exposure intent but could not extract a portfolio name.",
                result=None,
                error=(
                    "Could not determine which portfolio to analyse. "
                    "Please specify a portfolio name, e.g. "
                    "'Show sector exposure for Tech Innovation Fund'."
                ),
            )

        try:
            result = self.exposure_tool.calculate(portfolio_name)
            return self._make_response(
                status=result.get("status", "error"),
                question=question,
                selected_tool="exposure_calculator",
                route_reason=f"Question matched sector-exposure intent for portfolio '{portfolio_name}'.",
                result=result,
                error=result.get("error"),
            )
        except Exception as exc:
            logger.error("ExposureCalculator raised an exception: %s", exc)
            return self._make_response(
                status="error",
                question=question,
                selected_tool="exposure_calculator",
                route_reason=f"Detected sector-exposure intent for portfolio '{portfolio_name}'.",
                result=None,
                error=f"Exposure calculation failed: {exc}",
            )

    def _handle_sql(self, question: str) -> Dict[str, Any]:
        """Route a general analytical question to SQLQueryTool."""
        try:
            result = self.sql_tool.execute(question)
            return self._make_response(
                status=result.get("status", "error"),
                question=question,
                selected_tool="sql_query",
                route_reason="Question routed to SQL query tool for database analysis.",
                result=result,
                error=result.get("error"),
            )
        except Exception as exc:
            logger.error("SQLQueryTool raised an exception: %s", exc)
            return self._make_response(
                status="error",
                question=question,
                selected_tool="sql_query",
                route_reason="Question routed to SQL query tool for database analysis.",
                result=None,
                error=f"SQL query tool failed: {exc}",
            )

    @staticmethod
    def _make_response(
        status: str,
        question: str,
        selected_tool: Optional[str],
        route_reason: str,
        result: Optional[Dict[str, Any]],
        error: Optional[str],
    ) -> Dict[str, Any]:
        """Build a standardised agent response dictionary."""
        return {
            "status": status,
            "question": question,
            "selected_tool": selected_tool,
            "route_reason": route_reason,
            "result": result,
            "error": error,
        }
