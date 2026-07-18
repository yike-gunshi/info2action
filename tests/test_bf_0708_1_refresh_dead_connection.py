"""BF-0708-1: Supabase transaction pooler 回收空闲连接 → refresh 500 → 用户被踢登录页。

覆盖两层后端修复:
- 连接池必须配 pre-ping(check) + max_idle,死连接不得被借出(根治)
- /api/auth/refresh 遇 RemoteDBError 必须返回 503 且不清 cookie(纵深防御)
  —— DB 瞬断不是"登录失效",不能让用户登出
"""
import os
import sys
import uuid
from datetime import datetime, timezone, timedelta

import jwt
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import remote_db  # noqa: E402


# ── 第 1 层:连接池 pre-ping + 空闲过期 ──

class _FakePool:
    """捕获 ConnectionPool 的构造参数,不建真连接。

    check_connection 对齐真实 psycopg_pool.ConnectionPool 的 staticmethod
    (生产实测 psycopg_pool 3.3.1 具备该属性)。
    """
    last_kwargs: dict = {}

    def __init__(self, **kwargs):
        _FakePool.last_kwargs = kwargs

    @staticmethod
    def check_connection(conn):
        pass

    def close(self):
        pass


@pytest.fixture
def _pool_env(monkeypatch):
    """注入假的 psycopg_pool。

    本地开发环境未安装 psycopg_pool,`_get_pool` 会静默 `return None` 走无池路径——
    这正是本 bug 长期未被测试覆盖的原因(生产走池,测试走裸连接)。
    """
    import types
    fake_mod = types.ModuleType('psycopg_pool')
    fake_mod.ConnectionPool = _FakePool
    monkeypatch.setitem(sys.modules, 'psycopg_pool', fake_mod)

    monkeypatch.setattr(
        remote_db, 'database_url',
        lambda: 'postgresql://u:p@aws-1-ap-northeast-1.pooler.supabase.com:6543/postgres',
    )
    monkeypatch.setattr(remote_db, '_POOL', None, raising=False)
    monkeypatch.setattr(remote_db, '_POOL_DSN', None, raising=False)
    monkeypatch.delenv('REMOTE_DB_POOL_DISABLED', raising=False)
    _FakePool.last_kwargs = {}
    yield
    remote_db._POOL = None
    remote_db._POOL_DSN = None


def _build_pool(monkeypatch):
    """_get_pool 只把 dict_row 塞进 kwargs,psycopg 模块本身不被解引用 → 传 sentinel 即可。"""
    dict_row = object()
    pool = remote_db._get_pool(object(), dict_row)
    assert pool is not None, '_get_pool 返回 None,池路径未被走到(检查 fake psycopg_pool 注入)'
    return pool


def test_pool_has_pre_ping_check(_pool_env, monkeypatch):
    """RED: 池没配 check= → 借出的连接可能已被 pooler 关闭(EDBHANDLEREXITED)。"""
    _build_pool(monkeypatch)
    kwargs = _FakePool.last_kwargs

    assert kwargs.get('check') is not None, (
        'ConnectionPool 必须配置 check=(借出前 pre-ping),否则 Supabase transaction '
        'pooler 回收空闲连接后,池仍会把死连接借给调用方 → EDBHANDLEREXITED'
    )


def test_pool_has_max_idle(_pool_env, monkeypatch):
    """RED: 池没配 max_idle → 空闲连接无限期滞留(实测最老 idle 达 2 天)。"""
    _build_pool(monkeypatch)
    kwargs = _FakePool.last_kwargs

    max_idle = kwargs.get('max_idle')
    assert max_idle is not None, 'ConnectionPool 必须配置 max_idle,让空闲连接主动过期'
    assert 0 < max_idle <= 600, (
        f'max_idle={max_idle} 必须短于 Supabase pooler 的空闲回收窗口(取 <=10min)'
    )


def test_pool_keeps_prepare_threshold_none(_pool_env, monkeypatch):
    """回归 BF-0515-1: transaction pooler 下必须禁用 prepared statement cache。"""
    _build_pool(monkeypatch)
    assert _FakePool.last_kwargs.get('kwargs', {}).get('prepare_threshold') is None


# ── 第 2 层:refresh 端点语义 —— DB 瞬断 ≠ 登录失效 ──

@pytest.fixture
def _client(monkeypatch):
    monkeypatch.setenv('RATELIMIT_ENABLED', 'false')
    from fastapi.testclient import TestClient
    from app import app
    try:
        app.state.limiter._default_limits = []
        app.state.limiter.enabled = False
    except Exception:
        pass
    return TestClient(app)


def _refresh_cookie(user_id='u-1', jti=None):
    from routes.auth import JWT_SECRET, JWT_ALGORITHM
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            'sub': user_id,
            'type': 'refresh',
            'jti': jti or str(uuid.uuid4()),
            'exp': now + timedelta(days=7),
            'iat': now,
        },
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )


def test_refresh_db_outage_returns_503_not_500(_client, monkeypatch):
    """RED: DB 瞬断时 RemoteDBError 冒泡 → 500。应为 503(服务暂时不可用)。"""
    import routes.auth as auth_mod

    monkeypatch.setattr(auth_mod.remote_db, 'app_state_to_remote', lambda: True)

    def _boom(**kwargs):
        raise remote_db.RemoteDBError(
            'Remote DB connection/query failed: (EDBHANDLEREXITED) connection to database closed.'
        )

    monkeypatch.setattr(auth_mod.remote_db, 'refresh_access_session_remote', _boom)

    res = _client.post('/api/auth/refresh', cookies={'refresh_token': _refresh_cookie()})

    assert res.status_code == 503, (
        f'DB 瞬断应返回 503 而非 {res.status_code};'
        '500 会被前端 tryRefresh 判为登录失效并强制登出'
    )
    assert res.status_code != 401, 'DB 故障绝不能表达为"登录已失效"'


def test_refresh_db_outage_does_not_clear_cookies(_client, monkeypatch):
    """RED: DB 瞬断不得清 refresh_token cookie —— 用户的登录态本身是有效的。"""
    import routes.auth as auth_mod

    monkeypatch.setattr(auth_mod.remote_db, 'app_state_to_remote', lambda: True)

    def _boom(**kwargs):
        raise remote_db.RemoteDBError('(EDBHANDLEREXITED) connection to database closed.')

    monkeypatch.setattr(auth_mod.remote_db, 'refresh_access_session_remote', _boom)

    res = _client.post('/api/auth/refresh', cookies={'refresh_token': _refresh_cookie()})

    set_cookie = res.headers.get('set-cookie', '')
    assert 'refresh_token=;' not in set_cookie and 'Max-Age=0' not in set_cookie, (
        f'DB 故障时不得删除 auth cookie,实际 set-cookie={set_cookie!r}'
    )


def test_refresh_invalid_session_still_returns_401(_client, monkeypatch):
    """边界:会话真的失效(查库成功但无记录)时,仍必须 401 + 清 cookie。不能被 503 掩盖。"""
    import routes.auth as auth_mod

    monkeypatch.setattr(auth_mod.remote_db, 'app_state_to_remote', lambda: True)
    monkeypatch.setattr(auth_mod.remote_db, 'refresh_access_session_remote', lambda **kw: None)

    res = _client.post('/api/auth/refresh', cookies={'refresh_token': _refresh_cookie()})

    assert res.status_code == 401, '真实的会话失效必须仍然是 401'
    assert 'Max-Age=0' in res.headers.get('set-cookie', '') or 'refresh_token=;' in res.headers.get('set-cookie', '')
