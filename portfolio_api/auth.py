"""
Shared reviewer access for the hosted API.

How it works
------------
* ``REVIEWER_ACCESS_CODE`` is the shared code reviewers type into the access screen. It lives only in server-side
  configuration. It is compared in constant time (SHA-256 digests with ``hmac.compare_digest``) and never returned.
* A successful login issues a **signed session token** in an ``HttpOnly`` cookie. The token is
  ``base64url(payload).base64url(HMAC-SHA256(SESSION_SECRET, payload))`` and carries an expiry, a random session id and a
  keyed fingerprint of the access code (so rotating the code invalidates existing sessions). ``SESSION_SECRET`` is a
  separate secret: it is never the access code and never the Gemini key.
* The cookie is ``HttpOnly``, ``SameSite=Strict``, ``Path=/``, expires with the session and is ``Secure`` on HTTPS
  (always on Vercel). Nothing is stored in the browser's ``localStorage`` or ``sessionStorage``.
* Sessions are stateless, so they work across serverless instances. Signing out clears the cookie and records the session id
  in the revocation list of ``limits``; with the optional shared Redis store that revocation holds on every instance, without
  it only on the instance that handled the sign-out (a copied cookie otherwise stays valid until it expires).

Modes
-----
``disabled``       neither variable is set and access control is not required (local development): behaviour is unchanged.
``enabled``        both are set and valid.
``misconfigured``  anything else (only one set, too short, reused as another secret, or required but missing). Protected
                   endpoints then answer 503 and never serve data: the module fails closed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Any, Callable, Dict, Iterable, Mapping, Optional
from urllib.parse import urlparse

from fastapi import HTTPException, Request, Response

logger = logging.getLogger(__name__)

COOKIE_NAME = "pl_session"
TOKEN_VERSION = 1
MIN_CODE_LENGTH = 12
MIN_SECRET_LENGTH = 32
DEFAULT_TTL_SECONDS = 2 * 60 * 60
MIN_TTL_SECONDS = 5 * 60
MAX_TTL_SECONDS = 24 * 60 * 60
CLOCK_SKEW_SECONDS = 60

DISABLED, ENABLED, MISCONFIGURED = "disabled", "enabled", "misconfigured"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _host_matches(host: str, allowed: Iterable[str]) -> bool:
    host = host.lower()
    for pattern in allowed:
        pattern = pattern.lower().split(":")[0]
        if pattern == host or (pattern.startswith("*.") and host.endswith(pattern[1:])):
            return True
    return False


class AuthManager:
    """Validates the access code, issues and verifies signed sessions, and guards requests."""

    def __init__(
        self,
        access_code: Optional[str] = None,
        session_secret: Optional[str] = None,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        required: bool = False,
        forbidden_secrets: Iterable[str] = (),
        cookie_secure: Optional[bool] = None,
        allowed_origin_hosts: Iterable[str] = ("127.0.0.1", "localhost", "testserver"),
        revocations: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        self._origin_hosts = list(allowed_origin_hosts)
        self._cookie_secure = cookie_secure
        self.ttl = min(MAX_TTL_SECONDS, max(MIN_TTL_SECONDS, int(ttl_seconds)))
        self._revocations = revocations
        self._code_digest = b""
        self._key = b""
        self._code_tag = ""
        self.mode = DISABLED
        self.problem: Optional[str] = None  # a reason without any secret value, for server logs only

        code = (access_code or "").strip()
        secret = (session_secret or "").strip()
        if not code and not secret:
            if required:
                self._fail("REVIEWER_ACCESS_CODE and SESSION_SECRET are required but not set.")
            return
        if not code or not secret:
            self._fail("REVIEWER_ACCESS_CODE and SESSION_SECRET must be set together.")
            return
        if len(code) < MIN_CODE_LENGTH:
            self._fail(f"REVIEWER_ACCESS_CODE must be at least {MIN_CODE_LENGTH} characters.")
            return
        if len(secret) < MIN_SECRET_LENGTH:
            self._fail(f"SESSION_SECRET must be at least {MIN_SECRET_LENGTH} characters.")
            return
        if secret == code:
            self._fail("SESSION_SECRET must differ from REVIEWER_ACCESS_CODE.")
            return
        if any(secret == other.strip() or code == other.strip() for other in forbidden_secrets if other and other.strip()):
            self._fail("SESSION_SECRET and REVIEWER_ACCESS_CODE must not reuse the Gemini API key.")
            return
        self._code_digest = hashlib.sha256(code.encode("utf-8")).digest()
        self._key = secret.encode("utf-8")
        self._code_tag = hmac.new(self._key, b"code-fingerprint:" + code.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
        self.mode = ENABLED

    # ------------------------------------------------------------------ construction

    def _fail(self, reason: str) -> None:
        self.mode = MISCONFIGURED
        self.problem = reason
        logger.error("Access control is misconfigured: %s Protected endpoints will refuse all requests.", reason)

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str],
        *,
        required: bool = False,
        allowed_origin_hosts: Iterable[str] = ("127.0.0.1", "localhost", "testserver"),
        revocations: Any = None,
    ) -> "AuthManager":
        try:
            ttl = int(str(env.get("PORTFOLIO_SESSION_TTL_SECONDS", "")).strip() or DEFAULT_TTL_SECONDS)
        except ValueError:
            ttl = DEFAULT_TTL_SECONDS
        forced = (env.get("PORTFOLIO_COOKIE_SECURE") or "").strip().lower()
        secure: Optional[bool] = True if forced in {"1", "true", "yes"} else False if forced in {"0", "false", "no"} else None
        if secure is None and env.get("VERCEL"):
            secure = True
        return cls(
            env.get("REVIEWER_ACCESS_CODE"),
            env.get("SESSION_SECRET"),
            ttl_seconds=ttl,
            required=required,
            forbidden_secrets=[env.get("GEMINI_API_KEY", ""), env.get("GOOGLE_API_KEY", "")],
            cookie_secure=secure,
            allowed_origin_hosts=allowed_origin_hosts,
            revocations=revocations,
        )

    # ------------------------------------------------------------------ access code and tokens

    def verify_code(self, candidate: str) -> bool:
        """Constant-time check of the access code (digests have equal length whatever the input)."""
        if self.mode != ENABLED:
            return False
        return hmac.compare_digest(hashlib.sha256(candidate.encode("utf-8")).digest(), self._code_digest)

    def _sign(self, payload_b64: str) -> str:
        return _b64(hmac.new(self._key, b"pl-session.v1." + payload_b64.encode("ascii"), hashlib.sha256).digest())

    def issue_token(self) -> str:
        now = int(self._clock())
        payload = {"v": TOKEN_VERSION, "sid": secrets.token_hex(16), "iat": now, "exp": now + self.ttl, "ck": self._code_tag}
        payload_b64 = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        return f"{payload_b64}.{self._sign(payload_b64)}"

    def verify_token(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        """The decoded session if the token is authentic, current and not signed out; otherwise None."""
        if self.mode != ENABLED or not token or token.count(".") != 1 or len(token) > 1024:
            return None
        payload_b64, signature = token.split(".")
        if not hmac.compare_digest(signature.encode("ascii", "ignore"), self._sign(payload_b64).encode("ascii")):
            return None
        try:
            payload = json.loads(_unb64(payload_b64))
        except (ValueError, binascii.Error, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("v") != TOKEN_VERSION:
            return None
        now = self._clock()
        try:
            expires, issued = float(payload["exp"]), float(payload["iat"])
        except (KeyError, TypeError, ValueError):
            return None
        if expires <= now or issued > now + CLOCK_SKEW_SECONDS:
            return None
        if not hmac.compare_digest(str(payload.get("ck", "")).encode("utf-8"), self._code_tag.encode("utf-8")):
            return None  # the access code was rotated
        sid = payload.get("sid")
        if not isinstance(sid, str) or not sid:
            return None
        if self._revocations is not None and self._revocations.is_marked(f"revoked:{sid}"):
            return None
        return payload

    def revoke(self, session: Optional[Dict[str, Any]]) -> None:
        if session and self._revocations is not None:
            remaining = int(float(session.get("exp", 0)) - self._clock())
            if remaining > 0:
                self._revocations.mark(f"revoked:{session['sid']}", remaining)

    # ------------------------------------------------------------------ request handling

    def peek(self, request: Request) -> Optional[Dict[str, Any]]:
        """The current session, or None. Never raises."""
        return self.verify_token(request.cookies.get(COOKIE_NAME))

    def check_origin(self, request: Request) -> None:
        """Refuse state-changing requests that a browser marks as coming from another site."""
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return
        origin = request.headers.get("origin")
        if origin:
            host = urlparse(origin).hostname or ""
            if not host or not _host_matches(host, self._origin_hosts):
                raise HTTPException(status_code=403, detail="Cross-origin request refused.")
        elif request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            raise HTTPException(status_code=403, detail="Cross-origin request refused.")

    def authorize(self, request: Request) -> Optional[Dict[str, Any]]:
        """FastAPI guard: the session for an authenticated request, ``None`` when access control is off; raises otherwise."""
        if self.mode == DISABLED:
            return None
        if self.mode == MISCONFIGURED:
            raise HTTPException(status_code=503, detail="Access control is not configured.")
        self.check_origin(request)
        session = self.peek(request)
        if session is None:
            raise HTTPException(status_code=401, detail="Authentication required.")
        request.state.session = session
        return session

    # ------------------------------------------------------------------ cookies

    def _secure(self, request: Request) -> bool:
        if self._cookie_secure is not None:
            return self._cookie_secure
        forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        return request.url.scheme == "https" or forwarded == "https"

    def set_cookie(self, response: Response, request: Request, token: str) -> None:
        response.set_cookie(
            COOKIE_NAME, token, max_age=self.ttl, path="/", httponly=True, secure=self._secure(request), samesite="strict",
        )

    def clear_cookie(self, response: Response, request: Request) -> None:
        response.set_cookie(
            COOKIE_NAME, "", max_age=0, expires=0, path="/", httponly=True, secure=self._secure(request), samesite="strict",
        )


def client_key(request: Request) -> str:
    """A non-reversible per-client key for limits (never the raw address)."""
    address = (
        request.headers.get("x-vercel-forwarded-for")
        or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        or (request.client.host if request.client else "")
        or "unknown"
    )
    return hashlib.sha256(address.encode("utf-8")).hexdigest()[:16]
