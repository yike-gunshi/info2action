from __future__ import annotations

import os
import sys


BASE = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(BASE, "src"))


def test_candidate_query_uses_active_highlights_and_missing_reddit_media():
    import reddit_media_completion

    class Cursor:
        def fetchall(self):
            return [
                {
                    "item_id": "reddit_one",
                    "source_url": "https://reddit.com/one",
                }
            ]

    class Conn:
        def __init__(self):
            self.sql = ""
            self.params = None

        def execute(self, sql, params):
            self.sql = " ".join(sql.split())
            self.params = params
            return Cursor()

    conn = Conn()
    rows = reddit_media_completion.select_candidates(
        conn,
        schema="remote_poc",
        limit=12,
    )

    assert rows[0]["item_id"] == "reddit_one"
    assert "highlights_read_model_state" in conn.sql
    assert "highlights_scope_items" in conn.sql
    assert "h.scope_key = 'all'" in conn.sql
    assert "cluster_items" in conn.sql
    assert "i.platform = 'reddit'" in conn.sql
    assert "i.media_json IS NULL" in conn.sql
    assert conn.params["limit"] == 12


def test_completion_is_bounded_and_isolates_single_item_failures():
    import reddit_media_completion

    candidates = [
        {"item_id": "reddit_ok", "source_url": "https://reddit.com/ok"},
        {"item_id": "reddit_empty", "source_url": "https://reddit.com/empty"},
        {"item_id": "reddit_fail", "source_url": "https://reddit.com/fail"},
    ]
    updates = []
    asr_ids = []

    def media_loader(url):
        if url.endswith("/fail"):
            raise RuntimeError("extractor crash")
        if url.endswith("/empty"):
            return []
        return [
            {
                "type": "video",
                "url": "https://v.redd.it/ok.mp4",
                "poster_url": "https://preview.redd.it/ok.jpg",
            }
        ]

    result = reddit_media_completion.complete_highlighted_reddit_media(
        limit=3,
        candidate_loader=lambda _limit: candidates,
        media_loader=media_loader,
        item_updater=lambda item_id, media: updates.append((item_id, media)),
        asr_runner=lambda item_ids: asr_ids.extend(item_ids),
    )

    assert result == {
        "candidates": 3,
        "completed": 1,
        "no_media": 1,
        "failed": 1,
        "asr_triggered": 1,
    }
    assert [item_id for item_id, _media in updates] == ["reddit_ok"]
    assert asr_ids == ["reddit_ok"]


def test_post_fetch_hook_is_daemon_and_does_not_block_caller(monkeypatch):
    from routes import fetch as fetch_orchestrator
    import reddit_media_completion

    callbacks = []
    order = []

    class DeferredThread:
        def __init__(self, *, target, daemon=None, name=None, args=(), kwargs=None):
            callbacks.append((target, args, kwargs or {}, daemon, name))

        def start(self):
            return None

    monkeypatch.setenv("INFO2ACTION_CACHE_PREWARM", "0")
    monkeypatch.setenv("INFO2ACTION_PREWARM_PLATFORMS", "0")
    monkeypatch.setenv("INFO2ACTION_INFO_READ_MODEL", "0")
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL", "1")
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL_REFRESH", "1")
    monkeypatch.setattr(fetch_orchestrator, "_remote_db_pressure_skip_reason", lambda: None)
    monkeypatch.setattr(fetch_orchestrator.threading, "Thread", DeferredThread)
    monkeypatch.setattr(
        fetch_orchestrator.remote_db,
        "refresh_info_pill_counts",
        lambda: {"ok": True},
    )
    monkeypatch.setattr(
        fetch_orchestrator.remote_db,
        "refresh_highlights_read_model_if_stale",
        lambda **_kwargs: order.append("refresh") or {"ok": True},
    )
    monkeypatch.setattr(
        reddit_media_completion,
        "complete_highlighted_reddit_media",
        lambda: order.append("media") or {"candidates": 0},
    )

    fetch_orchestrator._schedule_post_fetch_read_model_refresh(99)

    assert order == []
    assert len(callbacks) == 1
    target, args, kwargs, daemon, _name = callbacks[0]
    assert daemon is True
    target(*args, **kwargs)
    assert order == ["refresh", "media"]
