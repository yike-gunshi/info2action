"""Tiny serialized-response cache for anonymous public GET bursts."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from starlette.responses import Response

_CACHE_TTL_SEC = 15
_MAX_ENTRIES = 64
_LOCK = threading.Lock()
_CACHE: dict[tuple[Any, ...], tuple[float, bytes]] = {}


def is_public_get_request(request: Any, *, public_only: bool) -> bool:
    return public_only and getattr(request, "method", None) == "GET"


def _response(body: bytes, *, ttl_sec: int, hit: bool) -> Response:
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Cache-Control": f"public, max-age={max(1, int(ttl_sec))}",
            "X-Info2Act-Response-Cache": "hit" if hit else "miss",
        },
    )


def get_public_json_response(key: tuple[Any, ...]) -> Response | None:
    now = time.monotonic()
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is None:
            return None
        expires_at, body = cached
        if expires_at <= now:
            _CACHE.pop(key, None)
            return None
        return _response(body, ttl_sec=int(expires_at - now), hit=True)


def set_public_json_response(
    key: tuple[Any, ...],
    payload: Any,
    *,
    ttl_sec: int = _CACHE_TTL_SEC,
) -> Response:
    body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    now = time.monotonic()
    expires_at = now + max(1, int(ttl_sec))
    with _LOCK:
        for cached_key, (cached_expires_at, _) in list(_CACHE.items()):
            if cached_expires_at <= now:
                _CACHE.pop(cached_key, None)
        if len(_CACHE) >= _MAX_ENTRIES:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = (expires_at, body)
    return _response(body, ttl_sec=ttl_sec, hit=False)


def clear_public_response_cache() -> None:
    with _LOCK:
        _CACHE.clear()
