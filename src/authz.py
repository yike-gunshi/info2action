"""Shared authorization helpers for route-level gates."""

from fastapi import Request
from fastapi.responses import JSONResponse


def require_admin(request: Request):
    """Return an error response unless the request has admin-level access."""
    if getattr(request.state, 'legacy_authenticated', False):
        return None

    user = getattr(request.state, 'user', None)
    if not user:
        return JSONResponse({'error': 'Not authenticated'}, status_code=401)
    if user.get('role') != 'admin':
        return JSONResponse({'error': 'Admin access required'}, status_code=403)
    return None


def current_user_id(request: Request):
    """Return the authenticated JWT user's id, or None for legacy/anonymous requests."""
    user = getattr(request.state, 'user', None)
    return user.get('id') if user else None


def can_access_all(request: Request) -> bool:
    """Admin users and legacy AUTH_TOKEN callers can see legacy/global data."""
    if getattr(request.state, 'legacy_authenticated', False):
        return True
    user = getattr(request.state, 'user', None)
    return bool(user and user.get('role') == 'admin')


def owner_scope_user_id(request: Request):
    """Return a user_id filter for regular users; None means unrestricted scope."""
    if can_access_all(request):
        return None
    return current_user_id(request)
