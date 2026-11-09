"""
Local integration layer between the web frontend and the existing Python agent.

This module only *wraps* the existing project classes (PortfolioAgent, SQLQueryTool,
ExposureCalculator, PortfolioDatabase). It does not change them, and it adds no new
business logic:

* ``ask`` and ``exposure`` return the existing agent / calculator responses verbatim.
* The portfolio views run FIXED, parameterised, read-only queries through
  ``PortfolioDatabase.execute_read_only`` (the same validation + read-only connection +
  authorizer used everywhere else).
* Nothing here reads, returns or logs API keys. ``health`` reports booleans only.

The agent and the database share one SQLite connection, so every call is serialised
with a lock (the application is a single-user local prototype).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from portfolio_agent.agent import PortfolioAgent
from portfolio_agent.financial_verifier import FinancialVerifier, apply_verification
from portfolio_agent.scenario_tool import SHOCK_MAX, SHOCK_MIN, ScenarioTool
from portfolio_agent.scenario_verifier import ScenarioVerifier
from portfolio_agent.intent_classifier import build_default_classifier
from portfolio_agent.database import PortfolioDatabase, ReadOnlyQueryError
from portfolio_agent.tools import ExposureCalculator, SQLQueryTool

logger = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 500
MAX_PORTFOLIO_NAME_CHARS = 100

# Fixed table list used only for the "rows in the sample dataset" figure.
_DATASET_TABLES = (
    "portfolios", "holdings", "securities", "sectors", "benchmarks",
    "transactions", "historical_prices", "portfolio_performance", "risk_metrics",
)

_PORTFOLIOS_SQL = (
    "SELECT portfolio_id, portfolio_name, strategy_type, target_risk_level, status, total_aum "
    "FROM portfolios ORDER BY portfolio_id"
)
_PORTFOLIO_SQL = (
    "SELECT portfolio_id, portfolio_name, strategy_type, target_risk_level, status, total_aum "
    "FROM portfolios WHERE portfolio_id = ?"
)
_HOLDINGS_SQL = (
    "SELECT s.symbol, s.company_name, s.asset_type, sec.sector_name AS sector, "
    "h.quantity, h.cost_basis, s.current_price, h.current_weight "
    "FROM holdings h "
    "JOIN securities s ON s.security_id = h.security_id "
    "LEFT JOIN sectors sec ON sec.sector_id = s.sector_id "
    "WHERE h.portfolio_id = ? ORDER BY h.cost_basis DESC, h.holding_id"
)
_PERFORMANCE_SQL = (
    "SELECT performance_date, nav, total_return_1m, total_return_3m, total_return_6m, "
    "total_return_1y, volatility, sharpe_ratio, max_drawdown "
    "FROM portfolio_performance WHERE portfolio_id = ? ORDER BY performance_date"
)
_RISK_SQL = (
    "SELECT calculation_date, var_95, var_99, cvar_95, beta, correlation_sp500, "
    "tracking_error, information_ratio, sortino_ratio "
    "FROM risk_metrics WHERE portfolio_id = ? ORDER BY calculation_date"
)


class QuestionValidationError(ValueError):
    """Raised for an empty or oversized question before it reaches the agent."""


class PortfolioService:
    """Thread-safe facade over the existing agent for the local web interface."""

    def __init__(self, db_path: Optional[Union[str, Path]] = None, use_llm: bool = True, use_intent_classifier: Optional[bool] = None) -> None:
        self.db = PortfolioDatabase(db_path)
        self.use_llm = use_llm
        self.sql_tool = SQLQueryTool(db=self.db, use_gemini=use_llm)
        self.exposure_tool = ExposureCalculator(db=self.db)
        extra = {} if use_intent_classifier is None else {"intent_classifier": build_default_classifier() if use_intent_classifier else None}
        self.agent = PortfolioAgent(db=self.db, sql_tool=self.sql_tool, exposure_tool=self.exposure_tool, **extra)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ status

    def health(self) -> Dict[str, Any]:
        """Non-sensitive status: booleans and a row count only (never keys or paths)."""
        with self._lock:
            ready = self.db.is_initialized()
            total = None
            if ready:
                try:
                    total = sum(
                        int(self.db.execute_read_only(f"SELECT COUNT(*) AS n FROM {t}")["rows"][0]["n"])
                        for t in _DATASET_TABLES
                    )
                except ReadOnlyQueryError:
                    total = None
        generator = getattr(self.sql_tool, "gemini_generator", None)
        return {
            "status": "ok" if ready else "database_not_initialised",
            "database_ready": ready,
            "dataset_rows": total,
            "llm_enabled": bool(self.use_llm),
            "gemini_configured": bool(self.use_llm and generator is not None and generator.is_configured),
        }

    # ------------------------------------------------------------------ agent

    @staticmethod
    def _clean(text: str, limit: int) -> str:
        cleaned = (text or "").strip()
        if not cleaned:
            raise QuestionValidationError("Value cannot be empty.")
        if len(cleaned) > limit:
            raise QuestionValidationError(f"Value is too long (maximum {limit} characters).")
        return cleaned

    def ask(self, question: str) -> Dict[str, Any]:
        """Run the existing PortfolioAgent. The response contract is returned verbatim."""
        question = self._clean(question, MAX_QUESTION_CHARS)
        try:
            with self._lock:
                return self.agent.answer_question(question)
        except Exception:  # defensive: the agent normally returns status='error' itself
            logger.exception("PortfolioAgent raised unexpectedly")
            return {
                "status": "error",
                "question": question,
                "selected_tool": None,
                "route_reason": "The agent could not process this question.",
                "result": None,
                "error": "The agent failed to process this question.",
            }

    def exposure(self, portfolio_name: str) -> Dict[str, Any]:
        """Run the existing deterministic ExposureCalculator (result returned verbatim)."""
        name = self._clean(portfolio_name, MAX_PORTFOLIO_NAME_CHARS)
        with self._lock:
            result = self.exposure_tool.calculate(name)
            return apply_verification(result, FinancialVerifier(self.db).verify(result))

    def scenario(self, portfolio_name: str, scenario_id: Optional[str] = None, market_shock_pct: Any = None, sector_shocks: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Deterministic, independently verified scenario (hypothetical; read-only; Gemini is never involved)."""
        name = self._clean(portfolio_name, MAX_PORTFOLIO_NAME_CHARS)
        with self._lock:
            return ScenarioTool(self.db, self.exposure_tool, ScenarioVerifier(self.db)).run(name, scenario_id, market_shock_pct, sector_shocks)

    def scenario_options(self) -> Dict[str, Any]:
        """Static choices for the UI: predefined scenarios, valid sector names and the shock limits."""
        with self._lock:
            sectors = [r["sector_name"] for r in self.db.execute_read_only("SELECT sector_name FROM sectors ORDER BY sector_name")["rows"]]
        return {"scenarios": ScenarioTool.list_scenarios(), "sectors": sectors, "shock_limits_pct": {"min": float(SHOCK_MIN), "max": float(SHOCK_MAX)}}

    # ------------------------------------------------------------------ read-only views

    def _rows(self, sql: str, params: Optional[tuple] = None) -> List[Dict[str, Any]]:
        return self.db.execute_read_only(sql, params)["rows"]

    def portfolios(self) -> List[Dict[str, Any]]:
        with self._lock:
            return self._rows(_PORTFOLIOS_SQL)

    def portfolio_detail(self, portfolio_id: int) -> Optional[Dict[str, Any]]:
        """Portfolio row + holdings + performance + risk records, or None if not found."""
        with self._lock:
            found = self._rows(_PORTFOLIO_SQL, (portfolio_id,))
            if not found:
                return None
            return {
                "portfolio": found[0],
                "holdings": self._rows(_HOLDINGS_SQL, (portfolio_id,)),
                "performance": self._rows(_PERFORMANCE_SQL, (portfolio_id,)),
                "risk": self._rows(_RISK_SQL, (portfolio_id,)),
            }

    def close(self) -> None:
        with self._lock:
            self.db.close()
