"""Remote sources admin registry dispatch tests."""
import asyncio
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import db as db_mod  # noqa: E402
import remote_db  # noqa: E402
import snapshot_sources  # noqa: E402
import routes.sources as sources_route  # noqa: E402


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _One:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


def _squash(sql):
    return " ".join(sql.split())


def _admin_request(body=None, query_params=None):
    return SimpleNamespace(
        state=SimpleNamespace(user=None, legacy_authenticated=True),
        query_params=query_params or {},
        json=lambda: body or {},
    )


class _AsyncRequest:
    def __init__(self, body):
        self.state = SimpleNamespace(user=None, legacy_authenticated=True)
        self._body = body

    async def json(self):
        return self._body


def _source_row(source_id, platform, source_key, **overrides):
    now = overrides.get("now", datetime(2026, 7, 7, 1, 2, 3, tzinfo=timezone.utc))
    return {
        "id": source_id,
        "platform": platform,
        "source_key": source_key,
        "display_name": overrides.get("display_name", source_key),
        "status": overrides.get("status", "active"),
        "config_json": overrides.get("config_json"),
        "origin": overrides.get("origin", "admin_add"),
        "validated_at": overrides.get("validated_at", now),
        "consecutive_failures": overrides.get("consecutive_failures", 0),
        "last_success_at": overrides.get("last_success_at"),
        "last_error": overrides.get("last_error"),
        "created_at": overrides.get("created_at", now),
        "updated_at": overrides.get("updated_at", now),
    }


def _patch_remote(monkeypatch, fake_conn, *, enabled=True):
    @contextmanager
    def fake_connect():
        yield fake_conn

    monkeypatch.setattr(remote_db, "fetch_write_to_remote", lambda: enabled)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(sources_route, "remote_db", remote_db, raising=False)
    monkeypatch.setattr(snapshot_sources, "remote_db", remote_db, raising=False)


def test_list_sources_remote_groups_and_uses_batched_health(monkeypatch):
    executed = []

    class FakeConn:
        def execute(self, sql, params=None):
            executed.append((_squash(sql), params))
            if "FROM remote_poc.sources" in sql:
                return _Rows(
                    [
                        _source_row(
                            1,
                            "rss",
                            "https://example.test/feed.xml",
                            config_json='{"slug":"example"}',
                            consecutive_failures=2,
                            last_success_at=datetime(2026, 7, 6, tzinfo=timezone.utc),
                        ),
                        _source_row(2, "x_user", "openai", status="paused"),
                    ]
                )
            if "FROM remote_poc.items" in sql:
                return _Rows([{"source_id": 1, "c": 3}])
            if "FROM remote_poc.fetch_runs" in sql:
                return _Rows([{
                    "id": 77,
                    "started_at": datetime(2026, 7, 7, 2, 0, tzinfo=timezone.utc),
                    "finished_at": datetime(2026, 7, 7, 2, 10, tzinfo=timezone.utc),
                    "stats_json": {
                        "_x_source_attempts": {
                            "mode": "list",
                            "list_id": "123456",
                            "unmatched_posts": 2,
                            "planned": 1,
                            "attempted": 0,
                            "succeeded": 0,
                            "no_new": 0,
                            "failed": 0,
                            "missed": 1,
                            "missed_source_ids": [2],
                            "results": [],
                        }
                    },
                }])
            raise AssertionError(sql)

    fake_conn = FakeConn()
    _patch_remote(monkeypatch, fake_conn)
    monkeypatch.setattr(
        db_mod,
        "get_conn",
        lambda: (_ for _ in ()).throw(AssertionError("local sqlite should not be used")),
    )
    monkeypatch.setattr(
        sources_route.x_list_registry,
        "status_for_sources",
        lambda sources: {
            "configured": True,
            "mode": "list",
            "list_id": "123456",
            "registry_count": len(sources),
            "synced_count": 0,
            "pending_count": len(sources),
        },
        raising=False,
    )

    body = sources_route.list_sources(_admin_request())

    assert body["total"] == 2
    groups = {group["platform"]: group["sources"] for group in body["groups"]}
    assert set(groups) == {"rss", "x_user"}
    assert groups["rss"][0]["health"] == {
        "last_fetched_at": "2026-07-06T00:00:00+00:00",
        "inserted_7d": 3,
        "consecutive_failures": 2,
    }
    assert groups["x_user"][0]["health"]["inserted_7d"] == 0
    assert groups["x_user"][0]["health"]["latest_attempt"]["outcome"] == "missed"
    assert groups["rss"][0]["created_at"] == "2026-07-07T01:02:03+00:00"
    assert body["latest_x_run"]["run_id"] == 77
    assert body["latest_x_run"]["planned"] == 1
    assert body["latest_x_run"]["missed"] == 1
    assert body["latest_x_run"]["mode"] == "list"
    assert body["latest_x_run"]["list_id"] == "123456"
    assert body["latest_x_run"]["unmatched_posts"] == 2
    assert body["x_list"]["configured"] is True
    assert body["x_list"]["registry_count"] == 0

    assert len(executed) <= 3
    assert any("FROM remote_poc.sources" in sql and "%s" in sql for sql, _ in executed)
    assert any("FROM remote_poc.items" in sql for sql, _ in executed)


def test_search_wechat_remote_marks_existing_sources_with_single_any_query(monkeypatch):
    executed = []

    class FakeConn:
        def execute(self, sql, params=None):
            executed.append((_squash(sql), params))
            assert "source_key = ANY" in sql
            return _Rows([{"source_key": "ch-existing"}])

    fake_conn = FakeConn()
    _patch_remote(monkeypatch, fake_conn)
    monkeypatch.setattr(
        sources_route.fetch_lingowhale,
        "search_channels",
        lambda q, limit=20: [
            {"channel_id": "ch-existing", "name": "已添加"},
            {"channel_id": "ch-new", "name": "新号"},
        ],
    )
    monkeypatch.setattr(
        db_mod,
        "get_conn",
        lambda: (_ for _ in ()).throw(AssertionError("local sqlite should not be used")),
    )

    body = sources_route.search_wechat_sources(
        _admin_request(query_params={"q": "赛博", "limit": "2"})
    )

    assert [ch["already_in_registry"] for ch in body["channels"]] == [True, False]
    assert len(executed) == 1
    sql, params = executed[0]
    assert "FROM remote_poc.sources" in sql
    assert params == ("wechat_mp", ["ch-existing", "ch-new"], "deleted")


def test_create_source_remote_inserts_x_without_retired_gray_count(monkeypatch):
    executed = []

    class FakeConn:
        def execute(self, sql, params=None):
            executed.append((_squash(sql), params))
            if "WHERE platform = %s AND source_key = %s" in sql:
                return _One(None)
            if sql.lstrip().startswith("INSERT"):
                return _One({"id": 101})
            if "WHERE id = %s" in sql:
                return _One(_source_row(101, "x_user", "openai", display_name="OpenAI"))
            raise AssertionError(sql)

        def commit(self):
            executed.append(("COMMIT", None))

    fake_conn = FakeConn()
    _patch_remote(monkeypatch, fake_conn)
    monkeypatch.setattr(
        db_mod,
        "get_conn",
        lambda: (_ for _ in ()).throw(AssertionError("local sqlite should not be used")),
    )

    resp = asyncio.run(
        sources_route.create_source(
            _AsyncRequest(
                {
                    "platform": "x_user",
                    "source_key": "openai",
                    "display_name": "OpenAI",
                    "validated_at": "2026-07-07T00:00:00Z",
                }
            )
        )
    )

    assert resp["ok"] is True
    assert resp["source"]["id"] == 101
    assert not any("COUNT(*)" in sql for sql, _params in executed)
    insert_sql, insert_params = next(item for item in executed if item[0].startswith("INSERT"))
    assert "INSERT INTO remote_poc.sources" in insert_sql
    assert "RETURNING id" in insert_sql
    assert insert_params[:6] == ("x_user", "openai", "OpenAI", "active", None, "admin_add")
    assert insert_params[6] == "2026-07-07T00:00:00Z"


def test_patch_and_delete_source_remote_use_remote_updates(monkeypatch):
    patch_executed = []

    class PatchConn:
        def execute(self, sql, params=None):
            patch_executed.append((_squash(sql), params))
            if "SELECT * FROM remote_poc.sources WHERE id = %s" in sql:
                status = "paused" if any(item[0].startswith("UPDATE") for item in patch_executed) else "active"
                return _One(_source_row(7, "rss", "https://example.test/feed.xml", status=status))
            if sql.lstrip().startswith("UPDATE"):
                return _Rows([])
            raise AssertionError(sql)

        def commit(self):
            patch_executed.append(("COMMIT", None))

    _patch_remote(monkeypatch, PatchConn())
    patch_resp = asyncio.run(
        sources_route.patch_source(
            7,
            _AsyncRequest({"status": "paused", "config_json": {"limit": 12}}),
        )
    )

    assert patch_resp["ok"] is True
    patch_sql, patch_params = next(item for item in patch_executed if item[0].startswith("UPDATE"))
    assert patch_sql == (
        "UPDATE remote_poc.sources SET status = %s, config_json = %s, updated_at = %s WHERE id = %s"
    )
    assert patch_params[0] == "paused"
    assert json.loads(patch_params[1]) == {"limit": 12}
    assert patch_params[3] == 7

    delete_executed = []

    class DeleteConn:
        def execute(self, sql, params=None):
            delete_executed.append((_squash(sql), params))
            if "SELECT * FROM remote_poc.sources WHERE id = %s" in sql:
                status = "deleted" if any(item[0].startswith("UPDATE") for item in delete_executed) else "active"
                return _One(_source_row(7, "rss", "https://example.test/feed.xml", status=status))
            if sql.lstrip().startswith("UPDATE"):
                return _Rows([])
            raise AssertionError(sql)

        def commit(self):
            delete_executed.append(("COMMIT", None))

    _patch_remote(monkeypatch, DeleteConn())
    delete_resp = sources_route.delete_source(7, _admin_request())

    assert delete_resp["ok"] is True
    delete_sql, delete_params = next(item for item in delete_executed if item[0].startswith("UPDATE"))
    assert delete_sql == "UPDATE remote_poc.sources SET status = %s, updated_at = %s WHERE id = %s"
    assert delete_params[0] == "deleted"
    assert delete_params[2] == 7


def test_snapshot_sources_remote_exports_remote_rows(monkeypatch, tmp_path):
    executed = []

    class FakeConn:
        def execute(self, sql, params=None):
            executed.append((_squash(sql), params))
            return _Rows(
                [
                    _source_row(
                        55,
                        "rss",
                        "https://example.test/feed.xml",
                        config_json='{"slug":"example"}',
                    )
                ]
            )

    _patch_remote(monkeypatch, FakeConn())
    monkeypatch.setattr(snapshot_sources, "_snapshot_stamp", lambda: "20260707")
    monkeypatch.setattr(
        snapshot_sources.db,
        "get_conn",
        lambda: (_ for _ in ()).throw(AssertionError("local sqlite should not be used")),
    )

    result = snapshot_sources.snapshot_sources(base=str(tmp_path))

    path = tmp_path / "data" / "backups" / "sources-20260707.json"
    assert result == {"path": str(path), "rows": 1, "cleaned": 0}
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data[0]["id"] == 55
    assert data[0]["config_json"] == {"slug": "example"}
    assert data[0]["created_at"] == "2026-07-07T01:02:03+00:00"
    assert executed == [("SELECT * FROM remote_poc.sources ORDER BY id", None)]


def test_sources_dispatch_false_uses_local_sqlite(monkeypatch, tmp_path):
    local_calls = []

    class LocalConn:
        def execute(self, sql, params=None):
            local_calls.append((_squash(sql), params))
            return _Rows([])

        def close(self):
            local_calls.append(("CLOSE", None))

    monkeypatch.setattr(remote_db, "fetch_write_to_remote", lambda: False)
    monkeypatch.setattr(sources_route, "remote_db", remote_db, raising=False)
    monkeypatch.setattr(db_mod, "get_conn", lambda: LocalConn())
    monkeypatch.setattr(
        sources_route.x_list_registry,
        "status_for_sources",
        lambda sources: {"configured": True, "registry_count": len(sources)},
    )

    result = sources_route.list_sources(_admin_request())

    assert result == {
        "groups": [],
        "total": 0,
        "latest_x_run": None,
        "x_list": {"configured": True, "registry_count": 0},
    }
    assert local_calls[0][0].startswith("SELECT * FROM sources")
    assert local_calls[-1] == ("CLOSE", None)
