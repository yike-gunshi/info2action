"""Regression tests for config/admin gates and auth lifecycle hardening."""
import json
import os
import sys
import uuid
from datetime import datetime, timezone, timedelta

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
def auth_config_env(monkeypatch, tmp_path):
    monkeypatch.setenv('JWT_SECRET', 'auth-config-hardening-secret-with-enough-entropy')
    monkeypatch.setenv('RATELIMIT_ENABLED', 'false')
    monkeypatch.setattr(db_mod, 'DB_PATH', str(tmp_path / 'feed.db'))
    db_mod._item_status_has_user_id = None

    conn = db_mod.get_conn()
    try:
        admin_id = _create_user(conn, 'admin-config', 'admin-config@test.local', 'admin')
        _create_user(conn, 'user-config', 'user-config@test.local', 'user')
        db_mod.migrate_item_status_add_user_id(conn, admin_id)
        db_mod.create_invite_code(conn, 'INVITE1', admin_id, max_uses=1)
    finally:
        conn.close()

    (tmp_path / 'config').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'config' / 'config.json').write_text(
        json.dumps({'ai_summary': {'api_key': 'secret-key'}, 'client': {'theme': 'light'}}),
        encoding='utf-8',
    )
    (tmp_path / '.api_token').write_text('legacy-token-value', encoding='utf-8')

    import app as app_mod
    import middleware.auth as auth_mw
    import routes.auth as auth_route
    import routes.config as config_route
    import routes.health as health_route

    monkeypatch.setattr(auth_route, 'JWT_SECRET', 'auth-config-hardening-secret-with-enough-entropy')
    monkeypatch.setattr(auth_mw, '_AUTH_TOKEN', '')
    monkeypatch.setattr(app_mod, 'BASE', str(tmp_path))
    monkeypatch.setattr(config_route, 'BASE', str(tmp_path))
    monkeypatch.setattr(health_route, 'BASE', str(tmp_path))
    app_mod.app.state.limiter.enabled = False
    return {'app': app_mod.app, 'tmp_path': tmp_path}


def _login(app, email: str) -> TestClient:
    client = TestClient(app)
    resp = client.post('/api/auth/login', json={'login': email, 'password': PASSWORD})
    assert resp.status_code == 200, resp.text
    return client


def test_config_token_and_credential_sync_are_admin_only(auth_config_env, monkeypatch):
    import routes.health as health_route

    monkeypatch.setattr(health_route.subprocess, 'run',
                        lambda *a, **k: pytest.fail('sync script should be admin gated'))
    regular = _login(auth_config_env['app'], 'user-config@test.local')
    admin = _login(auth_config_env['app'], 'admin-config@test.local')

    assert regular.get('/api/config').status_code == 403
    assert regular.post('/api/config', json={'client': {'theme': 'evil'}}).status_code == 403
    assert regular.get('/api/token').status_code == 403
    assert regular.post('/api/health/sync-credentials', json={'platform': 'twitter'}).status_code == 403

    assert admin.get('/api/config').status_code == 200
    assert admin.get('/api/token').json()['token'] == 'legacy-token-value'


def test_register_succeeds_even_when_verification_email_fails(auth_config_env, monkeypatch):
    """P1-4 邮件异步化后的新契约:先建用户并消耗邀请码,邮件后台发送;
    发送失败不回滚注册,用户通过 /api/auth/resend-code 自助补发。
    (旧契约"邮件失败→不建用户不耗邀请码"随同步发信一起废弃。)"""
    import routes.auth as auth_route

    monkeypatch.setattr(auth_route, 'send_verification_code', lambda *a, **k: False)
    client = TestClient(auth_config_env['app'])

    resp = client.post('/api/auth/register', json={
        'username': 'newuser',
        'email': 'newuser@test.local',
        'password': PASSWORD,
        'invite_code': 'INVITE1',
    })

    assert resp.status_code == 200
    conn = db_mod.get_conn()
    try:
        user = db_mod.get_user_by_email(conn, 'newuser@test.local')
        assert user is not None
        assert user['verification_code']  # 验证码已入库,可通过 resend-code 补发
        assert db_mod.get_invite_code(conn, 'INVITE1')['used_count'] == 1
    finally:
        conn.close()


def test_password_reset_revokes_existing_sessions(auth_config_env):
    client = _login(auth_config_env['app'], 'user-config@test.local')
    assert client.get('/api/auth/me').status_code == 200

    conn = db_mod.get_conn()
    try:
        user = db_mod.get_user_by_email(conn, 'user-config@test.local')
        db_mod.update_user(
            conn,
            user['id'],
            reset_token='reset-token',
            reset_token_expires=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        )
    finally:
        conn.close()

    reset_resp = client.post('/api/auth/reset-password', json={
        'token': 'reset-token',
        'password': 'newpassword123',
    })

    assert reset_resp.status_code == 200
    assert client.get('/api/auth/me').status_code == 401


def test_verify_email_route_is_rate_limited():
    import routes.auth as auth_route

    assert 'routes.auth.verify_email' in auth_route.limiter._route_limits
