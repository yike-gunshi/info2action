"""Cloudflare zone traffic (UV/PV) for the admin 总览.

Pulls daily unique visitors + page views from the Cloudflare GraphQL Analytics
API (`httpRequests1dGroups` dataset) so the admin console can show site-wide
traffic that *includes unregistered / anonymous visitors* — data the app's own
registered-user状态表 (item_status / cluster_status) cannot capture.

口径说明（免费套餐 API 限制，实测）:
- PV = `sum.pageViews`，可跨天累加，精确。
- UV 只能拿到"每天各自去重"的 `uniq.uniques`；免费套餐无法跨天再去重
  （`httpRequestsAdaptiveGroups` 被限制单次≤1天）。因此 UV 以"日均独立访客"
  （每日去重值取均值）呈现，而非会误导的跨天累加值。

Read-only. Degrades gracefully: if the token/zone are unconfigured or the
Cloudflare call fails, callers get ``{"available": False, ...}`` and the rest of
the console summary is unaffected. ``fetch_traffic_summary`` never raises.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

_CF_GRAPHQL_URL = "https://api.cloudflare.com/client/v4/graphql"
_TREND_DAYS = 30
_HTTP_TIMEOUT = 12  # seconds; keep well under the request's own budget
_CACHE_TTL_OK = 900  # 15 min — CF data updates on a multi-minute lag; don't hammer
_CACHE_TTL_ERR = 120  # 2 min — retry failures sooner without spamming

_QUERY = """
query ($zone: String!, $geq: String!, $leq: String!) {
  viewer {
    zones(filter: {zoneTag: $zone}) {
      httpRequests1dGroups(
        limit: 60
        filter: {date_geq: $geq, date_leq: $leq}
        orderBy: [date_ASC]
      ) {
        dimensions { date }
        sum { pageViews }
        uniq { uniques }
      }
    }
  }
}
"""

_cache_lock = threading.Lock()
_cache: dict[str, Any] | None = None
_cache_at: float = 0.0


def _config() -> tuple[str | None, str | None]:
    token = (os.environ.get("CF_API_TOKEN") or "").strip()
    zone = (os.environ.get("CF_ZONE_ID") or "").strip()
    return (token or None, zone or None)


def _round_avg(values: list[int]) -> int | None:
    if not values:
        return None
    return round(sum(values) / len(values))


def _compute(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Shape ascending-by-date CF rows into the traffic contract."""
    uv_trend: list[dict[str, Any]] = []
    pv_trend: list[dict[str, Any]] = []
    for r in rows:
        date = ((r.get("dimensions") or {}).get("date"))
        if not date:
            continue
        uv_trend.append({"date": date, "value": int((r.get("uniq") or {}).get("uniques") or 0)})
        pv_trend.append({"date": date, "value": int((r.get("sum") or {}).get("pageViews") or 0)})
    uv_vals = [p["value"] for p in uv_trend]
    pv_vals = [p["value"] for p in pv_trend]
    return {
        "available": True,
        "source": "cloudflare",
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "uv_avg_7d": _round_avg(uv_vals[-7:]),
        "uv_avg_30d": _round_avg(uv_vals),
        "pv_7d": sum(pv_vals[-7:]) if pv_vals else None,
        "pv_30d": sum(pv_vals) if pv_vals else None,
        "uv_trend_30d": uv_trend,
        "pv_trend_30d": pv_trend,
    }


def _fetch_uncached() -> dict[str, Any]:
    token, zone = _config()
    if not token or not zone:
        return {"available": False, "reason": "not_configured"}

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=_TREND_DAYS - 1)
    try:
        resp = requests.post(
            _CF_GRAPHQL_URL,
            json={
                "query": _QUERY,
                "variables": {"zone": zone, "geq": start.isoformat(), "leq": end.isoformat()},
            },
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # network / HTTP / JSON decode
        return {"available": False, "reason": "cf_error", "error": str(exc)[:200]}

    if payload.get("errors"):
        msg = "; ".join(str(e.get("message", "")) for e in (payload.get("errors") or []))
        return {"available": False, "reason": "cf_error", "error": (msg or "graphql_error")[:200]}

    zones = (((payload.get("data") or {}).get("viewer") or {}).get("zones")) or []
    if not zones:
        return {"available": False, "reason": "cf_error", "error": "zone_not_found"}
    rows = zones[0].get("httpRequests1dGroups") or []
    return _compute(rows)


def fetch_traffic_summary(*, force: bool = False) -> dict[str, Any]:
    """Return the (process-cached) Cloudflare traffic summary. Never raises.

    ``force=True`` bypasses the cache (used by ad-hoc verification scripts).
    """
    global _cache, _cache_at
    now = time.monotonic()
    if not force:
        with _cache_lock:
            if _cache is not None:
                ttl = _CACHE_TTL_OK if _cache.get("available") else _CACHE_TTL_ERR
                if now - _cache_at < ttl:
                    return _cache
    result = _fetch_uncached()
    with _cache_lock:
        _cache = result
        _cache_at = time.monotonic()
    return result
