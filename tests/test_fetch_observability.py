import json
import os
import sys
import uuid

import bcrypt as _bcrypt
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import db as db_mod  # noqa: E402

PASSWORD = 'password123'


def _hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt(rounds=4)).decode()


def _create_user(conn, username: str, email: str, role: str) -> str:
    user_id = str(uuid.uuid4())
    db_mod.create_user(conn, user_id, username, email, _hash_password(PASSWORD), role=role)
    db_mod.update_user(conn, user_id, email_verified=1)
    return user_id


def _item(item_id: str, *, title: str, source: str = 'following', run_id: int | None = None):
    return {
        'id': item_id,
        'platform': 'twitter',
        'source': source,
        'fetch_run_id': run_id,
        'title': title,
        'content': title,
        'author_name': 'alice',
        'fetched_at': '2026-05-12T10:00:00',
        'published_at': '2026-05-12T09:55:00',
        'ai_summary': 'summary',
        'ai_category': 'products',
        'ai_categories': json.dumps(['products']),
    }


@pytest.fixture()
def fetch_observability_env(monkeypatch, tmp_path):
    monkeypatch.setenv('JWT_SECRET', 'fetch-observability-secret-with-enough-entropy')
    monkeypatch.setenv('RATELIMIT_ENABLED', 'false')
    monkeypatch.setenv('INFO2ACTION_DATA_AUTHORITY', 'local')
    monkeypatch.setenv('INFO2ACTION_READ_BACKEND', 'sqlite')
    monkeypatch.setenv('INFO2ACTION_FEED_READ_BACKEND', 'sqlite')
    monkeypatch.setenv('INFO2ACTION_EVENT_READ_BACKEND', 'sqlite')
    monkeypatch.setenv('INFO2ACTION_STATUS_BACKEND', 'sqlite')
    monkeypatch.setenv('INFO2ACTION_APP_STATE_BACKEND', 'sqlite')
    monkeypatch.setenv('INFO2ACTION_STORAGE_MODE', 'local')
    monkeypatch.setenv('INFO2ACTION_ASSET_BACKEND', 'local')
    monkeypatch.setattr(db_mod, 'DB_PATH', str(tmp_path / 'feed.db'))
    db_mod._item_status_has_user_id = None

    conn = db_mod.get_conn()
    try:
        admin_id = _create_user(conn, 'admin-fetch', 'admin-fetch@test.local', 'admin')
        _create_user(conn, 'user-fetch', 'user-fetch@test.local', 'user')
        db_mod.migrate_item_status_add_user_id(conn, admin_id)

        db_mod.batch_upsert(conn, [_item('old-item', title='Old title before run')])
        run_id = db_mod.start_fetch_run(conn)
        db_mod.batch_upsert(
            conn,
            [
                _item('old-item', title='Old title touched again', run_id=run_id),
                _item('new-item', title='New title from this run', run_id=run_id),
            ],
            fetch_run_id=run_id,
        )
        conn.execute(
            """INSERT INTO clusters
                 (id, ai_title, first_doc_at, last_doc_at, last_updated_at,
                  unique_source_count, is_visible_in_feed, published_run_id, published_at)
               VALUES (101, 'Published event', '2026-05-12T09:55:00',
                       '2026-05-12T10:00:00', '2026-05-12T10:02:00',
                       2, 1, ?, '2026-05-12T10:03:00')""",
            (run_id,),
        )
        conn.execute("UPDATE items SET cluster_id = 101 WHERE id = 'new-item'")
        conn.execute(
            """UPDATE items
                  SET ai_category = 'products',
                      ai_categories = ?
                WHERE id = 'new-item'""",
            (json.dumps(['products']),),
        )
        db_mod.finish_fetch_run(
            conn,
            run_id,
            {'_stage_durations_sec': {'source_fetch': 1.2, 'ingest': 0.4}},
        )
        db_mod.record_embedding_usage({
            'provider': 'minimax-embo-01',
            'model': 'embo-01',
            'mode': 'db',
            'source': 'clustering.pipeline',
            'stage': 'stage0_item_embedding',
            'run_id': run_id,
            'caller_file': 'src/clustering/pipeline.py',
            'caller_func': '_embed_pending_items',
            'input_count': 2,
            'input_chars': 3200,
            'input_bytes': 6400,
            'estimated_tokens': 2000,
            'token_estimator': 'unit-test',
            'output_count': 2,
            'output_dim': 1536,
            'status': 'success',
            'latency_ms': 123,
            'price_yuan_per_1k_tokens': 0.0005,
            'estimated_cost_yuan': 0.001,
            'item_ids_json': ['new-item', 'other-item'],
        })
    finally:
        conn.close()

    import app as app_mod
    import middleware.auth as auth_mw
    import routes.auth as auth_route

    monkeypatch.setattr(auth_route, 'JWT_SECRET', 'fetch-observability-secret-with-enough-entropy')
    monkeypatch.setattr(auth_mw, '_AUTH_TOKEN', '')
    app_mod.app.state.limiter.enabled = False
    return {'app': app_mod.app, 'run_id': run_id}


def _login(app, email: str) -> TestClient:
    client = TestClient(app)
    resp = client.post('/api/auth/login', json={'login': email, 'password': PASSWORD})
    assert resp.status_code == 200, resp.text
    return client


def test_fetch_run_items_track_inserted_vs_upserted(fetch_observability_env):
    conn = db_mod.get_conn()
    try:
        run_id = fetch_observability_env['run_id']
        run = db_mod.get_fetch_run_audit(conn, run_id)
        assert run['audit']['new_items_count'] == 1
        assert run['audit']['platform_source_counts'] == [
            {'platform': 'twitter', 'source': 'following', 'count': 1}
        ]
        assert run['audit']['pill_counts'] == [{'pill': 'products', 'count': 1}]
        assert run['audit']['ai_summary'] == {'summarized': 1, 'failed': 0, 'pending': 0}
        assert run['audit']['event_cluster']['published_clusters'] == 1

        items = db_mod.query_fetch_run_audit_items(
            conn,
            run_id,
            platform='twitter',
            source='following',
        )
        assert items['total'] == 1
        assert [item['id'] for item in items['items']] == ['new-item']
        assert items['items'][0]['ai_status'] == 'summarized'
        assert items['items'][0]['cluster_status'] == 'clustered'
    finally:
        conn.close()


def test_admin_fetch_run_api_is_admin_only_and_drilldown(fetch_observability_env):
    regular = _login(fetch_observability_env['app'], 'user-fetch@test.local')
    admin = _login(fetch_observability_env['app'], 'admin-fetch@test.local')
    run_id = fetch_observability_env['run_id']

    assert regular.get('/api/admin/fetch-runs').status_code == 403

    list_resp = admin.get('/api/admin/fetch-runs')
    assert list_resp.status_code == 200, list_resp.text
    assert list_resp.json()['runs'][0]['id'] == run_id
    assert list_resp.json()['runs'][0]['total_new_items'] == 1

    detail_resp = admin.get(f'/api/admin/fetch-runs/{run_id}')
    assert detail_resp.status_code == 200, detail_resp.text
    detail = detail_resp.json()['run']
    assert detail['audit']['stage_durations_sec']['source_fetch'] == 1.2
    assert detail['audit']['platform_source_counts'][0]['source'] == 'following'

    items_resp = admin.get(
        f'/api/admin/fetch-runs/{run_id}/items?platform=twitter&source=following'
    )
    assert items_resp.status_code == 200, items_resp.text
    body = items_resp.json()
    assert body['total'] == 1
    assert body['items'][0]['id'] == 'new-item'
    assert body['items'][0]['title'] == 'New title from this run'

    missing_resp = admin.get('/api/admin/fetch-runs/999999')
    assert missing_resp.status_code == 404


def test_admin_embedding_usage_api_is_admin_only(fetch_observability_env):
    regular = _login(fetch_observability_env['app'], 'user-fetch@test.local')
    admin = _login(fetch_observability_env['app'], 'admin-fetch@test.local')
    run_id = fetch_observability_env['run_id']

    assert regular.get('/api/admin/embedding-usage').status_code == 403

    resp = admin.get(f'/api/admin/embedding-usage?hours=24&run_id={run_id}')
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body['summary']['total_calls'] == 1
    assert body['summary']['success_calls'] == 1
    assert body['summary']['estimated_tokens_success'] == 2000
    assert body['summary']['estimated_cost_yuan_success'] == 0.001
    assert body['by_source'][0]['source'] == 'clustering.pipeline'
    assert body['logs'][0]['run_id'] == run_id
