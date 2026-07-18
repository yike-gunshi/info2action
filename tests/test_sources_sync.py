"""Admin bulk source sync tests for live Lingowhale and the configured X List."""
import asyncio
import json
import os
import sys
from contextlib import contextmanager
from types import SimpleNamespace

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))

import db as db_mod  # noqa: E402
import remote_db  # noqa: E402
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


class _AsyncRequest:
    def __init__(self, body=None):
        self.state = SimpleNamespace(user=None, legacy_authenticated=True)
        self._body = body or {}

    async def json(self):
        return self._body


def _squash(sql):
    return " ".join(sql.split())


def _source_row(source_id, platform, source_key, **overrides):
    return {
        "id": source_id,
        "platform": platform,
        "source_key": source_key,
        "display_name": overrides.get("display_name", source_key),
        "status": overrides.get("status", "active"),
        "config_json": overrides.get("config_json"),
        "origin": overrides.get("origin", "admin_add"),
        "validated_at": None,
        "consecutive_failures": 0,
        "last_success_at": None,
        "last_error": None,
        "created_at": "2026-07-07T00:00:00Z",
        "updated_at": "2026-07-07T00:00:00Z",
    }


def _patch_remote(monkeypatch, fake_conn, *, enabled=True):
    @contextmanager
    def fake_connect():
        yield fake_conn

    monkeypatch.setattr(remote_db, "fetch_write_to_remote", lambda: enabled)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(sources_route, "remote_db", remote_db, raising=False)


def test_sync_lingowhale_remote_imports_live_tuple_groups(monkeypatch):
    groups = [
        {
            "name": "G1",
            "channels": [
                {"channel_id": "c1", "name": "葬AI-公众号"},
                {"channel_id": "c2", "name": "OnBoard!-播客"},
            ],
        },
    ]
    executed = []

    class FakeConn:
        def execute(self, sql, params=None):
            executed.append((_squash(sql), params))
            if "SELECT * FROM remote_poc.sources" in sql:
                return _One(None)
            if sql.lstrip().startswith("UPDATE"):
                return _Rows([])
            if sql.lstrip().startswith("INSERT"):
                return _One({"id": 1000 + len(executed)})
            raise AssertionError(sql)

        def commit(self):
            executed.append(("COMMIT", None))

    _patch_remote(monkeypatch, FakeConn())
    monkeypatch.setattr(sources_route.fetch_lingowhale, "fetch_groups", lambda: ({"c1": "g"}, groups))
    monkeypatch.setattr(
        db_mod,
        "get_conn",
        lambda: (_ for _ in ()).throw(AssertionError("local sqlite should not be used")),
    )

    body = asyncio.run(sources_route.sync_lingowhale_sources(_AsyncRequest({})))

    assert body == {"imported": 2, "existing": 0, "total": 2}
    insert_calls = [item for item in executed if item[0].startswith("INSERT")]
    assert len(insert_calls) == 2
    insert_sql, insert_params = insert_calls[0]
    assert "INSERT INTO remote_poc.sources" in insert_sql
    assert insert_params[:6] == (
        "wechat_mp",
        "c1",
        "葬AI-公众号",
        "active",
        json.dumps({"backend": "lingowhale"}, ensure_ascii=False),
        "reconcile_import",
    )
    assert insert_calls[1][1][:3] == ("wechat_mp", "c2", "OnBoard!-播客")


def test_sync_lingowhale_fetch_failure_returns_note(monkeypatch):
    def boom():
        raise RuntimeError("code=10010 msg=token expired")

    monkeypatch.setattr(sources_route.fetch_lingowhale, "fetch_groups", boom)

    body = asyncio.run(sources_route.sync_lingowhale_sources(_AsyncRequest({})))

    assert body["imported"] == 0
    assert body["existing"] == 0
    assert body["total"] == 0
    assert body["note"].startswith("语鲸订阅拉取失败或为空:")
    assert "语鲸 token 失效，需刷新" in body["note"]


def test_sync_twitter_following_endpoint_is_retired():
    response = asyncio.run(sources_route.sync_twitter_following(_AsyncRequest({})))

    assert response.status_code == 410
    assert json.loads(response.body)["error"] == (
        "personal X Following sync is disabled; sources registry and X List are authoritative"
    )


def test_sync_x_list_uses_registry_snapshot_and_supports_full_reconcile(monkeypatch):
    sources = [
        {"id": 1, "source_key": "alpha"},
        {"id": 2, "source_key": "beta"},
    ]
    calls = []
    monkeypatch.setattr(
        sources_route.fetch_x_users,
        "_active_x_sources",
        lambda: sources,
        raising=False,
    )

    def fake_sync(got_sources, *, full):
        calls.append((got_sources, full))
        return {
            "configured": True,
            "mode": "list",
            "list_id": "123456",
            "registry_count": 2,
            "synced_count": 2,
            "pending_count": 0,
            "synced_handles": ["alpha", "beta"],
            "pending_handles": [],
            "failed": [],
        }

    monkeypatch.setattr(
        sources_route.x_list_registry,
        "sync_registry_members",
        fake_sync,
        raising=False,
    )

    body = asyncio.run(sources_route.sync_x_list(_AsyncRequest({"full": True})))

    assert body["synced_count"] == 2
    assert body["pending_count"] == 0
    assert calls == [(sources, True)]


def test_sync_lingowhale_dispatch_false_uses_local_sqlite(monkeypatch):
    local_calls = []

    class LocalConn:
        def execute(self, sql, params=None):
            local_calls.append((_squash(sql), params))
            if sql.lstrip().startswith("SELECT"):
                return _One(None)
            if sql.lstrip().startswith("INSERT"):
                return SimpleNamespace(lastrowid=77)
            raise AssertionError(sql)

        def commit(self):
            local_calls.append(("COMMIT", None))

        def close(self):
            local_calls.append(("CLOSE", None))

    monkeypatch.setattr(remote_db, "fetch_write_to_remote", lambda: False)
    monkeypatch.setattr(sources_route, "remote_db", remote_db, raising=False)
    monkeypatch.setattr(
        remote_db,
        "connect",
        lambda: (_ for _ in ()).throw(AssertionError("remote should not be used")),
    )
    monkeypatch.setattr(db_mod, "get_conn", lambda: LocalConn())
    monkeypatch.setattr(
        sources_route.fetch_lingowhale,
        "fetch_groups",
        lambda: ({}, [{"name": "g", "channels": [{"channel_id": "local-ch", "name": "本地号"}]}]),
    )

    body = asyncio.run(sources_route.sync_lingowhale_sources(_AsyncRequest({})))

    assert body == {"imported": 1, "existing": 0, "total": 1}
    assert local_calls[0][0].startswith("SELECT * FROM sources")
    assert local_calls[1][0].startswith("INSERT INTO sources")
    assert local_calls[1][1][3] == "active"
    assert local_calls[-1] == ("CLOSE", None)

