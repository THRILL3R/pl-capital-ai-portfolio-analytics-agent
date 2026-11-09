"""
FastAPI application: a thin, local-only HTTP adapter over the existing Python agent.

Design rules
------------
* Local, single-user, no authentication -> bind to loopback only (enforced in __main__)
  and reject unexpected ``Host`` headers (DNS-rebinding protection).
* No endpoint returns environment values or keys.
* Agent and calculator responses are returned verbatim (their existing contracts).
* No CORS: the Vite dev server proxies ``/api``; in production this app serves the built
  frontend itself, so the browser only ever talks to one origin.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .evaluation_reports import load_evaluation
from .service import (
    MAX_PORTFOLIO_NAME_CHARS,
    MAX_QUESTION_CHARS,
    PortfolioService,
    QuestionValidationError,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATIC_DIR = PROJECT_ROOT / "frontend" / "dist"
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]


class AskRequest(BaseModel):
    question: str = Field(..., max_length=MAX_QUESTION_CHARS * 2)

    @field_validator("question")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question cannot be empty")
        return value


class ExposureRequest(BaseModel):
    portfolio_name: str = Field(..., max_length=MAX_PORTFOLIO_NAME_CHARS * 2)

    @field_validator("portfolio_name")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("portfolio_name cannot be empty")
        return value


class ScenarioRequest(BaseModel):
    portfolio_name: str = Field(..., max_length=MAX_PORTFOLIO_NAME_CHARS * 2)
    scenario_id: Optional[str] = Field(default=None, max_length=64)
    market_shock_pct: Optional[float] = Field(default=None, allow_inf_nan=False)
    sector_shocks: Optional[Dict[str, float]] = None

    @field_validator("portfolio_name")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("portfolio_name cannot be empty")
        return value

    @field_validator("sector_shocks")
    @classmethod
    def _small(cls, value: Optional[Dict[str, float]]) -> Optional[Dict[str, float]]:
        if value is not None and len(value) > 20:
            raise ValueError("too many sector shocks")
        return value


def create_app(
    service: Optional[PortfolioService] = None,
    *,
    static_dir: Optional[Path] = None,
    project_root: Optional[Path] = None,
) -> FastAPI:
    svc = service or PortfolioService()
    root = Path(project_root) if project_root else PROJECT_ROOT
    dist = Path(static_dir) if static_dir is not None else DEFAULT_STATIC_DIR

    app = FastAPI(
        title="PL Capital Portfolio Analytics (local adapter)",
        docs_url=None, redoc_url=None, openapi_url=None,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

    @app.middleware("http")
    async def _headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={"detail": "Invalid request."})

    @app.exception_handler(QuestionValidationError)
    async def _question_validation(_: Request, exc: QuestionValidationError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception):
        logger.exception("Unhandled error in request")
        return JSONResponse(status_code=500, content={"detail": "Internal error."})

    # ------------------------------------------------------------------ API

    @app.get("/api/health")
    def health() -> Dict[str, Any]:
        return svc.health()

    @app.post("/api/ask")
    def ask(body: AskRequest) -> Dict[str, Any]:
        return svc.ask(body.question)

    @app.post("/api/exposure")
    def exposure(body: ExposureRequest) -> Dict[str, Any]:
        return svc.exposure(body.portfolio_name)

    @app.post("/api/scenario")
    def scenario(body: ScenarioRequest) -> Dict[str, Any]:
        return svc.scenario(body.portfolio_name, body.scenario_id, body.market_shock_pct, body.sector_shocks)

    @app.get("/api/scenarios")
    def scenarios() -> Dict[str, Any]:
        return svc.scenario_options()

    @app.get("/api/portfolios")
    def portfolios() -> List[Dict[str, Any]]:
        return svc.portfolios()

    @app.get("/api/portfolios/{portfolio_id}")
    def portfolio_detail(portfolio_id: int) -> Dict[str, Any]:
        detail = svc.portfolio_detail(portfolio_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Portfolio not found.")
        return detail

    @app.get("/api/evaluation")
    def evaluation() -> Dict[str, Any]:
        return load_evaluation(root)

    # ------------------------------------------------------------------ built frontend (optional)

    index_file = dist / "index.html"
    if index_file.is_file():
        assets = dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str):
            if full_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="Not found.")
            candidate = (dist / full_path).resolve()
            if full_path and candidate.is_file() and dist.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(index_file)

    app.state.service = svc
    return app
