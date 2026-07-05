"""P1-4 开放注册 — register 开关、邀请码兼容、邮件异步化。

Scope(local sqlite 模式):
- 开关关闭:无邀请码注册被拒(行为不变)
- 开关打开:无邀请码注册成功,用户带验证码待验证;邮件后台发送
- 开关打开且带邀请码:仍校验并消耗邀请码
- 邮件发送失败不影响注册成功(异步兜底语义)
- /api/auth/register-config 匿名可读且反映开关
"""
from __future__ import annotations

import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

os.environ.setdefault('RATELIMIT_ENABLED', 'false')

import db as db_mod  # noqa: E402


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv('RATELIMIT_ENABLED', 'false')
    monkeypatch.setenv('JWT_SECRET', 'open-reg-test-secret-enough-32-chars!!')
    monkeypatch.setenv('INFO2ACTION_DATA_AUTHORITY', 'local')
    monkeypatch.setenv('INFO2ACTION_APP_STATE_BACKEND', 'sqlite')
    monkeypatch.setattr(db_mod, 'DB_PATH', str(tmp_path / 'openreg.db'))
    db_mod._item_status_has_user_id = None
    conn = db_mod.get_conn()
    conn.close()

    from fastapi.testclient import TestClient
    from app import app
    app.state.limiter.enabled = False
    return TestClient(app)


def _register_payload(**overrides):
    suffix = uuid.uuid4().hex[:8]
    payload = {
        'username': f'user_{suffix}',
        'email': f'{suffix}@test.local',
        'password': 'password123',
    }
    payload.update(overrides)
    return payload


def _wait_for(cond, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_closed_mode_requires_invite(client, monkeypatch):
    monkeypatch.delenv('INFO2ACTION_OPEN_REGISTRATION', raising=False)
    r = client.post('/api/auth/register', json=_register_payload())
    assert r.status_code == 400
    assert '邀请码' in r.json()['error']


def test_open_mode_registers_without_invite(client, monkeypatch):
    monkeypatch.setenv('INFO2ACTION_OPEN_REGISTRATION', '1')
    sent = []
    import routes.auth as auth_route
    monkeypatch.setattr(auth_route, 'send_verification_code',
                        lambda email, code, username='': sent.append((email, code)) or True)

    payload = _register_payload()
    r = client.post('/api/auth/register', json=payload)
    assert r.status_code == 200
    assert r.json()['verify_email'] is True

    conn = db_mod.get_conn()
    try:
        user = db_mod.get_user_by_email(conn, payload['email'])
    finally:
        conn.close()
    assert user is not None
    assert user['verification_code']  # 验证码已写入,等待 verify-email
    assert not user.get('email_verified')
    # 邮件异步发送
    assert _wait_for(lambda: len(sent) == 1)
    assert sent[0][0] == payload['email']
    assert sent[0][1] == user['verification_code']


def test_open_mode_still_consumes_valid_invite(client, monkeypatch):
    monkeypatch.setenv('INFO2ACTION_OPEN_REGISTRATION', '1')
    import routes.auth as auth_route
    monkeypatch.setattr(auth_route, 'send_verification_code', lambda *a, **k: True)

    conn = db_mod.get_conn()
    try:
        conn.execute(
            "INSERT INTO invite_codes (code, max_uses, used_count) VALUES ('OPENCODE', 1, 0)")
        conn.commit()
    finally:
        conn.close()

    r = client.post('/api/auth/register',
                    json=_register_payload(invite_code='OPENCODE'))
    assert r.status_code == 200

    conn = db_mod.get_conn()
    try:
        row = conn.execute(
            "SELECT used_count FROM invite_codes WHERE code = 'OPENCODE'").fetchone()
    finally:
        conn.close()
    assert row['used_count'] == 1


def test_open_mode_rejects_bad_invite_when_provided(client, monkeypatch):
    """填了邀请码就必须有效——开放注册不放水假邀请码。"""
    monkeypatch.setenv('INFO2ACTION_OPEN_REGISTRATION', '1')
    r = client.post('/api/auth/register',
                    json=_register_payload(invite_code='BADBAD00'))
    assert r.status_code == 400
    assert '无效' in r.json()['error']


def test_email_failure_does_not_fail_registration(client, monkeypatch):
    monkeypatch.setenv('INFO2ACTION_OPEN_REGISTRATION', '1')
    import routes.auth as auth_route
    monkeypatch.setattr(auth_route, 'send_verification_code',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('resend down')))

    payload = _register_payload()
    r = client.post('/api/auth/register', json=payload)
    assert r.status_code == 200  # 用户已建,邮件由 resend-code 兜底

    conn = db_mod.get_conn()
    try:
        assert db_mod.get_user_by_email(conn, payload['email']) is not None
    finally:
        conn.close()


def test_register_config_endpoint(client, monkeypatch):
    monkeypatch.delenv('INFO2ACTION_OPEN_REGISTRATION', raising=False)
    r = client.get('/api/auth/register-config')
    assert r.status_code == 200
    assert r.json() == {'open_registration': False}

    monkeypatch.setenv('INFO2ACTION_OPEN_REGISTRATION', '1')
    r = client.get('/api/auth/register-config')
    assert r.json() == {'open_registration': True}
