"""BF-0515-13 F2 验证: finish_login_remote 合并为单 CTE round-trip.

不连真 DB,用 monkeypatch 拦截 connect() 检查 SQL 结构与参数,
确保 CTE 包含 modifying CTEs (UPDATE + INSERT) 且只发 1 个 statement
+ 1 个 commit (即 2 个 round-trip 而非原来 4 个).
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

import remote_db


class FakeCursor:
    def __init__(self, profile_row=None):
        self._profile = profile_row

    def fetchone(self):
        return self._profile


class FakeConn:
    def __init__(self, profile_row=None):
        self.executed = []  # [(sql, params)]
        self.commit_count = 0
        self._profile = profile_row

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return FakeCursor(self._profile)

    def commit(self):
        self.commit_count += 1


@pytest.fixture
def fake_connect(monkeypatch):
    """Yield (FakeConn, install_fn) — install_fn(conn) replaces connect() to yield it."""
    holder = {}

    def install(conn):
        holder["conn"] = conn

        @contextmanager
        def _connect():
            yield conn

        monkeypatch.setattr(remote_db, "connect", _connect)

    return install


def test_finish_login_remote_single_round_trip_with_profile(fake_connect, monkeypatch):
    """profile 存在 → 1 statement + 1 commit (2 RTT total)"""
    fake = FakeConn(profile_row={
        "user_id": "u-1",
        "interests": "[]",
        "tools": "[]",
        "manifest": "{}",
    })
    fake_connect(fake)
    monkeypatch.setattr(remote_db, "clear_user_cache_keys", lambda *a, **k: None)

    result = remote_db.finish_login_remote(
        "u-1",
        access_jti="acc-jti",
        access_expires_at="2030-01-01T00:00:00",
        refresh_jti="ref-jti",
        refresh_expires_at="2031-01-01T00:00:00",
        last_login_at="2026-05-15T19:00:00",
    )

    # 1 个 statement (CTE bundle) — 旧版本是 4 个: UPDATE + executemany + SELECT + commit
    assert len(fake.executed) == 1, f"Expected single round-trip, got {len(fake.executed)} executes"

    sql, params = fake.executed[0]
    sql_lower = sql.lower()

    # 必须包含 3 个 modifying / read ops
    assert "update" in sql_lower and "users" in sql_lower
    assert "insert into" in sql_lower and "sessions" in sql_lower
    assert "select" in sql_lower and "user_profiles" in sql_lower

    # 必须是单一 statement (CTE 形式)
    assert "with " in sql_lower, "Expected WITH-CTE form"

    # ON CONFLICT 仍然存在(idempotent session insert)
    assert "on conflict" in sql_lower

    # 参数都进了 dict
    assert params["user_id"] == "u-1"
    assert params["access_jti"] == "acc-jti"
    assert params["refresh_jti"] == "ref-jti"

    # 1 个 commit(原版本也是 1 个,不变)
    assert fake.commit_count == 1

    # JSON fields 已 _json_value 处理
    assert isinstance(result, dict)
    assert result.get("user_id") == "u-1"


def test_finish_login_remote_returns_none_when_profile_missing(fake_connect, monkeypatch):
    """profile 不存在(legacy user) → 返回 None,不 crash"""
    fake = FakeConn(profile_row=None)
    fake_connect(fake)
    monkeypatch.setattr(remote_db, "clear_user_cache_keys", lambda *a, **k: None)

    result = remote_db.finish_login_remote(
        "u-2",
        access_jti="a",
        access_expires_at="2030-01-01T00:00:00",
        refresh_jti="r",
        refresh_expires_at="2031-01-01T00:00:00",
        last_login_at="2026-05-15T19:00:00",
    )

    # 还是只有 1 个 RTT(modifying CTEs 仍然 fired,即使 outer SELECT 返回空)
    assert len(fake.executed) == 1
    assert fake.commit_count == 1
    assert result is None


def test_finish_login_remote_clears_user_cache(fake_connect, monkeypatch):
    """成功后必须 clear_user_cache_keys(user_id),否则下个 GET /me 仍读旧 profile"""
    fake = FakeConn(profile_row=None)
    fake_connect(fake)
    cleared = []
    monkeypatch.setattr(remote_db, "clear_user_cache_keys", lambda uid: cleared.append(uid))

    remote_db.finish_login_remote(
        "u-3",
        access_jti="a",
        access_expires_at="2030-01-01T00:00:00",
        refresh_jti="r",
        refresh_expires_at="2031-01-01T00:00:00",
        last_login_at="2026-05-15T19:00:00",
    )

    assert cleared == ["u-3"]
