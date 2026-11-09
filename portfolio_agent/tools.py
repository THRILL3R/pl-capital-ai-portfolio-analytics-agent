"""
Core Analytical Tools for PL Capital AI Portfolio Analytics Agent.
Provides SQLQueryTool and ExposureCalculator.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple, Union

from .database import PortfolioDatabase, ReadOnlyQueryError
from .sql_generator import (
    RuleBasedSQLGenerator,
    GeminiSQLGenerator,
    UnsupportedQuestionError,
    GeminiGenerationError,
)

logger = logging.getLogger(__name__)


class SQLQueryTool:
    """
    Executes natural-language portfolio data queries by translating questions to SQL
    and safely executing them through the PortfolioDatabase read-only layer.
    Supports Gemini LLM generation with automatic deterministic fallback.
    """

    def __init__(
        self,
        db: Optional[PortfolioDatabase] = None,
        generator: Optional[RuleBasedSQLGenerator] = None,
        gemini_generator: Optional[GeminiSQLGenerator] = None,
        use_gemini: bool = True,
    ) -> None:
        self.db = db or PortfolioDatabase()
        self.generator = generator or RuleBasedSQLGenerator()
        self.use_gemini = use_gemini
        self.gemini_generator = gemini_generator or GeminiSQLGenerator(db=self.db)

    @staticmethod
    def _response(
        question: Any,
        status: str,
        generation_method: str,
        model_name: Optional[str] = None,
        fallback_used: bool = False,
        fallback_reason: Optional[str] = None,
        generated_sql: Optional[str] = None,
        query_parameters: Optional[List[Any]] = None,
        db_result: Optional[Dict[str, Any]] = None,
        query_description: Optional[str] = None,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build the structured SQLQueryTool response (stable key set)."""
        db_result = db_result or {}
        return {
            "status": status,
            "tool": "sql_query",
            "original_question": question,
            "generated_sql": db_result.get("sql", generated_sql),
            "query_parameters": query_parameters,
            "row_count": db_result.get("row_count", 0),
            "rows": db_result.get("rows", []),
            "columns": db_result.get("columns", []),
            "generation_method": generation_method,
            "model_name": model_name,
            "fallback_used": fallback_used,
            "fallback_reason": fallback_reason,
            "query_description": query_description,
            "error": error,
        }

    def _try_gemini(self, question: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        Attempt Gemini generation + read-only execution.

        Returns (response, None) on success, or (None, fallback_reason) on failure.
        A successful query that returns zero rows IS a success: an empty result can be a
        legitimate answer, so it does not trigger the rule-based fallback.
        """
        gen = self.gemini_generator
        try:
            if hasattr(gen, "generate_structured"):
                generated = gen.generate_structured(question)
            else:  # duck-typed generators exposing only generate()
                sql, params = gen.generate(question)
                generated = {"sql": sql, "parameters": tuple(params or ()), "description": None}
        except GeminiGenerationError as exc:
            category = getattr(exc, "category", "api_error")
            return None, f"Gemini generation failed ({category}): {exc}"
        except Exception as exc:  # defensive: never let a generator bug break the tool
            return None, f"Gemini generation failed (unexpected): {type(exc).__name__}: {exc}"

        sql = generated["sql"]
        params = tuple(generated.get("parameters") or ())
        try:
            db_result = self.db.execute_read_only(sql, params)
        except ReadOnlyQueryError as exc:
            # Covers unknown tables/columns, syntax errors and safety violations.
            return None, f"Gemini SQL rejected by database layer: {exc}"
        except Exception as exc:
            return None, f"Gemini SQL execution failed (unexpected): {type(exc).__name__}: {exc}"

        response = self._response(
            question,
            status="success",
            generation_method="gemini",
            model_name=getattr(gen, "model_name", None),
            query_parameters=list(params),
            db_result=db_result,
            query_description=generated.get("description"),
        )
        return response, None

    def execute(self, question: str) -> Dict[str, Any]:
        """
        Execute a natural-language portfolio question.

        Strategy:
        1. If Gemini is enabled and configured, ask it for SQL and execute that SQL through
           the read-only database layer.
        2. Fall back to RuleBasedSQLGenerator when Gemini is not configured, the API fails
           (timeout, quota, network, auth), the output is invalid (empty, malformed JSON,
           bad parameters, unsafe SQL) or the database layer rejects the SQL.
        3. When Gemini is explicitly disabled (use_gemini=False / --no-llm), the rule-based
           generator is the primary method and no fallback is reported.
        """
        if not question or not isinstance(question, str) or not question.strip():
            return self._response(
                question,
                status="error",
                generation_method="none",
                error="Question cannot be empty or whitespace.",
            )

        fallback_used = False
        fallback_reason: Optional[str] = None

        if self.use_gemini:
            if self.gemini_generator.is_configured:
                gemini_response, fallback_reason = self._try_gemini(question)
                if gemini_response is not None:
                    return gemini_response
                logger.warning("Falling back to rule-based generator: %s", fallback_reason)
            else:
                fallback_reason = "Gemini not configured (GEMINI_API_KEY is not set)."
            fallback_used = True

        meta = {"fallback_used": fallback_used, "fallback_reason": fallback_reason}

        try:
            sql, params = self.generator.generate(question)
        except UnsupportedQuestionError as exc:
            return self._response(
                question, status="unsupported", generation_method="rule_based", error=str(exc), **meta
            )
        except Exception as exc:
            return self._response(
                question,
                status="error",
                generation_method="rule_based",
                error=f"Unexpected execution error: {exc}",
                **meta,
            )

        try:
            db_result = self.db.execute_read_only(sql, params)
        except ReadOnlyQueryError as exc:
            return self._response(
                question,
                status="error",
                generation_method="rule_based",
                generated_sql=sql,
                query_parameters=list(params) if params else [],
                error=f"Database execution error: {exc}",
                **meta,
            )
        except Exception as exc:
            return self._response(
                question,
                status="error",
                generation_method="rule_based",
                error=f"Unexpected execution error: {exc}",
                **meta,
            )

        return self._response(
            question,
            status="success",
            generation_method="rule_based",
            query_parameters=list(params) if params else [],
            db_result=db_result,
            **meta,
        )


class ExposureCalculator:
    """
    Calculates equity sector exposures for a given portfolio.

    Strict calculation rules:
    - Formula: SUM(holdings.current_weight) GROUPED BY sectors.sector_name.
    - Asset filter: securities.asset_type = 'Stock' ONLY.
    - Bonds (asset_type = 'Bond') are strictly EXCLUDED.
    - Weights are fractional (e.g. 0.18) and converted to percentages (18.0%).
    - Never renormalizes weights silently to 100%.
    """

    def __init__(self, db: Optional[PortfolioDatabase] = None) -> None:
        self.db = db or PortfolioDatabase()

    def resolve_portfolio(self, identifier: Union[str, int]) -> Tuple[int, str]:
        """
        Resolve a portfolio name or ID to an exact (portfolio_id, portfolio_name) pair.

        Resolution strategy:
        1. If integer or digit string: lookup by portfolio_id.
        2. Exact case-insensitive match on portfolio_name.
        3. Unique, unambiguous partial substring match.
        4. Rejects ambiguous matches.
        5. Raises ValueError for nonexistent or ambiguous portfolios.

        Returns:
            Tuple of (portfolio_id, canonical_portfolio_name).

        Raises:
            ValueError: If the identifier is ambiguous or not found.
        """
        if identifier is None:
            raise ValueError("Portfolio identifier cannot be None.")

        # Check if integer ID
        if isinstance(identifier, int) or (isinstance(identifier, str) and identifier.strip().isdigit()):
            pid = int(identifier)
            res = self.db.execute_read_only(
                "SELECT portfolio_id, portfolio_name FROM portfolios WHERE portfolio_id = ?;",
                (pid,),
            )
            if res["rows"]:
                return res["rows"][0]["portfolio_id"], res["rows"][0]["portfolio_name"]
            raise ValueError(f"Portfolio with ID {pid} not found.")

        name = str(identifier).strip()
        if not name:
            raise ValueError("Portfolio name cannot be empty.")

        # 1. Exact case-insensitive match
        exact_res = self.db.execute_read_only(
            "SELECT portfolio_id, portfolio_name FROM portfolios WHERE LOWER(portfolio_name) = LOWER(?);",
            (name,),
        )
        if exact_res["rows"]:
            return exact_res["rows"][0]["portfolio_id"], exact_res["rows"][0]["portfolio_name"]

        # 2. Case-insensitive substring match
        like_pattern = f"%{name.lower()}%"
        sub_res = self.db.execute_read_only(
            "SELECT portfolio_id, portfolio_name FROM portfolios WHERE LOWER(portfolio_name) LIKE ?;",
            (like_pattern,),
        )
        matches = sub_res["rows"]

        if len(matches) == 1:
            return matches[0]["portfolio_id"], matches[0]["portfolio_name"]
        elif len(matches) > 1:
            matched_names = [r["portfolio_name"] for r in matches]
            raise ValueError(
                f"Ambiguous portfolio name '{name}'. Matches multiple portfolios: {matched_names}. "
                "Please specify a more specific name."
            )

        # 3. Token-based word overlap for phrases like 'international equity' -> 'International Equity Fund'
        all_portfolios_res = self.db.execute_read_only(
            "SELECT portfolio_id, portfolio_name FROM portfolios;"
        )
        search_tokens = set(name.lower().split())
        token_matches: List[Dict[str, Any]] = []

        for p in all_portfolios_res["rows"]:
            p_tokens = set(p["portfolio_name"].lower().split())
            # Remove generic words like 'fund' or 'portfolio' from required match if specific words match
            core_search = search_tokens - {"fund", "portfolio"}
            if core_search and core_search.issubset(p_tokens):
                token_matches.append(p)

        if len(token_matches) == 1:
            return token_matches[0]["portfolio_id"], token_matches[0]["portfolio_name"]
        elif len(token_matches) > 1:
            matched_names = [r["portfolio_name"] for r in token_matches]
            raise ValueError(
                f"Ambiguous portfolio name '{name}'. Matches multiple portfolios: {matched_names}."
            )

        raise ValueError(f"Portfolio '{name}' not found.")

    def calculate(
        self,
        portfolio_identifier: Union[str, int],
        raise_on_error: bool = False,
    ) -> Dict[str, Any]:
        """
        Calculate equity sector exposures for a specified portfolio.

        Parameters:
            portfolio_identifier: Portfolio name (exact or partial) or integer ID.
            raise_on_error: If True, raises ValueError on invalid/ambiguous portfolio.
                            If False, returns structured dict with status='error'.

        Returns:
            Structured dictionary with exposure breakdown:
            {
                "status": "success" | "no_equity_holdings" | "error",
                "tool": "exposure_calculator",
                "portfolio_id": int,
                "portfolio_name": str,
                "exposure_basis": str,
                "asset_filter": str,
                "exposures": [
                    {
                        "sector": str,
                        "weight_fraction": float,
                        "exposure_pct": float,
                        "holding_count": int
                    }, ...
                ],
                "total_equity_weight_pct": float,
                "total_holdings_count": int,
                "equity_holdings_count": int,
                "excluded_bond_holdings": int,
                "unmapped_equity_holdings": int,
                "error": Optional[str]
            }
        """
        try:
            portfolio_id, portfolio_name = self.resolve_portfolio(portfolio_identifier)
        except ValueError as exc:
            if raise_on_error:
                raise
            return {
                "status": "error",
                "tool": "exposure_calculator",
                "portfolio_id": None,
                "portfolio_name": str(portfolio_identifier),
                "exposure_basis": "SUM(holdings.current_weight) grouped by security sector",
                "asset_filter": "Equities only (asset_type='Stock'); bond holdings excluded",
                "exposures": [],
                "total_equity_weight_pct": 0.0,
                "total_holdings_count": 0,
                "equity_holdings_count": 0,
                "excluded_bond_holdings": 0,
                "unmapped_equity_holdings": 0,
                "error": str(exc),
            }

        try:
            # 1. Sector exposures for Stock holdings only
            exposure_sql = (
                "SELECT sect.sector_name, SUM(h.current_weight) as weight_fraction, "
                "COUNT(h.holding_id) as holding_count "
                "FROM holdings h "
                "JOIN securities sec ON h.security_id = sec.security_id "
                "JOIN sectors sect ON sec.sector_id = sect.sector_id "
                "WHERE h.portfolio_id = ? AND sec.asset_type = 'Stock' "
                "GROUP BY sect.sector_name "
                "ORDER BY weight_fraction DESC;"
            )
            exposure_res = self.db.execute_read_only(exposure_sql, (portfolio_id,))

            # 2. Total holdings count in portfolio
            total_holdings_res = self.db.execute_read_only(
                "SELECT COUNT(*) as cnt FROM holdings WHERE portfolio_id = ?;",
                (portfolio_id,),
            )
            total_holdings_count = total_holdings_res["rows"][0]["cnt"] if total_holdings_res["rows"] else 0

            # 3. Bond holdings count (excluded)
            bond_res = self.db.execute_read_only(
                "SELECT COUNT(*) as cnt FROM holdings h "
                "JOIN securities sec ON h.security_id = sec.security_id "
                "WHERE h.portfolio_id = ? AND sec.asset_type = 'Bond';",
                (portfolio_id,),
            )
            excluded_bond_count = bond_res["rows"][0]["cnt"] if bond_res["rows"] else 0

            # 4. Stock holdings count
            stock_res = self.db.execute_read_only(
                "SELECT COUNT(*) as cnt FROM holdings h "
                "JOIN securities sec ON h.security_id = sec.security_id "
                "WHERE h.portfolio_id = ? AND sec.asset_type = 'Stock';",
                (portfolio_id,),
            )
            equity_holdings_count = stock_res["rows"][0]["cnt"] if stock_res["rows"] else 0

            # 5. Stock holdings without a mapped sector (data hygiene check)
            unmapped_res = self.db.execute_read_only(
                "SELECT COUNT(*) as cnt FROM holdings h "
                "JOIN securities sec ON h.security_id = sec.security_id "
                "WHERE h.portfolio_id = ? AND sec.asset_type = 'Stock' AND sec.sector_id IS NULL;",
                (portfolio_id,),
            )
            unmapped_equity_count = unmapped_res["rows"][0]["cnt"] if unmapped_res["rows"] else 0

            # Build exposure list
            exposures: List[Dict[str, Any]] = []
            for row in exposure_res["rows"]:
                weight_fraction = round(float(row["weight_fraction"]), 6)
                exposure_pct = round(weight_fraction * 100.0, 4)
                exposures.append(
                    {
                        "sector": row["sector_name"],
                        "weight_fraction": weight_fraction,
                        "exposure_pct": exposure_pct,
                        "holding_count": int(row["holding_count"]),
                    }
                )

            total_equity_weight_pct = round(sum(e["exposure_pct"] for e in exposures), 4)

            status = "no_equity_holdings" if equity_holdings_count == 0 else "success"

            return {
                "status": status,
                "tool": "exposure_calculator",
                "portfolio_id": portfolio_id,
                "portfolio_name": portfolio_name,
                "exposure_basis": "SUM(holdings.current_weight) grouped by security sector",
                "asset_filter": "Equities only (asset_type='Stock'); bond holdings excluded",
                "exposures": exposures,
                "total_equity_weight_pct": total_equity_weight_pct,
                "total_holdings_count": total_holdings_count,
                "equity_holdings_count": equity_holdings_count,
                "excluded_bond_holdings": excluded_bond_count,
                "unmapped_equity_holdings": unmapped_equity_count,
                "error": None,
            }

        except Exception as exc:
            logger.error("Exposure calculation error: %s", exc)
            if raise_on_error:
                raise
            return {
                "status": "error",
                "tool": "exposure_calculator",
                "portfolio_id": portfolio_id,
                "portfolio_name": portfolio_name,
                "exposure_basis": "SUM(holdings.current_weight) grouped by security sector",
                "asset_filter": "Equities only (asset_type='Stock'); bond holdings excluded",
                "exposures": [],
                "total_equity_weight_pct": 0.0,
                "total_holdings_count": 0,
                "equity_holdings_count": 0,
                "excluded_bond_holdings": 0,
                "unmapped_equity_holdings": 0,
                "error": f"Calculation failed: {exc}",
            }
