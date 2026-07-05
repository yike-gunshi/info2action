"""Regression tests for runtime secret hygiene and submit URL SSRF guards."""
import os
import sys
import uuid

import bcrypt as _bcrypt
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import db as db_mod

PASSWORD = 'password123'


def _hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt(rounds=4)).decode()


@pytest.fixture()
def runtime_client(monkeypatch, tmp_path):
    monkeypatch.setenv('JWT_SECRET', 'runtime-hardening-test-secret-with-enough-entropy')
    monkeypatch.setenv('RATELIMIT_ENABLED', 'false')
    monkeypatch.setenv('INFO2ACTION_DATA_AUTHORITY', 'local')
    monkeypatch.setenv('INFO2ACTION_STORAGE_MODE', 'local')
    monkeypatch.setenv('INFO2ACTION_APP_STATE_BACKEND', 'sqlite')
    monkeypatch.setattr(db_mod, 'DB_PATH', str(tmp_path / 'feed.db'))
    db_mod._item_status_has_user_id = None

    conn = db_mod.get_conn()
    try:
        user_id = str(uuid.uuid4())
        db_mod.create_user(conn, user_id, 'runtime-user', 'runtime@test.local',
                           _hash_password(PASSWORD), role='user')
        db_mod.update_user(conn, user_id, email_verified=1)
        db_mod.migrate_item_status_add_user_id(conn, user_id)
    finally:
        conn.close()

    import app as app_mod
    import middleware.auth as auth_mw
    import routes.auth as auth_route

    monkeypatch.setattr(auth_route, 'JWT_SECRET', 'runtime-hardening-test-secret-with-enough-entropy')
    monkeypatch.setattr(auth_mw, '_AUTH_TOKEN', '')
    app_mod.app.state.limiter.enabled = False

    client = TestClient(app_mod.app)
    resp = client.post('/api/auth/login', json={'login': 'runtime@test.local', 'password': PASSWORD})
    assert resp.status_code == 200, resp.text
    return client


def test_action_execution_env_drops_host_secrets(monkeypatch):
    import execute_action

    monkeypatch.setenv('PATH', '/usr/bin')
    monkeypatch.setenv('HOME', '/Users/tester')
    monkeypatch.setenv('TOP_SECRET_FOR_REVIEW', 'super-secret-value')
    monkeypatch.setenv('OPENAI_API_KEY', 'api-secret')
    monkeypatch.delenv('ACTION_EXEC_ENV_ALLOWLIST', raising=False)

    env = execute_action._clean_env()

    assert env['PATH'] == '/usr/bin'
    assert env['HOME'] == '/Users/tester'
    assert 'TOP_SECRET_FOR_REVIEW' not in env
    assert 'OPENAI_API_KEY' not in env


def test_action_execution_env_allows_explicit_opt_in(monkeypatch):
    import execute_action

    monkeypatch.setenv('OPENAI_API_KEY', 'api-secret')
    monkeypatch.setenv('ACTION_EXEC_ENV_ALLOWLIST', 'OPENAI_API_KEY')

    env = execute_action._clean_env()

    assert env['OPENAI_API_KEY'] == 'api-secret'


@pytest.mark.parametrize('url', [
    'http://127.0.0.1:8080/private',
    'http://localhost:8080/private',
    'http://169.254.169.254/latest/meta-data',
    'http://10.0.0.5/internal',
    'http://[::1]:8080/private',
])
def test_submit_url_rejects_local_and_private_targets(runtime_client, monkeypatch, url):
    import routes.submit as submit_route

    started = {'value': False}

    class DummyThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            started['value'] = True

    monkeypatch.setattr(submit_route.threading, 'Thread', DummyThread)

    resp = runtime_client.post('/api/submit-url', json={'url': url})

    assert resp.status_code == 400
    assert started['value'] is False


def test_fetch_url_blocks_private_network_redirect_targets(monkeypatch):
    import socket
    import fetch_url

    with pytest.raises(ValueError):
        fetch_url._SafeRedirectHandler().redirect_request(
            None, None, 302, 'Found', {}, 'http://127.0.0.1/private',
        )

    def fake_getaddrinfo(host, *args, **kwargs):
        if host == 'public-looking.example':
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, '', ('10.0.0.8', 80))]
        return []

    monkeypatch.setattr(fetch_url.socket, 'getaddrinfo', fake_getaddrinfo)

    with pytest.raises(ValueError):
        fetch_url._assert_public_http_url('http://public-looking.example/path')
