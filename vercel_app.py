"""
Vercel entry point for the FastAPI adapter (``"entrypoint": "vercel_app:app"`` in vercel.json).

Vercel needs a module-level ``app``; the project's own entry point is the factory
``portfolio_api.app.create_app``, so this file only wires it up for a hosted environment:

* **Two access modes.** By default reviewer access is mandatory: ``REVIEWER_ACCESS_CODE`` and ``SESSION_SECRET`` must be set
  (server-side), and if they are missing or invalid every data endpoint answers 503 and nothing is served (fail closed).
  ``PORTFOLIO_OPEN_ACCESS=1`` is an explicit opt-out for a demo that needs no code: any configured code is then ignored, and
  every visitor is rate-limited by address.
* **Gemini is OFF unless explicitly enabled AND the deployment is protected.** ``PORTFOLIO_USE_LLM=1`` plus ``GEMINI_API_KEY`` turn
  it on; if access control is neither active nor deliberately opened (``PORTFOLIO_OPEN_ACCESS=1``) the flag is ignored and an
  error is logged, so a key can never sit behind an open API by accident. In open mode Gemini always has a daily budget
  (``PORTFOLIO_ASK_PER_DAY``, default 300): once spent, questions are answered by the deterministic generator instead.
  The model comes from ``GEMINI_MODEL``. If Gemini fails, the deterministic generator answers.
* The SQLite database is built from the bundled schema and CSV files into the temporary directory on the first request of a
  cold instance (the deployed project directory should be treated as read-only). ``PORTFOLIO_DB_PATH`` overrides the location.
* The local semantic intent classifier is not used (its ~90 MB model is not part of the deployment), so routing falls back to
  the deterministic keyword rules.
* ``Host`` validation stays on: loopback names plus this deployment's own hostnames (Vercel's system variables) plus anything
  listed in ``PORTFOLIO_ALLOWED_HOSTS`` (comma-separated, e.g. a custom domain).
* Only ``/api/*`` is served here; the React frontend is a separate Vercel service.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import List, Mapping

from fastapi import FastAPI

from portfolio_agent.database import PortfolioDatabase
from portfolio_api.app import ALLOWED_HOSTS, create_app
from portfolio_api.auth import ENABLED, AuthManager
from portfolio_api.limits import RequestLimits, store_from_env
from portfolio_api.service import PortfolioService

logger = logging.getLogger("portfolio_api.vercel")

_VERCEL_HOST_VARIABLES = ("VERCEL_URL", "VERCEL_BRANCH_URL", "VERCEL_PROJECT_PRODUCTION_URL")
DEFAULT_OPEN_DAILY_BUDGET = 300   # Gemini questions per day in open mode, unless PORTFOLIO_ASK_PER_DAY says otherwise


def _flag(env: Mapping[str, str], name: str) -> bool:
    return str(env.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _allowed_hosts(env: Mapping[str, str]) -> List[str]:
    hosts = list(ALLOWED_HOSTS)
    for variable in _VERCEL_HOST_VARIABLES:
        value = str(env.get(variable, "")).strip()
        if value:
            hosts.append(value.split("/")[0])  # system variables hold a bare hostname; be tolerant of a scheme
    hosts.extend(h.strip() for h in str(env.get("PORTFOLIO_ALLOWED_HOSTS", "")).split(",") if h.strip())
    return list(dict.fromkeys(hosts))


def _build_service(env: Mapping[str, str], use_llm: bool) -> PortfolioService:
    db_path = Path(env.get("PORTFOLIO_DB_PATH") or Path(tempfile.gettempdir()) / "portfolio.db")
    PortfolioDatabase(db_path).initialize_database()  # builds from the bundled schema + CSVs; no-op when already built
    return PortfolioService(db_path=db_path, use_llm=use_llm, use_intent_classifier=False)


def build_app(env: Mapping[str, str] = os.environ) -> FastAPI:
    hosts = _allowed_hosts(env)
    store = store_from_env(env)
    open_access = _flag(env, "PORTFOLIO_OPEN_ACCESS")
    if open_access:
        auth = AuthManager(None, None, allowed_origin_hosts=hosts)   # deliberately open: any configured code is ignored
        logger.warning("PORTFOLIO_OPEN_ACCESS is set: no access code is required. Requests are limited per visitor address.")
    else:
        auth = AuthManager.from_env(env, required=True, allowed_origin_hosts=hosts, revocations=store)
    use_llm = _flag(env, "PORTFOLIO_USE_LLM")
    if use_llm and auth.mode != ENABLED and not open_access:
        logger.error("PORTFOLIO_USE_LLM is set but access control is not active: Gemini stays disabled.")
        use_llm = False
    limits = RequestLimits.from_env(env)
    if open_access and use_llm and not limits.ask_per_day:
        limits = replace(limits, ask_per_day=DEFAULT_OPEN_DAILY_BUDGET)   # an open demo never runs Gemini without a budget
    return create_app(
        _build_service(env, use_llm),
        allowed_hosts=hosts,
        auth=auth,
        store=store,
        limits=limits,
        public_limits=open_access,
        static_dir=Path(tempfile.gettempdir()) / "no-static-frontend",  # this service serves the API only
    )


app = build_app()
