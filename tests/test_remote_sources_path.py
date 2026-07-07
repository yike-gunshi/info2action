from contextlib import contextmanager
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))

import db
import ingest
import remote_db


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _One:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


def _squash(sql):
    return " ".join(sql.split())


def test_remote_item_upsert_writes_source_id(monkeypatch):
    executed = []

    class FakeConn:
        def execute(self, sql, params=None):
            executed.append((_squash(sql), params))
            return _Rows([])

    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")

    assert "source_id" in remote_db.REMOTE_ITEM_WRITE_COLUMNS

    remote_db.upsert_item_remote(
        FakeConn(),
        {
            "id": "item-1",
            "platform": "rss",
            "source": "feed:example",
            "source_id": 42,
            "title": "Hello",
        },
    )

    sql, params = executed[0]
    assert "INSERT INTO remote_poc.items AS target" in sql
    assert "source_id" in sql
    assert "source_id = COALESCE(excluded.source_id, target.source_id)" in sql
    assert params[remote_db.REMOTE_ITEM_WRITE_COLUMNS.index("source_id")] == 42


def test_load_source_index_remote_builds_platform_maps(monkeypatch):
    executed = []

    class FakeConn:
        def execute(self, sql, params=None):
            executed.append((_squash(sql), params))
            return _Rows(
                [
                    {
                        "id": 1,
                        "platform": "rss",
                        "source_key": "https://example.test/feed.xml",
                        "status": "active",
                        "config_json": {"slug": "example"},
                    },
                    {
                        "id": 2,
                        "platform": "x_user",
                        "source_key": "openai",
                        "status": "broken",
                        "config_json": "{}",
                    },
                    {
                        "id": 3,
                        "platform": "wechat_mp",
                        "source_key": "lw-channel",
                        "status": "active",
                        "config_json": '{"backend":"lingowhale"}',
                    },
                ]
            )

    @contextmanager
    def fake_connect():
        yield FakeConn()

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")

    idx = remote_db.load_source_index_remote()

    assert idx["rss_by_slug"]["example"] == (1, "active")
    assert idx["x_by_handle"]["openai"] == (2, "broken")
    assert idx["wechat_by_channel_id"]["lw-channel"] == (3, "active")
    assert executed[0] == (
        "SELECT id, platform, source_key, status, config_json FROM remote_poc.sources",
        None,
    )


def test_list_active_sources_remote_normalizes_rows(monkeypatch):
    executed = []

    class FakeConn:
        def execute(self, sql, params=None):
            executed.append((_squash(sql), params))
            return _Rows(
                [
                    {
                        "id": 7,
                        "source_key": "openai",
                        "display_name": "OpenAI",
                        "config_json": '{"batch": 3}',
                    }
                ]
            )

    @contextmanager
    def fake_connect():
        yield FakeConn()

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")

    rows = remote_db.list_active_sources_remote("x_user")

    assert rows == [
        {
            "id": 7,
            "source_key": "openai",
            "display_name": "OpenAI",
            "config_json": {"batch": 3},
        }
    ]
    assert executed[0] == (
        "SELECT id, source_key, display_name, config_json FROM remote_poc.sources "
        "WHERE platform=%s AND status='active' ORDER BY id",
        ("x_user",),
    )


def test_record_source_fetch_result_remote_ok_resets_broken_source(monkeypatch):
    executed = []

    class FakeConn:
        def execute(self, sql, params=None):
            executed.append((_squash(sql), params))
            if sql.lstrip().startswith("SELECT"):
                return _One({"status": "broken", "consecutive_failures": 4})
            return _Rows([])

    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")

    remote_db.record_source_fetch_result_remote(10, ok=True, broken_after=5, pg_conn=FakeConn())

    sql, params = executed[-1]
    assert "UPDATE remote_poc.sources SET status = %s" in sql
    assert "consecutive_failures = 0" in sql
    assert "last_success_at = %s" in sql
    assert "last_error = NULL" in sql
    assert params[0] == "active"
    assert params[1].endswith("Z")
    assert params[2] == params[1]
    assert params[3] == 10


def test_record_source_fetch_result_remote_failure_breaks_after_threshold(monkeypatch):
    executed = []

    class FakeConn:
        def execute(self, sql, params=None):
            executed.append((_squash(sql), params))
            if sql.lstrip().startswith("SELECT"):
                return _One({"status": "active", "consecutive_failures": 4})
            return _Rows([])

    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")

    remote_db.record_source_fetch_result_remote(
        11,
        ok=False,
        error="x" * 520,
        broken_after=5,
        pg_conn=FakeConn(),
    )

    sql, params = executed[-1]
    assert "UPDATE remote_poc.sources SET status = %s" in sql
    assert "consecutive_failures = %s" in sql
    assert "last_error = %s" in sql
    assert params[0] == "broken"
    assert params[1] == 5
    assert params[2] == "x" * 500
    assert params[3].endswith("Z")
    assert params[4] == 11


def test_source_index_for_dispatches_to_current_backend(monkeypatch):
    remote_calls = []
    local_calls = []

    monkeypatch.setattr(remote_db, "load_source_index_remote", lambda: remote_calls.append(True) or {"remote": True})
    monkeypatch.setattr(db, "load_source_index", lambda conn: local_calls.append(conn) or {"local": True})

    ingest._source_index_loaded = False
    ingest._source_index_cache = None
    monkeypatch.setattr(remote_db, "fetch_write_to_remote", lambda: True)
    assert ingest._source_index_for(object()) == {"remote": True}
    assert remote_calls == [True]
    assert local_calls == []

    ingest._source_index_loaded = False
    ingest._source_index_cache = None
    conn = object()
    monkeypatch.setattr(remote_db, "fetch_write_to_remote", lambda: False)
    assert ingest._source_index_for(conn) == {"local": True}
    assert local_calls == [conn]


def test_record_source_fetch_result_current_backend_dispatches(monkeypatch):
    remote_calls = []
    local_calls = []

    monkeypatch.setattr(db, "_broken_after_threshold", lambda: 8)
    monkeypatch.setattr(
        remote_db,
        "record_source_fetch_result_remote",
        lambda source_id, ok, error=None, broken_after=5: remote_calls.append(
            (source_id, ok, error, broken_after)
        ),
    )

    monkeypatch.setattr(remote_db, "fetch_write_to_remote", lambda: True)
    ingest.record_source_fetch_result_current_backend(21, ok=False, error="remote err")
    assert remote_calls == [(21, False, "remote err", 8)]

    class FakeLocalConn:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    local_conn = FakeLocalConn()
    monkeypatch.setattr(remote_db, "fetch_write_to_remote", lambda: False)
    monkeypatch.setattr(db, "get_conn", lambda: local_conn)
    monkeypatch.setattr(
        db,
        "record_source_fetch_result",
        lambda conn, source_id, ok, error=None, broken_after=5: local_calls.append(
            (conn, source_id, ok, error, broken_after)
        ),
    )

    ingest.record_source_fetch_result_current_backend(22, ok=True)
    assert local_calls == [(local_conn, 22, True, None, 8)]
    assert local_conn.closed is True
