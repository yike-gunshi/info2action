"""Regression tests for manual submission and ASR cross-account isolation."""
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


def _create_user(conn, username: str, email: str, role: str = 'user') -> str:
    user_id = str(uuid.uuid4())
    db_mod.create_user(conn, user_id, username, email, _hash_password(PASSWORD), role=role)
    db_mod.update_user(conn, user_id, email_verified=1)
    return user_id


def _make_manual_item(item_id='manual-private-1'):
    return dict(
        id=item_id,
        platform='manual',
        source='user-submit',
        title='Alice private manual',
        content='secret manual body',
        author_name='alice',
        author_id='',
        author_avatar='',
        url='https://private.example/alice',
        cover_url=None,
        media_json=None,
        metrics_json='{}',
        tags_json=None,
        lang='en',
        detail_json=None,
        comments_json=None,
        ai_summary='private summary',
        ai_key_points=None,
        relevance_score=5.0,
        fetched_at='2026-04-24T00:00:00',
        published_at='2026-04-24T00:00:00',
    )


@pytest.fixture()
def manual_env(monkeypatch, tmp_path):
    monkeypatch.setenv('JWT_SECRET', 'manual-asr-isolation-test-secret-with-enough-entropy')
    monkeypatch.setenv('RATELIMIT_ENABLED', 'false')
    monkeypatch.setattr(db_mod, 'DB_PATH', str(tmp_path / 'feed.db'))
    db_mod._item_status_has_user_id = None

    conn = db_mod.get_conn()
    try:
        alice_id = _create_user(conn, 'alice-manual', 'alice-manual@test.local')
        bob_id = _create_user(conn, 'bob-manual', 'bob-manual@test.local')
        admin_id = _create_user(conn, 'admin-manual', 'admin-manual@test.local', role='admin')
        db_mod.migrate_item_status_add_user_id(conn, alice_id)
        db_mod.batch_upsert(conn, [_make_manual_item()])
        conn.execute(
            """UPDATE items
               SET asr_text=?,
                   asr_status=?,
                   ai_summary=?,
                   asr_text_cn=?,
                   ai_category=?,
                   fetched_at=datetime('now'),
                   published_at=datetime('now')
               WHERE id=?""",
            ('private transcript', 'success', 'private summary', '私密转写', 'ai_tools', 'manual-private-1'),
        )
        conn.commit()
        db_mod.set_status(conn, 'manual-private-1', 'starred', force=True, user_id=alice_id)
    finally:
        conn.close()

    import app as app_mod
    import feedback_store
    import middleware.auth as auth_mw
    import routes.auth as auth_route
    import routes.feed as feed_route
    import routes.submit as submit_route

    feed_route._trend_cache.clear()
    submit_route._submit_tasks.clear()
    monkeypatch.setattr(feedback_store, 'FB_DB_PATH', str(tmp_path / 'user_feedback.db'))
    monkeypatch.setattr(auth_route, 'JWT_SECRET', 'manual-asr-isolation-test-secret-with-enough-entropy')
    monkeypatch.setattr(auth_mw, '_AUTH_TOKEN', '')
    app_mod.app.state.limiter.enabled = False
    return {
        'app': app_mod.app,
        'alice_id': alice_id,
        'bob_id': bob_id,
        'admin_id': admin_id,
    }


def _login(app, email: str) -> TestClient:
    client = TestClient(app)
    resp = client.post('/api/auth/login', json={'login': email, 'password': PASSWORD})
    assert resp.status_code == 200, resp.text
    return client


def test_manual_submission_surfaces_are_owner_scoped(manual_env):
    alice = _login(manual_env['app'], 'alice-manual@test.local')
    bob = _login(manual_env['app'], 'bob-manual@test.local')
    admin = _login(manual_env['app'], 'admin-manual@test.local')

    assert alice.get('/api/submit-history').json()['items'][0]['id'] == 'manual-private-1'
    assert admin.get('/api/submit-history').json()['items'][0]['id'] == 'manual-private-1'
    assert bob.get('/api/submit-history').json()['items'] == []

    assert bob.post('/api/submit-url/status', json={'task_id': 'manual-private-1'}).status_code == 404
    assert bob.get('/api/feed/item/manual-private-1').status_code == 404

    assert [i['id'] for i in alice.get('/api/feed').json()['items']] == ['manual-private-1']
    assert bob.get('/api/feed').json()['items'] == []
    assert [i['id'] for i in admin.get('/api/feed').json()['items']] == ['manual-private-1']

    bob_sections = bob.get('/api/feed/sections').json()
    bob_section_items = [
        item['id']
        for items in bob_sections['sections'].values()
        for item in items
    ]
    assert 'manual-private-1' not in bob_section_items
    assert bob_sections['total'] == 0

    bob_platforms = bob.get('/api/feed/platforms').json()
    assert 'manual' not in bob_platforms['sections']
    assert bob.get('/api/feed/platforms/more?platform=manual').json()['items'] == []
    assert 'manual' not in bob.get('/api/stats').json()
    assert bob.get('/api/trends').json()['item_count'] == 0
    assert alice.get('/api/trends').json()['item_count'] == 1
    assert admin.get('/api/trends').json()['item_count'] == 1

    export_resp = bob.get('/api/export?platform=manual')
    assert export_resp.status_code == 200
    assert 'manual-private-1' not in export_resp.text
    assert 'Alice private manual' not in export_resp.text

    assert bob.post('/api/feedback', json={
        'item_id': 'manual-private-1',
        'type': 'text',
        'text': 'should not attach to Alice manual item',
    }).status_code == 404
    alice_feedback = alice.post('/api/feedback', json={
        'item_id': 'manual-private-1',
        'type': 'text',
        'text': 'owner feedback',
    })
    assert alice_feedback.status_code == 200


def test_submit_task_status_is_owner_scoped(manual_env):
    import routes.submit as submit_route

    submit_route._submit_tasks['task-123'] = {
        'status': 'fetching',
        'url': 'https://private.example/task',
        'title': 'Alice pending task',
        'error': '',
        'user_id': manual_env['alice_id'],
    }
    alice = _login(manual_env['app'], 'alice-manual@test.local')
    bob = _login(manual_env['app'], 'bob-manual@test.local')

    assert alice.post('/api/submit-url/status', json={'task_id': 'task-123'}).status_code == 200
    assert bob.post('/api/submit-url/status', json={'task_id': 'task-123'}).status_code == 404


def test_asr_read_and_write_are_owner_scoped_for_manual_items(manual_env, monkeypatch):
    import routes.asr as asr_route

    async def noop_transcribe(*_args, **_kwargs):
        return None

    monkeypatch.setattr(asr_route.asr_worker, 'transcribe_and_summarize', noop_transcribe)
    monkeypatch.setattr(asr_route.asr_worker, 'translate_transcript_cn', lambda text: 'translated')
    bob = _login(manual_env['app'], 'bob-manual@test.local')
    alice = _login(manual_env['app'], 'alice-manual@test.local')

    assert alice.get('/api/items/manual-private-1/asr').status_code == 200
    assert bob.get('/api/items/manual-private-1/asr').status_code == 404
    assert bob.post('/api/items/manual-private-1/asr').status_code == 404
    assert bob.post('/api/items/manual-private-1/asr/translate').status_code == 404
