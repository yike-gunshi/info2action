"""Dual-mode authentication middleware.

Supports both:
1. AUTH_TOKEN (legacy, backward-compatible) — cookie/header/query param
2. JWT (P2 user auth) — access_token HttpOnly cookie

When JWT mode is active (users table has records), JWT takes priority.
AUTH_TOKEN still works as a fallback for backward compatibility during transition.
"""
import os
import re
from datetime import datetime, timedelta, timezone

from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse

import db
import remote_db

_AUTH_TOKEN = os.environ.get('AUTH_TOKEN', '')

_AUTH_SKIP_EXTS = ('.css', '.js', '.svg', '.png', '.ico', '.woff', '.woff2',
                   '.jpg', '.jpeg', '.webp', '.gif', '.avif', '.map')
_AUTH_SKIP_PATHS = frozenset((
    '/favicon.ico', '/manifest.json', '/sw.js',
    '/api/auth/login', '/api/auth/register', '/api/auth/refresh',
    '/api/auth/register-config',
    '/api/auth/send-verification', '/api/auth/verify-email',
    '/api/auth/resend-code',
    '/api/auth/forgot-password', '/api/auth/reset-password',
    '/api/auth/google', '/api/auth/google/callback',
))

# Public API paths: allow anonymous access, but still attach user if JWT present.
# Keep exact paths separate from subtree prefixes to avoid /api/feed matching
# /api/feedback or /api/health matching privileged sub-routes.
_PUBLIC_API_EXACT_PATHS = frozenset((
    '/api/stats',       # aggregate stats
    '/api/trends',      # keyword trends
    '/api/health',      # system health read only
    '/api/status',      # anonymous status writes route-level no-op safely
    '/api/fetch/status',  # read-only fetch progress for public dashboards
    '/api/classification',  # category list
    '/api/topics',      # topic list
    '/api/search',      # public remote-only search; route filters private/manual data
    '/api/auth/me',     # returns 401 if not logged in (handled by route)
    '/api/actions/by-item',  # route returns [] for anonymous users
    '/api/lingowhale/groups',
    # v17.0: 精选 tab 公开可见,搜索也允许匿名（与 /api/feed/events 一致）;
    # route 内根据 context 分支处理 channel/collection/history 仍要登录
    '/api/search',
))

_PUBLIC_API_PREFIXES = (
    '/api/feed',        # feed listing, sections, item detail
    # v12.2 Round 2 媒体代理(Twitter CDN Referer 绕行):匿名可看视频/封面
    '/api/media',
    # v12.2/v12.3 ASR:GET stream/status 匿名可观察,POST 在路由内 requireAuth
    '/api/items',
)

_LOGIN_HTML = (
    '<!DOCTYPE html><html><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    '<title>Login</title><style>'
    'body{margin:0;height:100vh;display:flex;align-items:center;'
    'justify-content:center;font-family:system-ui,sans-serif;'
    'background:#f5f5f5;color:#333}'
    '@media(prefers-color-scheme:dark){body{background:#1a1a1a;color:#e0e0e0}'
    'input{background:#2a2a2a;color:#e0e0e0;border-color:#555}'
    'button{background:#4a9eff;color:#fff}}'
    '.box{text-align:center}'
    'input{padding:10px 14px;font-size:16px;border:1px solid #ccc;'
    'border-radius:6px;width:260px;margin-bottom:12px;display:block}'
    'button{padding:10px 28px;font-size:16px;border:none;border-radius:6px;'
    'background:#0066cc;color:#fff;cursor:pointer}'
    'button:hover{opacity:.85}'
    '</style></head><body>'
    '<div class="box"><h2>Info Radar</h2>'
    '<form onsubmit="go(event)"><input id="t" type="password" '
    'placeholder="Enter token" autofocus>'
    '<button type="submit">Login</button></form></div>'
    '<script>function go(e){e.preventDefault();var t=document.getElementById("t").value;'
    'if(!t)return;var u=new URL(location.href);u.searchParams.set("token",t);'
    'location.href=u.toString()}</script>'
    '</body></html>'
)


def _should_skip_auth(path: str) -> bool:
    """Check if path should skip authentication entirely (no user attachment)."""
    if any(path.endswith(ext) for ext in _AUTH_SKIP_EXTS):
        return True
    if path in _AUTH_SKIP_PATHS:
        return True
    if re.match(r'^/icon-.*\.svg$', path):
        return True
    if path.startswith('/assets/'):
        return True
    return False


def _is_public_cluster_api(path: str, method: str) -> bool:
    """Public cluster surface; user-specific writes become route-level no-ops."""
    if method == 'GET':
        if re.fullmatch(r'/api/clusters/\d+', path):
            return True
        if re.fullmatch(r'/api/clusters/\d+/sources', path):
            return True
        if re.fullmatch(r'/api/clusters/\d+/bundle', path):
            return True
        if re.fullmatch(r'/api/clusters/\d+/actions', path):
            return True
    if method == 'POST':
        if re.fullmatch(r'/api/clusters/\d+/(click|seen)', path):
            return True
    return False


def _is_public_api(path: str, method: str = 'GET') -> bool:
    """Check if path is a public API (anonymous allowed, but user attached if present)."""
    # Non-API paths are public (SPA fallback, images, lingowhale articles)
    if not path.startswith('/api/'):
        return True
    if path in _PUBLIC_API_EXACT_PATHS:
        return True
    if _is_public_cluster_api(path, method):
        return True
    return any(path == prefix or path.startswith(prefix + '/') for prefix in _PUBLIC_API_PREFIXES)


def _make_auth_cookie(host: str) -> str:
    """Build Set-Cookie header value for legacy auth token."""
    expires = datetime.now(timezone.utc) + timedelta(days=7)
    exp_str = expires.strftime('%a, %d %b %Y %H:%M:%S GMT')
    domain_part = ''
    if host and not host.replace('.', '').replace(':', '').isdigit():
        domain_part = f'Domain={host.split(":")[0]}; '
    return (
        f'auth_token={_AUTH_TOKEN}; Path=/; {domain_part}HttpOnly; '
        f'SameSite=Lax; Expires={exp_str}'
    )


def _try_jwt_auth(request: Request) -> dict | None:
    """Try to authenticate via JWT access_token cookie. Returns user dict or None."""
    access_token = request.cookies.get('access_token')
    if not access_token:
        return None

    # Lazy import to avoid circular deps at module level
    from routes.auth import decode_access_token

    payload = decode_access_token(access_token)
    if not payload:
        return None

    # Verify session not revoked
    if remote_db.app_state_to_remote():
        return remote_db.get_user_for_session_remote(payload.get('jti', ''), payload['sub'])

    conn = db.get_conn()
    try:
        session = db.get_session(conn, payload.get('jti', ''))
        if not session:
            return None

        user = db.get_user(conn, payload['sub'])
        return user
    finally:
        conn.close()


def _try_legacy_auth(request: Request) -> bool:
    """Try AUTH_TOKEN authentication. Returns True if authenticated."""
    if not _AUTH_TOKEN:
        return True  # auth disabled

    # URL query param
    if request.query_params.get('token') == _AUTH_TOKEN:
        return True

    # Authorization header
    auth_header = request.headers.get('authorization', '')
    if auth_header.startswith('Bearer ') and auth_header[7:] == _AUTH_TOKEN:
        return True

    # Cookie
    if request.cookies.get('auth_token') == _AUTH_TOKEN:
        return True

    return False


class AuthTokenMiddleware(BaseHTTPMiddleware):
    """Dual-mode auth: JWT first, then AUTH_TOKEN fallback."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Skip auth entirely for static assets and auth endpoints
        if _should_skip_auth(path):
            return await call_next(request)

        # Initialize request state
        request.state.user = None
        request.state.legacy_authenticated = False
        set_legacy_cookie = False

        # 1) Try JWT authentication
        # BE-2(C 端放量梳理): session 校验在 60s 缓存 miss 时是一条远程
        # 3 表 JOIN;本中间件覆盖所有端点,必须离开事件循环,否则每个登录
        # 用户每分钟一次全站级阻塞。
        user = await run_in_threadpool(_try_jwt_auth, request)
        if user:
            request.state.user = user
            return await call_next(request)

        # 2) Try legacy AUTH_TOKEN
        if _AUTH_TOKEN:
            token_param = request.query_params.get('token')
            if token_param == _AUTH_TOKEN:
                set_legacy_cookie = True

            if _try_legacy_auth(request):
                request.state.legacy_authenticated = True
                response = await call_next(request)
                if set_legacy_cookie:
                    host = request.headers.get('host', '')
                    response.headers.append('Set-Cookie', _make_auth_cookie(host))
                return response

        # 3) Public API paths — allow anonymous access (user is None)
        if _is_public_api(path, request.method):
            return await call_next(request)

        # Not authenticated — protected endpoint
        return HTMLResponse(content=_LOGIN_HTML, status_code=401)
