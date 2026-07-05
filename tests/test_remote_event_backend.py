from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import threading
import time
import urllib.error
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from starlette.responses import Response

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import asset_cache  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_asset_cache_dir(tmp_path, monkeypatch):
    """B1: media 代理接入磁盘缓存后隔离缓存目录,避免跨测试串味。"""
    monkeypatch.setenv(asset_cache.ASSET_CACHE_DIR_ENV, str(tmp_path / 'asset_cache'))
    asset_cache.clear()
    yield
    asset_cache.clear()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _request(*, user=None, legacy_authenticated=False):
    return SimpleNamespace(
        state=SimpleNamespace(user=user, legacy_authenticated=legacy_authenticated)
    )


def _isolate_remote_env(monkeypatch, remote_db):
    monkeypatch.setattr(remote_db, "load_project_env", lambda base: {})
    for key in (
        remote_db.GLOBAL_BACKEND_ENV,
        remote_db.BACKEND_ENV,
        remote_db.FEED_BACKEND_ENV,
        remote_db.STATUS_BACKEND_ENV,
        remote_db.REMOTE_SCHEMA_ENV,
        remote_db.DATA_AUTHORITY_ENV,
        "INFO2ACTION_STORAGE_MODE",
        "INFO2ACTION_PIPELINE_WRITE_MODE",
        "INFO2ACTION_FETCH_WRITE_BACKEND",
        "INFO2ACTION_ENRICH_BACKEND",
        "INFO2ACTION_EMBEDDING_BACKEND",
        "INFO2ACTION_CLUSTER_BACKEND",
        "INFO2ACTION_APP_STATE_BACKEND",
        "INFO2ACTION_ASSET_BACKEND",
        "INFO2ACTION_REMOTE_SYNC_AFTER_PIPELINE",
        "SUPABASE_DB_URL",
        "DATABASE_URL",
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_STORAGE_BUCKET",
    ):
        monkeypatch.delenv(key, raising=False)


def _assert_events_read_model_timeouts_before_scope_items(sqls):
    scope_idx = next(
        i for i, sql in enumerate(sqls)
        if "FROM remote_poc.highlights_scope_items" in sql
    )
    assert "SET LOCAL statement_timeout = '4500ms'" in sqls[:scope_idx]
    assert "SET LOCAL idle_in_transaction_session_timeout = '15000ms'" in sqls[:scope_idx]


def test_event_read_backend_defaults_to_sqlite(monkeypatch):
    import remote_db
    _isolate_remote_env(monkeypatch, remote_db)
    monkeypatch.delenv(remote_db.BACKEND_ENV, raising=False)
    assert remote_db.event_read_backend() == "sqlite"
    assert remote_db.events_read_from_remote() is False


def test_event_read_backend_can_enable_supabase(monkeypatch):
    import remote_db

    monkeypatch.setenv(remote_db.BACKEND_ENV, "supabase_poc")
    assert remote_db.event_read_backend() == "supabase_poc"
    assert remote_db.events_read_from_remote() is True


def test_feed_read_backend_defaults_to_sqlite(monkeypatch):
    import remote_db

    _isolate_remote_env(monkeypatch, remote_db)
    monkeypatch.delenv(remote_db.GLOBAL_BACKEND_ENV, raising=False)
    monkeypatch.delenv(remote_db.FEED_BACKEND_ENV, raising=False)
    assert remote_db.feed_read_backend() == "sqlite"
    assert remote_db.feed_read_from_remote() is False


def test_feed_read_backend_can_enable_supabase(monkeypatch):
    import remote_db

    monkeypatch.setenv(remote_db.FEED_BACKEND_ENV, "supabase_poc")
    assert remote_db.feed_read_backend() == "supabase_poc"
    assert remote_db.feed_read_from_remote() is True


def test_remote_connect_does_not_force_writable_session_by_default(monkeypatch):
    import remote_db

    _isolate_remote_env(monkeypatch, remote_db)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://example.invalid/postgres")

    class FakeConn:
        def __init__(self):
            self.calls = []
            self.commits = 0

        def execute(self, sql, params=None):
            self.calls.append((" ".join(sql.split()), params))
            return self

        def commit(self):
            self.commits += 1

    class FakeConnectionContext:
        def __init__(self, conn):
            self.conn = conn

        def __enter__(self):
            return self.conn

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakePool:
        def __init__(self, conn):
            self.conn = conn

        def connection(self):
            return FakeConnectionContext(self.conn)

    fake = FakeConn()
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "psycopg.rows", SimpleNamespace(dict_row=object()))
    monkeypatch.setattr(remote_db, "_get_pool", lambda *_args, **_kwargs: FakePool(fake))

    with remote_db.connect() as conn:
        conn.execute("select 1")

    assert ("SET search_path TO remote_poc, extensions, public", None) in fake.calls
    assert ("select 1", None) in fake.calls
    assert all("default_transaction_read_only=off" not in sql.lower() for sql, _ in fake.calls)


def test_status_backend_can_enable_supabase(monkeypatch):
    import remote_db

    monkeypatch.setenv(remote_db.STATUS_BACKEND_ENV, "supabase_poc")
    assert remote_db.status_backend() == "supabase_poc"
    assert remote_db.status_write_to_remote() is True


def test_fetch_write_backend_defaults_to_sqlite(monkeypatch):
    import remote_db

    _isolate_remote_env(monkeypatch, remote_db)

    assert remote_db.fetch_write_backend() == "sqlite"
    assert remote_db.fetch_write_to_remote() is False


def test_fetch_write_backend_can_enable_supabase(monkeypatch):
    import remote_db

    _isolate_remote_env(monkeypatch, remote_db)
    monkeypatch.setenv("INFO2ACTION_FETCH_WRITE_BACKEND", "supabase")

    assert remote_db.fetch_write_backend() == "supabase"
    assert remote_db.fetch_write_to_remote() is True


def test_remote_fetch_writer_creates_and_finishes_run(monkeypatch):
    import remote_db

    monkeypatch.setattr(remote_db, "_maybe_jsonb", lambda value: value)

    class FakeCursor:
        def __init__(self, row=None, rows=None):
            self.row = row
            self.rows = rows if rows is not None else ([] if row is None else [row])

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class FakeConn:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            lower = normalized.lower()
            self.calls.append((normalized, params))
            if "returning id" in lower:
                return FakeCursor({"id": 123})
            if "select * from remote_poc.fetch_runs where id = %s" in lower:
                return FakeCursor(
                    {
                        "id": 123,
                        "started_at": "2026-05-16T06:00:00Z",
                        "finished_at": None,
                        "status": "running",
                        "stats_json": None,
                        "error_msg": None,
                    }
                )
            if "select 1 from remote_poc.fetch_run_items where run_id = %s" in lower:
                return FakeCursor({"exists": 1})
            if "from remote_poc.fetch_run_items fri join remote_poc.items i" in lower:
                return FakeCursor(
                    rows=[
                        {
                            "platform": "twitter",
                            "source": "following",
                            "ai_summary": "summary",
                            "ai_error_count": 0,
                            "ai_last_error": None,
                            "cluster_id": 456,
                            "ai_categories": ["tech"],
                            "ai_category": None,
                        },
                        {
                            "platform": "reddit",
                            "source": "r/OpenAI",
                            "ai_summary": None,
                            "ai_error_count": 1,
                            "ai_last_error": "bad",
                            "cluster_id": None,
                            "ai_categories": None,
                            "ai_category": None,
                        },
                    ]
                )
            if "select count(*) as count from remote_poc.clusters where published_run_id = %s" in lower:
                return FakeCursor({"count": 1})
            return FakeCursor()

    fake = FakeConn()
    run_id = remote_db.start_fetch_run_remote(fake)
    remote_db.finish_fetch_run_remote(fake, run_id, {"twitter": 2}, None)

    assert run_id == 123
    assert any("setval" in sql.lower() and "fetch_runs_id_seq" in sql for sql, _ in fake.calls)
    assert any("insert into remote_poc.fetch_runs" in sql.lower() for sql, _ in fake.calls)
    assert any("update remote_poc.fetch_runs" in sql.lower() for sql, _ in fake.calls)
    update_payload = next(params[2] for sql, params in fake.calls if "update remote_poc.fetch_runs" in sql.lower())
    assert update_payload["_audit"]["new_items_count"] == 2
    assert update_payload["_audit"]["event_cluster"]["published_clusters"] == 1


def test_mark_orphaned_fetch_runs_remote_marks_rows_started_before_cutoff(monkeypatch):
    import remote_db
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(remote_db, "_maybe_jsonb", lambda value: value)

    class FakeCursor:
        def __init__(self, rows=None):
            self.rows = rows or []

        def fetchall(self):
            return self.rows

    class FakeConn:
        def __init__(self):
            self.calls = []
            self.commits = 0

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.calls.append((normalized, params))
            if "returning fr.id" in normalized.lower():
                return FakeCursor([{"id": 1514}, {"id": 1513}])
            return FakeCursor()

        def commit(self):
            self.commits += 1

    fake = FakeConn()
    cutoff = datetime(2026, 5, 20, 3, 10, 54, tzinfo=timezone.utc)
    heartbeat_stale_before = cutoff - timedelta(minutes=5)
    updated = remote_db.mark_orphaned_fetch_runs_remote(
        fake,
        started_before=cutoff,
        heartbeat_stale_before=heartbeat_stale_before,
        reason="service restarted",
        limit=5,
    )

    assert updated == [1514, 1513]
    sql, params = fake.calls[0]
    assert "status = 'running'" in sql
    assert "started_at < %s" in sql
    assert "stats_json->>'_heartbeat_at'" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert params[0] == cutoff
    assert params[1] == heartbeat_stale_before
    assert params[2] == 5
    assert params[4] == "error"
    assert params[5] == "service restarted"
    assert params[6]["_result_status"] == "interrupted"
    assert fake.commits == 1


def test_touch_fetch_run_heartbeat_remote_updates_running_row(monkeypatch):
    import remote_db
    from datetime import datetime, timezone

    monkeypatch.setattr(remote_db, "_maybe_jsonb", lambda value: value)
    touched_at = datetime(2026, 5, 25, 11, 52, 30, tzinfo=timezone.utc)

    class FakeCursor:
        rowcount = 1

    class FakeConn:
        def __init__(self):
            self.calls = []
            self.commits = 0

        def execute(self, sql, params=None):
            self.calls.append((" ".join(sql.split()), params))
            return FakeCursor()

        def commit(self):
            self.commits += 1

    fake = FakeConn()

    remote_db.touch_fetch_run_heartbeat_remote(
        fake,
        run_id=1748,
        owner="unit-host:123:456",
        touched_at=touched_at,
    )

    sql, params = fake.calls[0]
    assert "UPDATE remote_poc.fetch_runs" in sql
    assert "status = 'running'" in sql
    assert params[0]["_heartbeat_at"] == touched_at.isoformat()
    assert params[0]["_heartbeat_owner"] == "unit-host:123:456"
    assert params[1] == 1748
    assert fake.commits == 1


def test_mark_fetch_runs_interrupted_remote_marks_only_given_running_ids(monkeypatch):
    import remote_db

    monkeypatch.setattr(remote_db, "_maybe_jsonb", lambda value: value)

    class FakeCursor:
        def fetchall(self):
            return [{"id": 1524}]

    class FakeConn:
        def __init__(self):
            self.calls = []
            self.commits = 0

        def execute(self, sql, params=None):
            self.calls.append((" ".join(sql.split()), params))
            return FakeCursor()

        def commit(self):
            self.commits += 1

    fake = FakeConn()
    updated = remote_db.mark_fetch_runs_interrupted_remote(
        fake,
        run_ids=[1524, 1524, 1525],
        reason="service stopped",
    )

    assert updated == [1524]
    sql, params = fake.calls[0]
    assert "WHERE status = 'running' AND id = ANY(%s)" in sql
    assert params[2] == "service stopped"
    assert params[3]["_result_status"] == "interrupted"
    assert params[3]["_shutdown_interruption"] is True
    assert params[4] == [1524, 1525]
    assert fake.commits == 1


def test_remote_fetch_run_list_uses_stats_snapshot_without_heavy_audit(monkeypatch):
    import remote_db

    class FakeCursor:
        def __init__(self, rows=None):
            self.rows = rows or []

        def fetchall(self):
            return self.rows

    class FakeConn:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.calls.append((normalized, params))
            return FakeCursor([
                {
                    "id": 1324,
                    "started_at": "2026-05-15T13:30:00Z",
                    "finished_at": "2026-05-15T13:40:41Z",
                    "status": "done",
                    "stats_json": {
                        "_result_status": "success",
                        "_new_items_count": 20,
                        "_stage_durations_sec": {"source_fetch": 12.5},
                    },
                    "error_msg": None,
                }
            ])

    fake = FakeConn()
    runs = remote_db.list_fetch_run_audits_remote(pg_conn=fake, limit=20)

    assert runs[0]["total_new_items"] == 20
    assert runs[0]["audit"]["new_items_count"] == 20
    assert runs[0]["audit"]["result_status"] == "success"
    assert runs[0]["audit"]["stage_durations_sec"] == {"source_fetch": 12.5}
    assert runs[0]["audit"]["ai_summary"] == {"summarized": None, "failed": None, "pending": None}
    assert runs[0]["audit"]["event_cluster"] == {
        "clustered_items": None,
        "touched_clusters": None,
        "published_clusters": None,
    }
    assert not any("fetch_run_items" in sql.lower() for sql, _ in fake.calls)
    assert not any("clusters" in sql.lower() for sql, _ in fake.calls)


class _RemoteBatchCursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row

    def fetchall(self):
        return [] if self.row is None else [self.row]


class _RemoteBatchConn:
    def __init__(self):
        self.calls = []
        self.commits = 0

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        if "from remote_poc.fetch_runs" in normalized.lower():
            return _RemoteBatchCursor({"exists": 1})
        if "from remote_poc.items" in normalized.lower():
            return _RemoteBatchCursor(None)
        return _RemoteBatchCursor()

    def commit(self):
        self.commits += 1


def test_remote_batch_upsert_items_records_fetch_run_items(monkeypatch):
    import remote_db

    fake = _RemoteBatchConn()
    count = remote_db.batch_upsert_items_remote(
        fake,
        [
            {
                "id": "i1",
                "platform": "rss",
                "source": "feed",
                "title": "Title",
                "content": "Body",
                "fetched_at": "2026-05-13T00:00:00Z",
            },
            {
                "id": "i2",
                "platform": "rss",
                "source": "feed",
                "title": "Title 2",
                "content": "Body 2",
                "fetched_at": "2026-05-13T00:01:00Z",
            }
        ],
        fetch_run_id=123,
    )

    item_insert_calls = [
        call for call in fake.calls if "insert into remote_poc.items" in call[0].lower()
    ]
    run_item_calls = [
        call for call in fake.calls if "insert into remote_poc.fetch_run_items" in call[0].lower()
    ]

    assert count == 2
    assert fake.commits == 1
    assert len(item_insert_calls) == 1
    assert len(item_insert_calls[0][1]) == len(remote_db.REMOTE_ITEM_WRITE_COLUMNS) * 2
    assert len(run_item_calls) == 1
    assert run_item_calls[0][1] == [
        123,
        "i1",
        "rss",
        "feed",
        1,
        123,
        "i2",
        "rss",
        "feed",
        1,
    ]


def test_remote_batch_upsert_items_preserves_duplicate_ids_with_single_row_fallback(monkeypatch):
    import remote_db

    fake = _RemoteBatchConn()
    count = remote_db.batch_upsert_items_remote(
        fake,
        [
            {
                "id": "i1",
                "platform": "rss",
                "source": "old-feed",
                "title": "Old",
                "content": "Old body",
                "fetched_at": "2026-05-13T00:00:00Z",
            },
            {
                "id": "i1",
                "platform": "rss",
                "source": "new-feed",
                "title": "New",
                "content": "New body",
                "fetched_at": "2026-05-13T00:01:00Z",
            },
        ],
        fetch_run_id=123,
    )

    item_insert_calls = [
        call for call in fake.calls if "insert into remote_poc.items" in call[0].lower()
    ]
    run_item_calls = [
        call for call in fake.calls if "insert into remote_poc.fetch_run_items" in call[0].lower()
    ]
    assert count == 2
    assert len(item_insert_calls) == 2
    assert len(item_insert_calls[0][1]) == len(remote_db.REMOTE_ITEM_WRITE_COLUMNS)
    assert len(item_insert_calls[1][1]) == len(remote_db.REMOTE_ITEM_WRITE_COLUMNS)
    assert len(run_item_calls) == 2
    assert run_item_calls[0][1] == (123, "i1", "rss", "old-feed", 1)
    assert run_item_calls[1][1] == (123, "i1", "rss", "new-feed", 1)
    assert fake.commits == 1


def test_ingest_batch_upsert_routes_to_remote_fetch_writer(monkeypatch):
    import ingest

    calls = {}
    ingest.CURRENT_RUN_ID = 77
    monkeypatch.setattr(ingest.remote_db, "fetch_write_to_remote", lambda: True, raising=False)
    monkeypatch.setattr(
        ingest.remote_db,
        "batch_upsert_items_remote",
        lambda conn, items, fetch_run_id=None: calls.update(
            {"conn": conn, "items": items, "fetch_run_id": fetch_run_id}
        ) or len(items),
        raising=False,
    )
    monkeypatch.setattr(
        ingest.db,
        "batch_upsert",
        lambda *args, **kwargs: pytest.fail("remote fetch writer should not call SQLite batch_upsert"),
    )

    result = ingest.batch_upsert_current_run(object(), [{"id": "i1"}])

    assert result == 1
    assert calls == {"conn": None, "items": [{"id": "i1"}], "fetch_run_id": 77}


def test_ingest_fetch_run_lifecycle_routes_to_remote_fetch_writer(monkeypatch):
    import ingest

    calls = []
    monkeypatch.setattr(ingest.remote_db, "fetch_write_to_remote", lambda: True)
    monkeypatch.setattr(ingest.remote_db, "start_fetch_run_remote", lambda conn=None: calls.append(("start", conn)) or 88)
    monkeypatch.setattr(
        ingest.remote_db,
        "finish_fetch_run_remote",
        lambda conn, run_id, stats, error=None: calls.append(("finish", conn, run_id, stats, error)),
    )
    monkeypatch.setattr(
        ingest.db,
        "start_fetch_run",
        lambda *args, **kwargs: pytest.fail("remote fetch writer should not start SQLite run"),
    )
    monkeypatch.setattr(
        ingest.db,
        "finish_fetch_run",
        lambda *args, **kwargs: pytest.fail("remote fetch writer should not finish SQLite run"),
    )

    run_id = ingest.start_current_fetch_run(object())
    ingest.finish_current_fetch_run(object(), run_id, {"rss": 1}, None)

    assert run_id == 88
    assert calls == [("start", None), ("finish", None, 88, {"rss": 1}, None)]


def test_ingest_main_remote_writer_does_not_open_sqlite(monkeypatch):
    import sys

    import ingest

    calls = []
    monkeypatch.setattr(sys, "argv", ["ingest.py", "--skip-link-enrichment", "--skip-image-download"])
    monkeypatch.setattr(ingest.remote_db, "fetch_write_to_remote", lambda: True)
    monkeypatch.setattr(ingest.remote_db, "start_fetch_run_remote", lambda conn=None: calls.append(("start", conn)) or 1747)
    monkeypatch.setattr(
        ingest.remote_db,
        "finish_fetch_run_remote",
        lambda conn, run_id, stats, error=None: calls.append(("finish", conn, run_id, stats, error)),
    )
    monkeypatch.setattr(
        ingest.db,
        "get_conn",
        lambda: pytest.fail("remote ingest writer should not open ECS SQLite"),
    )

    def fake_ingest(name):
        def _inner(conn, *args, **kwargs):
            calls.append((name, conn))
            return 0
        return _inner

    monkeypatch.setattr(ingest, "ingest_twitter", fake_ingest("twitter"))
    monkeypatch.setattr(ingest, "ingest_xiaohongshu", fake_ingest("xiaohongshu"))
    monkeypatch.setattr(ingest, "ingest_bilibili", fake_ingest("bilibili"))
    monkeypatch.setattr(ingest, "ingest_rss", fake_ingest("rss"))
    monkeypatch.setattr(ingest, "ingest_hackernews", fake_ingest("hackernews"))
    monkeypatch.setattr(ingest, "ingest_reddit", fake_ingest("reddit"))
    monkeypatch.setattr(ingest, "ingest_github_trending", fake_ingest("github"))
    monkeypatch.setattr(ingest, "ingest_lingowhale", fake_ingest("lingowhale"))
    monkeypatch.setattr(ingest, "ingest_waytoagi", fake_ingest("waytoagi"))

    assert ingest.main() == 0

    assert ("start", None) in calls
    assert ("finish", None, 1747, {
        "twitter": 0,
        "xiaohongshu": 0,
        "bilibili": 0,
        "rss": 0,
        "hackernews": 0,
        "reddit": 0,
        "github": 0,
        "lingowhale": 0,
        "waytoagi": 0,
    }, None) in calls
    assert all(call[1] is None for call in calls if call[0] not in {"start", "finish"})


def test_enrich_backend_can_enable_supabase(monkeypatch):
    import remote_db

    _isolate_remote_env(monkeypatch, remote_db)
    monkeypatch.setenv("INFO2ACTION_ENRICH_BACKEND", "supabase")

    assert remote_db.enrich_backend() == "supabase"
    assert remote_db.enrich_to_remote() is True


def test_remote_enrichment_writer_updates_items(monkeypatch):
    import remote_db

    class FakeCursor:
        def fetchall(self):
            return []

    class FakeConn:
        def __init__(self):
            self.calls = []
            self.commits = 0

        def execute(self, sql, params=None):
            self.calls.append((" ".join(sql.split()), params))
            return FakeCursor()

        def commit(self):
            self.commits += 1

    fake = FakeConn()
    remote_db.write_enrichment_remote(
        fake,
        "i1",
        {
            "summary": "s",
            "key_points": ["a"],
            "keywords": ["k"],
            "dimensions": {"novelty": 1},
            "categories": ["models"],
            "subcategories": ["llm"],
            "ai_extracted": {"models": ["M"]},
            "visible": True,
            "category": "models",
            "content_type": "news",
            "quality_score": 0.8,
            "relevance_score": 0.7,
        },
    )

    assert fake.commits == 1
    assert any("update remote_poc.items" in sql.lower() for sql, _ in fake.calls)
    assert any("ai_summary" in sql for sql, _ in fake.calls)


def test_enrich_items_routes_success_and_failure_to_remote(monkeypatch):
    import enrich_items

    calls = []
    monkeypatch.setattr(enrich_items.remote_db, "enrich_to_remote", lambda: True, raising=False)
    monkeypatch.setattr(
        enrich_items.remote_db,
        "write_enrichment_remote",
        lambda conn, item_id, parsed: calls.append(("write", conn, item_id, parsed["summary"])),
        raising=False,
    )
    monkeypatch.setattr(
        enrich_items.remote_db,
        "record_ai_failure_remote",
        lambda conn, item_id, error, retry_after=None, increment=True: calls.append(
            ("failure", conn, item_id, error, retry_after, increment)
        ),
        raising=False,
    )
    monkeypatch.setattr(
        enrich_items.db,
        "get_conn",
        lambda: pytest.fail("remote enrichment should not open SQLite"),
    )

    enrich_items.write_enrichment_current("i1", {"summary": "ok"})
    enrich_items.record_failure("i2", "bad", retry_after=10, increment=False)

    assert calls == [
        ("write", None, "i1", "ok"),
        ("failure", None, "i2", "bad", 10, False),
    ]


def test_publish_run_remote_batches_cluster_and_action_updates(monkeypatch):
    import remote_db

    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setenv("INFO2ACTION_PUBLISH_RUN_BATCH_SIZE", "2")

    class FakeCursor:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    class FakeConn:
        def __init__(self):
            self.calls = []
            self.commits = 0
            self.select_calls = 0

        def execute(self, sql, params=None):
            compact = " ".join(sql.split())
            self.calls.append((compact, params))
            if "SELECT id, COALESCE(live_version, 0) + 1 AS new_version" in compact:
                self.select_calls += 1
                if self.select_calls == 1:
                    return FakeCursor([{"id": 11, "new_version": 2}, {"id": 12, "new_version": 4}])
                return FakeCursor([])
            if "UPDATE remote_poc.clusters c" in compact:
                return FakeCursor([{"id": 11, "new_version": 2}, {"id": 12, "new_version": 4}])
            return FakeCursor([])

        def commit(self):
            self.commits += 1

    fake = FakeConn()

    assert remote_db.publish_run_remote(fake, 1395) == 2

    assert len(fake.calls) == 4
    select_sql, select_params = fake.calls[0]
    cluster_sql, cluster_params = fake.calls[1]
    action_sql, action_params = fake.calls[2]
    assert "LIMIT %s" in select_sql
    assert select_params == (1395, 2)
    assert "UPDATE remote_poc.clusters c" in cluster_sql
    assert "VALUES (%s, %s), (%s, %s)" in cluster_sql
    assert cluster_params[:4] == (11, 2, 12, 4)
    assert "UPDATE remote_poc.actions a" in action_sql
    assert action_params == (11, 2, 12, 4)
    assert fake.commits == 1


def test_recall_top_k_clusters_remote_applies_temporal_filters(monkeypatch):
    import remote_db

    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")

    class FakeCursor:
        def fetchall(self):
            return []

    class FakeConn:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=None):
            self.calls.append((" ".join(sql.split()), params))
            return FakeCursor()

    fake = FakeConn()

    remote_db.recall_top_k_clusters_remote(
        fake,
        [1.0, 0.0, 0.0],
        k=5,
        window_days=30,
        cosine_min=0.75,
        item_time="2026-05-27T12:00:00+00:00",
        temporal_adjacency_days=3.0,
        max_merged_span_days=7.0,
    )

    sql, params = fake.calls[0]
    lower = sql.lower()

    assert "now() - (%s::int * interval '1 day')" not in lower
    assert "coalesce(c.first_doc_at, c.last_doc_at, c.last_updated_at)" in lower
    assert "coalesce(c.last_doc_at, c.first_doc_at, c.last_updated_at)" in lower
    assert "operator(extensions.<=>)" in lower
    assert " <=> " not in lower
    assert "greatest(" in lower
    assert "least(" in lower
    assert params[2] == 3.0
    assert params[4] == 3.0
    assert params[7] == 7.0


def test_recall_top_k_clusters_remote_keeps_window_without_item_time(monkeypatch):
    import remote_db

    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")

    class FakeCursor:
        def fetchall(self):
            return []

    class FakeConn:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=None):
            self.calls.append((" ".join(sql.split()), params))
            return FakeCursor()

    fake = FakeConn()

    remote_db.recall_top_k_clusters_remote(
        fake,
        [1.0, 0.0, 0.0],
        k=5,
        window_days=30,
        cosine_min=0.75,
    )

    sql, _params = fake.calls[0]
    assert "c.last_updated_at > now() - (%s::int * interval '1 day')" in sql
    assert "OPERATOR(extensions.<=>)" in sql
    assert " <=> " not in sql


def test_embedding_backend_can_enable_supabase(monkeypatch):
    import remote_db

    _isolate_remote_env(monkeypatch, remote_db)
    monkeypatch.setenv("INFO2ACTION_EMBEDDING_BACKEND", "supabase")

    assert remote_db.embedding_backend() == "supabase"
    assert remote_db.embedding_to_remote() is True


def test_cluster_backend_can_enable_supabase(monkeypatch):
    import remote_db

    _isolate_remote_env(monkeypatch, remote_db)
    monkeypatch.setenv("INFO2ACTION_CLUSTER_BACKEND", "supabase")

    assert remote_db.cluster_backend() == "supabase"
    assert remote_db.cluster_to_remote() is True


def test_app_state_backend_can_enable_supabase(monkeypatch):
    import remote_db

    _isolate_remote_env(monkeypatch, remote_db)
    monkeypatch.setenv("INFO2ACTION_APP_STATE_BACKEND", "supabase")

    assert remote_db.app_state_backend() == "supabase"
    assert remote_db.app_state_to_remote() is True


def test_remote_asr_quota_uses_postgres_upsert(monkeypatch):
    import remote_db

    class FakeCursor:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class FakeConn:
        def __init__(self):
            self.calls = []
            self.commits = 0
            self.used = 0

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.calls.append((normalized, params))
            if "select seconds_used from remote_poc.asr_usage" in normalized.lower():
                return FakeCursor({"seconds_used": self.used})
            if "insert into remote_poc.asr_usage" in normalized.lower():
                self.used += int(params[2])
            return FakeCursor()

        def commit(self):
            self.commits += 1

    monkeypatch.setattr(remote_db, "_asr_today_cst", lambda: "2026-05-13")
    fake = FakeConn()

    usage = remote_db.consume_asr_quota_remote(fake, 1800, user_id="u1")
    allowed, checked = remote_db.check_asr_quota_remote(fake, 1800, user_id="u1")

    assert usage["seconds_used"] == 1800
    assert checked["seconds_used"] == 1800
    assert allowed is True
    assert fake.commits == 1
    assert any("on conflict (user_id, date_cst) do update" in sql.lower() for sql, _ in fake.calls)


def test_asr_status_dispatches_to_remote_backend(monkeypatch):
    import routes.asr as asr

    monkeypatch.setattr(asr.remote_db, "app_state_to_remote", lambda: True)
    monkeypatch.setattr(
        asr.remote_db,
        "get_item_asr_state_remote",
        lambda item_id: {
            "id": item_id,
            "platform": "twitter",
            "asr_status": "success",
            "asr_text": "hello",
            "asr_segments": [{"text": "hello"}],
        },
    )
    monkeypatch.setattr(asr.db, "get_conn", lambda: pytest.fail("remote ASR status should not open SQLite"))

    result = asyncio.run(asr.get_asr_status(_request(), "remote-video"))

    assert result["id"] == "remote-video"
    assert result["asr_segments"] == [{"text": "hello"}]


def test_trigger_asr_persists_running_before_background_task(monkeypatch):
    import routes.asr as asr

    remote_state = {
        "id": "remote-video",
        "platform": "twitter",
        "asr_status": None,
        "asr_text": None,
        "ai_summary": None,
    }
    created = []

    def fake_update(item_id, **fields):
        assert item_id == "remote-video"
        remote_state.update(fields)

    def fake_create_task(coro):
        created.append(coro)
        return SimpleNamespace()

    request = SimpleNamespace(
        state=SimpleNamespace(user={"id": 7}),
        app=SimpleNamespace(state=SimpleNamespace(user_asr_sems={}, asr_event_buses={})),
    )

    monkeypatch.setattr(asr.remote_db, "app_state_to_remote", lambda: True)
    monkeypatch.setattr(asr.remote_db, "get_item_asr_state_remote", lambda item_id: dict(remote_state, id=item_id))
    monkeypatch.setattr(asr.remote_db, "update_item_asr_fields_remote", fake_update)
    monkeypatch.setattr(asr.asyncio, "create_task", fake_create_task)

    async def scenario():
        result = await asr.trigger_asr(request, "remote-video")
        assert result == {"task_id": "remote-video", "status": "running"}
        assert remote_state["asr_status"] == "running"
        status = await asr.get_asr_status(_request(), "remote-video")
        assert status["asr_status"] == "running"
        created[0].close()

    asyncio.run(scenario())


def test_trigger_asr_worker_crash_marks_failed(monkeypatch):
    import routes.asr as asr

    remote_state = {
        "id": "remote-video",
        "platform": "twitter",
        "asr_status": None,
        "asr_text": None,
        "ai_summary": None,
    }
    created = []

    async def fake_transcribe_and_summarize(*args, **kwargs):
        raise RuntimeError("boom")

    def fake_update(item_id, **fields):
        assert item_id == "remote-video"
        remote_state.update(fields)

    def fake_create_task(coro):
        created.append(coro)
        return SimpleNamespace()

    request = SimpleNamespace(
        state=SimpleNamespace(user={"id": 7}),
        app=SimpleNamespace(state=SimpleNamespace(user_asr_sems={}, asr_event_buses={})),
    )

    monkeypatch.setattr(asr.remote_db, "app_state_to_remote", lambda: True)
    monkeypatch.setattr(asr.remote_db, "get_item_asr_state_remote", lambda item_id: dict(remote_state, id=item_id))
    monkeypatch.setattr(asr.remote_db, "update_item_asr_fields_remote", fake_update)
    monkeypatch.setattr(asr.asr_worker, "transcribe_and_summarize", fake_transcribe_and_summarize)
    monkeypatch.setattr(asr.asyncio, "create_task", fake_create_task)

    async def scenario():
        result = await asr.trigger_asr(request, "remote-video")
        assert result["status"] == "running"
        await created[0]
        assert remote_state["asr_status"] == "failed_asr"
        assert remote_state["asr_failed_reason"].startswith("worker_crash:")

    asyncio.run(scenario())


def test_asr_worker_status_and_media_route_to_remote_backend(monkeypatch):
    import asr_worker

    calls = {}

    class FakeConn:
        def execute(self, *args, **kwargs):
            pytest.fail("remote ASR worker should not execute SQLite SQL")

        def commit(self):
            pytest.fail("remote ASR worker should not commit SQLite")

    monkeypatch.setattr(asr_worker.remote_db, "app_state_to_remote", lambda: True)
    monkeypatch.setattr(
        asr_worker.remote_db,
        "update_item_asr_fields_remote",
        lambda item_id, **fields: calls.update({"item_id": item_id, "fields": fields}),
    )
    monkeypatch.setattr(
        asr_worker.remote_db,
        "get_item_media_json_remote",
        lambda item_id: json.dumps([{"type": "video", "url": "https://video.example/x.mp4"}]),
    )

    asr_worker._write_asr_status(FakeConn(), "remote-video", asr_status="running")

    assert calls["item_id"] == "remote-video"
    assert calls["fields"]["asr_status"] == "running"
    assert "asr_attempted_at" in calls["fields"]
    assert asr_worker._find_media_url_for_asr(FakeConn(), "remote-video") == "https://video.example/x.mp4"


def test_remote_feed_detail_select_includes_asr_fields():
    import remote_db

    cols = remote_db._feed_cols(include_content=True, include_heavy_json=True)

    for fragment in (
        "i.asr_text",
        "i.asr_status",
        "i.asr_duration_sec",
        "i.asr_cost_yuan",
        "i.asr_attempted_at",
        "i.asr_segments",
        "i.asr_text_cn",
        "i.asr_segments_cn",
    ):
        assert fragment in cols

    normalized = remote_db._normalize_item(
        {
            "id": "remote-video",
            "platform": "twitter",
            "ai_category": "Coding",
            "asr_segments": json.dumps([{"start_ms": 0, "end_ms": 1000, "text": "hello"}]),
            "asr_segments_cn": json.dumps(["你好"]),
            "asr_attempted_at": "2026-05-25T01:02:03Z",
        },
        detail=True,
    )

    assert normalized["asr_segments"] == [{"start_ms": 0, "end_ms": 1000, "text": "hello"}]
    assert normalized["asr_segments_cn"] == ["你好"]
    assert normalized["asr_attempted_at"] == "2026-05-25T01:02:03Z"


def test_twitter_poster_uses_remote_asset_storage(monkeypatch, tmp_path):
    import routes.media as media

    uploads = {}
    generated = tmp_path / "poster.jpg"

    monkeypatch.setattr(media.remote_db, "asset_storage_to_remote", lambda: True)
    monkeypatch.setattr(media.remote_db, "download_asset_bytes_remote", lambda path: None)
    monkeypatch.setattr(media, "_get_twitter_mp4_url", lambda item_id: "https://video.example/x.mp4")

    def fake_generate(item_id, mp4_url, cache_path):
        assert item_id == "remotevideo"
        assert mp4_url == "https://video.example/x.mp4"
        generated.write_bytes(b"jpg-bytes")
        with open(cache_path, "wb") as f:
            f.write(generated.read_bytes())

    def fake_upload(object_path, data, **kwargs):
        uploads.update({"object_path": object_path, "data": data, "kwargs": kwargs})

    monkeypatch.setattr(media, "_generate_poster_file", fake_generate)
    monkeypatch.setattr(media.remote_db, "upload_asset_bytes_remote", fake_upload)

    response = asyncio.run(media.twitter_poster("remotevideo", SimpleNamespace(headers={})))

    assert response.body == b"jpg-bytes"
    assert uploads["object_path"] == "video_posters/remotevideo.jpg"
    assert uploads["kwargs"]["kind"] == "video_poster"


def test_remote_storage_download_treats_supabase_not_found_body_as_cache_miss(monkeypatch):
    import remote_db

    _isolate_remote_env(monkeypatch, remote_db)
    monkeypatch.setattr(remote_db, "_storage_object_url", lambda path: f"https://storage.test/{path}")
    monkeypatch.setattr(remote_db, "_storage_headers", lambda *args, **kwargs: {})

    def fake_urlopen(*args, **kwargs):
        raise urllib.error.HTTPError(
            url="https://storage.test/video_posters/missing.jpg",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=io.BytesIO(b'{"statusCode":"404","error":"not_found","message":"Object not found"}'),
        )

    monkeypatch.setattr(remote_db.urllib.request, "urlopen", fake_urlopen)

    assert remote_db.download_asset_bytes_remote("video_posters/missing.jpg") is None


def test_briefing_route_dispatches_to_remote_backend(monkeypatch):
    import routes.briefing as briefing

    monkeypatch.setattr(briefing.remote_db, "app_state_to_remote", lambda: True)
    monkeypatch.setattr(briefing.remote_db, "get_briefing_remote", lambda date: {"date": date})
    monkeypatch.setattr(briefing.remote_db, "list_briefing_dates_remote", lambda: ["2026-05-13"])
    monkeypatch.setattr(briefing.db, "get_conn", lambda: pytest.fail("remote briefing should not open SQLite"))

    result = briefing.get_briefing(date="2026-05-13")

    assert result == {"briefing": {"date": "2026-05-13"}, "dates": ["2026-05-13"]}


def test_feedback_post_writes_remote_store(monkeypatch):
    import routes.feed as feed

    calls = []

    class FakeRequest:
        state = SimpleNamespace(user=None, legacy_authenticated=True)

        async def json(self):
            return {
                "item_id": "remote-item",
                "type": "positive",
                "topic": "AI",
                "text": "good",
            }

    monkeypatch.setattr(feed.remote_db, "app_state_to_remote", lambda: True)
    monkeypatch.setattr(feed.remote_db, "app_state_backend", lambda: "supabase")
    monkeypatch.setattr(
        feed.remote_db,
        "get_feedback_item_context_remote",
        lambda item_id: {
            "id": item_id,
            "user_id": None,
            "platform": "twitter",
            "title": "T",
            "author_name": "A",
            "url": "https://x",
            "ai_summary": "S",
        },
    )
    monkeypatch.setattr(
        feed.remote_db,
        "add_feedback_remote",
        lambda *args: calls.append(("feedback", args)),
    )
    monkeypatch.setattr(
        feed.remote_db,
        "record_item_feedback_remote",
        lambda **kwargs: calls.append(("item_feedback", kwargs)),
    )
    monkeypatch.setattr(feed.db, "get_conn", lambda: pytest.fail("remote feedback should not open SQLite"))

    result = asyncio.run(feed.post_feedback(FakeRequest()))

    assert result["ok"] is True
    assert calls[0] == ("feedback", ("remote-item", "positive", "AI", "good"))
    assert calls[1][0] == "item_feedback"
    assert calls[1][1]["author"] == "A"


def test_remote_embedding_writer_updates_pgvector(monkeypatch):
    import remote_db

    class FakeCursor:
        pass

    class FakeConn:
        def __init__(self):
            self.calls = []
            self.commits = 0

        def execute(self, sql, params=None):
            self.calls.append((" ".join(sql.split()), params))
            return FakeCursor()

        def commit(self):
            self.commits += 1

    fake = FakeConn()
    remote_db.update_item_embedding_remote(fake, "i1", [0.1, 0.2], "fake-provider")

    assert fake.commits == 1
    sql, params = fake.calls[0]
    assert "update remote_poc.items" in sql.lower()
    assert params[0] == "[0.1,0.2]"
    assert params[1] == "fake-provider"


def test_remote_cluster_writer_creates_singleton_and_membership(monkeypatch):
    import remote_db

    class FakeCursor:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

        def fetchall(self):
            return []

    class FakeConn:
        def __init__(self):
            self.calls = []
            self.commits = 0

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.calls.append((normalized, params))
            if "returning id" in normalized.lower():
                return FakeCursor({"id": 456})
            if "select id, url from remote_poc.items" in normalized.lower():
                return FakeCursor({"id": "i1", "url": "https://example.test/a"})
            if "count(distinct" in normalized.lower():
                return FakeCursor({"doc_count": 1, "unique_source_count": 1})
            if "select distinct i.platform" in normalized.lower():
                return FakeCursor()
            return FakeCursor()

        def commit(self):
            self.commits += 1

    fake = FakeConn()
    cluster_id = remote_db.create_singleton_cluster_remote(
        fake,
        "i1",
        [0.1, 0.2],
        "2026-05-13T00:00:00Z",
        run_id=123,
    )

    assert cluster_id == 456
    assert any("pg_advisory_xact_lock" in sql.lower() for sql, _ in fake.calls)
    assert any("setval" in sql.lower() and "clusters_id_seq" in sql for sql, _ in fake.calls)
    assert any("set local statement_timeout = '300000ms'" in sql.lower() for sql, _ in fake.calls)
    cluster_insert_sql = next(sql for sql, _ in fake.calls if "insert into remote_poc.clusters" in sql.lower())
    assert "representative_vector" not in cluster_insert_sql
    assert any("insert into remote_poc.cluster_items" in sql.lower() for sql, _ in fake.calls)
    assert any("update remote_poc.items set cluster_id" in sql.lower() for sql, _ in fake.calls)
    assert any("representative_vector = coalesce" in sql.lower() for sql, _ in fake.calls)
    assert fake.commits >= 1


def test_remote_cluster_judge_log_returns_id():
    import remote_db

    class FakeCursor:
        def fetchone(self):
            return {"id": 789}

    class FakeConn:
        def __init__(self):
            self.calls = []
            self.commits = 0

        def execute(self, sql, params=None):
            self.calls.append((" ".join(sql.split()), params))
            return FakeCursor()

        def commit(self):
            self.commits += 1

    fake = FakeConn()
    log_id = remote_db.write_judge_log_remote(
        fake,
        item_id="i1",
        candidate_cluster_ids=[1, 2],
        estimated_input_tokens=12,
        matches=[{"cluster_id": 1, "same_event": True}],
        selected_cluster_id=1,
        selection_reason="match",
        possible_merge_candidates=[2],
        decision_model="model",
    )

    assert log_id == 789
    assert fake.commits == 1
    assert any("setval" in sql.lower() and "cluster_judge_log_id_seq" in sql for sql, _ in fake.calls)
    assert any("insert into remote_poc.cluster_judge_log" in sql.lower() for sql, _ in fake.calls)


def test_pipeline_cluster_writes_route_to_remote(monkeypatch):
    from clustering import pipeline

    calls = []
    monkeypatch.setattr(pipeline.remote_db, "cluster_to_remote", lambda: True, raising=False)
    monkeypatch.setattr(
        pipeline.remote_db,
        "add_item_to_cluster_remote",
        lambda conn, cluster_id, item_id, **kwargs: calls.append(
            ("add", conn, cluster_id, item_id, kwargs)
        ),
        raising=False,
    )
    monkeypatch.setattr(
        pipeline.remote_db,
        "mark_cluster_touched_by_run_remote",
        lambda conn, cluster_id, run_id: calls.append(("touch", conn, cluster_id, run_id)),
        raising=False,
    )

    pipeline._add_item_to_cluster(
        object(),
        10,
        "i1",
        source_identity="source",
        join_decision_id=7,
    )
    pipeline._mark_cluster_touched_by_run(object(), 10, 99)

    assert calls[0] == (
        "add",
        None,
        10,
        "i1",
        {
            "rank_in_cluster": 9999,
            "is_primary_source": 0,
            "source_identity": "source",
            "join_decision_id": 7,
        },
    )
    assert calls[1] == ("touch", None, 10, 99)


def test_pipeline_embed_pending_routes_to_remote(monkeypatch):
    from clustering import pipeline

    calls = {}
    monkeypatch.setattr(pipeline.remote_db, "embedding_to_remote", lambda: True, raising=False)
    monkeypatch.setattr(
        pipeline,
        "_embed_pending_items_remote",
        lambda provider, batch_size=16, **kwargs: calls.update(
            {"provider": provider, "batch_size": batch_size, **kwargs}
        ) or 3,
        raising=False,
    )

    result = pipeline._embed_pending_items(
        object(),
        provider="provider",
        batch_size=4,
        run_id=99,
        window_start="2026-05-13T00:00:00Z",
    )

    assert result == 3
    assert calls["provider"] == "provider"
    assert calls["batch_size"] == 4
    assert calls["run_id"] == 99


def test_remote_pending_enrichment_query_sets_statement_timeout(monkeypatch):
    import remote_db

    calls = []

    class FakeCursor:
        def fetchall(self):
            return []

    class FakeConn:
        def execute(self, sql, params=None):
            calls.append((" ".join(str(sql).split()), params))
            return FakeCursor()

    @contextmanager
    def fake_connect():
        yield FakeConn()

    monkeypatch.setenv(remote_db.REMOTE_PENDING_SCAN_TIMEOUT_MS_ENV, "12345")
    monkeypatch.setattr(remote_db, "connect", fake_connect)

    rows = remote_db.query_pending_enrichment_items_remote(limit=10)

    assert rows == []
    assert calls[0] == ("SET LOCAL statement_timeout = '12345ms'", None)
    assert "FROM remote_poc.items" in calls[1][0]


def test_remote_pending_highlight_query_can_rescore_prompt_mismatch(monkeypatch):
    import remote_db

    calls = []

    class FakeCursor:
        def fetchall(self):
            return []

    class FakeConn:
        def execute(self, sql, params=None):
            calls.append((" ".join(str(sql).split()), params))
            return FakeCursor()

    @contextmanager
    def fake_connect():
        yield FakeConn()

    monkeypatch.setattr(remote_db, "connect", fake_connect)

    rows = remote_db.query_pending_highlight_verdict_items_remote(
        limit=10,
        window_start="2026-06-13T00:00:00Z",
        rescore_prompt_version="item_verdict_v3_6_preference_calibrated_2026_06_16",
    )

    assert rows == []
    sql, params = calls[1]
    assert "highlight_verdict IS NULL OR highlight_prompt_version IS DISTINCT FROM %s" in sql
    assert "COALESCE(published_at, fetched_at) >= %s" in sql
    assert params[-2:] == ("item_verdict_v3_6_preference_calibrated_2026_06_16", 10)


def test_highlights_verdict_filter_disabled_by_default(monkeypatch):
    import remote_db

    monkeypatch.delenv("INFO2ACTION_HIGHLIGHTS_VERDICT_FILTER_ENABLED", raising=False)

    assert remote_db._highlights_verdict_cluster_filter("remote_poc", "c") == ""


def test_highlights_verdict_filter_requires_included_item_when_enabled(monkeypatch):
    import remote_db

    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_VERDICT_FILTER_ENABLED", "1")
    monkeypatch.delenv("INFO2ACTION_HIGHLIGHTS_VERDICT_FILTER_RECENT_DAYS", raising=False)

    sql = " ".join(remote_db._highlights_verdict_cluster_filter("remote_poc", "c").split())

    assert "EXISTS" in sql
    assert "remote_poc.cluster_items" in sql
    assert "remote_poc.items" in sql
    assert "highlight_include_in_highlights IS TRUE" in sql
    assert "last_doc_at" not in sql


def test_highlights_verdict_filter_recent_days_keeps_older_clusters(monkeypatch):
    import remote_db

    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_VERDICT_FILTER_ENABLED", "1")
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_VERDICT_FILTER_RECENT_DAYS", "3")

    sql = " ".join(remote_db._highlights_verdict_cluster_filter("remote_poc", "c").split())

    assert "COALESCE(c.last_doc_at, c.first_doc_at, c.last_updated_at, now())" in sql
    assert "< now() - (3::int * interval '1 day')" in sql
    assert "OR EXISTS" in sql
    assert "highlight_include_in_highlights IS TRUE" in sql


def test_write_highlight_verdict_remote_updates_item_fields(monkeypatch):
    import remote_db

    monkeypatch.setattr(remote_db, "_maybe_jsonb", lambda value: value)
    calls = []

    class FakeConn:
        def execute(self, sql, params=None):
            calls.append((" ".join(str(sql).split()), params))

        def commit(self):
            calls.append(("commit", None))

    remote_db.write_highlight_verdict_remote(
        FakeConn(),
        "item-1",
        {
            "highlight_verdict": "borderline",
            "highlight_value_path": "lead_value",
            "highlight_uncertainty": "thin_detail",
            "highlight_include_in_highlights": True,
            "highlight_reason": "③线索价值",
            "highlight_scores": {"importance": 1},
            "highlight_ai_relevant": "yes",
            "highlight_spam": 2,
            "highlight_confidence": 0.8,
            "highlight_prompt_version": "prompt-v",
            "highlight_model": "model-v",
            "highlight_scored_at": "2026-06-16T00:00:00+00:00",
        },
    )

    sql, params = calls[0]
    assert "UPDATE remote_poc.items" in sql
    assert "highlight_include_in_highlights" in sql
    assert params[0:4] == ("borderline", "lead_value", "thin_detail", True)
    assert params[-1] == "item-1"
    assert calls[-1] == ("commit", None)


def test_record_highlight_verdict_failure_remote_uses_dedicated_fields():
    import remote_db

    calls = []

    class FakeConn:
        def execute(self, sql, params=None):
            calls.append((" ".join(str(sql).split()), params))

        def commit(self):
            calls.append(("commit", None))

    remote_db.record_highlight_verdict_failure_remote(
        FakeConn(),
        "item-1",
        "LLM timeout",
        retry_after=60,
    )

    sql, params = calls[0]
    assert "highlight_error_count" in sql
    assert "ai_error_count" not in sql
    assert params[0] == "LLM timeout"
    assert params[-1] == "item-1"
    assert calls[-1] == ("commit", None)


def test_write_highlight_exclusion_review_remote_appends_review():
    import remote_db

    calls = []

    class FakeConn:
        def execute(self, sql, params=None):
            calls.append((" ".join(str(sql).split()), params))

        def commit(self):
            calls.append(("commit", None))

    remote_db.write_highlight_exclusion_review_remote(
        FakeConn(),
        cluster_id=123,
        human_verdict="should_feature",
        machine_decision_at="2026-06-16T00:00:00+00:00",
        error_kind="value_path",
        notes="应该进精选",
        reviewer="dbwu",
    )

    sql, params = calls[0]
    assert "INSERT INTO remote_poc.highlight_exclusion_reviews" in sql
    assert params[0] == 123
    assert params[2] == "should_feature"
    assert params[3] == "value_path"
    assert calls[-1] == ("commit", None)


def test_query_highlight_cluster_decisions_remote_includes_latest_review(monkeypatch):
    import remote_db

    calls = []

    class FakeCursor:
        def fetchall(self):
            return []

    class FakeConn:
        def execute(self, sql, params=None):
            calls.append((" ".join(str(sql).split()), params))
            return FakeCursor()

    class FakeConnect:
        def __enter__(self):
            return FakeConn()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(remote_db, "connect", lambda: FakeConnect())

    rows = remote_db.query_highlight_cluster_decisions_remote(decision="excluded", limit=55)

    assert rows == []
    sql, params = calls[0]
    assert "LEFT JOIN LATERAL" in sql
    assert "highlight_exclusion_reviews" in sql
    assert "latest_human_verdict" in sql
    assert params == ("excluded", 55)


def test_query_highlight_cluster_decisions_remote_filters_cluster_verdict(monkeypatch):
    import remote_db

    calls = []

    class FakeCursor:
        def fetchall(self):
            return []

    class FakeConn:
        def execute(self, sql, params=None):
            calls.append((" ".join(str(sql).split()), params))
            return FakeCursor()

    class FakeConnect:
        def __enter__(self):
            return FakeConn()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(remote_db, "connect", lambda: FakeConnect())

    rows = remote_db.query_highlight_cluster_decisions_remote(
        decision="excluded",
        cluster_verdict="drop",
        limit=500,
    )

    assert rows == []
    sql, params = calls[0]
    assert "d.decision = %s AND d.cluster_verdict = %s" in sql
    assert params == ("excluded", "drop", 500)


def test_query_highlight_cluster_decisions_remote_filters_recent_days(monkeypatch):
    import remote_db

    calls = []

    class FakeCursor:
        def fetchall(self):
            return []

    class FakeConn:
        def execute(self, sql, params=None):
            calls.append((" ".join(str(sql).split()), params))
            return FakeCursor()

    class FakeConnect:
        def __enter__(self):
            return FakeConn()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(remote_db, "connect", lambda: FakeConnect())

    rows = remote_db.query_highlight_cluster_decisions_remote(
        decision="excluded",
        cluster_verdict="drop",
        recent_days=3,
        limit=500,
    )

    assert rows == []
    sql, params = calls[0]
    assert "COALESCE(c.last_doc_at, c.first_doc_at, c.last_updated_at, d.decided_at)" in sql
    assert ">= now() - (%s::int * interval '1 day')" in sql
    assert params == ("excluded", "drop", 3, 500)


def test_pipeline_remote_embed_pending_sets_statement_timeout(monkeypatch):
    from clustering import pipeline

    calls = []

    class FakeCursor:
        def fetchall(self):
            return []

    class FakeConn:
        def execute(self, sql, params=None):
            calls.append((" ".join(str(sql).split()), params))
            return FakeCursor()

    @contextmanager
    def fake_connect():
        yield FakeConn()

    monkeypatch.setenv(pipeline.remote_db.REMOTE_PENDING_SCAN_TIMEOUT_MS_ENV, "23456")
    monkeypatch.setattr(pipeline.remote_db, "connect", fake_connect)

    result = pipeline._embed_pending_items_remote(object(), batch_size=4, run_id=99)

    assert result == 0
    assert calls[0] == ("SET LOCAL statement_timeout = '23456ms'", None)
    assert "FROM remote_poc.items" in calls[1][0]


def test_remote_feed_list_omits_content_but_detail_selects_it():
    import remote_db

    list_cols = remote_db._feed_cols(None)
    detail_cols = remote_db._feed_cols(None, include_content=True)

    assert "i.content," not in list_cols
    assert "i.content," in detail_cols


def test_data_authority_defaults_to_local(monkeypatch):
    import remote_db

    _isolate_remote_env(monkeypatch, remote_db)

    assert remote_db.data_authority() == "local"
    assert remote_db.remote_authority_enabled() is False


def test_remote_authority_requires_all_production_surfaces(monkeypatch):
    import remote_db

    _isolate_remote_env(monkeypatch, remote_db)
    monkeypatch.setenv(remote_db.DATA_AUTHORITY_ENV, "supabase")
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://example.test/postgres")
    monkeypatch.setenv(remote_db.FEED_BACKEND_ENV, "supabase_poc")
    monkeypatch.setenv(remote_db.BACKEND_ENV, "sqlite")
    monkeypatch.setenv(remote_db.STATUS_BACKEND_ENV, "supabase_poc")

    with pytest.raises(remote_db.RemoteDBConfigError, match=remote_db.BACKEND_ENV):
        remote_db.assert_remote_authority_ready()


def test_remote_authority_accepts_global_backend(monkeypatch):
    import remote_db

    _isolate_remote_env(monkeypatch, remote_db)
    monkeypatch.setenv(remote_db.DATA_AUTHORITY_ENV, "supabase")
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://example.test/postgres")
    monkeypatch.setenv(remote_db.GLOBAL_BACKEND_ENV, "supabase_poc")

    readiness = remote_db.assert_remote_authority_ready()

    assert readiness["authority"] == "supabase"
    assert readiness["schema"] == remote_db.DEFAULT_REMOTE_SCHEMA
    assert readiness["backends"] == {
        "feed": "supabase_poc",
        "event": "supabase_poc",
        "status": "supabase_poc",
    }


def test_pipeline_write_mode_defaults_to_sqlite_then_sync(monkeypatch):
    import remote_db

    _isolate_remote_env(monkeypatch, remote_db)

    readiness = remote_db.assert_pipeline_write_mode_ready()

    assert readiness == {
        "mode": "sqlite_then_sync",
        "remote_sync_after_pipeline": False,
    }


def test_pipeline_write_mode_reports_enabled_sync(monkeypatch):
    import remote_db

    _isolate_remote_env(monkeypatch, remote_db)
    monkeypatch.setenv("INFO2ACTION_REMOTE_SYNC_AFTER_PIPELINE", "1")

    readiness = remote_db.assert_pipeline_write_mode_ready()

    assert readiness == {
        "mode": "sqlite_then_sync",
        "remote_sync_after_pipeline": True,
    }


def test_pipeline_write_mode_requires_all_direct_writers_for_supabase_direct(monkeypatch):
    import remote_db

    _isolate_remote_env(monkeypatch, remote_db)
    monkeypatch.setenv("INFO2ACTION_PIPELINE_WRITE_MODE", "supabase_direct")

    with pytest.raises(remote_db.RemoteDBConfigError, match="requires direct Supabase writers"):
        remote_db.assert_pipeline_write_mode_ready()


def test_pipeline_write_mode_accepts_supabase_direct_when_all_writers_enabled(monkeypatch):
    import remote_db

    _isolate_remote_env(monkeypatch, remote_db)
    monkeypatch.setenv("INFO2ACTION_PIPELINE_WRITE_MODE", "supabase_direct")
    monkeypatch.setenv("INFO2ACTION_FETCH_WRITE_BACKEND", "supabase")
    monkeypatch.setenv("INFO2ACTION_ENRICH_BACKEND", "supabase")
    monkeypatch.setenv("INFO2ACTION_EMBEDDING_BACKEND", "supabase")
    monkeypatch.setenv("INFO2ACTION_CLUSTER_BACKEND", "supabase")
    monkeypatch.setenv("INFO2ACTION_APP_STATE_BACKEND", "supabase")

    readiness = remote_db.assert_pipeline_write_mode_ready()

    assert readiness["mode"] == "supabase_direct"
    assert readiness["direct_writers"] == {
        "fetch": "supabase",
        "enrich": "supabase",
        "embedding": "supabase",
        "cluster": "supabase",
        "app_state": "supabase",
    }


def test_remote_only_storage_mode_is_explicit_but_not_ready_yet(monkeypatch):
    import remote_db

    _isolate_remote_env(monkeypatch, remote_db)
    monkeypatch.setenv("INFO2ACTION_STORAGE_MODE", "remote_only")
    monkeypatch.setenv(remote_db.DATA_AUTHORITY_ENV, "supabase")
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://example.test/postgres")
    monkeypatch.setenv(remote_db.GLOBAL_BACKEND_ENV, "supabase_poc")

    with pytest.raises(remote_db.RemoteDBConfigError, match="remote-only target is not ready"):
        remote_db.assert_storage_contract_ready()


def test_remote_only_storage_contract_ready_when_all_remote_surfaces_configured(monkeypatch):
    import remote_db

    _isolate_remote_env(monkeypatch, remote_db)
    monkeypatch.setenv("INFO2ACTION_STORAGE_MODE", "remote_only")
    monkeypatch.setenv(remote_db.DATA_AUTHORITY_ENV, "supabase")
    monkeypatch.setenv(remote_db.GLOBAL_BACKEND_ENV, "supabase_poc")
    monkeypatch.setenv("INFO2ACTION_FETCH_WRITE_BACKEND", "supabase")
    monkeypatch.setenv("INFO2ACTION_ENRICH_BACKEND", "supabase")
    monkeypatch.setenv("INFO2ACTION_EMBEDDING_BACKEND", "supabase")
    monkeypatch.setenv("INFO2ACTION_CLUSTER_BACKEND", "supabase")
    monkeypatch.setenv("INFO2ACTION_APP_STATE_BACKEND", "supabase")
    monkeypatch.setenv("INFO2ACTION_ASSET_BACKEND", "supabase")
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://example.test/postgres")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role")

    readiness = remote_db.assert_storage_contract_ready()

    assert readiness["remote_only"] is True
    assert readiness["asset_storage"]["remote_assets"] is True


def test_default_storage_contract_keeps_local_dev_safe(monkeypatch):
    import remote_db

    _isolate_remote_env(monkeypatch, remote_db)

    readiness = remote_db.assert_storage_contract_ready()

    assert readiness == {
        "mode": "local",
        "remote_only": False,
        "blockers": [],
    }


def test_remote_authority_global_backend_overrides_stale_sqlite_surface_defaults(monkeypatch):
    import remote_db

    _isolate_remote_env(monkeypatch, remote_db)
    monkeypatch.setenv(remote_db.DATA_AUTHORITY_ENV, "supabase")
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://example.test/postgres")
    monkeypatch.setenv(remote_db.GLOBAL_BACKEND_ENV, "supabase_poc")
    monkeypatch.setenv(remote_db.FEED_BACKEND_ENV, "sqlite")
    monkeypatch.setenv(remote_db.BACKEND_ENV, "sqlite")
    monkeypatch.setenv(remote_db.STATUS_BACKEND_ENV, "sqlite")

    assert remote_db.feed_read_backend() == "supabase_poc"
    assert remote_db.event_read_backend() == "supabase_poc"
    assert remote_db.status_backend() == "supabase_poc"
    assert remote_db.assert_remote_authority_ready()["backends"] == {
        "feed": "supabase_poc",
        "event": "supabase_poc",
        "status": "supabase_poc",
    }


def test_remote_authority_startup_skips_local_sqlite(monkeypatch):
    import app as app_mod

    calls = {"ready": 0, "write": 0, "status": 0, "tmux": 0}

    def fake_ready():
        calls["ready"] += 1
        return {"authority": "supabase"}

    def fake_status():
        calls["status"] += 1
        return {
            "backend": "supabase_poc",
            "schema": "remote_poc",
            "counts": {"items": 12, "clusters": 3},
        }

    def fake_write_ready():
        calls["write"] += 1
        return {
            "mode": "sqlite_then_sync",
            "remote_sync_after_pipeline": True,
        }

    monkeypatch.setattr(app_mod.remote_db, "remote_authority_enabled", lambda: True)
    monkeypatch.setattr(app_mod.remote_db, "assert_remote_authority_ready", fake_ready)
    monkeypatch.setattr(app_mod.remote_db, "assert_pipeline_write_mode_ready", fake_write_ready, raising=False)
    monkeypatch.setattr(app_mod.remote_db, "assert_storage_contract_ready", lambda: {
        "mode": "sqlite_then_sync",
        "remote_only": False,
        "blockers": [],
    }, raising=False)
    monkeypatch.setattr(app_mod.remote_db, "status", fake_status)
    monkeypatch.setattr(app_mod.db, "get_conn", lambda: pytest.fail("remote authority startup should not open SQLite"))
    monkeypatch.setattr(app_mod.terminal, "recover_tmux_sessions", lambda: calls.__setitem__("tmux", calls["tmux"] + 1))

    async def run_lifespan():
        async with app_mod.lifespan(app_mod.app):
            pass

    asyncio.run(run_lifespan())

    assert calls == {"ready": 1, "write": 1, "status": 1, "tmux": 1}


def test_remote_authority_startup_tolerates_status_failure(monkeypatch, caplog):
    """BF-0515-3: status() 撞 transaction pooler statement_timeout 时 lifespan 不应崩溃。

    startup summary 只是诊断日志输出，失败应降级（log warning + 占位 dict），
    不应阻断服务启动 / 不应让 uvicorn 起不来。
    """
    import logging as _logging
    import app as app_mod

    calls = {"ready": 0, "write": 0, "status": 0, "tmux": 0, "yield": 0}

    def fake_ready():
        calls["ready"] += 1
        return {"authority": "supabase"}

    def fake_write_ready():
        calls["write"] += 1
        return {
            "mode": "sqlite_then_sync",
            "remote_sync_after_pipeline": True,
        }

    def boom_status():
        calls["status"] += 1
        raise app_mod.remote_db.RemoteDBError(
            "canceling statement due to statement timeout"
        )

    monkeypatch.setattr(app_mod.remote_db, "remote_authority_enabled", lambda: True)
    monkeypatch.setattr(app_mod.remote_db, "assert_remote_authority_ready", fake_ready)
    monkeypatch.setattr(app_mod.remote_db, "assert_pipeline_write_mode_ready", fake_write_ready, raising=False)
    monkeypatch.setattr(app_mod.remote_db, "assert_storage_contract_ready", lambda: {
        "mode": "sqlite_then_sync",
        "remote_only": False,
        "blockers": [],
    }, raising=False)
    monkeypatch.setattr(app_mod.remote_db, "status", boom_status)
    monkeypatch.setattr(app_mod.remote_db, "data_authority", lambda: "supabase")
    monkeypatch.setattr(app_mod.db, "get_conn", lambda: pytest.fail("remote authority startup should not open SQLite"))
    monkeypatch.setattr(app_mod.terminal, "recover_tmux_sessions", lambda: calls.__setitem__("tmux", calls["tmux"] + 1))

    async def run_lifespan():
        async with app_mod.lifespan(app_mod.app):
            calls["yield"] += 1

    with caplog.at_level(_logging.WARNING):
        asyncio.run(run_lifespan())

    # lifespan 仍完整跑过 (yield 进入 + tmux 注册成功)
    assert calls["yield"] == 1, "lifespan should yield even when status() fails"
    assert calls["tmux"] == 1, "downstream tmux recovery should still run"
    assert calls["status"] == 1, "status() should be called once before failing"
    # warning 日志记录了 startup-summary failure
    assert any(
        "Startup summary failed" in r.getMessage()
        or "statement timeout" in r.getMessage().lower()
        for r in caplog.records
    ), f"expected startup-summary warning in logs, got: {[r.getMessage() for r in caplog.records]}"


def test_lifespan_starts_dynamic_fetch_scheduler_when_enabled(monkeypatch):
    import app as app_mod

    schedulers = []

    class FakeScheduler:
        def __init__(self, start_fetch, **kwargs):
            self.start_fetch = start_fetch
            self.kwargs = kwargs
            self.started = False
            self.stopped = False
            schedulers.append(self)

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

    monkeypatch.setenv("INFO2ACTION_BACKEND_HOURLY_FETCH", "0")
    monkeypatch.setenv("INFO2ACTION_DYNAMIC_FETCH_ENABLED", "1")
    monkeypatch.setenv("INFO2ACTION_CACHE_PREWARM", "0")
    monkeypatch.setattr(app_mod, "BackendFetchScheduler", FakeScheduler)
    monkeypatch.setattr(app_mod.remote_db, "remote_authority_enabled", lambda: False)
    monkeypatch.setattr(app_mod, "_sqlite_startup_summary", lambda: {
        "authority": "sqlite",
        "database": "sqlite",
        "items": 0,
    })
    monkeypatch.setattr(app_mod.terminal, "recover_tmux_sessions", lambda: None)
    monkeypatch.setattr(app_mod.fetch, "recover_orphaned_fetch_runs_from_previous_process", lambda: [])
    monkeypatch.setattr(app_mod.fetch, "has_active_fetch_runs", lambda: False)

    async def run_lifespan():
        async with app_mod.lifespan(app_mod.app):
            assert any(
                scheduler.start_fetch is app_mod.fetch.start_dynamic_micro_fetch
                and scheduler.started
                for scheduler in schedulers
            )

    asyncio.run(run_lifespan())

    dynamic_scheduler = next(
        scheduler for scheduler in schedulers
        if scheduler.start_fetch is app_mod.fetch.start_dynamic_micro_fetch
    )
    assert dynamic_scheduler.stopped is True
    assert dynamic_scheduler.kwargs["should_start"]() is True
    assert dynamic_scheduler.kwargs["sleep_until_next_tick"]() == 60.0


def test_remote_cache_prewarm_skips_events_by_default(monkeypatch):
    import app as app_mod

    calls = {"platforms": 0, "events": 0, "posters": 0}

    monkeypatch.setattr(app_mod.fetch, "has_local_active_fetch_runs", lambda: False)
    monkeypatch.setattr(
        app_mod.remote_db,
        "remote_db_pressure",
        lambda: {"ok": True, "pressure": False, "reasons": []},
    )
    monkeypatch.delenv("INFO2ACTION_PREWARM_EVENTS", raising=False)
    monkeypatch.delenv("INFO2ACTION_PREWARM_POSTERS", raising=False)
    monkeypatch.delenv("INFO2ACTION_POSTER_CACHE_PREWARM", raising=False)
    monkeypatch.setattr(
        app_mod.remote_db,
        "prewarm_platforms",
        lambda: calls.__setitem__("platforms", calls["platforms"] + 1) or {"ok": True},
    )
    monkeypatch.setattr(
        app_mod.remote_db,
        "prewarm_events_categories",
        lambda: calls.__setitem__("events", calls["events"] + 1) or {"success": 1, "failed": 0},
    )

    app_mod._run_remote_cache_prewarm_iteration(1)

    assert calls == {"platforms": 1, "events": 0, "posters": 0}


def test_remote_cache_prewarm_does_not_refresh_highlights_when_platforms_disabled(monkeypatch):
    import app as app_mod

    monkeypatch.setattr(app_mod.fetch, "has_local_active_fetch_runs", lambda: False)
    monkeypatch.setattr(
        app_mod.remote_db,
        "remote_db_pressure",
        lambda: {"ok": True, "pressure": False, "reasons": []},
    )
    monkeypatch.setenv("INFO2ACTION_PREWARM_PLATFORMS", "0")
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL", "1")
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL_STALE_FALLBACK", "0")
    monkeypatch.delenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL_REFRESH", raising=False)
    monkeypatch.setattr(
        app_mod.remote_db,
        "prewarm_platforms",
        lambda: pytest.fail("platform prewarm should remain disabled"),
    )
    monkeypatch.setattr(
        app_mod.remote_db,
        "refresh_highlights_read_model_if_stale",
        lambda **_kwargs: pytest.fail("periodic prewarm must not refresh highlights read model"),
    )

    app_mod._run_remote_cache_prewarm_iteration(1)


def test_remote_cache_prewarm_skips_all_when_fetch_running(monkeypatch):
    import app as app_mod

    monkeypatch.setattr(app_mod.fetch, "has_local_active_fetch_runs", lambda: True)
    monkeypatch.setattr(
        app_mod.remote_db,
        "prewarm_platforms",
        lambda: pytest.fail("periodic prewarm must not run during fetch"),
    )
    monkeypatch.setattr(
        app_mod.remote_db,
        "prewarm_events_categories",
        lambda: pytest.fail("events prewarm must not run during fetch"),
    )

    app_mod._run_remote_cache_prewarm_iteration(1)


def test_remote_cache_prewarm_can_opt_into_events(monkeypatch):
    import app as app_mod

    calls = {"platforms": 0, "events": 0, "posters": 0}

    monkeypatch.setattr(app_mod.fetch, "has_local_active_fetch_runs", lambda: False)
    monkeypatch.setattr(
        app_mod.remote_db,
        "remote_db_pressure",
        lambda: {"ok": True, "pressure": False, "reasons": []},
    )
    monkeypatch.setenv("INFO2ACTION_PREWARM_EVENTS", "1")
    monkeypatch.setenv("INFO2ACTION_PREWARM_POSTERS", "0")
    monkeypatch.setattr(
        app_mod.remote_db,
        "prewarm_platforms",
        lambda: calls.__setitem__("platforms", calls["platforms"] + 1) or {"ok": True},
    )
    monkeypatch.setattr(
        app_mod.remote_db,
        "prewarm_events_categories",
        lambda: calls.__setitem__("events", calls["events"] + 1) or {"success": 1, "failed": 0},
    )

    app_mod._run_remote_cache_prewarm_iteration(1)

    assert calls == {"platforms": 1, "events": 1, "posters": 0}


def test_health_remote_authority_uses_remote_status_without_sqlite(monkeypatch):
    import routes.health as health

    calls = {"status": 0}

    def fake_status():
        calls["status"] += 1
        return {
            "backend": "supabase_poc",
            "schema": "remote_poc",
            "counts": {"items": 12, "clusters": 3, "cluster_items": 9},
            "postgres_version": "PostgreSQL test",
        }

    monkeypatch.setattr(health.remote_db, "remote_authority_enabled", lambda: True)
    monkeypatch.setattr(health.remote_db, "data_authority", lambda: "supabase")
    monkeypatch.setattr(health.remote_db, "status", fake_status)
    monkeypatch.setattr(health.db, "get_conn", lambda: pytest.fail("remote authority health should not open SQLite"))

    result = health.get_health()

    assert calls == {"status": 1}
    assert result["overall"] == "ok"
    assert result["data_authority"] == "supabase"
    assert result["remote_db"]["status"] == "ok"
    assert result["remote_db"]["counts"]["items"] == 12


def test_health_can_skip_remote_db_check_under_pressure(monkeypatch):
    import routes.health as health

    monkeypatch.setenv("INFO2ACTION_REMOTE_HEALTH_DB_CHECK", "0")
    monkeypatch.setattr(health.remote_db, "remote_authority_enabled", lambda: True)
    monkeypatch.setattr(health.remote_db, "data_authority", lambda: "supabase")
    monkeypatch.setattr(
        health.remote_db,
        "status",
        lambda: pytest.fail("disabled health db check should not query remote db"),
    )
    monkeypatch.setattr(health.db, "get_conn", lambda: pytest.fail("remote authority health should not open SQLite"))

    result = health.get_health(recheck="1")

    assert result["overall"] == "degraded"
    assert result["remote_db"]["status"] == "skipped"
    assert result["remote_db"]["reason"] == "remote_health_db_check_disabled"
    assert result["items_count"] == 0


def test_fetch_status_remote_authority_uses_remote_without_sqlite(monkeypatch):
    import routes.fetch as fetch

    monkeypatch.setattr(fetch.remote_db, "remote_authority_enabled", lambda: True)
    monkeypatch.setattr(fetch.remote_db, "status_write_to_remote", lambda: True)
    monkeypatch.setattr(
        fetch.remote_db,
        "get_last_fetch_remote",
        lambda: {"id": 1198, "status": "done", "stats_json": {"ok": True}},
    )
    monkeypatch.setattr(
        fetch.db,
        "get_conn",
        lambda: pytest.fail("remote fetch status should not open SQLite"),
    )
    monkeypatch.setattr(fetch, "_fetch_active_runs", {})
    monkeypatch.setattr(fetch, "_fetch_running", False)
    monkeypatch.setattr(fetch, "_fetch_finished_at", None)
    monkeypatch.setattr(fetch, "_fetch_progress", None)

    result = fetch.get_fetch_status()

    assert result["last_run"] == {"id": 1198, "status": "done", "stats_json": {"ok": True}}
    assert result["running"] is False


def test_fetch_status_can_skip_remote_live_read_under_pressure(monkeypatch):
    import routes.fetch as fetch

    monkeypatch.setenv("INFO2ACTION_FETCH_STATUS_LIVE_DISABLED", "1")
    monkeypatch.setattr(fetch.remote_db, "remote_authority_enabled", lambda: True)
    monkeypatch.setattr(fetch.remote_db, "status_write_to_remote", lambda: True)
    monkeypatch.setattr(
        fetch.remote_db,
        "get_last_fetch_remote",
        lambda: pytest.fail("disabled live status should not query remote db"),
    )
    monkeypatch.setattr(fetch, "_fetch_active_runs", {})
    monkeypatch.setattr(fetch, "_fetch_running", False)
    monkeypatch.setattr(fetch, "_fetch_finished_at", None)
    monkeypatch.setattr(fetch, "_fetch_progress", None)
    fetch._remote_last_fetch_cache["data"] = {"id": 3043, "status": "partial"}
    fetch._remote_last_fetch_cache["ts"] = time.monotonic() - 999

    result = fetch.get_fetch_status()

    assert result["last_run"] == {"id": 3043, "status": "partial"}
    assert result["remote_status_degraded"] is True
    assert result["running"] is False


def test_start_global_fetch_remote_backend_starts_remote_run_without_sqlite(monkeypatch):
    import routes.fetch as fetch

    calls = []

    class DummyThread:
        def __init__(self, *args, **kwargs):
            self.name = kwargs.get("name")

        def start(self):
            calls.append(("thread_start", self.name))

    monkeypatch.setattr(fetch.remote_db, "fetch_write_to_remote", lambda: True)
    monkeypatch.setattr(fetch.remote_db, "has_recent_running_fetch_remote", lambda: False)
    monkeypatch.setattr(
        fetch.remote_db,
        "remote_db_pressure",
        lambda: {"ok": True, "pressure": False, "reasons": []},
    )
    monkeypatch.setattr(
        fetch.remote_db,
        "start_fetch_run_remote",
        lambda conn=None: calls.append(("start", conn)) or 1199,
    )
    monkeypatch.setattr(
        fetch.db,
        "get_conn",
        lambda: pytest.fail("remote fetch start should not open SQLite"),
    )
    monkeypatch.setattr(fetch.threading, "Thread", DummyThread)
    monkeypatch.setattr(fetch, "_fetch_active_runs", {})
    monkeypatch.setattr(fetch, "_fetch_running", False)
    monkeypatch.setattr(fetch, "_fetch_progress", {"stages": [], "current_stage": 0, "total_new": 0})

    result = fetch.start_global_fetch("qa")

    assert result["ok"] is True
    assert result["run_id"] == 1199
    assert calls[0] == ("start", None)
    assert calls[1][0] == "thread_start"
    assert fetch._fetch_active_runs[1199]["progress"]["run_id"] == 1199


def test_feed_events_dispatches_to_remote_backend(monkeypatch):
    import routes.clusters as clusters

    calls = {}

    def fake_fetch_events(**kwargs):
        calls.update(kwargs)
        return {
            "enabled": True,
            "events": [{"id": 123, "ai_title": "remote"}],
            "next_cursor": None,
            "new_since_last_fetch": 0,
            "total_available_within_30d": 1,
            "data_backend": "supabase_poc",
        }

    monkeypatch.setattr(clusters.remote_db, "events_read_from_remote", lambda: True)
    monkeypatch.setattr(clusters.remote_db, "fetch_events", fake_fetch_events)
    monkeypatch.setattr(clusters, "_github_cluster_display_min_stars", lambda: 42)
    monkeypatch.setattr(clusters, "_config_flag", lambda key, default: True)
    response = Response()

    result = asyncio.run(
        clusters.feed_events(
            _request(),
            response=response,
            page=2,
            limit=3,
            since_version_snapshot=99,
            fetched_since="2026-05-12T00:00:00Z",
        )
    )

    assert result["events"][0]["ai_title"] == "remote"
    assert response.headers["Cache-Control"] == "no-store"
    assert calls == {
        "page": 2,
        "limit": 3,
        "cursor": None,
        "since_version_snapshot": 99,
        "fetched_since": "2026-05-12T00:00:00Z",
        "user_id": None,
        "public_only": True,
        "min_github_stars": 42,
        "enabled": True,
        # v17.0: 精选 tab L1 chip 多 OR 筛选; 测试场景默认 categories 参数 = Query 对象 → 解析为空列表
        "categories": [],
        "timezone_offset_minutes": -480,
    }


def test_feed_events_passes_read_model_cursor_to_remote_backend(monkeypatch):
    import routes.clusters as clusters

    calls = {}

    def fake_fetch_events(**kwargs):
        calls.update(kwargs)
        return {
            "enabled": True,
            "events": [],
            "next_cursor": None,
            "new_since_last_fetch": 0,
            "total_available_within_30d": 0,
            "data_backend": "supabase_poc",
        }

    monkeypatch.setattr(clusters.remote_db, "events_read_from_remote", lambda: True)
    monkeypatch.setattr(clusters.remote_db, "fetch_events", fake_fetch_events)
    monkeypatch.setattr(clusters, "_github_cluster_display_min_stars", lambda: 50)
    monkeypatch.setattr(clusters, "_config_flag", lambda key, default: True)

    cursor = json.dumps({
        "version_id": "00000000-0000-0000-0000-00000000abcd",
        "scope_key": "all",
        "rank_after": 20,
    })

    asyncio.run(
        clusters.feed_events(
            _request(),
            response=Response(),
            page=1,
            limit=20,
            cursor=cursor,
        )
    )

    assert calls["cursor"] == {
        "version_id": "00000000-0000-0000-0000-00000000abcd",
        "scope_key": "all",
        "rank_after": 20,
    }


def test_feed_route_read_model_cursor_preserves_exclude_ids():
    import routes.feed as feed

    cursor = json.dumps({
        "version_id": "00000000-0000-0000-0000-00000000abcd",
        "scope_key": "platform=_all|dimension=section_category|value=products",
        "rank_after": 45,
        "exclude_ids": ["dup-1", "dup-1", "", "dup-2"],
    })

    assert feed._optional_read_model_cursor(cursor) == {
        "version_id": "00000000-0000-0000-0000-00000000abcd",
        "scope_key": "platform=_all|dimension=section_category|value=products",
        "rank_after": 45,
        "exclude_ids": ["dup-1", "dup-2"],
    }


def test_remote_fetch_events_orders_by_first_doc_at(monkeypatch):
    import remote_db

    calls = []

    class FakeCursor:
        def __init__(self, *, rows=None, row=None):
            self._rows = rows or []
            self._row = row

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return self._row

    class FakeConn:
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            calls.append(normalized)
            if "GROUP BY day" in normalized:
                return FakeCursor(rows=[])
            if "SELECT count(*) AS n" in normalized:
                return FakeCursor(row={"n": 0})
            return FakeCursor(rows=[])

    @contextmanager
    def fake_connect():
        yield FakeConn()

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "_cache_get_copy", lambda key: None)
    monkeypatch.setattr(remote_db, "_cache_set_copy", lambda key, value: value)
    monkeypatch.setattr(remote_db, "_cache_get", lambda key: None)
    monkeypatch.setattr(remote_db, "_cache_set", lambda key, value: None)

    remote_db.fetch_events(page=1, limit=10)

    event_select = next(
        sql for sql in calls
        if "FROM remote_poc.clusters c" in sql
        and "COALESCE(NULLIF(c.cover_url, ''), event_cover.cover_url) AS cover_url" in sql
    )
    assert "COALESCE(NULLIF(c.cover_url, ''), event_cover.cover_url) AS cover_url" in event_select
    assert "LEFT JOIN LATERAL" in event_select
    assert "JOIN remote_poc.items i ON i.id = ci.item_id" in event_select
    assert "ORDER BY c.first_doc_at DESC" in event_select
    assert "coalesce(c.last_doc_at" not in event_select


def test_remote_events_snapshot_key_bumped_for_cover_payload():
    import remote_db

    key = remote_db._events_snapshot_key(
        limit=20,
        public_only=True,
        min_github_stars=50,
        enabled=True,
        categories=[],
    )

    assert key.startswith("events:v4:")
    assert "tz=-480" in key


def test_refresh_highlights_read_model_builds_versioned_scopes(monkeypatch):
    import remote_db

    class FakeCursor:
        def __init__(self, row=None, rows=None):
            self.row = row
            self.rows = [] if rows is None else rows

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class FakeConn:
        def __init__(self):
            self.sqls = []
            self.params = []
            self.commits = 0
            self.rollbacks = 0

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.sqls.append(normalized)
            self.params.append(params or {})
            if normalized.startswith("SET LOCAL"):
                return FakeCursor()
            if "SELECT count(*) AS n FROM remote_poc.highlights_scope_items" in normalized:
                return FakeCursor(row={"n": 6})
            if "SELECT count(*) AS n FROM remote_poc.highlights_scopes" in normalized:
                return FakeCursor(row={"n": 3})
            return FakeCursor()

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

    fake = FakeConn()

    @contextmanager
    def fake_connect():
        yield fake

    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL", "1")
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL_STALE_FALLBACK", "0")
    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "clear_feed_cache_keys", lambda: 0)

    result = remote_db.refresh_highlights_read_model(window_days=30, min_github_stars=50)

    joined = "\n".join(fake.sqls)
    assert result["ok"] is True
    assert result["scope_items"] == 6
    assert result["scopes"] == 3
    assert "SET LOCAL statement_timeout = '180000ms'" in joined
    assert "INSERT INTO remote_poc.highlights_read_model_versions" in joined
    assert "INSERT INTO remote_poc.highlights_scopes" in joined
    assert "INSERT INTO remote_poc.highlights_scope_items" in joined
    assert "INSERT INTO remote_poc.highlight_cluster_decisions" in joined
    assert "INSERT INTO remote_poc.highlights_read_model_state" in joined
    assert "DELETE FROM remote_poc.highlights_read_model_versions" in joined
    assert any((p or {}).get("scope_key_all") == "all" for p in fake.params)
    assert fake.commits == 1
    assert fake.rollbacks == 0


def test_refresh_highlights_read_model_skips_unchanged_highlight_cluster_decisions(monkeypatch):
    import remote_db

    class FakeCursor:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class FakeConn:
        def __init__(self):
            self.sqls = []

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.sqls.append(normalized)
            if "SELECT count(*) AS n FROM remote_poc.highlights_scope_items" in normalized:
                return FakeCursor(row={"n": 6})
            if "SELECT count(*) AS n FROM remote_poc.highlights_scopes" in normalized:
                return FakeCursor(row={"n": 3})
            return FakeCursor()

        def commit(self):
            pass

        def rollback(self):
            pass

    fake = FakeConn()

    @contextmanager
    def fake_connect():
        yield fake

    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL", "1")
    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "clear_feed_cache_keys", lambda: 0)

    remote_db.refresh_highlights_read_model(window_days=30, min_github_stars=50)

    decisions_sql = next(
        sql for sql in fake.sqls
        if "INSERT INTO remote_poc.highlight_cluster_decisions" in sql
    )
    assert "INSERT INTO remote_poc.highlight_cluster_decisions AS target" in decisions_sql
    assert "ON CONFLICT (cluster_id) DO UPDATE SET" in decisions_sql
    assert "WHERE target.decision IS DISTINCT FROM excluded.decision" in decisions_sql
    assert "target.snapshot_json IS DISTINCT FROM excluded.snapshot_json" in decisions_sql


def test_refresh_highlights_read_model_if_stale_uses_delta_by_default(monkeypatch):
    import remote_db

    delta_calls = []

    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL", "1")
    monkeypatch.delenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL_INCREMENTAL", raising=False)
    monkeypatch.setattr(
        remote_db,
        "refresh_highlights_read_model_delta_in_place",
        lambda **kwargs: delta_calls.append(kwargs) or {"ok": True, "mode": "delta_in_place"},
    )
    monkeypatch.setattr(
        remote_db,
        "refresh_highlights_read_model",
        lambda **_kwargs: pytest.fail("default highlights refresh should use incremental mode"),
    )
    remote_db._HIGHLIGHTS_READ_MODEL_REFRESH_LAST_ATTEMPT_AT = 0

    result = remote_db.refresh_highlights_read_model_if_stale(min_interval_sec=0)

    assert result["mode"] == "delta_in_place"
    assert delta_calls == [{}]


def test_refresh_highlights_read_model_if_stale_uses_delta_when_verdict_filter_enabled(monkeypatch):
    import remote_db

    delta_calls = []

    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL", "1")
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_VERDICT_FILTER_ENABLED", "1")
    monkeypatch.delenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL_INCREMENTAL", raising=False)
    monkeypatch.setattr(
        remote_db,
        "refresh_highlights_read_model_delta_in_place",
        lambda **kwargs: delta_calls.append(kwargs) or {"ok": True, "mode": "delta_in_place"},
    )
    monkeypatch.setattr(
        remote_db,
        "refresh_highlights_read_model",
        lambda **_kwargs: pytest.fail("verdict filter should use incremental refresh"),
    )
    remote_db._HIGHLIGHTS_READ_MODEL_REFRESH_LAST_ATTEMPT_AT = 0

    result = remote_db.refresh_highlights_read_model_if_stale(min_interval_sec=0)

    assert result["mode"] == "delta_in_place"
    assert delta_calls == [{}]


def test_refresh_highlights_read_model_delta_in_place_updates_active_scopes(monkeypatch):
    import remote_db

    class FakeCursor:
        def __init__(self, row=None, rows=None):
            self.row = row
            self.rows = [] if rows is None else rows

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class FakeConn:
        def __init__(self):
            self.sqls = []
            self.params = []
            self.commits = 0
            self.rollbacks = 0

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.sqls.append(normalized)
            self.params.append(params or {})
            if normalized.startswith("SET LOCAL"):
                return FakeCursor()
            if "FROM remote_poc.highlights_read_model_state" in normalized:
                return FakeCursor(row={
                    "version_id": "483df42f-7f13-4745-96bc-6d8b12f8bff7",
                    "generated_at": "2026-05-24T15:58:34Z",
                    "completed_at": "2026-05-24T15:58:34Z",
                    "max_cluster_updated_at": "2026-05-24T15:58:34Z",
                    "window_days": 30,
                    "min_github_stars": 50,
                    "meta_json": {"read_model": "highlights_v1"},
                    "max_sort_at": "2026-05-24T15:58:34Z",
                })
            if "max(delta_checkpoint_at) AS max_delta_checkpoint_at" in normalized:
                return FakeCursor(row={
                    "clusters": 2,
                    "max_delta_checkpoint_at": "2026-05-25T00:36:38Z",
                })
            if "SELECT count(*) AS scope_rows FROM pg_temp.highlights_read_model_delta_scope_rows" in normalized:
                return FakeCursor(row={"scope_rows": 4})
            if "SELECT count(*) AS n FROM remote_poc.highlights_scope_items" in normalized:
                return FakeCursor(row={"n": 8588})
            return FakeCursor()

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

    fake = FakeConn()

    @contextmanager
    def fake_connect():
        yield fake

    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL", "1")
    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "clear_feed_cache_keys", lambda: 0)

    result = remote_db.refresh_highlights_read_model_delta_in_place()

    joined = "\n".join(fake.sqls)
    assert result["ok"] is True
    assert result["mode"] == "delta_in_place"
    assert result["delta_clusters"] == 2
    assert result["scope_items"] == 8588
    assert "CREATE TEMP TABLE highlights_read_model_delta_clusters" in joined
    assert "CREATE TEMP TABLE highlights_read_model_delta_scope_rows" in joined
    assert "WITH candidate_delta_clusters AS" in joined
    assert "UNION ALL SELECT c.id AS cluster_id" in joined
    assert "c.last_updated_at > %(checkpoint_at)s::timestamptz" in joined
    assert "i_delta.highlight_scored_at > %(checkpoint_at)s::timestamptz" in joined
    assert "LEFT JOIN remote_poc.cluster_items ci_delta" not in joined
    assert "JOIN pg_temp.highlights_read_model_delta_clusters dc" in joined
    decisions_sql = next(
        sql for sql in fake.sqls
        if "INSERT INTO remote_poc.highlight_cluster_decisions" in sql
    )
    assert "JOIN pg_temp.highlights_read_model_delta_clusters decision_delta" in decisions_sql
    assert "DELETE FROM remote_poc.highlights_scope_items" in joined
    assert "UPDATE remote_poc.highlights_scope_items si" in joined
    assert "INSERT INTO remote_poc.highlight_cluster_decisions" in joined
    assert "scope_max_rank" in joined
    assert "CREATE TEMP TABLE highlights_read_model_affected_scope_rows" not in joined
    assert "DELETE FROM remote_poc.highlights_scope_items si USING pg_temp.highlights_read_model_affected_scopes" not in joined
    assert "UPDATE remote_poc.highlights_read_model_versions" in joined
    assert "INSERT INTO remote_poc.highlights_read_model_versions" not in joined
    assert any((p or {}).get("active_version_id") == "483df42f-7f13-4745-96bc-6d8b12f8bff7" for p in fake.params)
    assert fake.commits == 1
    assert fake.rollbacks == 0


def test_refresh_highlights_read_model_if_data_stale_refreshes_newer_live_top(monkeypatch):
    import remote_db

    class FakeCursor:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class FakeConn:
        def __init__(self):
            self.sqls = []

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.sqls.append(normalized)
            if normalized.startswith("SET TRANSACTION"):
                return FakeCursor()
            if "FROM remote_poc.highlights_read_model_state" in normalized:
                return FakeCursor(row={
                    "active_version_id": "old-version",
                    "cluster_id": 18208,
                    "sort_at": "2026-05-23T15:50:06Z",
                })
            if "FROM remote_poc.clusters c" in normalized:
                return FakeCursor(row={
                    "id": 18245,
                    "sort_at": "2026-05-23T17:29:36Z",
                })
            return FakeCursor()

    refresh_calls = []

    @contextmanager
    def fake_connect():
        yield FakeConn()

    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL", "1")
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL_STALE_FALLBACK", "0")
    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(
        remote_db,
        "refresh_highlights_read_model_if_stale",
        lambda **kwargs: refresh_calls.append(kwargs) or {"ok": True, "version_id": "new-version"},
    )

    result = remote_db.refresh_highlights_read_model_if_data_stale(min_interval_sec=0)

    assert result["stale"] is True
    assert result["reason"] == "live_top_newer"
    assert result["latest_cluster_id"] == 18245
    assert refresh_calls == [{"min_interval_sec": 0}]
    assert result["refresh"]["version_id"] == "new-version"


def test_refresh_highlights_read_model_if_data_stale_skips_fresh_model(monkeypatch):
    import remote_db

    class FakeCursor:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class FakeConn:
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SET TRANSACTION"):
                return FakeCursor()
            if "FROM remote_poc.highlights_read_model_state" in normalized:
                return FakeCursor(row={
                    "active_version_id": "current-version",
                    "cluster_id": 18245,
                    "sort_at": "2026-05-23T17:29:36Z",
                })
            if "FROM remote_poc.clusters c" in normalized:
                return FakeCursor(row={
                    "id": 18245,
                    "sort_at": "2026-05-23T17:29:36Z",
                })
            return FakeCursor()

    @contextmanager
    def fake_connect():
        yield FakeConn()

    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL", "1")
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL_STALE_FALLBACK", "0")
    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(
        remote_db,
        "refresh_highlights_read_model_if_stale",
        lambda **_kwargs: pytest.fail("fresh highlights model should not rebuild"),
    )

    result = remote_db.refresh_highlights_read_model_if_data_stale(min_interval_sec=0)

    assert result["stale"] is False
    assert result["skipped"] == "data_fresh"
    assert result["active_top_cluster_id"] == 18245


def test_fetch_events_uses_highlights_read_model_for_default_scope(monkeypatch):
    import remote_db

    remote_db.clear_feed_cache_keys()

    class FakeCursor:
        def __init__(self, row=None, rows=None):
            self.row = row
            self.rows = [] if rows is None else rows

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class FakeConn:
        def __init__(self):
            self.sqls = []
            self.params = []

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.sqls.append(normalized)
            self.params.append(params or {})
            if normalized.startswith("SET LOCAL"):
                return FakeCursor()
            if "FROM remote_poc.highlights_read_model_state" in normalized:
                return FakeCursor(row={
                    "version_id": "00000000-0000-0000-0000-00000000abcd",
                    "scope_key": "all",
                    "total_count": 2,
                })
            if "FROM remote_poc.highlights_scope_items" in normalized and "GROUP BY day" in normalized:
                return FakeCursor(rows=[{"day": "2026-05-23", "n": 2}])
            if "FROM remote_poc.highlights_scope_items" in normalized:
                return FakeCursor(rows=[
                    {
                        "rank": 1,
                        "cluster_id": 301,
                        "sort_at": "2026-05-23T01:00:00+00:00",
                        "card_json": {
                            "id": 301,
                            "ai_title": "Read model event",
                            "ai_summary": "summary",
                            "doc_count": 2,
                            "unique_source_count": 2,
                            "category": "products",
                            "source_preview": [{"platform": "twitter", "author": "A", "source": "following"}],
                            "first_doc_at": "2026-05-23T01:00:00+00:00",
                            "last_doc_at": "2026-05-23T01:00:00+00:00",
                            "platforms": ["twitter"],
                            "cover_url": None,
                            "live_version": 5,
                        },
                    }
                ])
            raise AssertionError(f"unexpected SQL: {normalized}")

    fake = FakeConn()

    @contextmanager
    def fake_connect():
        yield fake

    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL", "1")
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL_STALE_FALLBACK", "0")
    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "event_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_read_local_read_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(remote_db, "_read_feed_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(remote_db, "_write_feed_snapshot_async", lambda *args, **kwargs: None)
    monkeypatch.setattr(remote_db, "_write_local_read_cache_async", lambda *args, **kwargs: None)

    result = remote_db.fetch_events(
        page=1,
        limit=20,
        user_id=None,
        public_only=True,
        min_github_stars=50,
        enabled=True,
        categories=[],
    )

    assert result["read_model"] == "highlights_v1"
    assert result["read_model_version_id"] == "00000000-0000-0000-0000-00000000abcd"
    assert result["scope_key"] == "all"
    assert result["events"][0]["id"] == 301
    assert result["events"][0]["has_update"] is False
    assert result["date_counts"] == {"2026-05-23": 2}
    assert result["next_cursor"] is None
    assert not any("FROM remote_poc.clusters c" in sql for sql in fake.sqls)
    _assert_events_read_model_timeouts_before_scope_items(fake.sqls)

    remote_db.clear_feed_cache_keys()


def test_fetch_events_highlights_result_cache_skips_second_db_hit(monkeypatch):
    import remote_db

    remote_db.clear_feed_cache_keys()

    class FakeCursor:
        def __init__(self, row=None, rows=None):
            self.row = row
            self.rows = [] if rows is None else rows

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class FakeConn:
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SET LOCAL"):
                return FakeCursor()
            if "FROM remote_poc.highlights_read_model_state" in normalized:
                return FakeCursor(row={
                    "version_id": "00000000-0000-0000-0000-00000000abcd",
                    "scope_key": "all",
                    "total_count": 1,
                })
            if "FROM remote_poc.highlights_scope_items" in normalized and "GROUP BY day" in normalized:
                return FakeCursor(rows=[{"day": "2026-06-09", "n": 1}])
            if "FROM remote_poc.highlights_scope_items" in normalized:
                return FakeCursor(rows=[
                    {
                        "rank": 1,
                        "cluster_id": 801,
                        "sort_at": "2026-06-09T01:00:00+00:00",
                        "card_json": {
                            "id": 801,
                            "ai_title": "Cached read model event",
                            "doc_count": 2,
                            "unique_source_count": 2,
                            "first_doc_at": "2026-06-09T01:00:00+00:00",
                            "platforms": ["twitter"],
                            "live_version": 1,
                        },
                    }
                ])
            raise AssertionError(f"unexpected SQL: {normalized}")

    connect_count = {"n": 0}

    @contextmanager
    def fake_connect():
        connect_count["n"] += 1
        yield FakeConn()

    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL", "1")
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL_STALE_FALLBACK", "0")
    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "event_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_write_feed_snapshot_async", lambda *args, **kwargs: None)
    monkeypatch.setattr(remote_db, "_write_local_read_cache_async", lambda *args, **kwargs: None)

    first = remote_db.fetch_events(
        page=1,
        limit=20,
        user_id=None,
        public_only=True,
        min_github_stars=50,
        enabled=True,
        categories=[],
    )
    second = remote_db.fetch_events(
        page=1,
        limit=20,
        user_id=None,
        public_only=True,
        min_github_stars=50,
        enabled=True,
        categories=[],
    )

    assert first["events"][0]["id"] == 801
    assert second["events"][0]["id"] == 801
    assert connect_count["n"] == 1

    remote_db.clear_feed_cache_keys()


def test_fetch_events_highlights_singleflight_coalesces_concurrent_miss(monkeypatch):
    import remote_db

    remote_db.clear_feed_cache_keys()
    with remote_db._INFLIGHT_LOCK:
        remote_db._INFLIGHT.clear()

    class FakeCursor:
        def __init__(self, row=None, rows=None):
            self.row = row
            self.rows = [] if rows is None else rows

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class FakeConn:
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SET LOCAL"):
                return FakeCursor()
            if "FROM remote_poc.highlights_read_model_state" in normalized:
                time.sleep(0.1)
                return FakeCursor(row={
                    "version_id": "00000000-0000-0000-0000-00000000abcd",
                    "scope_key": "all",
                    "total_count": 1,
                })
            if "FROM remote_poc.highlights_scope_items" in normalized and "GROUP BY day" in normalized:
                return FakeCursor(rows=[{"day": "2026-06-09", "n": 1}])
            if "FROM remote_poc.highlights_scope_items" in normalized:
                return FakeCursor(rows=[
                    {
                        "rank": 1,
                        "cluster_id": 802,
                        "sort_at": "2026-06-09T02:00:00+00:00",
                        "card_json": {
                            "id": 802,
                            "ai_title": "Singleflight read model event",
                            "doc_count": 2,
                            "unique_source_count": 2,
                            "first_doc_at": "2026-06-09T02:00:00+00:00",
                            "platforms": ["twitter"],
                            "live_version": 1,
                        },
                    }
                ])
            raise AssertionError(f"unexpected SQL: {normalized}")

    connect_count = {"n": 0}
    lock = threading.Lock()

    @contextmanager
    def fake_connect():
        with lock:
            connect_count["n"] += 1
        yield FakeConn()

    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL", "1")
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL_STALE_FALLBACK", "0")
    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "event_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_write_feed_snapshot_async", lambda *args, **kwargs: None)
    monkeypatch.setattr(remote_db, "_write_local_read_cache_async", lambda *args, **kwargs: None)

    results = [None] * 8

    def worker(index):
        results[index] = remote_db.fetch_events(
            page=1,
            limit=20,
            user_id=None,
            public_only=True,
            min_github_stars=50,
            enabled=True,
            categories=[],
        )

    threads = [threading.Thread(target=worker, args=(idx,)) for idx in range(len(results))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert connect_count["n"] == 1
    assert all(result["events"][0]["id"] == 802 for result in results)

    remote_db.clear_feed_cache_keys()


def test_fetch_events_default_highlights_path_does_not_block_on_freshness(monkeypatch):
    import remote_db

    remote_db.clear_feed_cache_keys()

    class FakeCursor:
        def __init__(self, row=None, rows=None):
            self.row = row
            self.rows = [] if rows is None else rows

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class FakeConn:
        def __init__(self):
            self.sqls = []

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.sqls.append(normalized)
            if normalized.startswith("SET LOCAL"):
                return FakeCursor()
            if "FROM remote_poc.highlights_read_model_state" in normalized:
                return FakeCursor(row={
                    "version_id": "00000000-0000-0000-0000-00000000abcd",
                    "scope_key": "all",
                    "total_count": 1,
                })
            if "FROM remote_poc.highlights_scope_items" in normalized and "GROUP BY day" in normalized:
                return FakeCursor(rows=[{"day": "2026-05-31", "n": 1}])
            if "FROM remote_poc.highlights_scope_items" in normalized:
                return FakeCursor(rows=[
                    {
                        "rank": 1,
                        "cluster_id": 701,
                        "sort_at": "2026-05-31T04:00:00+00:00",
                        "card_json": {
                            "id": 701,
                            "ai_title": "Nonblocking read model event",
                            "doc_count": 2,
                            "unique_source_count": 2,
                            "first_doc_at": "2026-05-31T04:00:00+00:00",
                            "platforms": ["twitter"],
                            "live_version": 1,
                        },
                    }
                ])
            raise AssertionError(f"unexpected SQL: {normalized}")

    fake = FakeConn()

    @contextmanager
    def fake_connect():
        yield fake

    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL", "1")
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL_STALE_FALLBACK", "1")
    monkeypatch.delenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL_REQUEST_FRESHNESS", raising=False)
    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "event_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(
        remote_db,
        "highlights_read_model_freshness",
        lambda **_kwargs: pytest.fail("request path must not run live freshness by default"),
    )
    monkeypatch.setattr(
        remote_db,
        "_trigger_highlights_read_model_self_heal",
        lambda **_kwargs: pytest.fail("request path must not trigger self-heal by default"),
    )
    monkeypatch.setattr(remote_db, "_read_local_read_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(remote_db, "_read_feed_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(remote_db, "_write_feed_snapshot_async", lambda *args, **kwargs: None)
    monkeypatch.setattr(remote_db, "_write_local_read_cache_async", lambda *args, **kwargs: None)

    result = remote_db.fetch_events(
        page=1,
        limit=20,
        user_id=None,
        public_only=True,
        min_github_stars=50,
        enabled=True,
        categories=[],
    )

    assert result["read_model"] == "highlights_v1"
    assert result["events"][0]["id"] == 701
    assert not any("FROM remote_poc.clusters c" in sql for sql in fake.sqls)

    remote_db.clear_feed_cache_keys()


def test_fetch_events_prefers_highlights_read_model_over_stale_first_page_snapshot(monkeypatch):
    import remote_db

    remote_db.clear_feed_cache_keys()

    class FakeCursor:
        def __init__(self, row=None, rows=None):
            self.row = row
            self.rows = [] if rows is None else rows

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class FakeConn:
        def __init__(self):
            self.sqls = []

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.sqls.append(normalized)
            if normalized.startswith("SET LOCAL"):
                return FakeCursor()
            if "FROM remote_poc.highlights_read_model_state" in normalized:
                return FakeCursor(row={
                    "version_id": "00000000-0000-0000-0000-00000000abcd",
                    "scope_key": "all",
                    "total_count": 1,
                })
            if "FROM remote_poc.highlights_scope_items" in normalized and "GROUP BY day" in normalized:
                return FakeCursor(rows=[])
            if "FROM remote_poc.highlights_scope_items" in normalized:
                return FakeCursor(rows=[
                    {
                        "rank": 1,
                        "cluster_id": 501,
                        "sort_at": "2026-05-23T01:00:00+00:00",
                        "card_json": {
                            "id": 501,
                            "ai_title": "Read model beats snapshot",
                            "doc_count": 1,
                            "unique_source_count": 1,
                            "first_doc_at": "2026-05-23T01:00:00+00:00",
                            "platforms": ["twitter"],
                            "live_version": 1,
                        },
                    }
                ])
            raise AssertionError(f"unexpected SQL: {normalized}")

    fake = FakeConn()

    @contextmanager
    def fake_connect():
        yield fake

    stale_snapshot = {
        "enabled": True,
        "events": [{"id": 999, "ai_title": "stale live snapshot"}],
        "next_cursor": 2,
        "new_since_last_fetch": 0,
        "total_available_within_30d": 999,
    }

    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL", "1")
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL_STALE_FALLBACK", "0")
    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "event_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_read_local_read_cache", lambda *args, **kwargs: stale_snapshot)
    monkeypatch.setattr(remote_db, "_read_feed_snapshot", lambda *args, **kwargs: stale_snapshot)
    monkeypatch.setattr(remote_db, "_write_feed_snapshot_async", lambda *args, **kwargs: None)
    monkeypatch.setattr(remote_db, "_write_local_read_cache_async", lambda *args, **kwargs: None)

    result = remote_db.fetch_events(
        page=1,
        limit=20,
        user_id=None,
        public_only=True,
        min_github_stars=50,
        enabled=True,
        categories=[],
    )

    assert result["read_model"] == "highlights_v1"
    assert result["events"][0]["id"] == 501
    assert not any("FROM remote_poc.clusters c" in sql for sql in fake.sqls)

    remote_db.clear_feed_cache_keys()


def test_fetch_events_falls_back_live_when_highlights_read_model_is_stale(monkeypatch):
    import remote_db

    remote_db.clear_feed_cache_keys()

    class FakeCursor:
        def __init__(self, row=None, rows=None):
            self.row = row
            self.rows = [] if rows is None else rows

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class FakeConn:
        def __init__(self):
            self.sqls = []

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.sqls.append(normalized)
            if normalized.startswith("SET LOCAL"):
                return FakeCursor()
            if normalized.startswith("SELECT c.id, c.ai_title"):
                return FakeCursor(rows=[
                    {
                        "id": 901,
                        "ai_title": "Live cluster newer than read model",
                        "ai_summary": "fresh summary",
                        "doc_count": 2,
                        "unique_source_count": 2,
                        "first_doc_at": "2026-05-24T12:28:00+00:00",
                        "last_doc_at": "2026-05-24T12:28:00+00:00",
                        "platforms_json": ["twitter"],
                        "cover_url": None,
                        "live_version": 7,
                        "last_updated_at": "2026-05-24T12:28:00+00:00",
                    }
                ])
            if normalized.startswith("SELECT count(*) AS n FROM remote_poc.clusters c"):
                return FakeCursor(row={"n": 1})
            if "FROM remote_poc.clusters c" in normalized and "GROUP BY day" in normalized:
                return FakeCursor(rows=[{"day": "2026-05-24", "n": 1}])
            raise AssertionError(f"unexpected SQL: {normalized}")

    fake = FakeConn()

    @contextmanager
    def fake_connect():
        yield fake

    self_heal_calls = []

    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL", "1")
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL_STALE_FALLBACK", "1")
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL_REQUEST_FRESHNESS", "1")
    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "event_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "highlights_read_model_freshness", lambda **_kwargs: {
        "ok": True,
        "enabled": True,
        "stale": True,
        "reason": "live_top_newer",
        "active_top_cluster_id": 501,
        "latest_cluster_id": 901,
    })
    monkeypatch.setattr(
        remote_db,
        "_trigger_highlights_read_model_self_heal",
        lambda **kwargs: self_heal_calls.append(kwargs) or {"triggered": True},
    )
    monkeypatch.setattr(remote_db, "_read_local_read_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        remote_db,
        "_read_feed_snapshot",
        lambda *args, **kwargs: pytest.fail("stale snapshot should be skipped"),
    )
    monkeypatch.setattr(remote_db, "_write_feed_snapshot_async", lambda *args, **kwargs: None)
    monkeypatch.setattr(remote_db, "_write_local_read_cache_async", lambda *args, **kwargs: None)
    monkeypatch.setattr(remote_db, "_fetch_event_source_metadata", lambda *args, **kwargs: {})

    result = remote_db.fetch_events(
        page=1,
        limit=20,
        user_id=None,
        public_only=True,
        min_github_stars=50,
        enabled=True,
        categories=[],
    )

    assert result["events"][0]["id"] == 901
    assert result["read_model_stale"] is True
    assert result["fallback_reason"] == "highlights_read_model_stale"
    assert result["read_model_freshness"]["latest_cluster_id"] == 901
    assert self_heal_calls == [{"reason": "live_top_newer", "min_interval_sec": 60}]
    assert any("FROM remote_poc.clusters c" in sql for sql in fake.sqls)

    remote_db.clear_feed_cache_keys()


def test_fetch_events_highlights_read_model_returns_versioned_cursor(monkeypatch):
    import remote_db

    remote_db.clear_feed_cache_keys()

    class FakeCursor:
        def __init__(self, row=None, rows=None):
            self.row = row
            self.rows = [] if rows is None else rows

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class FakeConn:
        def __init__(self):
            self.commits = 0

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SET LOCAL"):
                return FakeCursor()
            if "FROM remote_poc.highlights_read_model_state" in normalized:
                return FakeCursor(row={
                    "version_id": "00000000-0000-0000-0000-00000000beef",
                    "scope_key": "all",
                    "total_count": 2,
                })
            if "FROM remote_poc.highlights_scope_items" in normalized and "GROUP BY day" in normalized:
                return FakeCursor(rows=[])
            if "FROM remote_poc.highlights_scope_items" in normalized:
                return FakeCursor(rows=[
                    {
                        "rank": 1,
                        "cluster_id": 401,
                        "sort_at": "2026-05-23T01:00:00+00:00",
                        "card_json": {
                            "id": 401,
                            "ai_title": "first",
                            "doc_count": 2,
                            "unique_source_count": 2,
                            "first_doc_at": "2026-05-23T01:00:00+00:00",
                            "platforms": ["twitter"],
                            "live_version": 1,
                        },
                    },
                    {
                        "rank": 2,
                        "cluster_id": 402,
                        "sort_at": "2026-05-23T00:00:00+00:00",
                        "card_json": {
                            "id": 402,
                            "ai_title": "second",
                            "doc_count": 2,
                            "unique_source_count": 2,
                            "first_doc_at": "2026-05-23T00:00:00+00:00",
                            "platforms": ["github"],
                            "live_version": 1,
                        },
                    },
                ])
            raise AssertionError(f"unexpected SQL: {normalized}")

        def commit(self):
            self.commits += 1

    fake = FakeConn()

    @contextmanager
    def fake_connect():
        yield fake

    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL", "1")
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL_STALE_FALLBACK", "0")
    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "event_read_backend", lambda: "supabase_poc")

    result = remote_db.fetch_events(
        page=1,
        limit=1,
        user_id=None,
        public_only=True,
        min_github_stars=50,
        enabled=True,
    )

    assert [event["id"] for event in result["events"]] == [401]
    assert result["next_cursor"] == {
        "version_id": "00000000-0000-0000-0000-00000000beef",
        "scope_key": "all",
        "rank_after": 1,
    }
    assert fake.commits == 1

    remote_db.clear_feed_cache_keys()


def test_fetch_events_highlights_read_model_cursor_pins_complete_version(monkeypatch):
    import remote_db

    remote_db.clear_feed_cache_keys()

    old_version = "00000000-0000-0000-0000-00000000aaaa"

    class FakeCursor:
        def __init__(self, row=None, rows=None):
            self.row = row
            self.rows = [] if rows is None else rows

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class FakeConn:
        def __init__(self):
            self.params = []
            self.sqls = []

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.sqls.append(normalized)
            self.params.append(params or {})
            if normalized.startswith("SET LOCAL"):
                return FakeCursor()
            if (
                "FROM remote_poc.highlights_read_model_versions v" in normalized
                and "JOIN remote_poc.highlights_scopes sc" in normalized
            ):
                assert params["version_id"] == old_version
                return FakeCursor(row={
                    "version_id": old_version,
                    "scope_key": "all",
                    "total_count": 50,
                })
            if "FROM remote_poc.highlights_read_model_state" in normalized:
                raise AssertionError("cursor pagination should use the pinned complete version before active")
            if "FROM remote_poc.highlights_scope_items" in normalized and "GROUP BY day" in normalized:
                assert params["version_id"] == old_version
                return FakeCursor(rows=[])
            if "FROM remote_poc.highlights_scope_items" in normalized:
                assert params["version_id"] == old_version
                assert params["rank_after"] == 20
                assert "ORDER BY highlights_scope_items.sort_at DESC NULLS LAST" in normalized
                assert "OFFSET %(rank_after)s" in normalized
                assert "AND rank > %(rank_after)s" not in normalized
                return FakeCursor(rows=[
                    {
                        "rank": 21,
                        "cluster_id": 421,
                        "sort_at": "2026-05-23T01:00:00+00:00",
                        "card_json": {
                            "id": 421,
                            "ai_title": "old version page two",
                            "doc_count": 1,
                            "unique_source_count": 1,
                            "first_doc_at": "2026-05-23T01:00:00+00:00",
                            "platforms": ["twitter"],
                            "live_version": 1,
                        },
                    },
                    {
                        "rank": 22,
                        "cluster_id": 422,
                        "sort_at": "2026-05-23T00:00:00+00:00",
                        "card_json": {
                            "id": 422,
                            "ai_title": "has more",
                            "doc_count": 1,
                            "unique_source_count": 1,
                            "first_doc_at": "2026-05-23T00:00:00+00:00",
                            "platforms": ["github"],
                            "live_version": 1,
                        },
                    },
                ])
            raise AssertionError(f"unexpected SQL: {normalized}")

    fake = FakeConn()

    @contextmanager
    def fake_connect():
        yield fake

    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL", "1")
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL_STALE_FALLBACK", "0")
    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "event_read_backend", lambda: "supabase_poc")

    result = remote_db.fetch_events(
        page=1,
        limit=1,
        cursor={
            "version_id": old_version,
            "scope_key": "all",
            "rank_after": 20,
        },
        user_id=None,
        public_only=True,
        min_github_stars=50,
        enabled=True,
    )

    assert [event["id"] for event in result["events"]] == [421]
    assert result["read_model_version_id"] == old_version
    assert result["next_cursor"] == {
        "version_id": old_version,
        "scope_key": "all",
        "rank_after": 21,
    }
    _assert_events_read_model_timeouts_before_scope_items(fake.sqls)

    remote_db.clear_feed_cache_keys()


def test_fetch_events_highlights_read_model_caches_date_counts(monkeypatch):
    import remote_db

    remote_db.clear_feed_cache_keys()

    version_id = "00000000-0000-0000-0000-00000000cafe"

    class FakeCursor:
        def __init__(self, row=None, rows=None):
            self.row = row
            self.rows = [] if rows is None else rows

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class FakeConn:
        def __init__(self):
            self.date_count_calls = 0
            self.page_calls = 0

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SET LOCAL"):
                return FakeCursor()
            if (
                "FROM remote_poc.highlights_read_model_versions v" in normalized
                and "JOIN remote_poc.highlights_scopes sc" in normalized
            ):
                return FakeCursor(row={
                    "version_id": version_id,
                    "scope_key": "all",
                    "total_count": 50,
                    "max_sort_at": "2026-05-23T01:00:00Z",
                    "generated_at": "2026-06-03T09:00:00Z",
                })
            if "FROM remote_poc.highlights_scope_items" in normalized and "GROUP BY day" in normalized:
                self.date_count_calls += 1
                return FakeCursor(rows=[{"day": "2026-05-23", "n": 50}])
            if "FROM remote_poc.highlights_scope_items" in normalized:
                self.page_calls += 1
                return FakeCursor(rows=[
                    {
                        "rank": 41,
                        "cluster_id": 441,
                        "sort_at": "2026-05-23T01:00:00+00:00",
                        "card_json": {
                            "id": 441,
                            "ai_title": "cached date counts",
                            "doc_count": 1,
                            "unique_source_count": 1,
                            "first_doc_at": "2026-05-23T01:00:00+00:00",
                            "platforms": ["twitter"],
                            "live_version": 1,
                        },
                    }
                ])
            raise AssertionError(f"unexpected SQL: {normalized}")

    fake = FakeConn()

    @contextmanager
    def fake_connect():
        yield fake

    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL", "1")
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL_STALE_FALLBACK", "0")
    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "event_read_backend", lambda: "supabase_poc")

    kwargs = {
        "page": 1,
        "limit": 20,
        "cursor": {
            "version_id": version_id,
            "scope_key": "all",
            "rank_after": 40,
        },
        "user_id": None,
        "public_only": True,
        "min_github_stars": 50,
        "enabled": True,
    }
    first = remote_db.fetch_events(**kwargs)
    second = remote_db.fetch_events(**kwargs)

    assert first["date_counts"] == {"2026-05-23": 50}
    assert second["date_counts"] == {"2026-05-23": 50}
    assert fake.page_calls == 1
    assert fake.date_count_calls == 1

    remote_db.clear_feed_cache_keys()


def test_fetch_events_highlights_read_model_applies_login_overlay(monkeypatch):
    import remote_db

    remote_db.clear_feed_cache_keys()

    class FakeCursor:
        def __init__(self, row=None, rows=None):
            self.row = row
            self.rows = [] if rows is None else rows

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class FakeConn:
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SET LOCAL"):
                return FakeCursor()
            if "FROM remote_poc.highlights_read_model_state" in normalized:
                return FakeCursor(row={
                    "version_id": "00000000-0000-0000-0000-00000000dcba",
                    "scope_key": "category:products",
                    "total_count": 1,
                })
            if "FROM remote_poc.highlights_scope_items" in normalized and "GROUP BY day" in normalized:
                return FakeCursor(rows=[])
            if "FROM remote_poc.highlights_scope_items" in normalized:
                return FakeCursor(rows=[
                    {
                        "rank": 1,
                        "cluster_id": 302,
                        "sort_at": "2026-05-23T02:00:00+00:00",
                        "card_json": {
                            "id": 302,
                            "ai_title": "Overlay event",
                            "doc_count": 2,
                            "unique_source_count": 2,
                            "first_doc_at": "2026-05-23T02:00:00+00:00",
                            "last_doc_at": None,
                            "platforms": ["github"],
                            "cover_url": None,
                            "live_version": 7,
                        },
                    }
                ])
            if "FROM remote_poc.cluster_status" in normalized:
                assert (params or {}).get("user_id") == "user-1"
                assert (params or {}).get("cluster_ids") == [302]
                return FakeCursor(rows=[{"cluster_id": 302, "last_seen_version": 4}])
            raise AssertionError(f"unexpected SQL: {normalized}")

    @contextmanager
    def fake_connect():
        yield FakeConn()

    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL", "1")
    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "event_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_read_local_read_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(remote_db, "_read_feed_snapshot", lambda *args, **kwargs: None)

    result = remote_db.fetch_events(
        page=1,
        limit=20,
        user_id="user-1",
        public_only=False,
        min_github_stars=50,
        enabled=True,
        categories=["products"],
    )

    assert result["read_model"] == "highlights_v1"
    assert result["scope_key"] == "category:products"
    assert result["events"][0]["has_update"] is True
    assert result["events"][0]["last_seen_version"] == 4

    remote_db.clear_feed_cache_keys()


def test_remote_cluster_detail_and_bundle_use_member_cover_fallback(monkeypatch):
    import remote_db

    calls = []

    class FakeCursor:
        def __init__(self, row=None, rows=None):
            self.row = row
            self.rows = [] if rows is None else rows

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class FakeConn:
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            calls.append(normalized)
            if "FROM remote_poc.clusters c" in normalized:
                return FakeCursor(row={
                    "id": 8,
                    "ai_title": "remote detail",
                    "ai_summary": None,
                    "ai_key_points": [],
                    "doc_count": 1,
                    "unique_source_count": 1,
                    "platforms_json": [],
                    "cover_url": "/images/events/member-cover.jpg",
                    "first_doc_at": None,
                    "last_doc_at": None,
                    "live_version": 1,
                    "merged_into": None,
                    "is_visible_in_feed": True,
                })
            return FakeCursor(rows=[])

    @contextmanager
    def fake_connect():
        yield FakeConn()

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "_cache_get_copy", lambda key: None)
    monkeypatch.setattr(remote_db, "_cache_set_copy", lambda key, value: value)

    assert remote_db.cluster_detail(cluster_id=8)["cover_url"] == "/images/events/member-cover.jpg"
    assert remote_db.cluster_bundle(cluster_id=8)["cluster"]["cover_url"] == "/images/events/member-cover.jpg"

    cluster_selects = [sql for sql in calls if "FROM remote_poc.clusters c" in sql]
    assert len(cluster_selects) == 2
    assert all("COALESCE(NULLIF(c.cover_url, ''), detail_cover.cover_url) AS cover_url" in sql for sql in cluster_selects)
    assert all("LEFT JOIN LATERAL" in sql for sql in cluster_selects)


def test_cluster_detail_dispatches_to_remote_backend(monkeypatch):
    import routes.clusters as clusters

    calls = {}

    def fake_detail(**kwargs):
        calls.update(kwargs)
        return {"id": 7, "ai_title": "remote detail", "data_backend": "supabase_poc"}

    monkeypatch.setattr(clusters.remote_db, "events_read_from_remote", lambda: True)
    monkeypatch.setattr(clusters.remote_db, "cluster_detail", fake_detail)

    result = asyncio.run(clusters.cluster_detail(_request(), 7))

    assert result["ai_title"] == "remote detail"
    assert calls == {"cluster_id": 7, "public_only": True, "user_id": None}


def test_cluster_sources_dispatches_to_remote_backend(monkeypatch):
    import routes.clusters as clusters

    calls = {}

    def fake_sources(**kwargs):
        calls.update(kwargs)
        return {
            "sources": [{"item_id": "remote_item"}],
            "next_cursor": None,
            "data_backend": "supabase_poc",
        }

    monkeypatch.setattr(clusters.remote_db, "events_read_from_remote", lambda: True)
    monkeypatch.setattr(clusters.remote_db, "cluster_sources", fake_sources)

    result = asyncio.run(clusters.cluster_sources(_request(), 8, page=3, limit=4))

    assert result["sources"][0]["item_id"] == "remote_item"
    assert calls == {"cluster_id": 8, "page": 3, "limit": 4, "public_only": True}


def test_cluster_bundle_dispatches_to_remote_backend(monkeypatch):
    import routes.clusters as clusters

    calls = {}

    def fake_bundle(**kwargs):
        calls.update(kwargs)
        return {
            "cluster": {"id": kwargs["cluster_id"], "ai_title": "remote bundle"},
            "sources": [{"item_id": "remote_item"}],
            "sources_next_cursor": None,
            "data_backend": "supabase_poc",
        }

    monkeypatch.setattr(clusters.remote_db, "events_read_from_remote", lambda: True)
    monkeypatch.setattr(clusters.remote_db, "cluster_bundle", fake_bundle)

    result = asyncio.run(clusters.cluster_bundle(_request(), 8, page=3, limit=4))

    assert result["cluster"]["ai_title"] == "remote bundle"
    assert result["sources"][0]["item_id"] == "remote_item"
    assert calls == {
        "cluster_id": 8,
        "page": 3,
        "limit": 4,
        "public_only": True,
        "user_id": None,
    }


def test_remote_cluster_source_queries_filter_manual_items_for_public_requests(monkeypatch):
    import remote_db

    class FakeCursor:
        def __init__(self, row=None, rows=None):
            self.row = row
            self.rows = [] if rows is None else rows

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class FakeConn:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.calls.append((normalized, params or {}))
            lower = normalized.lower()
            if "from remote_poc.clusters c" in lower:
                return FakeCursor(row={
                    "id": 8,
                    "ai_title": "remote bundle",
                    "ai_summary": None,
                    "ai_key_points": [],
                    "doc_count": 1,
                    "unique_source_count": 1,
                    "platforms_json": [],
                    "cover_url": None,
                    "first_doc_at": None,
                    "last_doc_at": None,
                    "live_version": 1,
                    "merged_into": None,
                    "is_visible_in_feed": True,
                })
            return FakeCursor(rows=[])

    fake = FakeConn()

    @contextmanager
    def fake_connect():
        yield fake

    monkeypatch.setattr(remote_db, "connect", fake_connect)

    remote_db.cluster_sources(cluster_id=8, public_only=True)
    remote_db.cluster_bundle(cluster_id=8, public_only=True)

    source_queries = [
        sql for sql, _ in fake.calls
        if sql.lower().startswith("select i.id as item_id")
        and "from remote_poc.cluster_items ci join remote_poc.items i" in sql.lower()
    ]
    assert len(source_queries) == 2
    assert all("i.platform != 'manual'" in sql for sql in source_queries)


def test_cluster_seen_dispatches_to_remote_backend(monkeypatch):
    import routes.clusters as clusters

    calls = {}

    def fake_seen(**kwargs):
        calls.update(kwargs)
        return {"cluster_id": kwargs["cluster_id"], "last_seen_version": 3}

    monkeypatch.setattr(clusters.remote_db, "events_read_from_remote", lambda: True)
    monkeypatch.setattr(clusters.remote_db, "mark_cluster_seen", fake_seen)

    result = asyncio.run(clusters.cluster_seen(_request(user={"id": "u1"}), 9))

    assert result == {"cluster_id": 9, "last_seen_version": 3}
    assert calls == {"cluster_id": 9, "user_id": "u1"}


def test_context_search_dispatches_to_remote_without_sqlite(monkeypatch):
    import routes.clusters as clusters

    calls = {}

    def fake_context_search(**kwargs):
        calls.update(kwargs)
        return {"docs": [{"id": "remote_doc"}], "docs_total": 1, "events": [], "events_total": 0}

    def forbidden_conn():
        raise AssertionError("SQLite should not be opened")

    monkeypatch.setattr(clusters.remote_db, "feed_read_from_remote", lambda: True)
    monkeypatch.setattr(clusters.remote_db, "events_read_from_remote", lambda: True)
    monkeypatch.setattr(clusters.remote_db, "context_search", fake_context_search)
    monkeypatch.setattr(clusters, "_github_cluster_display_min_stars", lambda: 42)
    monkeypatch.setattr(clusters.db, "get_conn", forbidden_conn)

    result = asyncio.run(clusters.context_search(_request(), q="claude", context="recommend", limit=7))

    assert result["docs"][0]["id"] == "remote_doc"
    assert calls == {
        "q": "claude",
        "context": "recommend",
        "limit": 7,
        "user_id": None,
        "public_only": True,
        "manual_owner_user_id": None,
        "min_github_stars": 42,
        "categories": [],
        "events_only": False,
    }

    calls.clear()
    asyncio.run(clusters.context_search(_request(), q="claude", context="recommend", limit=7, events_only=True))
    assert calls["events_only"] is True


def test_context_search_events_only_sets_timeouts_before_live_cluster_query(monkeypatch):
    import remote_db

    class FakeCursor:
        def __init__(self, row=None, rows=None):
            self.row = row
            self.rows = [] if rows is None else rows

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class FakeConn:
        def __init__(self):
            self.sqls = []

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.sqls.append(normalized)
            lower = normalized.lower()
            if normalized.startswith("SET LOCAL"):
                return FakeCursor()
            if "select count(*) as n from remote_poc.clusters c" in lower:
                return FakeCursor(row={"n": 0})
            if "from remote_poc.clusters c left join lateral" in lower:
                return FakeCursor(rows=[])
            return FakeCursor(rows=[])

    fake = FakeConn()

    @contextmanager
    def fake_connect():
        yield fake

    monkeypatch.setattr(remote_db, "connect", fake_connect)

    result = remote_db.context_search(
        q="polymarket",
        context="recommend",
        limit=30,
        public_only=True,
        events_only=True,
    )

    cluster_idx = next(
        i for i, sql in enumerate(fake.sqls)
        if "FROM remote_poc.clusters c LEFT JOIN LATERAL" in sql
    )
    expected_ms = remote_db._context_search_events_only_statement_timeout_ms()
    assert f"SET LOCAL statement_timeout = '{expected_ms}ms'" in fake.sqls[:cluster_idx]
    assert "SET LOCAL idle_in_transaction_session_timeout = '15000ms'" in fake.sqls[:cluster_idx]
    assert result["events"] == []
    assert result["events_total"] == 0


def test_context_search_events_only_degrades_when_live_cluster_query_fails(monkeypatch):
    import remote_db

    class FakeCursor:
        def fetchall(self):
            return []

    class FakeConn:
        def __init__(self):
            self.sqls = []

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.sqls.append(normalized)
            if normalized.startswith("SET LOCAL"):
                return FakeCursor()
            if "FROM remote_poc.clusters c LEFT JOIN LATERAL" in normalized:
                raise RuntimeError("simulated slow search failure")
            return FakeCursor()

    fake = FakeConn()

    @contextmanager
    def fake_connect():
        yield fake

    monkeypatch.setattr(remote_db, "connect", fake_connect)

    result = remote_db.context_search(
        q="polymarket",
        context="recommend",
        limit=30,
        public_only=True,
        events_only=True,
    )

    assert result["docs"] == []
    assert result["events"] == []
    assert result["events_total"] == 0
    assert result["degraded"] is True
    assert result["degraded_reason"] == "context_search_events_unavailable"


def test_context_search_events_only_caches_degraded_failure(monkeypatch):
    import remote_db

    remote_db.clear_feed_cache_keys()

    class FakeCursor:
        def fetchall(self):
            return []

    class FakeConn:
        def __init__(self):
            self.sqls = []

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.sqls.append(normalized)
            if normalized.startswith("SET LOCAL"):
                return FakeCursor()
            if "FROM remote_poc.clusters c LEFT JOIN LATERAL" in normalized:
                raise RuntimeError("simulated slow search failure")
            return FakeCursor()

    fake = FakeConn()

    @contextmanager
    def fake_connect():
        yield fake

    monkeypatch.setattr(remote_db, "connect", fake_connect)

    first = remote_db.context_search(
        q="unique-cache-keyword",
        context="recommend",
        limit=30,
        public_only=True,
        events_only=True,
    )
    first_cluster_queries = sum(
        1 for sql in fake.sqls if "FROM remote_poc.clusters c LEFT JOIN LATERAL" in sql
    )
    second = remote_db.context_search(
        q="unique-cache-keyword",
        context="recommend",
        limit=30,
        public_only=True,
        events_only=True,
    )
    second_cluster_queries = sum(
        1 for sql in fake.sqls if "FROM remote_poc.clusters c LEFT JOIN LATERAL" in sql
    )

    assert first["degraded_reason"] == "context_search_events_unavailable"
    assert second["degraded_reason"] == "context_search_events_unavailable"
    assert first_cluster_queries == 1
    assert second_cluster_queries == 1

    remote_db.clear_feed_cache_keys()


def test_get_feed_dispatches_to_remote_backend(monkeypatch):
    import routes.feed as feed

    calls = {}

    def fake_query_feed(**kwargs):
        calls.update(kwargs)
        return {
            "items": [{"id": "remote_item"}],
            "total": 1,
            "offset": kwargs["offset"],
            "limit": kwargs["limit"],
            "data_backend": "supabase_poc",
        }

    monkeypatch.setattr(feed.remote_db, "feed_read_from_remote", lambda: True)
    monkeypatch.setattr(feed.remote_db, "query_feed", fake_query_feed)
    monkeypatch.setattr(feed, "_github_display_min_stars", lambda: 42)

    response = feed.get_feed(
        _request(),
        platform="reddit",
        source="ClaudeAI",
        unread=False,
        starred=False,
        clicked=False,
        search="claude",
        limit=5,
        offset=10,
    )
    body = json.loads(response.body)

    assert body["items"][0]["id"] == "remote_item"
    assert calls == {
        "platform": "reddit",
        "source": "ClaudeAI",
        "unread": False,
        "starred": False,
        "clicked": False,
        "search": "claude",
        "limit": 5,
        "offset": 10,
        "user_id": None,
        "public_only": True,
        "manual_owner_user_id": None,
        "min_github_stars": 42,
    }


def test_feed_sections_dispatches_search_to_remote_backend(monkeypatch):
    import routes.feed as feed

    calls = {}

    def fake_sections(**kwargs):
        calls.update(kwargs)
        return {
            "sections": {"models": [{"id": "remote_item"}]},
            "cat_counts": {"models": 200},
            "total": 200,
            "data_backend": "supabase_poc",
        }

    monkeypatch.setattr(feed.remote_db, "feed_read_from_remote", lambda: True)
    monkeypatch.setattr(feed.remote_db, "query_feed_sections", fake_sections)
    monkeypatch.setattr(feed, "_github_display_min_stars", lambda: 42)

    result = feed.get_feed_sections(_request(), search="claude")

    assert result["sections"]["models"][0]["id"] == "remote_item"
    assert calls == {
        "per_category": 50,
        "search": "claude",
        "user_id": None,
        "public_only": True,
        "manual_owner_user_id": None,
        "min_github_stars": 42,
    }


def test_feed_sections_more_dispatches_search_to_remote_backend(monkeypatch):
    import routes.feed as feed

    calls = {}

    def fake_category(**kwargs):
        calls.update(kwargs)
        return {
            "items": [{"id": "remote_item"}],
            "category": kwargs["category"],
            "total": 88,
            "data_backend": "supabase_poc",
        }

    monkeypatch.setattr(feed.remote_db, "feed_read_from_remote", lambda: True)
    monkeypatch.setattr(feed.remote_db, "query_feed_by_category", fake_category)
    monkeypatch.setattr(feed, "_github_display_min_stars", lambda: 42)

    result = feed.get_feed_sections_more(
        _request(),
        category="models",
        offset=50,
        limit=25,
        keyword=None,
        search="claude",
        subcategory="llm",
    )

    assert result["items"][0]["id"] == "remote_item"
    assert calls == {
        "category": "models",
        "offset": 50,
        "limit": 25,
        "keyword": None,
        "search": "claude",
        "subcategory": "llm",
        "cursor": None,
        "user_id": None,
        "public_only": True,
        "manual_owner_user_id": None,
        "min_github_stars": 42,
    }


def test_feed_platforms_dispatches_to_remote_backend(monkeypatch):
    import routes.feed as feed

    calls = {}

    def fake_platforms(**kwargs):
        calls.update(kwargs)
        return {
        "sections": {"reddit": [{"id": "r1"}]},
            "platform_counts": {"reddit": 1},
            "source_counts": {"reddit": {"ClaudeAI": 1}},
            "category_counts": {"reddit": {"models": 1}},
            "data_backend": "supabase_poc",
        }

    monkeypatch.setattr(feed.remote_db, "feed_read_from_remote", lambda: True)
    monkeypatch.setattr(feed.remote_db, "query_feed_platforms", fake_platforms)
    monkeypatch.setattr(feed, "_github_display_min_stars", lambda: 42)

    result = feed.get_feed_platforms(_request())

    assert result["sections"]["reddit"][0]["id"] == "r1"
    assert calls == {
        "per_platform": 50,
        "search": None,
        "user_id": None,
        "public_only": True,
        "manual_owner_user_id": None,
        "min_github_stars": 42,
    }


def test_feed_item_dispatches_to_remote_backend(monkeypatch):
    import routes.feed as feed

    calls = {}

    def fake_item(**kwargs):
        calls.update(kwargs)
        return {"id": kwargs["item_id"], "data_backend": "supabase_poc"}

    monkeypatch.setattr(feed.remote_db, "feed_read_from_remote", lambda: True)
    monkeypatch.setattr(feed.remote_db, "get_feed_item", fake_item)
    monkeypatch.setattr(feed, "_github_display_min_stars", lambda: 42)

    response = feed.get_feed_item("remote_item", _request())
    body = json.loads(response.body)

    assert body["id"] == "remote_item"
    assert calls == {
        "item_id": "remote_item",
        "public_only": True,
        "can_access_all": False,
        "user_id": None,
        "min_github_stars": 42,
    }


def test_admin_overview_dispatches_to_remote_without_sqlite(monkeypatch):
    import routes.admin as admin

    def forbidden_conn():
        raise AssertionError("remote admin overview should not open SQLite")

    monkeypatch.setattr(admin.remote_db, "app_state_to_remote", lambda: True)
    monkeypatch.setattr(
        admin.remote_db,
        "admin_overview_remote",
        lambda **kwargs: {
            "codes": [],
            "users": [],
            "fetch_runs": {"runs": [], "limit": kwargs["fetch_run_limit"], "offset": kwargs["fetch_run_offset"]},
            "embedding_usage": {"summary": {}, "by_source": [], "by_run": [], "logs": []},
        },
    )
    monkeypatch.setattr(admin.db, "get_conn", forbidden_conn)

    result = asyncio.run(admin.admin_overview(_request(user={"id": "u1", "role": "admin"})))

    assert result["fetch_runs"]["limit"] == 20


def test_admin_fetch_run_detail_dispatches_to_remote_without_sqlite(monkeypatch):
    import routes.admin as admin

    def forbidden_conn():
        raise AssertionError("remote fetch-run detail should not open SQLite")

    monkeypatch.setattr(admin.remote_db, "app_state_to_remote", lambda: True)
    monkeypatch.setattr(admin.remote_db, "get_fetch_run_audit_remote", lambda run_id: {"id": run_id, "audit": {}})
    monkeypatch.setattr(admin.db, "get_conn", forbidden_conn)

    result = asyncio.run(admin.get_fetch_run(77, _request(user={"id": "u1", "role": "admin"})))

    assert result["run"]["id"] == 77


def test_admin_embedding_usage_dispatches_to_remote_without_sqlite(monkeypatch):
    import routes.admin as admin

    calls = {}

    def forbidden_conn():
        raise AssertionError("remote embedding usage should not open SQLite")

    def fake_usage(**kwargs):
        calls.update(kwargs)
        return {"summary": {}, "by_source": [], "by_run": [], "logs": []}

    monkeypatch.setattr(admin.remote_db, "app_state_to_remote", lambda: True)
    monkeypatch.setattr(admin.remote_db, "get_embedding_usage_audit_remote", fake_usage)
    monkeypatch.setattr(admin.db, "get_conn", forbidden_conn)

    result = asyncio.run(admin.get_embedding_usage(_request(user={"id": "u1", "role": "admin"}), hours=48, limit=10))

    assert result["summary"] == {}
    assert calls == {"hours": 48.0, "run_id": None, "limit": 10}


def test_record_embedding_usage_remote_falls_back_to_rest_when_pool_full(monkeypatch):
    import remote_db

    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'[{"id":123}]'

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResp()

    monkeypatch.setattr(
        remote_db,
        "connect",
        lambda: (_ for _ in ()).throw(remote_db.RemoteDBError("pool full")),
    )
    monkeypatch.setattr(remote_db, "supabase_project_url", lambda: "https://example.supabase.co")
    monkeypatch.setattr(remote_db, "supabase_service_role_key", lambda: "service-role")
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db.urllib.request, "urlopen", fake_urlopen)

    row_id = remote_db.record_embedding_usage_remote({
        "provider": "openrouter-text-embedding-3-small",
        "model": "openai/text-embedding-3-small",
        "mode": "db",
        "source": "unit-test",
        "stage": "rest-fallback",
        "input_count": 1,
        "input_chars": 5,
        "input_bytes": 5,
        "estimated_tokens": 2,
        "output_count": 1,
        "output_dim": 1536,
        "status": "success",
        "item_ids_json": ["item-1"],
    })

    assert row_id == 123
    assert captured["url"] == "https://example.supabase.co/rest/v1/embedding_usage_logs"
    assert captured["body"]["item_ids_json"] == ["item-1"]
    assert captured["body"]["provider"] == "openrouter-text-embedding-3-small"
    assert captured["timeout"] == 20


def test_status_dispatches_to_remote_backend(monkeypatch):
    import routes.feed as feed

    calls = {}

    def fake_set_status(**kwargs):
        calls.update(kwargs)
        return {"ok": True, "data_backend": "supabase_poc"}

    monkeypatch.setattr(feed.remote_db, "status_write_to_remote", lambda: True)
    monkeypatch.setattr(feed.remote_db, "set_status", fake_set_status)

    request = _request(user={"id": "u1", "role": "user"})

    async def json_body():
        return {"item_id": "remote_item", "action": "starred"}

    request.json = json_body
    result = asyncio.run(feed.set_item_status(request))

    assert result == {"ok": True, "data_backend": "supabase_poc"}
    assert calls == {
        "item_id": "remote_item",
        "action": "starred",
        "user_id": "u1",
        "can_access_all": False,
    }


def test_remote_db_pressure_reports_recent_statement_timeout(monkeypatch):
    import remote_db

    class FakeCursor:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class FakeConn:
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SET LOCAL"):
                return FakeCursor({})
            if "status = 'running'" in normalized:
                return FakeCursor({"has_running": False})
            if "has_recent_timeout" in normalized:
                return FakeCursor({"has_recent_timeout": True})
            if "pg_stat_progress_vacuum" in normalized:
                return FakeCursor({"active_vacuums": 0, "max_autovacuum_age_sec": 0})
            raise AssertionError(normalized)

    @contextmanager
    def fake_connect():
        yield FakeConn()

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "fetch_run_heartbeat_grace_seconds", lambda: 600)

    result = remote_db.remote_db_pressure(timeout_minutes=15, probe_timeout_ms=1000)

    assert result["ok"] is True
    assert result["pressure"] is True
    assert result["reasons"] == ["recent_statement_timeout"]


def test_remote_db_pressure_reports_long_autovacuum(monkeypatch):
    import remote_db

    class FakeCursor:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class FakeConn:
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SET LOCAL"):
                return FakeCursor({})
            if "status = 'running'" in normalized:
                return FakeCursor({"has_running": False})
            if "has_recent_timeout" in normalized:
                return FakeCursor({"has_recent_timeout": False})
            if "pg_stat_progress_vacuum" in normalized:
                return FakeCursor({"active_vacuums": 1, "max_autovacuum_age_sec": 2400})
            raise AssertionError(normalized)

    @contextmanager
    def fake_connect():
        yield FakeConn()

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "fetch_run_heartbeat_grace_seconds", lambda: 600)

    result = remote_db.remote_db_pressure(autovacuum_age_sec=1800, probe_timeout_ms=1000)

    assert result["pressure"] is True
    assert result["reasons"] == ["long_autovacuum"]
    assert result["detail"]["active_vacuums"] == 1


def test_remote_db_pressure_fails_closed_on_probe_error(monkeypatch):
    import remote_db

    class FakeConn:
        def execute(self, sql, params=None):
            if " ".join(sql.split()).startswith("SET LOCAL"):
                return SimpleNamespace(fetchone=lambda: {})
            raise RuntimeError("statement timeout")

    @contextmanager
    def fake_connect():
        yield FakeConn()

    monkeypatch.setattr(remote_db, "connect", fake_connect)

    result = remote_db.remote_db_pressure(probe_timeout_ms=1000)

    assert result["ok"] is False
    assert result["pressure"] is True
    assert result["reasons"] == ["pressure_probe_failed"]
    assert "statement timeout" in result["error"]


def test_cache_prewarm_interval_defaults_to_stable_600s(monkeypatch):
    import app

    monkeypatch.delenv("INFO2ACTION_CACHE_PREWARM_INTERVAL_SEC", raising=False)
    assert app._cache_prewarm_interval_sec() == 600

    monkeypatch.setenv("INFO2ACTION_CACHE_PREWARM_INTERVAL_SEC", "90")
    assert app._cache_prewarm_interval_sec() == 90


def test_remote_cache_prewarm_skips_all_when_remote_db_pressure(monkeypatch):
    import app

    calls = []
    monkeypatch.setattr(app.fetch, "has_local_active_fetch_runs", lambda: False)
    monkeypatch.setattr(app.remote_db, "remote_authority_enabled", lambda: True)
    monkeypatch.setattr(
        app.remote_db,
        "remote_db_pressure",
        lambda: {"ok": True, "pressure": True, "reasons": ["recent_statement_timeout"]},
    )
    monkeypatch.setattr(app.remote_db, "prewarm_platforms", lambda: calls.append("platforms"))
    monkeypatch.setattr(app.remote_db, "prewarm_events_categories", lambda: calls.append("events"))

    app._run_remote_cache_prewarm_iteration(7)

    assert calls == []


def test_fetch_events_content_shared_across_users_with_seen_overlay(monkeypatch):
    """P0-2: N 个登录用户共享一份内容缓存,seen 状态按用户薄覆盖。"""
    import remote_db

    remote_db.clear_feed_cache_keys()
    with remote_db._INFLIGHT_LOCK:
        remote_db._INFLIGHT.clear()

    class FakeCursor:
        def __init__(self, row=None, rows=None):
            self.row = row
            self.rows = [] if rows is None else rows

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    heavy_hits = {"n": 0}

    class FakeConn:
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SET LOCAL"):
                return FakeCursor()
            if "FROM remote_poc.highlights_read_model_state" in normalized:
                return FakeCursor(row={
                    "version_id": "00000000-0000-0000-0000-00000000beef",
                    "scope_key": "all",
                    "total_count": 1,
                })
            if "FROM remote_poc.highlights_scope_items" in normalized and "GROUP BY day" in normalized:
                return FakeCursor(rows=[{"day": "2026-07-04", "n": 1}])
            if "FROM remote_poc.highlights_scope_items" in normalized:
                heavy_hits["n"] += 1
                return FakeCursor(rows=[{
                    "rank": 1,
                    "cluster_id": 901,
                    "sort_at": "2026-07-04T01:00:00+00:00",
                    "card_json": {
                        "id": 901,
                        "ai_title": "Shared content event",
                        "doc_count": 2,
                        "unique_source_count": 2,
                        "first_doc_at": "2026-07-04T01:00:00+00:00",
                        "live_version": 3,
                    },
                }])
            if "FROM remote_poc.cluster_status" in normalized:
                if params["user_id"] == "user-a":
                    return FakeCursor(rows=[{"cluster_id": 901, "last_seen_version": 1}])
                return FakeCursor(rows=[])
            raise AssertionError(f"unexpected SQL: {normalized}")

    from contextlib import contextmanager

    @contextmanager
    def fake_connect():
        yield FakeConn()

    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL", "1")
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL_STALE_FALLBACK", "0")
    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "event_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_write_feed_snapshot_async", lambda *a, **k: None)
    monkeypatch.setattr(remote_db, "_write_local_read_cache_async", lambda *a, **k: None)

    kw = dict(page=1, limit=20, public_only=False, min_github_stars=50,
              enabled=True, categories=[])
    a = remote_db.fetch_events(user_id="user-a", **kw)
    b = remote_db.fetch_events(user_id="user-b", **kw)

    # 内容只计算一次:第二个用户吃共享缓存
    assert heavy_hits["n"] == 1
    ev_a, ev_b = a["events"][0], b["events"][0]
    assert ev_a["id"] == ev_b["id"] == 901
    # user-a 看过 v1,live v3 → 有更新;user-b 没看过 → 无更新标记
    assert ev_a["last_seen_version"] == 1 and ev_a["has_update"] is True
    assert ev_b["last_seen_version"] is None and ev_b["has_update"] is False

    remote_db.clear_feed_cache_keys()


# ── BF-0704-6: 搜索超时可配置 + count 封顶 + 搜索失败不熔断 feed ──


def test_context_search_events_only_timeout_env_override(monkeypatch):
    import remote_db

    monkeypatch.setenv(
        "INFO2ACTION_CONTEXT_SEARCH_EVENTS_ONLY_STATEMENT_TIMEOUT_MS", "2500"
    )
    assert remote_db._context_search_events_only_statement_timeout_ms() == 2500
    # 仍不得超过通用 context search 预算
    monkeypatch.setenv(
        "INFO2ACTION_CONTEXT_SEARCH_EVENTS_ONLY_STATEMENT_TIMEOUT_MS", "99999"
    )
    assert (
        remote_db._context_search_events_only_statement_timeout_ms()
        == remote_db._context_search_statement_timeout_ms()
    )


def test_context_search_events_total_count_is_capped(monkeypatch):
    import remote_db

    remote_db.clear_feed_cache_keys()

    class FakeCursor:
        def __init__(self, row=None, rows=None):
            self.row = row
            self.rows = [] if rows is None else rows

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    captured = {}

    class FakeConn:
        def __init__(self):
            self.sqls = []

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.sqls.append(normalized)
            if normalized.startswith("SET LOCAL"):
                return FakeCursor()
            if "SELECT count(*) AS n FROM" in normalized:
                captured["count_sql"] = normalized
                captured["count_params"] = dict(params or {})
                return FakeCursor(row={"n": 1001})
            if "FROM remote_poc.clusters c LEFT JOIN LATERAL" in normalized:
                return FakeCursor(rows=[])
            return FakeCursor(rows=[])

    fake = FakeConn()

    @contextmanager
    def fake_connect():
        yield fake

    monkeypatch.setattr(remote_db, "connect", fake_connect)

    result = remote_db.context_search(
        q="bf0704-capped-count",
        context="recommend",
        limit=30,
        public_only=True,
        events_only=True,
    )

    # count 必须走 LIMIT 封顶的子查询,不允许全量 count 扫描
    assert "count_sql" in captured, "events total count query not executed"
    assert "SELECT 1 FROM remote_poc.clusters c" in captured["count_sql"]
    assert "LIMIT %(count_cap)s" in captured["count_sql"]
    assert captured["count_params"].get("count_cap") == remote_db.CONTEXT_SEARCH_EVENTS_TOTAL_CAP
    assert result["events_total"] == 1001


def test_query_feed_search_uses_search_timeout_and_does_not_open_feed_circuit(monkeypatch):
    import remote_db

    remote_db.clear_feed_cache_keys()
    # 前序测试可能已打开 feed live 熔断器(模块级状态),先复位
    monkeypatch.setattr(remote_db, "_REMOTE_FEED_LIVE_CIRCUIT_OPEN_UNTIL", 0.0)

    timeouts = []
    monkeypatch.setattr(
        remote_db,
        "_set_short_statement_timeout",
        lambda conn, timeout_ms=0: timeouts.append(timeout_ms),
    )

    circuit_marks = []
    monkeypatch.setattr(
        remote_db,
        "_mark_remote_feed_live_circuit_open",
        lambda: circuit_marks.append(True),
    )

    class FailingConn:
        def execute(self, sql, params=None):
            raise RuntimeError("simulated search timeout")

    @contextmanager
    def fake_connect():
        yield FailingConn()

    monkeypatch.setattr(remote_db, "connect", fake_connect)

    result = remote_db.query_feed(
        search="bf0704-search-keyword",
        limit=30,
        public_only=True,
    )

    assert result["degraded"] is True
    assert result["items"] == []
    # 搜索查询必须使用独立的搜索超时预算(默认大于 live feed 的 2.5s)
    assert timeouts and timeouts[0] == remote_db._remote_feed_search_timeout_ms()
    assert timeouts[0] > remote_db._remote_feed_live_timeout_ms()
    # 搜索失败不得打开 feed live 熔断器(避免拖垮无关的信息流读取)
    assert circuit_marks == []


# ── BF-0704-6 rev2: 事件搜索标题优先 + 摘要补充 ──


class _TitleFirstFakeCursor:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = [] if rows is None else rows

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


def _title_first_conn(title_rows, supplement_rows=None, supplement_error=None):
    class FakeConn:
        def __init__(self):
            self.sqls = []
            self.rolled_back = 0

        def rollback(self):
            self.rolled_back += 1

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.sqls.append(normalized)
            if normalized.startswith("SET LOCAL"):
                return _TitleFirstFakeCursor()
            if "SELECT count(*) AS n FROM" in normalized:
                return _TitleFirstFakeCursor(row={"n": len(title_rows)})
            if "LEFT JOIN LATERAL" in normalized:
                if "AND NOT (c.ai_title ILIKE" in normalized:
                    if supplement_error is not None:
                        raise supplement_error
                    return _TitleFirstFakeCursor(rows=list(supplement_rows or []))
                return _TitleFirstFakeCursor(rows=list(title_rows))
            return _TitleFirstFakeCursor(rows=[])

    return FakeConn()


def _mk_cluster_row(cluster_id, first_doc_at):
    return {
        "id": cluster_id,
        "ai_title": f"t{cluster_id}",
        "ai_summary": "s",
        "doc_count": 1,
        "unique_source_count": 2,
        "first_doc_at": first_doc_at,
        "last_doc_at": first_doc_at,
        "platforms_json": None,
        "cover_url": None,
        "live_version": 1,
    }


def test_context_search_events_query_is_title_first(monkeypatch):
    import remote_db

    remote_db.clear_feed_cache_keys()
    rows = [_mk_cluster_row(i, f"2026-07-0{9 - i}T00:00:00+00:00") for i in range(1, 4)]
    fake = _title_first_conn(rows)

    @contextmanager
    def fake_connect():
        yield fake

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "_fetch_event_source_metadata", lambda *a, **k: {})

    result = remote_db.context_search(
        q="bf0704-title-first",
        context="recommend",
        limit=3,
        public_only=True,
        events_only=True,
    )

    event_sqls = [s for s in fake.sqls if "LEFT JOIN LATERAL" in s]
    # 标题命中已满一页 → 只发一条事件查询,且谓词是 ai_title 而不是 concat 全文
    assert len(event_sqls) == 1
    assert "c.ai_title ILIKE %(search_like)s" in event_sqls[0]
    assert "|| ' ' ||" not in event_sqls[0]
    # count 同样只按标题算
    count_sqls = [s for s in fake.sqls if "SELECT count(*) AS n FROM" in s]
    assert len(count_sqls) == 1 and "c.ai_title ILIKE" in count_sqls[0]
    assert "|| ' ' ||" not in count_sqls[0]
    assert [e["id"] for e in result["events"]] == [1, 2, 3]


def test_context_search_events_supplements_from_summary_when_title_short(monkeypatch):
    import remote_db

    remote_db.clear_feed_cache_keys()
    title_rows = [_mk_cluster_row(1, "2026-07-01T00:00:00+00:00")]
    supplement = [_mk_cluster_row(2, "2026-07-03T00:00:00+00:00")]
    fake = _title_first_conn(title_rows, supplement_rows=supplement)

    @contextmanager
    def fake_connect():
        yield fake

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "_fetch_event_source_metadata", lambda *a, **k: {})

    result = remote_db.context_search(
        q="bf0704-supplement",
        context="recommend",
        limit=5,
        public_only=True,
        events_only=True,
    )

    event_sqls = [s for s in fake.sqls if "LEFT JOIN LATERAL" in s]
    assert len(event_sqls) == 2
    assert "AND NOT (c.ai_title ILIKE %(search_like)s)" in event_sqls[1]
    # 合并后按 first_doc_at DESC 重排:补充命中(07-03)排在标题命中(07-01)前
    assert [e["id"] for e in result["events"]] == [2, 1]
    # total = 标题 capped count + 补充命中数
    assert result["events_total"] == 2


def test_context_search_events_supplement_failure_keeps_title_results(monkeypatch):
    import remote_db

    remote_db.clear_feed_cache_keys()
    title_rows = [_mk_cluster_row(1, "2026-07-01T00:00:00+00:00")]
    fake = _title_first_conn(
        title_rows, supplement_error=RuntimeError("supplement timeout")
    )

    @contextmanager
    def fake_connect():
        yield fake

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "_fetch_event_source_metadata", lambda *a, **k: {})

    result = remote_db.context_search(
        q="bf0704-supplement-fail",
        context="recommend",
        limit=5,
        public_only=True,
        events_only=True,
    )

    # 补充失败只丢补充:标题结果照常返回,不整体降级
    assert result.get("degraded") is None
    assert [e["id"] for e in result["events"]] == [1]
    assert fake.rolled_back == 1
