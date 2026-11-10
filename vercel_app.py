"""
Vercel entry point for the FastAPI adapter (``"entrypoint": "vercel_app:app"`` in vercel.json).

Vercel needs a module-level ``app``; the project's own entry point is the factory
``portfolio_api.app.create_app``, so this file only wires it up for a hosted environment:

* The SQLite database is built from the bundled schema and CSV files into the temporary directory on
  the first request of a cold instance (the deployed project directory should be treated as read-only).
  ``PORTFOLIO_DB_PATH`` overrides the location.
* Gemini is OFF unless ``PORTFOLIO_USE_LLM`` is set to ``1``. The API has no authentication, so a public
  deployment with a key would let anyone spend that key's quota. With it off, SQL questions are answered by
  the deterministic rule-based generator, and exposure, hybrid and scenario analysis work as usual.
* The local semantic intent classifier is not used (its ~90 MB model is not part of the deployment), so
  routing falls back to the deterministic keyword rules.
* ``Host`` validation stays on: loopback names plus this deployment's own hostnames (Vercel's system
  variables) plus anything listed in ``PORTFOLIO_ALLOWED_HOSTS`` (comma-separated, e.g. a custom domain).
* Only ``/api/*`` is served here; the React frontend is a separate Vercel service.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import List

from portfolio_agent.database import PortfolioDatabase
from portfolio_api.app import ALLOWED_HOSTS, create_app
from portfolio_api.service import PortfolioService

_VERCEL_HOST_VARIABLES = ("VERCEL_URL", "VERCEL_BRANCH_URL", "VERCEL_PROJECT_PRODUCTION_URL")


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _allowed_hosts() -> List[str]:
    hosts = list(ALLOWED_HOSTS)
    for variable in _VERCEL_HOST_VARIABLES:
        value = os.environ.get(variable, "").strip()
        if value:
            hosts.append(value.split("/")[0])  # system variables hold a bare hostname; be tolerant of a scheme
    hosts.extend(h.strip() for h in os.environ.get("PORTFOLIO_ALLOWED_HOSTS", "").split(",") if h.strip())
    return list(dict.fromkeys(hosts))


def _build_service() -> PortfolioService:
    db_path = Path(os.environ.get("PORTFOLIO_DB_PATH") or Path(tempfile.gettempdir()) / "portfolio.db")
    PortfolioDatabase(db_path).initialize_database()  # builds from the bundled schema + CSVs; no-op when already built
    return PortfolioService(db_path=db_path, use_llm=_flag("PORTFOLIO_USE_LLM"), use_intent_classifier=False)


app = create_app(
    _build_service(),
    allowed_hosts=_allowed_hosts(),
    static_dir=Path(tempfile.gettempdir()) / "no-static-frontend",  # this service serves the API only
)
