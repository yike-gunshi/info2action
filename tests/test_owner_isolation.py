"""Regression tests for per-user action and interest isolation."""
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


def _make_manual_item(owner_id: str):
    return dict(
        id='alice-manual-source',
        user_id=owner_id,
        platform='manual',
        source='user-submit',
        title='Alice source item',
        content='private source content',
        author_name='alice',
        author_id='',
        author_avatar='',
        url='https://private.example/source',
        cover_url=None,
        media_json=None,
        metrics_json='{}',
        tags_json=None,
        lang='en',
        detail_json=None,
        comments_json=None,
        ai_summary='private source summary',
        ai_key_points=None,
        relevance_score=5.0,
        fetched_at='2026-04-24T00:00:00',
        published_at='2026-04-24T00:00:00',
    )


@pytest.fixture()
def owner_env(monkeypatch, tmp_path):
    monkeypatch.setenv('JWT_SECRET', 'owner-isolation-test-secret-with-enough-entropy')
    monkeypatch.setenv('RATELIMIT_ENABLED', 'false')
    monkeypatch.setattr(db_mod, 'DB_PATH', str(tmp_path / 'feed.db'))
    db_mod._item_status_has_user_id = None

    conn = db_mod.get_conn()
    try:
        _create_user(conn, 'alice', 'alice@test.local', 'user')
        _create_user(conn, 'bob', 'bob@test.local', 'user')
        _create_user(conn, 'admin', 'admin-owner@test.local', 'admin')
    finally:
        conn.close()

    import app as app_mod
    import middleware.auth as auth_mw
    import routes.auth as auth_route

    monkeypatch.setattr(auth_route, 'JWT_SECRET', 'owner-isolation-test-secret-with-enough-entropy')
    monkeypatch.setattr(auth_mw, '_AUTH_TOKEN', '')
    app_mod.app.state.limiter.enabled = False
    return app_mod.app


def _login(app, email: str) -> TestClient:
    client = TestClient(app)
    resp = client.post('/api/auth/login', json={'login': email, 'password': PASSWORD})
    assert resp.status_code == 200, resp.text
    return client


def test_actions_are_visible_and_mutable_only_by_owner_or_admin(owner_env):
    alice = _login(owner_env, 'alice@test.local')
    bob = _login(owner_env, 'bob@test.local')
    admin = _login(owner_env, 'admin-owner@test.local')

    created = alice.post('/api/actions', json={
        'title': 'Alice private action',
        'prompt': 'Only Alice should see this',
        'action_type': 'implement',
    })
    assert created.status_code == 200
    action_id = created.json()['id']

    alice_list = alice.get('/api/actions')
    bob_list = bob.get('/api/actions')
    admin_list = admin.get('/api/actions')

    assert [a['id'] for a in alice_list.json()['actions']] == [action_id]
    assert [a['id'] for a in bob_list.json()['actions']] == []
    assert action_id in [a['id'] for a in admin_list.json()['actions']]
    assert bob.get(f'/api/actions/{action_id}').status_code == 404

    priority_resp = bob.patch(f'/api/actions/{action_id}/priority', json={'priority': 'high'})
    done_resp = bob.post(f'/api/actions/{action_id}/done')
    dismiss_resp = bob.post(f'/api/actions/{action_id}/dismiss', json={'feedback_type': 'nope'})
    delete_resp = bob.delete(f'/api/actions/{action_id}')

    assert priority_resp.status_code == 404
    assert done_resp.status_code == 404
    assert dismiss_resp.status_code == 404
    assert delete_resp.status_code == 404
    assert alice.get(f'/api/actions/{action_id}').status_code == 200


def test_action_source_items_do_not_leak_other_users_manual_items(owner_env):
    bob = _login(owner_env, 'bob@test.local')
    admin = _login(owner_env, 'admin-owner@test.local')

    conn = db_mod.get_conn()
    try:
        alice_id = db_mod.get_user_by_email(conn, 'alice@test.local')['id']
        db_mod.batch_upsert(conn, [_make_manual_item(alice_id)])
    finally:
        conn.close()

    created = bob.post('/api/actions', json={
        'title': 'Bob action with foreign source id',
        'prompt': 'Do not leak source item details',
        'action_type': 'investigate',
        'source_item_ids': ['alice-manual-source'],
    })
    assert created.status_code == 200
    action_id = created.json()['id']

    bob_detail = bob.get(f'/api/actions/{action_id}')
    admin_detail = admin.get(f'/api/actions/{action_id}')

    assert bob_detail.status_code == 200
    assert bob_detail.json()['source_items'] == []
    assert admin_detail.status_code == 200
    assert admin_detail.json()['source_items'][0]['id'] == 'alice-manual-source'


def test_interests_are_visible_and_mutable_only_by_owner_or_admin(owner_env):
    alice = _login(owner_env, 'alice@test.local')
    bob = _login(owner_env, 'bob@test.local')
    admin = _login(owner_env, 'admin-owner@test.local')

    created = alice.post('/api/interests', json={
        'name': 'Alice private interest',
        'description': 'private',
        'keywords': ['alice-only'],
    })
    assert created.status_code == 200
    interest_id = created.json()['interest']['id']

    alice_list = alice.get('/api/interests')
    bob_list = bob.get('/api/interests')
    admin_list = admin.get('/api/interests')

    assert [i['id'] for i in alice_list.json()['interests']] == [interest_id]
    assert [i['id'] for i in bob_list.json()['interests']] == []
    assert interest_id in [i['id'] for i in admin_list.json()['interests']]
    assert bob.get(f'/api/interests/{interest_id}/matches').status_code == 404

    update_resp = bob.post(f'/api/interests/{interest_id}', json={'name': 'stolen'})
    delete_resp = bob.delete(f'/api/interests/{interest_id}')

    assert update_resp.status_code == 404
    assert delete_resp.status_code == 404
    assert alice.get('/api/interests').json()['interests'][0]['name'] == 'Alice private interest'


def test_interest_keyword_generation_static_route_is_reachable(owner_env, monkeypatch):
    import routes.interests as interests_route

    monkeypatch.setattr(interests_route.interest_engine, 'generate_keywords',
                        lambda description: ['alpha', 'beta'])
    alice = _login(owner_env, 'alice@test.local')

    resp = alice.post('/api/interests/generate-keywords', json={'description': 'find AI infra'})

    assert resp.status_code == 200
    assert resp.json() == {'ok': True, 'keywords': ['alpha', 'beta']}
