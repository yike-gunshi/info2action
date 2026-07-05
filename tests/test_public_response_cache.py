from __future__ import annotations

import asyncio
import json
import os
import sys
from types import SimpleNamespace

from starlette.responses import Response

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _request(*, user=None, legacy_authenticated=False, method="GET"):
    return SimpleNamespace(
        method=method,
        state=SimpleNamespace(user=user, legacy_authenticated=legacy_authenticated),
    )


def _json_body(response: Response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def test_public_json_response_cache_hit_and_expiry(monkeypatch):
    import routes.public_response_cache as cache

    now = [100.0]
    monkeypatch.setattr(cache.time, "monotonic", lambda: now[0])
    cache.clear_public_response_cache()

    first = cache.set_public_json_response(("k",), {"value": "初回"}, ttl_sec=2)
    cached = cache.get_public_json_response(("k",))
    assert first.headers["X-Info2Act-Response-Cache"] == "miss"
    assert cached is not None
    assert cached.headers["X-Info2Act-Response-Cache"] == "hit"
    assert _json_body(cached) == {"value": "初回"}

    now[0] = 103.0
    assert cache.get_public_json_response(("k",)) is None


def test_feed_platforms_public_get_uses_serialized_response_cache(monkeypatch):
    import routes.feed as feed
    from routes.public_response_cache import clear_public_response_cache

    clear_public_response_cache()
    calls = {"n": 0}

    def fake_platforms(**kwargs):
        calls["n"] += 1
        return {
            "sections": {"reddit": [{"id": f"r{calls['n']}"}]},
            "platform_counts": {"reddit": calls["n"]},
            "source_counts": {},
            "category_counts": {},
        }

    monkeypatch.setattr(feed.remote_db, "feed_read_from_remote", lambda: True)
    monkeypatch.setattr(feed.remote_db, "query_feed_platforms", fake_platforms)
    monkeypatch.setattr(feed, "_github_display_min_stars", lambda: 42)

    first = feed.get_feed_platforms(_request())
    second = feed.get_feed_platforms(_request())

    assert calls["n"] == 1
    assert first.headers["X-Info2Act-Response-Cache"] == "miss"
    assert second.headers["X-Info2Act-Response-Cache"] == "hit"
    assert _json_body(second)["sections"]["reddit"][0]["id"] == "r1"


def test_feed_sections_logged_in_get_bypasses_response_cache(monkeypatch):
    import routes.feed as feed
    from routes.public_response_cache import clear_public_response_cache

    clear_public_response_cache()
    calls = {"n": 0}

    def fake_sections(**kwargs):
        calls["n"] += 1
        return {
            "sections": {"models": [{"id": f"m{calls['n']}"}]},
            "cat_counts": {"models": calls["n"]},
            "total": calls["n"],
        }

    monkeypatch.setattr(feed.remote_db, "feed_read_from_remote", lambda: True)
    monkeypatch.setattr(feed.remote_db, "query_feed_sections", fake_sections)
    monkeypatch.setattr(feed, "_github_display_min_stars", lambda: 42)

    first = feed.get_feed_sections(_request(user={"id": "u1"}))
    second = feed.get_feed_sections(_request(user={"id": "u1"}))

    assert calls["n"] == 2
    assert first["sections"]["models"][0]["id"] == "m1"
    assert second["sections"]["models"][0]["id"] == "m2"


def test_feed_events_public_default_get_uses_serialized_response_cache(monkeypatch):
    import routes.clusters as clusters
    from routes.public_response_cache import clear_public_response_cache

    clear_public_response_cache()
    calls = {"n": 0}

    def fake_fetch_events(**kwargs):
        calls["n"] += 1
        return {
            "enabled": True,
            "events": [{"id": calls["n"], "ai_title": "cached"}],
            "next_cursor": None,
            "new_since_last_fetch": 0,
            "total_available_within_30d": 1,
            "date_counts": {},
        }

    monkeypatch.setattr(clusters.remote_db, "events_read_from_remote", lambda: True)
    monkeypatch.setattr(clusters.remote_db, "fetch_events", fake_fetch_events)
    monkeypatch.setattr(clusters, "_github_cluster_display_min_stars", lambda: 42)
    monkeypatch.setattr(clusters, "_config_flag", lambda key, default: True)

    first = asyncio.run(
        clusters.feed_events(
            _request(),
            response=Response(),
            page=1,
            limit=20,
            since_version_snapshot=None,
            fetched_since=None,
            cursor=None,
            categories=None,
            timezone_offset_minutes=-480,
        )
    )
    second = asyncio.run(
        clusters.feed_events(
            _request(),
            response=Response(),
            page=1,
            limit=20,
            since_version_snapshot=None,
            fetched_since=None,
            cursor=None,
            categories=None,
            timezone_offset_minutes=-480,
        )
    )

    assert calls["n"] == 1
    assert first.headers["X-Info2Act-Response-Cache"] == "miss"
    assert second.headers["X-Info2Act-Response-Cache"] == "hit"
    assert _json_body(second)["events"][0]["id"] == 1


def test_context_search_public_events_only_get_uses_serialized_response_cache(monkeypatch):
    import routes.clusters as clusters
    from routes.public_response_cache import clear_public_response_cache

    clear_public_response_cache()
    calls = {"n": 0}

    def fake_context_search(**kwargs):
        calls["n"] += 1
        return {
            "docs": [],
            "docs_total": 0,
            "events": [{"id": calls["n"], "ai_title": "AI"}],
            "events_total": calls["n"],
        }

    monkeypatch.setattr(clusters.remote_db, "feed_read_from_remote", lambda: True)
    monkeypatch.setattr(clusters.remote_db, "events_read_from_remote", lambda: True)
    monkeypatch.setattr(clusters.remote_db, "context_search", fake_context_search)
    monkeypatch.setattr(clusters, "_github_cluster_display_min_stars", lambda: 42)

    first = asyncio.run(
        clusters.context_search(_request(), q="AI", context="recommend", limit=20, events_only=True)
    )
    second = asyncio.run(
        clusters.context_search(_request(), q="AI", context="recommend", limit=20, events_only=True)
    )

    assert calls["n"] == 1
    assert first.headers["X-Info2Act-Response-Cache"] == "miss"
    assert second.headers["X-Info2Act-Response-Cache"] == "hit"
    assert _json_body(second)["events"][0]["id"] == 1
