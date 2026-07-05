"""Regression tests for public/auth boundary hardening."""
import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import db as db_mod


def _make_item(**overrides):
    base = dict(
        id='public-1',
        platform='twitter',
        source='following',
        title='Public item',
        content='Public content',
        author_name='alice',
        author_id='a1',
        author_avatar='',
        url='https://x.com/alice/status/1',
        cover_url=None,
        media_json=None,
        metrics_json='{}',
        tags_json=None,
        lang='en',
        detail_json=None,
        comments_json=None,
        ai_summary='Public summary',
        ai_key_points=None,
        relevance_score=5.0,
        fetched_at='2026-04-24T00:00:00',
        published_at='2026-04-24T00:00:00',
    )
    base.update(overrides)
    return base


@pytest.fixture()
def public_boundary_client(monkeypatch, tmp_path):
    """TestClient with temp DB/config and legacy auth enabled."""
    monkeypatch.setenv('JWT_SECRET', 'test-secret-key-for-public-boundary')
    monkeypatch.setenv('INFO2ACTION_DATA_AUTHORITY', 'local')
    monkeypatch.setenv('INFO2ACTION_READ_BACKEND', 'sqlite')
    monkeypatch.setenv('INFO2ACTION_FEED_READ_BACKEND', 'sqlite')
    monkeypatch.setenv('INFO2ACTION_EVENT_READ_BACKEND', 'sqlite')
    monkeypatch.setenv('INFO2ACTION_STATUS_BACKEND', 'sqlite')
    monkeypatch.setenv('INFO2ACTION_STORAGE_MODE', 'local')
    monkeypatch.setenv('INFO2ACTION_ASSET_BACKEND', 'local')
    monkeypatch.setattr(db_mod, 'DB_PATH', str(tmp_path / 'feed.db'))
    db_mod._item_status_has_user_id = None
    conn = db_mod.get_conn()
    conn.close()

    import app as app_mod
    import middleware.auth as auth_mw
    import routes.config as config_route
    import routes.feed as feed_route
    import routes.health as health_route

    feed_route._trend_cache.clear()
    monkeypatch.setattr(auth_mw, '_AUTH_TOKEN', 'review-token')
    monkeypatch.setattr(app_mod, 'BASE', str(tmp_path))
    monkeypatch.setattr(config_route, 'BASE', str(tmp_path))
    monkeypatch.setattr(feed_route, 'BASE', str(tmp_path))
    monkeypatch.setattr(health_route, 'BASE', str(tmp_path))
    app_mod.app.state.limiter.enabled = False
    return TestClient(app_mod.app)


def test_auth_token_unset_does_not_disable_protection(public_boundary_client, monkeypatch):
    import middleware.auth as auth_mw

    monkeypatch.setattr(auth_mw, '_AUTH_TOKEN', '')

    resp = public_boundary_client.get('/api/cli/status')

    assert resp.status_code == 401


def test_config_read_and_write_are_not_anonymous(public_boundary_client, tmp_path):
    cfg_path = tmp_path / 'config' / 'config.json'
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({
        'ai_summary': {'api_key': 'secret-key'},
        'wechat_exporter': {'auth_key': 'wechat-secret'},
        'client': {'theme': 'light'},
    }), encoding='utf-8')

    read_resp = public_boundary_client.get('/api/config')
    write_resp = public_boundary_client.post('/api/config', json={'client': {'theme': 'evil'}})

    assert read_resp.status_code == 401
    assert write_resp.status_code == 401
    saved = json.loads(cfg_path.read_text(encoding='utf-8'))
    assert saved['ai_summary']['api_key'] == 'secret-key'


def test_health_credential_sync_is_not_anonymous(public_boundary_client):
    resp = public_boundary_client.post('/api/health/sync-credentials', json={'platform': 'twitter'})

    assert resp.status_code == 401


def test_feedback_routes_are_not_public_via_feed_prefix(public_boundary_client):
    conn = db_mod.get_conn()
    try:
        db_mod.batch_upsert(conn, [_make_item(id='fb-1')])
        db_mod.add_feedback(conn, 'fb-1', 'text', text='private free-text feedback')
    finally:
        conn.close()

    read_resp = public_boundary_client.get('/api/feedback')
    write_resp = public_boundary_client.post('/api/feedback', json={
        'item_id': 'fb-1',
        'type': 'text',
        'text': 'anonymous poison',
    })

    assert read_resp.status_code == 401
    assert write_resp.status_code == 401


def test_anonymous_feed_does_not_expose_manual_items(public_boundary_client):
    conn = db_mod.get_conn()
    try:
        db_mod.batch_upsert(conn, [
            _make_item(id='manual-secret', platform='manual', title='Private manual',
                       url='https://private.example/manual', content='secret body',
                       ai_summary='secret summary', ai_category='ai_tools'),
            _make_item(id='public-twitter', platform='twitter', title='Public tweet',
                       ai_category='ai_tools'),
        ])
        conn.execute(
            "UPDATE items SET ai_category='ai_tools', fetched_at=datetime('now'), published_at=datetime('now')"
        )
        conn.commit()
    finally:
        conn.close()

    feed_resp = public_boundary_client.get('/api/feed')
    detail_resp = public_boundary_client.get('/api/feed/item/manual-secret')
    stats_resp = public_boundary_client.get('/api/stats')
    trends_resp = public_boundary_client.get('/api/trends')

    assert feed_resp.status_code == 200
    assert [item['id'] for item in feed_resp.json()['items']] == ['public-twitter']
    assert detail_resp.status_code == 404
    assert 'manual' not in stats_resp.json()
    assert trends_resp.json()['item_count'] == 1


def test_public_static_handlers_confine_paths(public_boundary_client, tmp_path):
    data_dir = tmp_path / 'data'
    images_dir = data_dir / 'images'
    assets_dir = tmp_path / 'frontend-react' / 'dist' / 'assets'
    images_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / 'feed.db').write_bytes(b'SQLite format 3 secret db')
    (tmp_path / 'config').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'config' / 'config.json').write_text('{"ai_summary":{"api_key":"secret"}}',
                                                     encoding='utf-8')

    image_resp = public_boundary_client.get('/images/..%2Ffeed.db')
    asset_resp = public_boundary_client.get('/assets/..%2F..%2F..%2Fconfig%2Fconfig.json')

    assert image_resp.status_code == 404
    assert asset_resp.status_code == 404
