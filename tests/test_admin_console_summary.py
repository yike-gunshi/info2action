from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import remote_db  # noqa: E402


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def _request(user=None):
    return SimpleNamespace(state=SimpleNamespace(user=user or {"id": "admin", "role": "admin"}))


def _decode_response(response):
    return json.loads(response.body.decode("utf-8"))


def test_admin_console_summary_non_remote_returns_unavailable_without_sqlite(monkeypatch):
    import routes.admin as admin

    def forbidden_conn():
        raise AssertionError("admin console summary must not open SQLite")

    monkeypatch.setattr(admin.remote_db, "app_state_to_remote", lambda: False)
    monkeypatch.setattr(admin.db, "get_conn", forbidden_conn)

    result = asyncio.run(admin.admin_console_summary(_request()))

    assert result == {"available": False, "reason": "remote_required"}


def test_admin_console_summary_remote_errors_return_503(monkeypatch):
    import routes.admin as admin

    monkeypatch.setattr(admin.remote_db, "app_state_to_remote", lambda: True)
    monkeypatch.setattr(
        admin.remote_db,
        "admin_console_summary_remote",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    response = asyncio.run(admin.admin_console_summary(_request()))

    assert response.status_code == 503
    assert _decode_response(response) == {
        "available": False,
        "reason": "remote_error",
        "error": "boom",
    }


def test_admin_console_health_boundaries_embedding_10_percent_crit_disk_80_warn():
    embedding = remote_db._admin_console_embedding_signal(total_calls=10, failed_calls=1)
    disk = remote_db._admin_console_disk_signal(used_percent=80.0, db_size="1.8 GB")

    assert embedding["level"] == "crit"
    assert "10%" in embedding["detail"]
    assert disk["level"] == "warn"
    assert "80%" in disk["detail"]
    assert "1.8 GB" in disk["detail"]


class _ConsoleConn:
    columns = {
        "users": {"id", "username", "created_at", "role"},
        "item_status": {"user_id", "item_id", "read_at", "clicked_at", "starred_at"},
        "cluster_status": {"user_id", "cluster_id", "clicked_at", "starred_at"},
        "fetch_runs": {"id", "started_at", "finished_at", "status", "stats_json", "error_msg"},
        "items": {"platform", "fetched_at"},
        "embedding_usage_logs": {"created_at", "status", "estimated_cost_yuan"},
    }

    def __init__(self, *, missing_embedding=False):
        self.missing_embedding = missing_embedding
        self.queries: list[str] = []

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        lower = text.lower()
        self.queries.append(text)
        params = params or {}

        if "from information_schema.columns" in lower:
            table = params["table_name"]
            columns = set(self.columns.get(table, set()))
            if self.missing_embedding and table == "embedding_usage_logs":
                columns = set()
            wanted = set(params.get("column_names") or [])
            return _Rows([{"column_name": col} for col in sorted(columns & wanted)])

        if "pg_size_pretty(pg_database_size(current_database()))" in lower:
            return _Rows([{"db_size": "1.8 GB"}])
        if "count(*) as total_users" in lower:
            return _Rows([{"total_users": 3, "new_users_today": 1, "new_users_7d": 2}])
        if "order by created_at desc" in lower and "from remote_poc.users" in lower:
            return _Rows([{"username": "latest", "created_at": datetime(2026, 7, 4, 4, 0, tzinfo=timezone.utc)}])
        if "as active_users" in lower:
            assert "item_status" in lower
            assert "cluster_status" in lower
            assert "read_at" in lower
            assert "clicked_at" in lower
            assert "starred_at" in lower
            assert "last_login_at" not in lower
            return _Rows([{"active_users": 2}])
        if "as info_click_users_7d" in lower:
            return _Rows([{"info_click_users_7d": 2, "info_click_items_7d": 4, "info_click_items_total": 5}])
        if "as highlight_click_users_7d" in lower:
            return _Rows([
                {
                    "highlight_click_users_7d": 1,
                    "highlight_click_events_7d": 2,
                    "highlight_click_events_total": 3,
                }
            ])
        if "as starred_users" in lower:
            return _Rows([{"starred_users": 2, "starred_total": 3, "read_users_7d": 2, "read_items_7d": 4}])
        if "as embedding_calls_24h" in lower:
            return _Rows([{"embedding_calls_24h": 10, "embedding_failed_24h": 1, "embedding_cost_yuan_24h": 0.25}])
        if "from remote_poc.fetch_runs" in lower and "order by id desc" in lower:
            return _Rows([
                {
                    "id": 42,
                    "status": "success",
                    "started_at": datetime(2026, 7, 5, 0, 0, tzinfo=timezone.utc),
                    "finished_at": datetime(2026, 7, 5, 1, 0, tzinfo=timezone.utc),
                    "error_msg": None,
                }
            ])
        if "as success_runs_24h" in lower:
            return _Rows([{"total_runs_24h": 1, "success_runs_24h": 1, "success_runs_48h": 1}])
        if "from remote_poc.items" in lower and "group by platform" in lower:
            return _Rows([{"platform": "xiaohongshu", "last_fetched_at": datetime(2026, 7, 5, 0, 0, tzinfo=timezone.utc)}])
        if "generate_series" in lower and "from remote_poc.users" in lower:
            return _Rows([{"date": "2026-07-05", "value": 1}])
        if "generate_series" in lower and "from remote_poc.fetch_runs" in lower:
            return _Rows([{"date": "2026-07-05", "value": 1.0}])
        raise AssertionError(f"unexpected SQL: {text}")


def test_admin_console_summary_active_users_use_interaction_dedup(monkeypatch):
    conn = _ConsoleConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "status", lambda: {"backend": "supabase", "schema": "remote_poc"})
    monkeypatch.setattr(remote_db.shutil, "disk_usage", lambda _path: SimpleNamespace(total=100, used=62))

    payload = remote_db.admin_console_summary_remote(
        now=datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)
    )

    assert payload["available"] is True
    assert payload["c_metrics"]["active_users_1d"] == 2
    assert payload["c_metrics"]["active_users_7d"] == 2
    assert payload["c_metrics"]["total_users"] == 3
    assert payload["interactions_detail"]["latest_signup"] == {
        "username": "latest",
        "created_at": "2026-07-04T12:00:00+08:00",
    }


def test_admin_console_summary_missing_embedding_schema_returns_null(monkeypatch):
    conn = _ConsoleConn(missing_embedding=True)

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "status", lambda: {"backend": "supabase", "schema": "remote_poc"})
    monkeypatch.setattr(remote_db.shutil, "disk_usage", lambda _path: SimpleNamespace(total=100, used=62))

    payload = remote_db.admin_console_summary_remote(
        now=datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)
    )

    assert payload["cost"] == {
        "embedding_cost_yuan_24h": None,
        "embedding_calls_24h": None,
    }
    embedding = next(signal for signal in payload["health"]["signals"] if signal["key"] == "embedding")
    assert embedding["level"] == "unknown"
