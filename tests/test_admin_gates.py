"""Regression tests for host-side/admin-only control surfaces."""
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


def _create_user(conn, username: str, email: str, role: str) -> str:
    user_id = str(uuid.uuid4())
    db_mod.create_user(conn, user_id, username, email, _hash_password(PASSWORD), role=role)
    db_mod.update_user(conn, user_id, email_verified=1)
    return user_id


@pytest.fixture()
def admin_gate_env(monkeypatch, tmp_path):
    """Create a temp app/DB with one regular user, one admin, and shared test data."""
    monkeypatch.setenv('JWT_SECRET', 'admin-gate-test-secret-with-enough-entropy')
    monkeypatch.setenv('RATELIMIT_ENABLED', 'false')
    monkeypatch.setattr(db_mod, 'DB_PATH', str(tmp_path / 'feed.db'))
    db_mod._item_status_has_user_id = None

    conn = db_mod.get_conn()
    try:
        _create_user(conn, 'regular', 'regular@test.local', 'user')
        _create_user(conn, 'admin', 'admin@test.local', 'admin')
        action_id = db_mod.create_action(
            conn,
            source_type='manual',
            title='Review action',
            action_type='implement',
            prompt='Do something safe',
            source_item_ids=[],
            reason='review fixture',
            priority='medium',
        )
        interest_id = db_mod.create_interest(
            conn,
            'Review interest',
            description='fixture',
            keywords=['review'],
        )
    finally:
        conn.close()

    import app as app_mod
    import middleware.auth as auth_mw
    import routes.auth as auth_route

    monkeypatch.setattr(auth_route, 'JWT_SECRET', 'admin-gate-test-secret-with-enough-entropy')
    monkeypatch.setattr(auth_mw, '_AUTH_TOKEN', '')
    app_mod.app.state.limiter.enabled = False

    return {
        'app': app_mod.app,
        'action_id': action_id,
        'interest_id': interest_id,
    }


def _logged_in_client(app, login: str) -> TestClient:
    client = TestClient(app)
    resp = client.post('/api/auth/login', json={'login': login, 'password': PASSWORD})
    assert resp.status_code == 200, resp.text
    return client


def test_regular_user_cannot_access_terminal_or_ttyd_controls(admin_gate_env, monkeypatch):
    import routes.terminal as terminal_route

    class DummyPopen:
        returncode = 0
        stdout = []

        class _Stderr:
            @staticmethod
            def read():
                return ''

        stderr = _Stderr()

        def wait(self):
            return 0

    monkeypatch.setattr(terminal_route.subprocess, 'Popen', lambda *a, **k: DummyPopen())
    client = _logged_in_client(admin_gate_env['app'], 'regular@test.local')

    checks = [
        ('GET', '/api/cli/status', None),
        ('GET', '/api/cli/exec?prompt=hello', None),
        ('POST', '/api/cli/stop?exec_id=missing', {}),
        ('POST', '/api/pty/create', {'prompt': 'hello'}),
        ('POST', '/api/pty/input', {'id': 'missing', 'data': 'x'}),
        ('POST', '/api/pty/resize', {'id': 'missing'}),
        ('POST', '/api/pty/kill', {'id': 'missing'}),
        ('GET', '/api/pty/stream?id=missing', None),
        ('POST', '/api/ttyd/start', {'prompt': 'hello', 'action_id': admin_gate_env['action_id']}),
        ('POST', '/api/ttyd/reconnect', {'action_id': admin_gate_env['action_id']}),
        ('POST', '/api/ttyd/send-keys', {'action_id': admin_gate_env['action_id'], 'keys': 'q'}),
        ('POST', '/api/ttyd/stop', {'action_id': admin_gate_env['action_id']}),
        ('GET', f'/api/ttyd/status/{admin_gate_env["action_id"]}', None),
        ('GET', '/api/ttyd/sessions', None),
    ]

    for method, path, payload in checks:
        resp = client.request(method, path, json=payload) if payload is not None else client.request(method, path)
        assert resp.status_code == 403, f'{method} {path} returned {resp.status_code}: {resp.text}'


def test_regular_user_cannot_access_workspace_context_or_rewrite_project_dirs(admin_gate_env, monkeypatch):
    import routes.context as context_route

    monkeypatch.setattr(context_route.execute_action, 'set_project_dirs', lambda dirs: None)
    client = _logged_in_client(admin_gate_env['app'], 'regular@test.local')

    read_resp = client.get('/api/user-context')
    list_resp = client.get('/api/settings/project-dirs')
    write_resp = client.post('/api/settings/project-dirs', json={'project_dirs': ['/tmp/evil']})

    assert read_resp.status_code == 403
    assert list_resp.status_code == 403
    assert write_resp.status_code == 403


def test_regular_user_cannot_trigger_global_jobs(admin_gate_env, monkeypatch):
    import routes.briefing as briefing_route
    import routes.fetch as fetch_route

    def reset_fetch_flag(*_args, **_kwargs):
        fetch_route._fetch_running = False

    monkeypatch.setattr(fetch_route, '_run_fetch', reset_fetch_flag)
    monkeypatch.setattr(briefing_route.subprocess, 'run', lambda *a, **k: None)
    client = _logged_in_client(admin_gate_env['app'], 'regular@test.local')

    assert client.post('/api/fetch').status_code == 403
    assert client.post('/api/fetch/quick', json={}).status_code == 403
    assert client.post('/api/briefing/generate').status_code == 403


def test_regular_user_cannot_read_global_feedback_scores(admin_gate_env):
    client = _logged_in_client(admin_gate_env['app'], 'regular@test.local')
    admin = _logged_in_client(admin_gate_env['app'], 'admin@test.local')

    assert client.get('/api/feedback').status_code == 403
    assert admin.get('/api/feedback').status_code == 200


def test_regular_user_cannot_trigger_host_sensitive_action_controls(admin_gate_env, monkeypatch):
    import routes.actions as actions_route

    monkeypatch.setattr(actions_route.subprocess, 'run', lambda *a, **k: None)
    monkeypatch.setattr(actions_route.execute_action, 'start_execution',
                        lambda *a, **k: {'ok': True, 'stubbed': True})
    client = _logged_in_client(admin_gate_env['app'], 'regular@test.local')
    action_id = admin_gate_env['action_id']

    checks = [
        ('GET', f'/api/actions/{action_id}/stream', None),
        ('POST', '/api/actions/auto-generate', {}),
        ('POST', '/api/actions/generate-from-item', {}),
        ('POST', f'/api/actions/{action_id}/confirm', {'tool': 'codex'}),
        ('POST', f'/api/actions/{action_id}/execute', {'tool': 'codex'}),
        ('PATCH', f'/api/actions/{action_id}', {'title': 'Retargeted', 'prompt': 'evil'}),
    ]

    for method, path, payload in checks:
        resp = client.request(method, path, json=payload) if payload is not None else client.request(method, path)
        assert resp.status_code == 403, f'{method} {path} returned {resp.status_code}: {resp.text}'


def test_regular_user_cannot_trigger_interest_scan(admin_gate_env, monkeypatch):
    import routes.interests as interests_route

    monkeypatch.setattr(interests_route.interest_engine, 'scan_interest', lambda interest_id: None)
    client = _logged_in_client(admin_gate_env['app'], 'regular@test.local')

    resp = client.post(f'/api/interests/{admin_gate_env["interest_id"]}/scan')

    assert resp.status_code == 403


def test_admin_and_legacy_token_keep_operational_access(admin_gate_env, monkeypatch):
    import middleware.auth as auth_mw

    admin_client = _logged_in_client(admin_gate_env['app'], 'admin@test.local')
    assert admin_client.get('/api/cli/status').status_code == 200
    assert admin_client.get('/api/user-context').status_code == 200

    monkeypatch.setattr(auth_mw, '_AUTH_TOKEN', 'legacy-token')
    legacy_client = TestClient(admin_gate_env['app'])
    legacy_resp = legacy_client.get('/api/cli/status', headers={'Authorization': 'Bearer legacy-token'})
    assert legacy_resp.status_code == 200
