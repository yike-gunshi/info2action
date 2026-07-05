from __future__ import annotations

import os
import sys
import types
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import backfill_since as bf  # noqa: E402
import db as db_mod  # noqa: E402


def _tmp_conn(monkeypatch, tmp_path):
    monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "feed.db"))
    return db_mod.get_conn()


def _insert_item(
    conn,
    item_id,
    *,
    published_at,
    fetched_at="2026-05-01T12:00:00",
    complete=False,
    category="products",
):
    conn.execute(
        """INSERT INTO items (
               id, platform, source, title, content, fetched_at, published_at,
               ai_summary, ai_category, ai_categories, ai_quality_score,
               embedding, cluster_id
           ) VALUES (?, 'twitter', 'test', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            item_id,
            item_id,
            "content",
            fetched_at,
            published_at,
            "summary" if complete else None,
            category if complete else None,
            f'["{category}"]' if complete else None,
            80 if complete else None,
            b"vec" if complete else None,
            99 if complete else None,
        ),
    )
    conn.commit()


def test_filter_rows_since_uses_published_or_fetched_window():
    since = datetime(2026, 4, 28, 16, tzinfo=timezone.utc)
    until = datetime(2026, 5, 9, 16, tzinfo=timezone.utc)
    rows = [
        {"id": "old", "published_at": "2026-04-28T23:59:00+08:00", "fetched_at": "2026-05-01T00:00:00"},
        {"id": "new", "published_at": "2026-04-29T00:00:00+08:00", "fetched_at": "2026-05-01T00:00:00"},
        {"id": "snapshot", "published_at": None, "fetched_at": None},
        {"id": "snapshot_after_until", "published_at": None, "fetched_at": "2026-05-10T01:00:00+08:00"},
    ]

    kept, skipped = bf._filter_rows_since(rows, since, until)

    assert [row["id"] for row in kept] == ["new", "snapshot", "snapshot_after_until"]
    assert skipped == 1


def test_attach_existing_since_only_scopes_unfinished_items(monkeypatch, tmp_path):
    conn = _tmp_conn(monkeypatch, tmp_path)
    _insert_item(conn, "ready", published_at="2026-05-01T12:00:00+08:00", complete=True)
    _insert_item(conn, "pending", published_at="2026-05-01T12:00:00+08:00")
    _insert_item(conn, "old_pending", published_at="2026-04-28T23:59:00+08:00")
    _insert_item(conn, "future_pending", published_at="2026-05-11T00:00:00+08:00")
    _insert_item(conn, "fetched_recent", published_at=None, fetched_at="2026-05-01T12:00:00")

    stats = bf.attach_existing_since(
        conn,
        run_id=77,
        since=datetime(2026, 4, 28, 16, tzinfo=timezone.utc),
        until=datetime(2026, 5, 9, 16, tzinfo=timezone.utc),
    )

    rows = {
        row["id"]: row["fetch_run_id"]
        for row in conn.execute("SELECT id, fetch_run_id FROM items").fetchall()
    }
    assert stats == {"attached_existing": 2}
    assert rows["pending"] == 77
    assert rows["fetched_recent"] == 77
    assert rows["ready"] is None
    assert rows["old_pending"] is None
    assert rows["future_pending"] is None


def test_attach_ready_cluster_only_scopes_only_published_ready_unclustered(monkeypatch, tmp_path):
    conn = _tmp_conn(monkeypatch, tmp_path)
    _insert_item(conn, "ready", published_at="2026-05-01T12:00:00+08:00", complete=True)
    _insert_item(conn, "missing_embedding", published_at="2026-05-01T12:00:00+08:00", complete=True)
    _insert_item(conn, "missing_ai", published_at="2026-05-01T12:00:00+08:00")
    _insert_item(conn, "fetched_only", published_at=None, fetched_at="2026-05-01T12:00:00", complete=True)
    _insert_item(conn, "old_ready", published_at="2026-04-28T23:59:00+08:00", complete=True)
    _insert_item(conn, "other_ready", published_at="2026-05-01T12:00:00+08:00", complete=True, category="other")
    _insert_item(conn, "clustered", published_at="2026-05-01T12:00:00+08:00", complete=True)
    conn.execute(
        """UPDATE items
              SET cluster_id=NULL
            WHERE id IN ('ready', 'missing_embedding', 'fetched_only', 'old_ready', 'other_ready')"""
    )
    conn.execute("UPDATE items SET embedding=NULL WHERE id='missing_embedding'")
    conn.execute("UPDATE items SET embedding=? WHERE id='missing_ai'", (b"vec",))
    conn.commit()

    stats = bf.attach_ready_cluster_only_since(
        conn,
        run_id=88,
        since=datetime(2026, 4, 28, 16, tzinfo=timezone.utc),
        until=datetime(2026, 5, 9, 16, tzinfo=timezone.utc),
        require_published_at=True,
    )

    rows = {
        row["id"]: row["fetch_run_id"]
        for row in conn.execute("SELECT id, fetch_run_id FROM items").fetchall()
    }
    assert stats == {"attached_existing": 1}
    assert rows["ready"] == 88
    assert rows["missing_embedding"] is None
    assert rows["missing_ai"] is None
    assert rows["fetched_only"] is None
    assert rows["old_ready"] is None
    assert rows["other_ready"] is None
    assert rows["clustered"] is None


def test_attach_ready_cluster_only_uses_pipeline_window_sql_semantics(monkeypatch, tmp_path):
    conn = _tmp_conn(monkeypatch, tmp_path)
    _insert_item(conn, "naive_ready", published_at="2026-05-09 22:28", complete=True)
    conn.execute("UPDATE items SET cluster_id=NULL WHERE id='naive_ready'")
    conn.commit()

    stats = bf.attach_ready_cluster_only_since(
        conn,
        run_id=89,
        since=datetime(2026, 5, 9, 18, tzinfo=timezone.utc),
        until=datetime(2026, 5, 10, 0, tzinfo=timezone.utc),
        require_published_at=True,
    )

    row = conn.execute("SELECT fetch_run_id FROM items WHERE id='naive_ready'").fetchone()
    assert stats == {"attached_existing": 1}
    assert row["fetch_run_id"] == 89


def test_run_processing_skips_cluster_when_ai_pending(monkeypatch, tmp_path):
    conn = _tmp_conn(monkeypatch, tmp_path)
    _insert_item(conn, "pending", published_at="2026-05-01T12:00:00+08:00")
    conn.execute("UPDATE items SET fetch_run_id=5 WHERE id='pending'")
    conn.commit()
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(bf.subprocess, "run", fake_run)
    args = types.SimpleNamespace(
        skip_ai=False,
        respect_ai_retry_after=False,
        batch_size=5,
        workers=20,
        chat_request_interval_sec=0.2,
        top_k=5,
        judge_workers=5,
        judge_min_interval_sec=1.0,
        ai_timeout=7200,
        skip_cluster=False,
        cluster_timeout=7200,
        process_window_hours=0,
    )
    ctx = bf.BackfillContext(
        conn=conn,
        config={},
        topics={},
        run_id=5,
        since=datetime(2026, 4, 28, 16, tzinfo=timezone.utc),
        until=datetime(2026, 5, 9, 16, tzinfo=timezone.utc),
        args=args,
    )

    assert bf.run_processing(ctx) is False
    assert len(calls) == 1
    assert "--batch-size" in calls[0]
    assert calls[0][calls[0].index("--batch-size") + 1] == "5"
    assert "--workers" in calls[0]
    assert calls[0][calls[0].index("--workers") + 1] == "20"
    assert "--request-interval-sec" in calls[0]
    assert calls[0][calls[0].index("--request-interval-sec") + 1] == "0.2"


def test_run_processing_ready_cluster_only_skips_enrich_and_runs_cluster(monkeypatch, tmp_path):
    conn = _tmp_conn(monkeypatch, tmp_path)
    _insert_item(conn, "ready", published_at="2026-05-10T10:00:00+08:00", complete=True)
    conn.execute("UPDATE items SET fetch_run_id=5, cluster_id=NULL WHERE id='ready'")
    conn.commit()
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout='{"published_clusters":1}\n', stderr="")

    monkeypatch.setattr(bf.subprocess, "run", fake_run)
    args = types.SimpleNamespace(
        ready_cluster_only=True,
        skip_ai=False,
        respect_ai_retry_after=False,
        batch_size=5,
        workers=20,
        chat_request_interval_sec=0.2,
        top_k=5,
        judge_workers=5,
        judge_min_interval_sec=1.0,
        ai_timeout=7200,
        skip_cluster=False,
        cluster_timeout=7200,
        process_window_days=1,
        process_window_hours=0,
        window_require_published_at=True,
    )
    ctx = bf.BackfillContext(
        conn=conn,
        config={},
        topics={},
        run_id=5,
        since=datetime(2026, 5, 9, 16, tzinfo=timezone.utc),
        until=datetime(2026, 5, 10, 3, tzinfo=timezone.utc),
        args=args,
    )

    assert bf.run_processing_window(
        ctx,
        datetime(2026, 5, 9, 16, tzinfo=timezone.utc),
        datetime(2026, 5, 10, 3, tzinfo=timezone.utc),
    ) is True
    assert len(calls) == 1
    assert calls[0][1].endswith("src/clustering/pipeline.py")
    assert "--window-start" in calls[0]
    assert "--window-end" in calls[0]
    assert "--window-require-published-at" in calls[0]
    assert "--feed-candidates-only" in calls[0]
    assert calls[0][calls[0].index("--top-k") + 1] == "5"
    assert "--request-interval-sec" not in calls[0]


def test_iter_processing_windows_newest_first_daily():
    windows = bf.iter_processing_windows(
        datetime(2026, 5, 7, 16, tzinfo=timezone.utc),
        datetime(2026, 5, 10, 3, tzinfo=timezone.utc),
        days=1,
    )

    assert windows == [
        (
            datetime(2026, 5, 9, 16, tzinfo=timezone.utc),
            datetime(2026, 5, 10, 3, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 5, 8, 16, tzinfo=timezone.utc),
            datetime(2026, 5, 9, 16, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 5, 7, 16, tzinfo=timezone.utc),
            datetime(2026, 5, 8, 16, tzinfo=timezone.utc),
        ),
    ]


def test_iter_processing_windows_newest_first_hourly():
    windows = bf.iter_processing_windows(
        datetime(2026, 5, 9, 16, tzinfo=timezone.utc),
        datetime(2026, 5, 10, 4, 0, tzinfo=timezone.utc),
        days=1,
        hours=6,
    )

    assert windows == [
        (
            datetime(2026, 5, 9, 22, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 10, 4, 0, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 5, 9, 16, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 9, 22, 0, tzinfo=timezone.utc),
        ),
    ]


def test_run_processing_window_passes_same_window_to_enrich_and_cluster(monkeypatch, tmp_path):
    conn = _tmp_conn(monkeypatch, tmp_path)
    _insert_item(conn, "ready", published_at="2026-05-10T10:00:00+08:00", complete=True)
    conn.execute("UPDATE items SET fetch_run_id=5, ai_quality_score=80 WHERE id='ready'")
    conn.commit()
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout='{"published_clusters":1}\n', stderr="")

    monkeypatch.setattr(bf.subprocess, "run", fake_run)
    args = types.SimpleNamespace(
        skip_ai=False,
        respect_ai_retry_after=False,
        batch_size=5,
        workers=20,
        chat_request_interval_sec=0.2,
        judge_workers=5,
        judge_min_interval_sec=1.0,
        ai_timeout=7200,
        skip_cluster=False,
        cluster_timeout=7200,
        process_window_days=1,
        process_window_hours=0,
        window_require_published_at=True,
    )
    ctx = bf.BackfillContext(
        conn=conn,
        config={},
        topics={},
        run_id=5,
        since=datetime(2026, 5, 9, 16, tzinfo=timezone.utc),
        until=datetime(2026, 5, 10, 3, tzinfo=timezone.utc),
        args=args,
    )

    assert bf.run_processing(ctx) is True
    assert len(calls) == 2
    for cmd in calls:
        assert "--window-start" in cmd
        assert "--window-end" in cmd
        assert "--window-require-published-at" in cmd
        assert "2026-05-09T16:00:00+00:00" in cmd
        assert "2026-05-10T03:00:00+00:00" in cmd
    assert "--request-interval-sec" in calls[0]
    assert "0.2" in calls[0]
    assert "--judge-workers" in calls[1]
    assert "5" in calls[1]


def test_parse_last_json_line():
    assert bf._parse_last_json_line("log\n{\"summary_failed\": 2}\n") == {"summary_failed": 2}
    assert bf._parse_last_json_line("no json") is None


def test_active_provider_messages_ignores_disabled_embedding_recharge(tmp_path, monkeypatch):
    monkeypatch.setattr(bf.ai_provider_guard, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(bf.ai_provider_guard, "LOCK_PATH", str(tmp_path / "state.lock"))
    bf.ai_provider_guard.record_action_required(
        bf.ai_provider_guard.MINIMAX_EMBEDDING_PROVIDER,
        action="recharge_embedding",
        source="unit-test",
    )

    messages = bf._active_provider_messages()

    assert messages == []


def test_search_queries_use_curated_topic_queries_not_all_keywords():
    ctx = types.SimpleNamespace(
        config={
            "global": {"search_keywords": ["claude"]},
            "twitter": {"search": {"extra_keywords": ["MCP protocol"]}},
        },
        topics={
            "topics": [
                {
                    "keywords": ["too broad keyword"],
                    "search_queries": ["Claude Code Cursor 开发"],
                }
            ]
        },
    )

    queries = bf._search_queries(ctx, "twitter")

    assert queries == ["claude", "MCP protocol", "Claude Code Cursor 开发"]


def test_twitter_row_skips_x_article_expansion_by_default(monkeypatch):
    monkeypatch.setattr(
        bf.ingest,
        "_expand_x_article",
        lambda _tweet_id: (_ for _ in ()).throw(AssertionError("must not expand")),
    )
    row = bf._twitter_row(
        {
            "id": "123",
            "text": "https://t.co/abc",
            "urls": ["https://x.com/i/article/123"],
            "author": {"screenName": "alice"},
            "metrics": {},
        },
        "bookmarks",
    )

    assert row["content"] == "https://t.co/abc"
    assert '"articleExpandSkipped": true' in row["detail_json"]
