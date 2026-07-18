"""P0-1 (C端放量) — worker leader lock + capacity env knobs.

Scope:
- _acquire_web_leader_lock: first acquire wins, second process(fd) loses,
  released handle frees the lock; unwritable lock dir degrades to leader
- _threadpool_tokens: parse/default/garbage
- __main__ worker count parsing mirrors INFO2ACTION_WEB_WORKERS semantics
"""
from __future__ import annotations

import fcntl
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

# 项目里有两个 slowapi Limiter 实例(app.py 与 routes/auth.py:24),它们只在
# import 时读 RATELIMIT_ENABLED。本文件在收集期导入 app,必须先关限流,
# 否则同一 pytest 进程里后跑的 test_auth 会被 5/min 打成 429。
os.environ.setdefault('RATELIMIT_ENABLED', 'false')

import app as app_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_lock_state(tmp_path, monkeypatch):
    monkeypatch.setenv('INFO2ACTION_WEB_LEADER_LOCK', str(tmp_path / 'leader.lock'))
    if app_mod._web_leader_lock_handle is not None:
        try:
            app_mod._web_leader_lock_handle.close()
        except Exception:
            pass
        app_mod._web_leader_lock_handle = None
    yield
    if app_mod._web_leader_lock_handle is not None:
        try:
            app_mod._web_leader_lock_handle.close()
        except Exception:
            pass
        app_mod._web_leader_lock_handle = None


def test_first_acquire_wins(tmp_path):
    assert app_mod._acquire_web_leader_lock() is True
    # Idempotent within the same process.
    assert app_mod._acquire_web_leader_lock() is True


def test_second_holder_becomes_follower(tmp_path):
    """Simulate another worker holding the lock via an independent fd."""
    lock_path = os.environ['INFO2ACTION_WEB_LEADER_LOCK']
    other = open(lock_path, 'w')
    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert app_mod._acquire_web_leader_lock() is False
    finally:
        other.close()


def test_lock_freed_after_holder_exits(tmp_path):
    lock_path = os.environ['INFO2ACTION_WEB_LEADER_LOCK']
    other = open(lock_path, 'w')
    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    other.close()  # holder "died"
    assert app_mod._acquire_web_leader_lock() is True


def test_unwritable_lock_dir_degrades_to_leader(monkeypatch, tmp_path):
    monkeypatch.setenv(
        'INFO2ACTION_WEB_LEADER_LOCK',
        str(tmp_path / 'no-perm' / 'deep' / 'leader.lock'),
    )
    blocked = tmp_path / 'no-perm'
    blocked.mkdir()
    blocked.chmod(0o444)
    try:
        assert app_mod._acquire_web_leader_lock() is True
        assert app_mod._web_leader_lock_handle is None  # degraded, not holding
    finally:
        blocked.chmod(0o755)


def test_threadpool_tokens_parse(monkeypatch):
    monkeypatch.delenv('INFO2ACTION_THREADPOOL_TOKENS', raising=False)
    assert app_mod._threadpool_tokens() == 0
    monkeypatch.setenv('INFO2ACTION_THREADPOOL_TOKENS', '64')
    assert app_mod._threadpool_tokens() == 64
    monkeypatch.setenv('INFO2ACTION_THREADPOOL_TOKENS', 'garbage')
    assert app_mod._threadpool_tokens() == 0


def test_limiter_default_limits(monkeypatch):
    monkeypatch.delenv('RATELIMIT_DEFAULT', raising=False)
    # 稳定性加固(2026-07-10): 注册 SlowAPIMiddleware 后 default_limits 才真正生效;
    # 默认从 100/min 放宽到 600/min 作为"防洪水"底线,避免运营商级 NAT 误伤真实用户。
    assert app_mod._limiter_default_limits() == ['600/minute']
    monkeypatch.setenv('RATELIMIT_DEFAULT', '200/minute')
    assert app_mod._limiter_default_limits() == ['200/minute']


def test_limiter_storage_env_reaches_both_instances(monkeypatch):
    """RATELIMIT_STORAGE_URL 必须能被新建 Limiter 实例读到(slowapi 原生
    env 机制)——这是双实例(app.py / routes/auth.py)共享 Redis 的前提。"""
    from slowapi import Limiter
    from slowapi.util import get_remote_address
    monkeypatch.setenv('RATELIMIT_STORAGE_URL', 'redis://127.0.0.1:59999')
    lim = Limiter(key_func=get_remote_address, in_memory_fallback_enabled=True)
    assert 'redis' in type(lim._storage).__module__.lower()  # env 生效,非默认 memory://
