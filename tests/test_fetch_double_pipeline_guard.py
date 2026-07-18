"""BF-0710-fetch-guards 回归测试:抓取双 pipeline 守卫加固。

覆盖:
- runtime-stale grace 默认/下限/env
- has_recent_running_fetch_remote: 默认不再有 180min 硬龄窗口(仅心跳判活),
  用 runtime grace;显式 env/参数时才加龄窗口
- recover_stale_remote_fetch_runs: 运行时孤儿回收用 runtime grace(比基础 grace 宽)
"""
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import remote_db
import routes.fetch as fetch_route


# ── runtime-stale grace ──────────────────────────────────────
def test_runtime_stale_grace_default_is_generous(monkeypatch):
    monkeypatch.delenv(remote_db.FETCH_RUN_RUNTIME_STALE_GRACE_SEC_ENV, raising=False)
    monkeypatch.delenv(remote_db.FETCH_RUN_HEARTBEAT_GRACE_SEC_ENV, raising=False)
    assert remote_db.fetch_run_runtime_stale_grace_seconds() == 1800


def test_runtime_stale_grace_floored_at_base(monkeypatch):
    # runtime 窗口不得比基础 grace 更激进(更小)
    monkeypatch.setenv(remote_db.FETCH_RUN_HEARTBEAT_GRACE_SEC_ENV, '1200')
    monkeypatch.setenv(remote_db.FETCH_RUN_RUNTIME_STALE_GRACE_SEC_ENV, '300')
    base = remote_db.fetch_run_heartbeat_grace_seconds()
    assert remote_db.fetch_run_runtime_stale_grace_seconds() >= base
    assert remote_db.fetch_run_runtime_stale_grace_seconds() == 1200


def test_runtime_stale_grace_respects_env(monkeypatch):
    monkeypatch.setenv(remote_db.FETCH_RUN_RUNTIME_STALE_GRACE_SEC_ENV, '2400')
    assert remote_db.fetch_run_runtime_stale_grace_seconds() == 2400


# ── has_recent_running_fetch_remote 查询构建 ─────────────────
class _FakeConn:
    def __init__(self, has_running=True):
        self.captured = None
        self._has_running = has_running

    def execute(self, sql, params=None):
        self.captured = (sql, params)
        return self

    def fetchone(self):
        return {"has_running": self._has_running}


def _patch_connect(monkeypatch, fake_conn):
    @contextmanager
    def _fake_connect():
        yield fake_conn
    monkeypatch.setattr(remote_db, 'connect', _fake_connect)
    monkeypatch.setattr(remote_db, '_set_short_statement_timeout', lambda conn, *a, **k: None)


def test_running_guard_default_has_no_age_window(monkeypatch):
    monkeypatch.delenv(remote_db.REMOTE_RUNNING_FETCH_MAX_AGE_MIN_ENV, raising=False)
    fake = _FakeConn(has_running=True)
    _patch_connect(monkeypatch, fake)
    assert remote_db.has_recent_running_fetch_remote() is True
    sql, params = fake.captured
    # 默认不再拼 started_at 龄窗口——长 run 不会被误判成"无 run"
    assert 'started_at >= now()' not in sql
    # 心跳窗口用 runtime grace
    assert remote_db.fetch_run_runtime_stale_grace_seconds() in params


def test_running_guard_opt_in_age_window_via_env(monkeypatch):
    monkeypatch.setenv(remote_db.REMOTE_RUNNING_FETCH_MAX_AGE_MIN_ENV, '360')
    fake = _FakeConn(has_running=False)
    _patch_connect(monkeypatch, fake)
    remote_db.has_recent_running_fetch_remote()
    sql, params = fake.captured
    assert 'started_at >= now()' in sql
    assert 360 in params


def test_running_guard_explicit_arg_age_window(monkeypatch):
    fake = _FakeConn(has_running=False)
    _patch_connect(monkeypatch, fake)
    remote_db.has_recent_running_fetch_remote(max_age_minutes=240)
    sql, params = fake.captured
    assert 'started_at >= now()' in sql
    assert 240 in params


# ── recover_stale_remote_fetch_runs 用 runtime grace ─────────
def test_runtime_recovery_uses_runtime_stale_grace(monkeypatch):
    monkeypatch.setenv('INFO2ACTION_BACKEND_HOURLY_FETCH', '1')
    monkeypatch.setenv(remote_db.FETCH_RUN_HEARTBEAT_GRACE_SEC_ENV, '600')
    monkeypatch.setenv(remote_db.FETCH_RUN_RUNTIME_STALE_GRACE_SEC_ENV, '1800')
    monkeypatch.setattr(fetch_route.remote_db, 'fetch_write_to_remote', lambda: True)

    captured = {}

    def fake_mark(*, started_before, heartbeat_stale_before, reason):
        captured['started_before'] = started_before
        captured['heartbeat_stale_before'] = heartbeat_stale_before
        return [999]

    monkeypatch.setattr(fetch_route.remote_db, 'mark_orphaned_fetch_runs_remote', fake_mark)

    assert fetch_route.recover_stale_remote_fetch_runs() == [999]
    # stale 窗口应约等于 runtime grace(1800s),而不是基础 grace(600s)
    delta = (captured['started_before'] - captured['heartbeat_stale_before']).total_seconds()
    assert 1700 <= delta <= 1900
    assert captured['heartbeat_stale_before'].tzinfo is not None


# ── #4 quick-fetch 对全局/调度守卫可见 ───────────────────────
def test_local_guard_sees_quick_fetch_running_flag(monkeypatch):
    # quick-fetch 只置 _fetch_running=True、不进 _fetch_active_runs;
    # 本地守卫必须也把它算作"有 run 在跑",否则全局/调度会并发启动第二条 pipeline。
    fetch_route._fetch_active_runs.clear()
    monkeypatch.setattr(fetch_route, '_fetch_running', True)
    assert fetch_route.has_local_active_fetch_runs() is True


def test_local_guard_false_when_idle(monkeypatch):
    fetch_route._fetch_active_runs.clear()
    monkeypatch.setattr(fetch_route, '_fetch_running', False)
    assert fetch_route.has_local_active_fetch_runs() is False
