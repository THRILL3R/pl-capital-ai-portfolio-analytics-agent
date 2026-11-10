"""
FastAPI application: a thin, local-only HTTP adapter over the existing Python agent.

Design rules
------------
* Local, single-user, no authentication -> bind to loopback only (enforced in __main__)
  and reject unexpected ``Host`` headers (DNS-rebinding protection).
* Optional shared reviewer access (``portfolio_api.auth``): when REVIEWER_ACCESS_CODE and SESSION_SECRET are set, every data
  endpoint requires a signed session cookie and the API itself (not just the page) answers 401 without one. The hosted
  entry point (vercel_app.py) makes this mandatory.
* No endpoint returns environment values or keys.
* Agent and calculator responses are returned verbatim (their existing contracts).
* No CORS: the Vite dev server proxies ``/api``; in production this app serves the built
  frontend itself, so the browser only ever talks to one origin.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import DISABLED, ENABLED, MISCONFIGURED, AuthManager, client_key
from .evaluation_reports import load_evaluation
from .limits import RequestLimits, store_from_env
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


class LoginRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=256)


FAILED_LOGIN_DELAY_SECONDS = 0.4


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
    allowed_hosts: Optional[List[str]] = None,
    auth: Optional[AuthManager] = None,
    store: Any = None,
    limits: Optional[RequestLimits] = None,
    public_limits: bool = False,
) -> FastAPI:
    """Build the adapter. ``allowed_hosts`` defaults to loopback only; a hosted deployment passes its own hostnames.

    ``auth`` defaults to access control read from the environment (off when REVIEWER_ACCESS_CODE and SESSION_SECRET are unset).
    ``public_limits`` is for a deliberately open hosted demo (no access code): requests are then limited per visitor address
    and ``limits.ask_per_day`` becomes a Gemini budget that, once spent, switches questions to the deterministic generator.
    """
    svc = service or PortfolioService()
    store = store if store is not None else store_from_env(os.environ)
    auth = auth or AuthManager.from_env(os.environ, allowed_origin_hosts=allowed_hosts or ALLOWED_HOSTS, revocations=store)
    limits = limits or RequestLimits.from_env(os.environ)
    root = Path(project_root) if project_root else PROJECT_ROOT
    dist = Path(static_dir) if static_dir is not None else DEFAULT_STATIC_DIR

    app = FastAPI(
        title="PL Capital Portfolio Analytics (local adapter)",
        docs_url=None, redoc_url=None, openapi_url=None,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts) if allowed_hosts is not None else ALLOWED_HOSTS)

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

    def too_many(retry_after: int, message: str = "Too many requests. Please wait a moment and try again.") -> HTTPException:
        return HTTPException(status_code=429, detail=message, headers={"Retry-After": str(max(1, retry_after))})

    def protected(request: Request) -> Optional[Dict[str, Any]]:
        """Every data endpoint depends on this: a valid session is required, in the API itself."""
        return auth.authorize(request)

    def session_limited(scope: str, per_minute: int):
        def dependency(request: Request, session: Optional[Dict[str, Any]] = Depends(protected)) -> None:
            if auth.mode == ENABLED and session is not None:
                key = f"{scope}:{session['sid']}"
            elif public_limits and auth.mode == DISABLED:
                key = f"{scope}:client:{client_key(request)}"   # open demo: one budget per visitor address
            else:
                return  # access control off (local use): behaviour unchanged
            decision = store.hit(key, per_minute, 60)
            if not decision.allowed:
                raise too_many(decision.retry_after)
        return dependency

    def ask_limited(session: Optional[Dict[str, Any]] = Depends(session_limited("ask", limits.ask_per_minute))) -> None:
        if auth.mode == ENABLED and limits.ask_per_day:
            decision = store.hit("ask:day:" + time.strftime("%Y%m%d", time.gmtime()), limits.ask_per_day, 86400)
            if not decision.allowed:
                raise too_many(decision.retry_after, "The daily question limit for this demo has been reached.")

    api_limited = session_limited("api", limits.api_per_minute)

    # --- public: access control (the minimum an unauthenticated visitor can reach)

    @app.get("/api/auth/session")
    def auth_session(request: Request) -> Dict[str, Any]:
        if auth.mode == DISABLED:
            return {"auth_required": False, "authenticated": True}
        return {"auth_required": True, "authenticated": auth.mode == ENABLED and auth.peek(request) is not None}

    @app.post("/api/auth/login")
    def auth_login(body: LoginRequest, request: Request, response: Response) -> Dict[str, Any]:
        if auth.mode == DISABLED:
            return {"authenticated": True, "auth_required": False}
        if auth.mode == MISCONFIGURED:
            raise HTTPException(status_code=503, detail="Access is temporarily unavailable.")
        auth.check_origin(request)
        client = client_key(request)
        for key, limit in ((f"login:client:{client}", limits.login_attempts), ("login:all", limits.login_global)):
            decision = store.hit(key, limit, limits.login_window)
            if not decision.allowed:
                raise too_many(decision.retry_after, "Too many attempts. Please try again later.")
        if not auth.verify_code(body.code):
            time.sleep(FAILED_LOGIN_DELAY_SECONDS)  # a small fixed cost per wrong guess
            raise HTTPException(status_code=401, detail="Invalid access code.")
        store.reset(f"login:client:{client}")
        auth.set_cookie(response, request, auth.issue_token())
        return {"authenticated": True, "auth_required": True, "expires_in": auth.ttl}

    @app.post("/api/auth/logout")
    def auth_logout(request: Request, response: Response) -> Dict[str, Any]:
        if auth.mode == ENABLED:
            auth.check_origin(request)
            auth.revoke(auth.peek(request))
        auth.clear_cookie(response, request)
        return {"authenticated": False}

    @app.get("/api/health")
    def health(request: Request) -> Dict[str, Any]:
        # Minimal and public so the host can check liveness; the dataset size and Gemini status need a session.
        if auth.mode == DISABLED or (auth.mode == ENABLED and auth.peek(request) is not None):
            return svc.health()
        return {"status": "ok"}

    # --- protected: all application data

    @app.post("/api/ask", dependencies=[Depends(ask_limited)])
    def ask(body: AskRequest) -> Dict[str, Any]:
        if public_limits and auth.mode == DISABLED and limits.ask_per_day:
            # Open demo: the daily Gemini budget degrades gracefully. Once spent, questions are still answered, by the
            # deterministic generator, so a burst of traffic can never lock reviewers out.
            budget = store.hit("ask:day:" + time.strftime("%Y%m%d", time.gmtime()), limits.ask_per_day, 86400)
            if not budget.allowed:
                return svc.ask(body.question, allow_gemini=False)
        return svc.ask(body.question)

    @app.post("/api/exposure", dependencies=[Depends(api_limited)])
    def exposure(body: ExposureRequest) -> Dict[str, Any]:
        return svc.exposure(body.portfolio_name)

    @app.post("/api/scenario", dependencies=[Depends(api_limited)])
    def scenario(body: ScenarioRequest) -> Dict[str, Any]:
        return svc.scenario(body.portfolio_name, body.scenario_id, body.market_shock_pct, body.sector_shocks)

    @app.get("/api/scenarios", dependencies=[Depends(api_limited)])
    def scenarios() -> Dict[str, Any]:
        return svc.scenario_options()

    @app.get("/api/portfolios", dependencies=[Depends(api_limited)])
    def portfolios() -> List[Dict[str, Any]]:
        return svc.portfolios()

    @app.get("/api/portfolios/{portfolio_id}", dependencies=[Depends(api_limited)])
    def portfolio_detail(portfolio_id: int) -> Dict[str, Any]:
        detail = svc.portfolio_detail(portfolio_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Portfolio not found.")
        return detail

    @app.get("/api/evaluation", dependencies=[Depends(api_limited)])
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
    app.state.auth = auth
    app.state.limits = limits
    return app
