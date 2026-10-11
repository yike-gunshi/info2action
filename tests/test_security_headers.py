"""Tests for default HTTP security headers and middleware rejection responses."""
import os
import sys

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from middleware.body_limit import MaxBodySizeMiddleware
from middleware.security_headers import SecurityHeadersMiddleware


EXPECTED_HEADERS = {
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'SAMEORIGIN',
    'Referrer-Policy': 'strict-origin-when-cross-origin',
}


@pytest.fixture(autouse=True)
def _reset_security_headers_env(monkeypatch):
    monkeypatch.delenv('INFO2ACTION_SECURITY_HEADERS', raising=False)


def _make_app(headers=None, with_guards=False):
    async def echo(request):
        return PlainTextResponse('ok', headers=headers)

    app = Starlette(routes=[Route('/echo', echo, methods=['GET', 'POST'])])
    if with_guards:
        from slowapi import Limiter, _rate_limit_exceeded_handler
        from slowapi.errors import RateLimitExceeded
        from slowapi.middleware import SlowAPIMiddleware
        from slowapi.util import get_remote_address

        app.state.limiter = Limiter(
            key_func=get_remote_address,
            default_limits=['1/minute'],
            storage_uri='memory://',
        )
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
        app.add_middleware(MaxBodySizeMiddleware, max_bytes=100)
        app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    return app


def _assert_security_headers(response):
    for name, value in EXPECTED_HEADERS.items():
        assert response.headers[name] == value


@pytest.mark.parametrize('value', [None, '', '1', 'true', 'on', 'unexpected'])
def test_security_headers_enabled_by_default_and_other_values(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv('INFO2ACTION_SECURITY_HEADERS', value)
    with TestClient(_make_app()) as client:
        response = client.get('/echo')
    assert response.status_code == 200
    _assert_security_headers(response)


@pytest.mark.parametrize('name,value', [
    ('x-frame-options', 'DENY'),
    ('x-content-type-options', 'custom'),
    ('referrer-policy', 'no-referrer'),
])
def test_security_headers_preserve_route_values(name, value):
    with TestClient(_make_app(headers={name: value})) as client:
        response = client.get('/echo')
    assert response.status_code == 200
    assert response.headers[name] == value
    for default_name, default_value in EXPECTED_HEADERS.items():
        if default_name.lower() != name:
            assert response.headers[default_name] == default_value


@pytest.mark.parametrize('value', ['0', 'false', 'off', 'FaLsE', 'OFF', ' false '])
def test_security_headers_disabled(monkeypatch, value):
    monkeypatch.setenv('INFO2ACTION_SECURITY_HEADERS', value)
    with TestClient(_make_app()) as client:
        response = client.get('/echo')
    assert response.status_code == 200
    for name in EXPECTED_HEADERS:
        assert name not in response.headers


def test_security_headers_on_not_found():
    with TestClient(_make_app()) as client:
        response = client.get('/missing')
    assert response.status_code == 404
    _assert_security_headers(response)


def test_security_headers_on_body_limit_rejection(monkeypatch):
    monkeypatch.setenv('RATELIMIT_ENABLED', 'true')
    with TestClient(_make_app(with_guards=True)) as client:
        response = client.post('/echo', content=b'x' * 101)
    assert response.status_code == 413
    _assert_security_headers(response)


def test_security_headers_on_rate_limit_rejection(monkeypatch):
    monkeypatch.setenv('RATELIMIT_ENABLED', 'true')
    with TestClient(_make_app(with_guards=True)) as client:
        assert client.get('/echo').status_code == 200
        response = client.get('/echo')
    assert response.status_code == 429
    _assert_security_headers(response)


def test_security_headers_registered_on_real_app(monkeypatch):
    monkeypatch.setenv('JWT_SECRET', 'security-headers-test-secret-long-enough-32')
    monkeypatch.setenv('RATELIMIT_ENABLED', 'false')

    import app as app_mod

    monkeypatch.setattr(app_mod.app.state.limiter, 'enabled', False)
    # 不进入 lifespan,避免启动任务触及数据库;缺失静态资源无需登录且直接返回 404。
    client = TestClient(app_mod.app)
    try:
        response = client.get('/assets/__security_headers_missing__.js')
    finally:
        client.close()

    assert response.status_code == 404
    _assert_security_headers(response)
    assert app_mod.app.user_middleware[0].cls is SecurityHeadersMiddleware
