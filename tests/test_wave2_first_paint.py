from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _request(*, legacy_authenticated: bool = True):
    return SimpleNamespace(
        method="GET",
        state=SimpleNamespace(user=None, legacy_authenticated=legacy_authenticated),
    )


def test_feed_first_paint_per_group_env_default_override_and_clamp(monkeypatch):
    import routes.feed as feed

    monkeypatch.delenv("INFO2ACTION_FEED_FIRST_PAINT_PER_GROUP", raising=False)
    assert feed._feed_first_paint_per_group() == 20

    monkeypatch.setenv("INFO2ACTION_FEED_FIRST_PAINT_PER_GROUP", "15")
    assert feed._feed_first_paint_per_group() == 15

    monkeypatch.setenv("INFO2ACTION_FEED_FIRST_PAINT_PER_GROUP", "1")
    assert feed._feed_first_paint_per_group() == 5

    monkeypatch.setenv("INFO2ACTION_FEED_FIRST_PAINT_PER_GROUP", "999")
    assert feed._feed_first_paint_per_group() == 100


def test_feed_sections_remote_first_paint_uses_default_per_group(monkeypatch):
    import routes.feed as feed

    calls = {}

    def fake_sections(**kwargs):
        calls.update(kwargs)
        return {"sections": {}, "cat_counts": {}, "total": 0}

    monkeypatch.delenv("INFO2ACTION_FEED_FIRST_PAINT_PER_GROUP", raising=False)
    monkeypatch.setattr(feed.remote_db, "feed_read_from_remote", lambda: True)
    monkeypatch.setattr(feed.remote_db, "query_feed_sections", fake_sections)
    monkeypatch.setattr(feed, "_github_display_min_stars", lambda: 42)

    feed.get_feed_sections(_request())

    assert calls["per_category"] == 20


def test_feed_platforms_remote_first_paint_uses_env_per_group(monkeypatch):
    import routes.feed as feed

    calls = {}

    def fake_platforms(**kwargs):
        calls.update(kwargs)
        return {
            "sections": {},
            "platform_counts": {},
            "source_counts": {},
            "category_counts": {},
        }

    monkeypatch.setenv("INFO2ACTION_FEED_FIRST_PAINT_PER_GROUP", "15")
    monkeypatch.setattr(feed.remote_db, "feed_read_from_remote", lambda: True)
    monkeypatch.setattr(feed.remote_db, "query_feed_platforms", fake_platforms)
    monkeypatch.setattr(feed, "_github_display_min_stars", lambda: 42)

    feed.get_feed_platforms(_request())

    assert calls["per_platform"] == 15
