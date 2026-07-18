"""稳定性加固回归测试(2026-07-10 C 端放量审计批次)。

覆盖本批次新增的纯逻辑护栏,不依赖真实 DB / 网络:
- fetch_url._read_capped: 响应体字节上限
- asr._evict_oldest_if_full + 两个 getter: app.state dict 有上限 LRU
- user._profile_field_error: profile 字段长度上限
- body_limit.MaxBodySizeMiddleware: 超大 body 413
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


# ── fetch_url 字节上限 ────────────────────────────────────────
class _FakeResp:
    def __init__(self, body: bytes, content_length: int | None = None):
        self._body = body
        self.headers = {}
        if content_length is not None:
            self.headers['Content-Length'] = str(content_length)

    def read(self, n=-1):
        if n is None or n < 0:
            return self._body
        return self._body[:n]


def test_read_capped_rejects_oversized_declared_length():
    import fetch_url
    resp = _FakeResp(b'x' * 10, content_length=fetch_url._MAX_FETCH_BYTES + 1)
    with pytest.raises(ValueError):
        fetch_url._read_capped(resp)


def test_read_capped_rejects_body_over_cap():
    import fetch_url
    resp = _FakeResp(b'y' * 100)
    with pytest.raises(ValueError):
        fetch_url._read_capped(resp, max_bytes=10)


def test_read_capped_allows_body_within_cap():
    import fetch_url
    resp = _FakeResp(b'ok')
    assert fetch_url._read_capped(resp, max_bytes=10) == b'ok'


# ── ASR app.state dict 有上限 LRU ────────────────────────────
class _FakeAppState:
    def __init__(self):
        self.user_asr_sems = {}
        self.asr_event_buses = {}


class _FakeApp:
    def __init__(self):
        self.state = _FakeAppState()


class _FakeRequest:
    def __init__(self, app):
        self.app = app


def test_event_bus_dict_is_bounded(monkeypatch):
    import routes.asr as asr
    monkeypatch.setattr(asr, '_EVENT_BUS_CAP', 4)
    app = _FakeApp()
    req = _FakeRequest(app)
    for i in range(20):
        asr._get_or_create_event_bus(req, f'item-{i}')
    assert len(app.state.asr_event_buses) <= 4


def test_user_sem_dict_is_bounded(monkeypatch):
    import routes.asr as asr
    monkeypatch.setattr(asr, '_USER_SEM_CAP', 3)
    app = _FakeApp()
    req = _FakeRequest(app)
    for uid in range(20):
        asr._get_or_create_user_sem(req, uid)
    assert len(app.state.user_asr_sems) <= 3


def test_event_bus_reuses_existing_and_can_be_discarded():
    import routes.asr as asr
    app = _FakeApp()
    req = _FakeRequest(app)
    a = asr._get_or_create_event_bus(req, 'same')
    b = asr._get_or_create_event_bus(req, 'same')
    assert a is b
    asr._discard_event_bus(req, 'same')
    assert 'same' not in app.state.asr_event_buses


# ── profile 字段长度上限 ─────────────────────────────────────
def test_profile_manifest_too_long_rejected():
    import routes.user as user
    err = user._profile_field_error({'manifest': 'x' * (user._MANIFEST_MAX_CHARS + 1)})
    assert err is not None


def test_profile_interests_too_many_items_rejected():
    import routes.user as user
    err = user._profile_field_error({'interests': ['a'] * (user._PROFILE_LIST_MAX_ITEMS + 1)})
    assert err is not None


def test_profile_reasonable_fields_ok():
    import routes.user as user
    assert user._profile_field_error({'manifest': 'hi', 'role': 'dev', 'interests': ['ai']}) is None


# ── body-size 中间件 ─────────────────────────────────────────
def test_body_limit_middleware_rejects_large_body():
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from middleware.body_limit import MaxBodySizeMiddleware

    async def echo(request):
        return PlainTextResponse('ok')

    app = Starlette(routes=[Route('/echo', echo, methods=['POST'])])
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=100)
    client = TestClient(app)

    small = client.post('/echo', content=b'x' * 50)
    assert small.status_code == 200

    big = client.post('/echo', content=b'x' * 500)
    assert big.status_code == 413


def test_body_limit_middleware_skips_multipart():
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from middleware.body_limit import MaxBodySizeMiddleware

    async def echo(request):
        return PlainTextResponse('ok')

    app = Starlette(routes=[Route('/echo', echo, methods=['POST'])])
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=100)
    client = TestClient(app)

    # multipart 上传不受 JSON body 上限拦截(声明 500 字节但 content-type 是 multipart)
    resp = client.post(
        '/echo',
        content=b'x' * 500,
        headers={'content-type': 'multipart/form-data; boundary=z'},
    )
    assert resp.status_code == 200
