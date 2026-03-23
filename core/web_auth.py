"""
HTTP Basic Auth для веб-панели (логин/пароль из .env).
"""

from __future__ import annotations

import base64
import secrets
from collections.abc import Awaitable, Callable

import config
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


def _unauthorized() -> Response:
    return Response(
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="ReUpload Detector"'},
    )


def _basic_auth_exempt_path(path: str) -> bool:
    """Статика и публичные API без Basic (основная защита — cookie-сессия)."""
    p = path.rstrip("/") or "/"
    if not p.startswith("/api"):
        return True
    if config.WEB_AUTH_ALLOW_HEALTH and p == "/api/health":
        return True
    if p in ("/api/auth/register", "/api/auth/login", "/api/me", "/api/config"):
        return True
    if p.startswith("/api/admin"):
        return True
    return False


class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if not config.web_auth_enabled():
            return await call_next(request)
        if request.method == "OPTIONS":
            return await call_next(request)
        if _basic_auth_exempt_path(request.url.path):
            return await call_next(request)
        auth = request.headers.get("authorization")
        if not auth or not auth.lower().startswith("basic "):
            return _unauthorized()
        try:
            raw = base64.b64decode(auth.split(" ", 1)[1].strip()).decode("utf-8")
            user, sep, pwd = raw.partition(":")
            if not sep:
                return _unauthorized()
        except Exception:
            return _unauthorized()
        if not secrets.compare_digest(user, config.WEB_AUTH_USER) or not secrets.compare_digest(
            pwd, config.WEB_AUTH_PASSWORD
        ):
            return _unauthorized()
        return await call_next(request)
