"""Add basic HTTP security headers without overriding route-specific policies.

应用层统一补默认安全头,不依赖 nginx / Cloudflare 配置。路由已设置的头保持原值;
INFO2ACTION_SECURITY_HEADERS=0 / false / off 时整体关闭(大小写不敏感)。
"""
from __future__ import annotations

import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


_DEFAULT_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


def _security_headers_enabled() -> bool:
    raw = os.environ.get("INFO2ACTION_SECURITY_HEADERS", "").strip().lower()
    return raw not in {"0", "false", "off"}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self.enabled = _security_headers_enabled()

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if self.enabled:
            for name, value in _DEFAULT_HEADERS.items():
                response.headers.setdefault(name, value)
        return response
