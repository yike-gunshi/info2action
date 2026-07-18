"""BF-0708-3: /api/feed/events 慢查询挂死 → Cloudflare 524 → 精选页永久骨架屏。

生产实测:fetch_events 耗时 129.2s 且返回 0 条,CF 100s 断开。根因是 Supabase
Micro(1GB) 撑不住 7GB 库、读模型停更导致落到慢路径。

本修复(候选 a)只做"防白屏":
- 给 feed/events 的查询设 statement_timeout(默认 30s,远小于 CF 100s)
- 超时抛 RemoteDBTimeoutError(区别于其他 RemoteDBError)
- 路由降级:优先返回上次成功的陈旧快照,否则空态;HTTP 200 + degraded 标记
- 语义边界:非超时的 RemoteDBError 仍返回 503,不得被降级掩盖
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import remote_db  # noqa: E402


# ── 第 1 层:超时异常类型可区分 ──

def test_remote_db_timeout_error_exists_and_subclasses_remote_db_error():
    """RED: 没有专门的超时异常类,路由无法区分'超时可降级'与'真故障要报错'。"""
    assert hasattr(remote_db, 'RemoteDBTimeoutError'), (
        '需要 RemoteDBTimeoutError 以区分查询超时与其他远程 DB 故障'
    )
    assert issubclass(remote_db.RemoteDBTimeoutError, remote_db.RemoteDBError)


@pytest.fixture
def _fake_psycopg(monkeypatch):
    """本地未安装 psycopg(生产才有),注入假模块以验证异常分类逻辑。

    这也解释了为何这类问题从未被本地测试暴露:生产走 psycopg,本地根本 import 不到。
    """
    import types
    mod = types.ModuleType('psycopg')
    mod.errors = types.SimpleNamespace(
        QueryCanceled=type('QueryCanceled', (Exception,), {}),
    )
    monkeypatch.setitem(sys.modules, 'psycopg', mod)
    return mod


def test_query_canceled_is_wrapped_as_timeout_error(_fake_psycopg):
    """RED: connect() 目前把 QueryCanceled 一律包成通用 RemoteDBError,调用方无法区分。"""
    exc = _fake_psycopg.errors.QueryCanceled('canceling statement due to statement timeout')
    wrapped = remote_db._wrap_remote_db_exception(exc)
    assert isinstance(wrapped, remote_db.RemoteDBTimeoutError), (
        'statement_timeout 取消必须归类为 RemoteDBTimeoutError,以便调用方降级'
    )


def test_other_driver_errors_stay_generic_remote_db_error(_fake_psycopg):
    """安全边界:非超时故障(如连接断开)不得被误判为可降级的超时。"""
    wrapped = remote_db._wrap_remote_db_exception(
        RuntimeError('(EDBHANDLEREXITED) connection to database closed')
    )
    assert isinstance(wrapped, remote_db.RemoteDBError)
    assert not isinstance(wrapped, remote_db.RemoteDBTimeoutError), (
        '真实的连接故障必须保持为普通 RemoteDBError,否则会被降级掩盖'
    )


# ── 第 2 层:feed/events 的查询必须带 statement_timeout ──

def test_feed_events_timeout_env_default_is_30s():
    """RED: 没有 feed/events 专用超时配置。默认 30s(> 冷查询 21.4s,<< CF 100s)。"""
    assert hasattr(remote_db, 'FEED_EVENTS_TIMEOUT_MS_ENV')
    ms = remote_db._feed_events_timeout_ms({})
    assert ms == 30000, f'默认应为 30000ms,实际 {ms}'


def test_feed_events_timeout_env_override():
    ms = remote_db._feed_events_timeout_ms({remote_db.FEED_EVENTS_TIMEOUT_MS_ENV: '8000'})
    assert ms == 8000


def test_feed_events_timeout_stays_below_cloudflare_limit():
    """边界:阈值必须显著小于 Cloudflare 100s,否则仍会 524。"""
    ms = remote_db._feed_events_timeout_ms({remote_db.FEED_EVENTS_TIMEOUT_MS_ENV: '999999'})
    assert ms <= 90000, 'feed/events 超时上限必须 <= 90s,否则 Cloudflare 先断开(524)'


def test_every_connect_in_fetch_events_content_sets_timeout():
    """静态断言:_fetch_events_content 内每个 connect() 都必须设 statement_timeout。

    本用例源于一次真实失误:超时最初只加在 `prefer_highlights_read_model` 分支的
    connect() 上。而读模型一旦停更,该标志翻为 False,请求落到 fallback 的实时聚合
    路径 —— 正是那条 129s 的慢查询 —— 它当时完全没有超时保护。生产实测才发现
    (statement_timeout=1ms 下查询仍跑完 2.24s)。

    本地无 psycopg,无法端到端执行这些分支,因此改用源码级断言防止回归:
    将来任何人在 _fetch_events_content 里新增 connect() 分支,忘记设超时就会红。
    """
    import inspect
    import re

    src = inspect.getsource(remote_db._fetch_events_content)
    lines = src.splitlines()

    connect_lines = [i for i, ln in enumerate(lines) if re.search(r'with connect\(\)', ln)]
    assert connect_lines, '未在 _fetch_events_content 中找到 connect(),测试假设已失效'

    for i in connect_lines:
        window = '\n'.join(lines[i:i + 12])
        assert '_set_short_statement_timeout' in window, (
            f'_fetch_events_content 第 {i+1} 行附近的 connect() 未设置 statement_timeout。\n'
            f'feed/events 的每条 DB 读取都必须有界,否则慢路径会跑到 Cloudflare 524。\n'
            f'上下文:\n' + window
        )


# ── 第 3 层:路由降级语义 ──

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


@pytest.fixture(autouse=True)
def _remote_mode(monkeypatch):
    monkeypatch.setattr(remote_db, 'events_read_from_remote', lambda: True)
    import routes.clusters as cl
    from routes.public_response_cache import clear_public_response_cache
    monkeypatch.setattr(cl.remote_db, 'events_read_from_remote', lambda: True)

    # public_response_cache 是模块级字典,跨用例残留会让后续请求命中旧 body。
    # 修复前 _reset_last_good_events_for_test 尚不存在:用 getattr 容错,
    # 让用例因断言失败而 RED,而非 setup ERROR。
    reset = getattr(cl, '_reset_last_good_events_for_test', lambda: None)
    clear_public_response_cache()
    reset()
    yield
    clear_public_response_cache()
    reset()


def _payload(n=2):
    return {'events': [{'id': i, 'title': f'e{i}'} for i in range(n)], 'total': n}


def test_slow_request_is_bounded_by_wall_clock_not_statement_timeout(_client, monkeypatch):
    """核心:请求总时长必须有界,即使每条 SQL 都很快。

    这是本 bug 的真实形态。生产已有 4.5s 的单语句 statement_timeout
    (_set_events_read_model_timeouts),而 fetch_events 仍跑了 129s ——
    因为那是几十条各自 <4.5s 的查询累加。statement_timeout 管不住请求总时长,
    只有请求级超时(asyncio.wait_for)可以。

    这里让 fetch_events 睡 5s(远超测试用的 0.3s 阈值)且不抛任何 DB 异常,
    模拟"每条 SQL 都不慢、但总时长失控"的情形。
    """
    import time as _time
    import routes.clusters as cl

    monkeypatch.setattr(cl, '_feed_events_request_timeout_sec', lambda: 0.3)

    def _slow(**kw):
        _time.sleep(5)
        return _payload(2)

    monkeypatch.setattr(cl.remote_db, 'fetch_events', _slow)

    t0 = _time.time()
    res = _client.get('/api/feed/events?limit=20')
    elapsed = _time.time() - t0

    assert elapsed < 3.0, (
        f'请求耗时 {elapsed:.1f}s,未被请求级超时截断。'
        'statement_timeout 只管单条 SQL,防不住 Cloudflare 524'
    )
    assert res.status_code == 200
    assert res.json().get('degraded') is True


def test_query_timeout_returns_200_degraded_not_503(_client, monkeypatch):
    """RED: 超时目前落到 _remote_error_response → 503(且生产上先被 CF 524 掐断)。"""
    import routes.clusters as cl

    def _boom(**kw):
        raise remote_db.RemoteDBTimeoutError('canceling statement due to statement timeout')

    monkeypatch.setattr(cl.remote_db, 'fetch_events', _boom)

    res = _client.get('/api/feed/events?limit=20')
    assert res.status_code == 200, f'查询超时应降级为 200,实际 {res.status_code}'
    body = res.json()
    assert body.get('degraded') is True, '降级响应必须带 degraded 标记'
    assert body.get('events') == [], '无 last-good 快照时应返回空事件列表'


def test_timeout_serves_last_good_snapshot(_client, monkeypatch):
    """成功过一次后再超时 → 返回上次的陈旧数据,而不是空态。"""
    import routes.clusters as cl

    monkeypatch.setattr(cl.remote_db, 'fetch_events', lambda **kw: _payload(2))
    ok = _client.get('/api/feed/events?limit=20')
    assert ok.status_code == 200
    assert len(ok.json()['events']) == 2
    assert ok.json().get('degraded') is not True, '正常响应不应带 degraded'

    def _boom(**kw):
        raise remote_db.RemoteDBTimeoutError('canceling statement due to statement timeout')

    monkeypatch.setattr(cl.remote_db, 'fetch_events', _boom)
    # 清掉 public response cache,强制下一次请求真正走 fetch_events(从而触发超时)
    from routes.public_response_cache import clear_public_response_cache
    clear_public_response_cache()

    stale = _client.get('/api/feed/events?limit=20')
    assert stale.status_code == 200
    body = stale.json()
    assert len(body['events']) == 2, '应回放上次成功的快照'
    assert body.get('degraded') is True
    assert body.get('stale') is True, '陈旧数据必须标记 stale,前端/调用方可感知'


def test_non_timeout_remote_error_still_returns_503(_client, monkeypatch):
    """安全边界:真实的 DB 故障不得被降级掩盖成 200。"""
    import routes.clusters as cl

    def _boom(**kw):
        raise remote_db.RemoteDBError('connection to database closed (EDBHANDLEREXITED)')

    monkeypatch.setattr(cl.remote_db, 'fetch_events', _boom)

    res = _client.get('/api/feed/events?limit=20')
    assert res.status_code == 503, (
        f'非超时的 RemoteDBError 必须仍是 503,实际 {res.status_code};'
        '否则真实故障会被伪装成"暂无数据"'
    )


def test_successful_response_has_no_degraded_flag(_client, monkeypatch):
    import routes.clusters as cl
    monkeypatch.setattr(cl.remote_db, 'fetch_events', lambda **kw: _payload(3))
    res = _client.get('/api/feed/events?limit=20')
    assert res.status_code == 200
    body = res.json()
    assert len(body['events']) == 3
    assert 'degraded' not in body or body['degraded'] is False
    assert 'stale' not in body or body['stale'] is False
