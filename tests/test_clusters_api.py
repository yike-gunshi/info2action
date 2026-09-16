"""Integration tests for src/routes/clusters.py (v15.0).

Covers 6 endpoints:
- GET  /api/feed/events
- GET  /api/clusters/{id}
- GET  /api/clusters/{id}/sources
- POST /api/clusters/{id}/click
- GET  /api/clusters/{id}/actions
- GET  /api/search?context=recommend|channel

(POST /api/clusters/{id}/actions SSE is smoke-tested separately to avoid
hitting live LLM.)
"""
import json
import os
import sys
import uuid

import bcrypt
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import db as db_mod  # noqa: E402

PASSWORD = 'password123'


@pytest.fixture()
def clusters_env(monkeypatch, tmp_path):
    monkeypatch.setenv('JWT_SECRET', 'clusters-test-secret-ent-enough-32-char!')
    monkeypatch.setenv('RATELIMIT_ENABLED', 'false')
    monkeypatch.setenv('INFO2ACTION_DATA_AUTHORITY', 'local')
    monkeypatch.setenv('INFO2ACTION_READ_BACKEND', 'sqlite')
    monkeypatch.setenv('INFO2ACTION_EVENT_READ_BACKEND', 'sqlite')
    monkeypatch.setenv('INFO2ACTION_FEED_READ_BACKEND', 'sqlite')
    monkeypatch.setenv('INFO2ACTION_STATUS_BACKEND', 'sqlite')
    monkeypatch.setenv('INFO2ACTION_APP_STATE_BACKEND', 'sqlite')
    monkeypatch.setattr(db_mod, 'DB_PATH', str(tmp_path / 'clusters.db'))
    db_mod._item_status_has_user_id = None
    cfg_dir = tmp_path / 'config'
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / 'config.json').write_text(
        json.dumps({
            'global': {'event_aggregation_ready': False},
            'display': {'github_min_stars': 50},
        }),
        encoding='utf-8',
    )

    conn = db_mod.get_conn()
    try:
        user_id = str(uuid.uuid4())
        hashed = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
        db_mod.create_user(conn, user_id, 'alice', 'alice@test.local', hashed, role='user')
        db_mod.update_user(conn, user_id, email_verified=1)

        # Seed: 2 visible clusters with unique_source_count>=2, 1 invisible
        # (unique_source_count<2). v15.1 visibility threshold rebased on
        # unique_source_count (PRD §5.17).
        c_ids = []
        for idx, (title, summary, doc_count, first_at, usc) in enumerate([
            ('OpenAI 发布 Claude-style', 'OpenAI 官博宣布...', 3, '2026-04-24T10:00:00', 2),
            ('Cursor 更新 1.0', 'Cursor 1.0 正式发布...', 2, '2026-04-24T08:00:00', 2),
            ('Singleton Event', 'Only one source', 1, '2026-04-24T07:00:00', 1),
        ]):
            is_vis = 1 if usc >= 2 else 0
            cur = conn.execute(
                """INSERT INTO clusters (ai_title, ai_summary, ai_key_points,
                                         doc_count, unique_source_count,
                                         is_visible_in_feed, first_doc_at,
                                         last_doc_at, last_updated_at, published_at,
                                         live_version, platforms_json, cover_url)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, 1, ?, ?)""",
                (title, summary, json.dumps(['kp1', 'kp2']), doc_count, usc,
                 is_vis, first_at, first_at, first_at,
                 json.dumps(['x', 'reddit']), None),
            )
            c_ids.append(cur.lastrowid)

        # Items for the first visible cluster (c_ids[0]): 3 items
        for i in range(3):
            iid = f'itm_{i}'
            conn.execute(
                """INSERT INTO items (id, platform, source, fetched_at, title,
                                      content, author_name, published_at, ai_summary,
                                      ai_category)
                   VALUES (?, ?, 'following', datetime('now'), ?, ?, ?, ?, ?, ?)""",
                (iid, 'x' if i < 2 else 'reddit', f'Item {i}',
                 f'body {i}', f'author_{i}',
                 f'2026-04-24T10:0{i}:00', f'summary {i}',
                 'products' if i < 2 else 'coding'),
            )
            conn.execute(
                """INSERT INTO cluster_items (cluster_id, item_id, rank_in_cluster,
                                              is_primary_source)
                   VALUES (?, ?, ?, ?)""",
                (c_ids[0], iid, i, 1 if i == 0 else 0),
            )
        # Item for second cluster (c_ids[1])
        conn.execute(
            """INSERT INTO items (id, platform, source, fetched_at, title,
                                  content, author_name, published_at, ai_summary,
                                  ai_category)
               VALUES ('itm_cur', 'x', 'following', datetime('now'),
                       'Cursor', 'Cursor body', 'cursor_team',
                       '2026-04-24T08:00:00', 'Cursor summary', 'coding')"""
        )
        conn.execute(
            """INSERT INTO cluster_items (cluster_id, item_id, is_primary_source)
               VALUES (?, 'itm_cur', 1)""",
            (c_ids[1],),
        )
        conn.commit()
    finally:
        conn.close()

    import app as app_mod
    import middleware.auth as auth_mw
    import routes.auth as auth_route
    import routes.clusters as clusters_route
    monkeypatch.setattr(auth_route, 'JWT_SECRET', 'clusters-test-secret-ent-enough-32-char!')
    auth_route.limiter.enabled = False
    auth_route.limiter._default_limits = []
    monkeypatch.setattr(auth_mw, '_AUTH_TOKEN', '')
    monkeypatch.setattr(clusters_route, 'BASE', str(tmp_path))
    app_mod.app.state.limiter.enabled = False

    return {
        'app': app_mod.app,
        'user_id': user_id,
        'clusters': c_ids,
    }


def _client(app, login='alice@test.local') -> TestClient:
    c = TestClient(app)
    resp = c.post('/api/auth/login', json={'login': login, 'password': PASSWORD})
    assert resp.status_code == 200, resp.text
    return c


class TestFeedEvents:
    def test_date_seek_returns_original_page_and_continues_without_skips(self, clusters_env):
        conn = db_mod.get_conn()
        try:
            conn.execute('UPDATE clusters SET is_visible_in_feed = 0')
            for index in range(45):
                day = '25' if index < 23 else '24'
                stamp = f'2026-04-{day}T{15 - index % 12:02d}:00:00'
                conn.execute(
                    """INSERT INTO clusters (ai_title, ai_summary, doc_count,
                        is_visible_in_feed, first_doc_at, last_doc_at,
                        last_updated_at, published_at, live_version, platforms_json)
                       VALUES (?, 'summary', 1, 1, ?, ?, datetime('now'), ?, 1, '[]')""",
                    (f'date seek {index}', stamp, stamp, stamp),
                )
            conn.commit()
        finally:
            conn.close()
        client = TestClient(clusters_env['app'])
        pages = [client.get(f'/api/feed/events?page={page}').json() for page in (1, 2, 3)]
        expected = [event for page in pages for event in page['events']]
        target = expected[23]
        seek = client.get('/api/feed/events?target_date=2026-04-24').json()
        assert seek.get('date_seek') == {
            'requested_date': '2026-04-24', 'status': 'found', 'anchor_event_id': target['id'],
        }
        assert seek['events'] == pages[1]['events']
        assert seek['date_counts'] == pages[0]['date_counts'] == {'2026-04-25': 23, '2026-04-24': 22}
        assert seek['next_cursor'] == 3
        continuation = client.get(f"/api/feed/events?page={seek['next_cursor']}").json()
        assert [event['id'] for event in seek['events'] + continuation['events']] == [
            event['id'] for event in expected[20:]
        ]
        assert 'date_seek' not in pages[0]

    def test_date_seek_keeps_categories_and_beijing_day_boundary(self, clusters_env):
        conn = db_mod.get_conn()
        try:
            conn.execute("UPDATE clusters SET first_doc_at = '2026-04-24T16:00:00Z' WHERE id = ?", (clusters_env['clusters'][0],))
            conn.execute("UPDATE clusters SET first_doc_at = '2026-04-24T15:59:59Z' WHERE id = ?", (clusters_env['clusters'][1],))
            conn.commit()
        finally:
            conn.close()
        client = TestClient(clusters_env['app'])
        newer = client.get('/api/feed/events?target_date=2026-04-25&categories=products,coding').json()
        older = client.get('/api/feed/events?target_date=2026-04-24&categories=coding').json()
        assert newer.get('date_seek', {}).get('anchor_event_id') == clusters_env['clusters'][0]
        assert older.get('date_seek', {}).get('anchor_event_id') == clusters_env['clusters'][1]
        missing = client.get('/api/feed/events?target_date=2026-04-24&categories=products').json()
        assert missing.get('date_seek') == {
            'requested_date': '2026-04-24', 'status': 'not_found', 'anchor_event_id': None,
        }
        assert missing['events'] == []
        assert missing['next_cursor'] is None
        assert missing['date_counts'] == {'2026-04-25': 1}

    @pytest.mark.parametrize('value', ['garbage', '2026-02-30', '2026-04-24T00:00:00', '0'])
    def test_date_seek_rejects_invalid_date(self, clusters_env, value):
        response = TestClient(clusters_env['app']).get('/api/feed/events', params={'target_date': value})
        assert response.status_code == 422

    def test_date_seek_remote_isolates_public_first_page_cache(self, clusters_env, monkeypatch):
        import routes.clusters as route
        from routes.public_response_cache import clear_public_response_cache
        clear_public_response_cache()
        calls = []

        def fetch(**kwargs):
            calls.append(kwargs)
            result = {'enabled': True, 'events': [], 'next_cursor': None, 'date_counts': {}}
            if kwargs.get('target_date'):
                result['date_seek'] = {'requested_date': kwargs['target_date'], 'status': 'not_found', 'anchor_event_id': None}
            return result

        monkeypatch.setattr(route.remote_db, 'events_read_from_remote', lambda: True)
        monkeypatch.setattr(route.remote_db, 'fetch_events', fetch)
        client = TestClient(clusters_env['app'])
        first = client.get('/api/feed/events')
        seek = client.get('/api/feed/events?target_date=2026-04-24')
        again = client.get('/api/feed/events')
        assert len(calls) == 2
        assert calls[1]['target_date'] == '2026-04-24'
        assert calls[1]['timezone_offset_minutes'] == -480
        assert seek.json()['date_seek']['status'] == 'not_found'
        assert seek.headers['Cache-Control'] == 'no-store'
        assert first.json() == again.json()
        assert 'date_seek' not in again.json()
        clear_public_response_cache()

    @pytest.mark.parametrize('error_type', ['TimeoutError', 'RemoteDBTimeoutError', 'RemoteDBError'])
    def test_date_seek_timeout_does_not_pretend_date_is_missing(self, clusters_env, monkeypatch, error_type):
        import routes.clusters as route

        def timeout(**kwargs):
            exception = TimeoutError if error_type == 'TimeoutError' else getattr(route.remote_db, error_type)
            raise exception('date lookup timed out')

        monkeypatch.setattr(route.remote_db, 'events_read_from_remote', lambda: True)
        monkeypatch.setattr(route.remote_db, 'fetch_events', timeout)
        response = TestClient(clusters_env['app']).get('/api/feed/events?target_date=2026-04-24')
        assert response.status_code == 503
        assert response.json().get('date_seek') is None
        assert response.headers.get('Cache-Control') == 'no-store'

    def test_anonymous_can_read_public_events(self, clusters_env):
        c = TestClient(clusters_env['app'])
        r = c.get('/api/feed/events')
        assert r.status_code == 200
        body = r.json()
        assert len(body['events']) == 2
        assert all(ev['has_update'] is False for ev in body['events'])
        assert body['date_counts'] == {'2026-04-24': 2}

    def test_date_counts_use_full_filtered_result_not_current_page(self, clusters_env):
        c = TestClient(clusters_env['app'])
        r = c.get('/api/feed/events?limit=1&timezone_offset_minutes=-480')
        assert r.status_code == 200
        body = r.json()
        assert len(body['events']) == 1
        assert body['next_cursor'] == 2
        assert body['date_counts']['2026-04-24'] == 2

    def test_anonymous_events_exclude_clusters_with_private_members(self, clusters_env):
        conn = db_mod.get_conn()
        try:
            cur = conn.execute(
                """INSERT INTO clusters (ai_title, ai_summary, ai_key_points,
                                         doc_count, is_visible_in_feed, first_doc_at,
                                         last_doc_at, last_updated_at, published_at,
                                         live_version,
                                         platforms_json, cover_url)
                   VALUES ('Private manual event', 'private summary', '[]',
                           2, 1, '2026-04-24T11:00:00',
                           '2026-04-24T11:00:00', datetime('now'),
                           '2026-04-24T11:00:00', 1, '["manual","x"]', NULL)"""
            )
            cid = cur.lastrowid
            conn.execute(
                """INSERT INTO items (id, user_id, platform, source, fetched_at,
                                      title, content, author_name, published_at,
                                      ai_summary)
                   VALUES ('manual-private', ?, 'manual', 'user-submit',
                           datetime('now'), 'Manual private', 'secret body',
                           'alice', '2026-04-24T11:00:00', 'secret summary')""",
                (clusters_env['user_id'],),
            )
            conn.execute(
                """INSERT INTO items (id, platform, source, fetched_at, title,
                                      content, author_name, published_at,
                                      ai_summary)
                   VALUES ('public-in-private-cluster', 'x', 'following',
                           datetime('now'), 'Public side', 'public body',
                           'bob', '2026-04-24T11:01:00', 'public summary')"""
            )
            conn.execute(
                """INSERT INTO cluster_items (cluster_id, item_id,
                                              rank_in_cluster, is_primary_source)
                   VALUES (?, 'manual-private', 0, 1)""",
                (cid,),
            )
            conn.execute(
                """INSERT INTO cluster_items (cluster_id, item_id,
                                              rank_in_cluster, is_primary_source)
                   VALUES (?, 'public-in-private-cluster', 1, 0)""",
                (cid,),
            )
            conn.commit()
        finally:
            conn.close()

        c = TestClient(clusters_env['app'])
        r = c.get('/api/feed/events')
        assert r.status_code == 200
        titles = [ev['ai_title'] for ev in r.json()['events']]
        assert 'Private manual event' not in titles

    def test_returns_only_visible_clusters(self, clusters_env):
        c = _client(clusters_env['app'])
        r = c.get('/api/feed/events')
        assert r.status_code == 200
        body = r.json()
        assert 'events' in body
        events = body['events']
        assert len(events) == 2  # singleton excluded
        # Each event has required fields
        ev0 = events[0]
        for k in ('id', 'ai_title', 'ai_summary', 'doc_count', 'first_doc_at', 'platforms'):
            assert k in ev0, f'missing field {k}'
        assert ev0['doc_count'] >= 2
        assert ev0['ai_summary'] == 'OpenAI 官博宣布...'
        assert ev0['category'] == 'products'
        assert ev0['source_preview'] == [
            {'platform': 'x', 'author': 'author_0', 'source': 'following'},
            {'platform': 'x', 'author': 'author_1', 'source': 'following'},
            {'platform': 'reddit', 'author': 'author_2', 'source': 'following'},
        ]

    def test_event_cover_falls_back_to_member_item_cover(self, clusters_env):
        conn = db_mod.get_conn()
        try:
            conn.execute(
                "UPDATE items SET cover_url = '/images/events/member-cover.jpg' WHERE id = 'itm_1'"
            )
            conn.commit()
        finally:
            conn.close()

        c = _client(clusters_env['app'])
        r = c.get('/api/feed/events')
        assert r.status_code == 200
        event = next(ev for ev in r.json()['events'] if ev['id'] == clusters_env['clusters'][0])
        assert event['cover_url'] == '/images/events/member-cover.jpg'

    def test_sort_first_doc_at_desc_ignores_fresh_last_doc_at(self, clusters_env):
        conn = db_mod.get_conn()
        try:
            conn.execute(
                """INSERT INTO clusters (ai_title, ai_summary, ai_key_points,
                                         doc_count, unique_source_count,
                                         is_visible_in_feed, first_doc_at,
                                         last_doc_at, last_updated_at, published_at,
                                         live_version, platforms_json, cover_url)
                   VALUES ('Older event with fresh update', 'fresh summary', '[]',
                           2, 2, 1, '2026-04-20T01:00:00Z',
                           '2026-04-26 09:35', datetime('now'),
                           '2026-04-20T01:00:00Z', 1, '["x"]', NULL)"""
            )
            conn.commit()
        finally:
            conn.close()

        c = _client(clusters_env['app'])
        r = c.get('/api/feed/events')
        events = r.json()['events']
        titles = [ev['ai_title'] for ev in events]
        assert titles.index('Older event with fresh update') > titles.index('OpenAI 发布 Claude-style')
        old_event = next(ev for ev in events if ev['ai_title'] == 'Older event with fresh update')
        assert old_event['first_doc_at'] == '2026-04-20T01:00:00Z'
        assert old_event['last_doc_at'] == '2026-04-26T01:35:00Z'

    def test_fetched_since_filters_to_recently_touched_clusters(self, clusters_env):
        c = _client(clusters_env['app'])

        body = c.get('/api/feed/events?fetched_since=2099-01-01').json()

        assert body['events'] == []
        assert body['total_available_within_30d'] == 0

    def test_github_only_low_star_event_is_hidden(self, clusters_env):
        conn = db_mod.get_conn()
        try:
            rows = [
                ('Low star GitHub repo', 'gh-low', 'github', '{"stars":49}'),
                ('High star GitHub repo', 'gh-high', 'github', '{"stars":50}'),
                ('Mixed source GitHub event', 'gh-mixed', 'github', '{"stars":1}'),
                ('Mixed source GitHub event', 'x-mixed', 'x', '{}'),
            ]
            cluster_ids = {}
            for title in {r[0] for r in rows}:
                cur = conn.execute(
                    """INSERT INTO clusters (ai_title, ai_summary, ai_key_points,
                                             doc_count, unique_source_count,
                                             is_visible_in_feed, first_doc_at,
                                             last_doc_at, last_updated_at,
                                             published_at, live_version,
                                             platforms_json, cover_url)
                       VALUES (?, 'summary', '[]', 1, 1, 1,
                               datetime('now'),
                               datetime('now'),
                               datetime('now'),
                               datetime('now'),
                               1, '["github"]', NULL)""",
                    (title,),
                )
                cluster_ids[title] = cur.lastrowid
            for title, item_id, platform, metrics in rows:
                conn.execute(
                    """INSERT INTO items (id, platform, source, fetched_at, title,
                                          content, author_name, published_at,
                                          ai_summary, metrics_json)
                       VALUES (?, ?, 'trending', datetime('now'), ?, 'body',
                               'author', datetime('now'),
                               'item summary', ?)""",
                    (item_id, platform, item_id, metrics),
                )
                conn.execute(
                    """INSERT INTO cluster_items (cluster_id, item_id,
                                                  rank_in_cluster, is_primary_source)
                       VALUES (?, ?, 0, 1)""",
                    (cluster_ids[title], item_id),
                )
            conn.commit()
        finally:
            conn.close()

        c = _client(clusters_env['app'])
        titles = [ev['ai_title'] for ev in c.get('/api/feed/events').json()['events']]

        assert 'Low star GitHub repo' not in titles
        assert 'High star GitHub repo' in titles
        assert 'Mixed source GitHub event' in titles

    def test_cluster_sources_sort_new_docs_first(self, clusters_env):
        cid = clusters_env['clusters'][0]
        conn = db_mod.get_conn()
        try:
            conn.execute(
                """INSERT INTO items (id, platform, source, fetched_at, title,
                                      content, author_name, published_at, ai_summary)
                   VALUES ('fresh-source', 'x', 'following', datetime('now'),
                           'Fresh source', 'fresh body', 'fresh_author',
                           '2026-04-26 09:35', 'fresh summary')"""
            )
            conn.execute(
                """INSERT INTO cluster_items (cluster_id, item_id, rank_in_cluster,
                                              is_primary_source)
                   VALUES (?, 'fresh-source', 9999, 0)""",
                (cid,),
            )
            conn.commit()
        finally:
            conn.close()

        c = _client(clusters_env['app'])
        r = c.get(f'/api/clusters/{cid}/sources')
        sources = r.json()['sources']
        assert sources[0]['item_id'] == 'fresh-source'
        assert sources[0]['published_at'] == '2026-04-26T01:35:00Z'

    def test_enabled_flag_from_config(self, clusters_env):
        c = _client(clusters_env['app'])
        body = c.get('/api/feed/events').json()
        # config.global.event_aggregation_ready is False by default → enabled=False
        assert body.get('enabled') is False

    def test_events_sort_by_first_doc_at_not_last_doc_at(self, clusters_env):
        conn = db_mod.get_conn()
        try:
            old_updated = conn.execute(
                """INSERT INTO clusters (ai_title, ai_summary, ai_key_points,
                                         doc_count, unique_source_count,
                                         is_visible_in_feed, first_doc_at,
                                         last_doc_at, last_updated_at,
                                         live_version, platforms_json, cover_url,
                                         published_at)
                   VALUES ('旧事件新增来源', 'old update', '[]',
                           4, 4, 1, datetime('now', '-5 days'),
                           datetime('now'), datetime('now'),
                           3, '["x"]', NULL, datetime('now'))"""
            ).lastrowid
            fresh = conn.execute(
                """INSERT INTO clusters (ai_title, ai_summary, ai_key_points,
                                         doc_count, unique_source_count,
                                         is_visible_in_feed, first_doc_at,
                                         last_doc_at, last_updated_at,
                                         live_version, platforms_json, cover_url,
                                         published_at)
                   VALUES ('新首发事件', 'fresh', '[]',
                           2, 2, 1, datetime('now', '-1 day'),
                           datetime('now', '-1 day', '+15 minutes'), datetime('now', '-1 day', '+15 minutes'),
                           1, '["reddit"]', NULL, datetime('now', '-1 day', '+20 minutes'))"""
            ).lastrowid
            conn.commit()
        finally:
            conn.close()

        c = TestClient(clusters_env['app'])
        events = c.get('/api/feed/events?limit=20').json()['events']
        ids = [ev['id'] for ev in events]
        assert ids.index(fresh) < ids.index(old_updated)

    def test_has_update_false_when_never_clicked(self, clusters_env):
        c = _client(clusters_env['app'])
        events = c.get('/api/feed/events').json()['events']
        # First-time viewer: has_update must be False for all (R9.3 boundary)
        assert all(ev['has_update'] is False for ev in events)


class TestClusterDetail:
    def test_anonymous_can_read_public_cluster_detail(self, clusters_env):
        cid = clusters_env['clusters'][0]
        c = TestClient(clusters_env['app'])
        r = c.get(f'/api/clusters/{cid}')
        assert r.status_code == 200
        assert r.json()['id'] == cid

    def test_detail_returns_all_fields(self, clusters_env):
        cid = clusters_env['clusters'][0]
        c = _client(clusters_env['app'])
        r = c.get(f'/api/clusters/{cid}')
        assert r.status_code == 200
        b = r.json()
        for k in ('id', 'ai_title', 'ai_summary', 'ai_key_points', 'doc_count',
                  'platforms', 'first_doc_at', 'live_version', 'why_read'):
            assert k in b

    def test_detail_and_bundle_cover_fall_back_to_member_item_cover(self, clusters_env):
        cid = clusters_env['clusters'][0]
        conn = db_mod.get_conn()
        try:
            conn.execute(
                "UPDATE clusters SET cover_url = NULL WHERE id = ?",
                (cid,),
            )
            conn.execute(
                "UPDATE items SET cover_url = '/images/events/member-cover.jpg' WHERE id = 'itm_1'"
            )
            conn.commit()
        finally:
            conn.close()

        c = _client(clusters_env['app'])
        detail = c.get(f'/api/clusters/{cid}')
        assert detail.status_code == 200
        assert detail.json()['cover_url'] == '/images/events/member-cover.jpg'

        bundle = c.get(f'/api/clusters/{cid}/bundle')
        assert bundle.status_code == 200
        assert bundle.json()['cluster']['cover_url'] == '/images/events/member-cover.jpg'
        assert 'why_read' in bundle.json()['cluster']

    def test_not_found_returns_404(self, clusters_env):
        c = _client(clusters_env['app'])
        r = c.get('/api/clusters/99999')
        assert r.status_code == 404

    def test_merged_into_redirect(self, clusters_env, monkeypatch):
        """R8.3: cluster A merged into B → response has redirect_to=B."""
        cid_a, cid_b = clusters_env['clusters'][0], clusters_env['clusters'][1]
        conn = db_mod.get_conn()
        conn.execute(
            "UPDATE clusters SET merged_into = ?, is_visible_in_feed = 0 WHERE id = ?",
            (cid_b, cid_a),
        )
        conn.commit()
        conn.close()
        c = _client(clusters_env['app'])
        r = c.get(f'/api/clusters/{cid_a}')
        assert r.status_code == 200
        assert r.json().get('redirect_to') == cid_b

    def test_detail_returns_cluster_viewer_status(self, clusters_env):
        cid = clusters_env['clusters'][0]
        conn = db_mod.get_conn()
        try:
            conn.execute(
                """INSERT INTO cluster_status (user_id, cluster_id, clicked_at,
                                               last_seen_version, starred_at)
                   VALUES (?, ?, '2026-05-25T09:00:00', 3, '2026-05-25T09:10:00')""",
                (clusters_env['user_id'], cid),
            )
            conn.commit()
        finally:
            conn.close()

        c = _client(clusters_env['app'])
        r = c.get(f'/api/clusters/{cid}')
        assert r.status_code == 200
        body = r.json()
        assert body['user_last_seen_version'] == 3
        assert body['viewer_status'] == {
            'clicked_at': '2026-05-25T01:00:00Z',
            'last_seen_version': 3,
            'starred_at': '2026-05-25T01:10:00Z',
            'feedback_kind': None,  # v25.0 F-D
            'feedback_note': None,
        }

    def test_bundle_normalizes_video_media_and_keeps_image_compat(self, clusters_env):
        cid = clusters_env['clusters'][0]
        conn = db_mod.get_conn()
        try:
            conn.execute(
                """UPDATE items
                      SET cover_url = '/images/reddit/poster.jpg',
                          media_json = ?
                    WHERE id = 'itm_1'""",
                (json.dumps([{
                    'type': 'video',
                    'url': 'https://v.redd.it/demo/DASH_720.mp4',
                    'poster_url': '/images/reddit/poster.jpg',
                    'provider': 'reddit',
                    'source_url': 'https://reddit.com/r/demo/comments/abc',
                }]),),
            )
            conn.commit()
        finally:
            conn.close()

        body = _client(clusters_env['app']).get(f'/api/clusters/{cid}/bundle').json()
        source = next(row for row in body['sources'] if row['item_id'] == 'itm_1')
        assert source['media'][0] == {
            'type': 'video',
            'url': 'https://v.redd.it/demo/DASH_720.mp4',
            'poster_url': '/images/reddit/poster.jpg',
            'provider': 'reddit',
            'source_url': 'https://reddit.com/r/demo/comments/abc',
        }
        assert source['media_urls'] == ['/images/reddit/poster.jpg']
        assert body['cluster']['media'][0]['type'] == 'video'


class TestV30ReadingState:
    def test_batch_status_requires_login_and_limits_500(self, clusters_env):
        anonymous = TestClient(clusters_env['app'])
        assert anonymous.post('/api/clusters/status/batch', json={'cluster_ids': [1]}).status_code == 401
        logged_in = _client(clusters_env['app'])
        response = logged_in.post(
            '/api/clusters/status/batch',
            json={'cluster_ids': list(range(501))},
        )
        assert response.status_code == 400

    def test_batch_status_returns_read_and_unread_rows(self, clusters_env):
        first, second = clusters_env['clusters'][:2]
        client = _client(clusters_env['app'])
        assert client.post(f'/api/clusters/{first}/click').status_code == 200

        response = client.post(
            '/api/clusters/status/batch',
            json={'cluster_ids': [first, second, first]},
        )
        assert response.status_code == 200
        statuses = {row['cluster_id']: row for row in response.json()['statuses']}
        assert statuses[first]['clicked_at'] is not None
        assert statuses[first]['last_seen_version'] == 1
        assert statuses[second] == {
            'cluster_id': second,
            'clicked_at': None,
            'last_seen_version': None,
        }

    def test_reading_progress_put_get_and_does_not_mark_read(self, clusters_env):
        cid = clusters_env['clusters'][0]
        conn = db_mod.get_conn()
        try:
            conn.execute(
                """UPDATE clusters
                      SET first_doc_at=datetime('now'), last_updated_at=datetime('now'),
                          published_at=datetime('now'), is_visible_in_feed=1
                    WHERE id=?""",
                (cid,),
            )
            conn.commit()
        finally:
            conn.close()

        client = _client(clusters_env['app'])
        saved = client.put('/api/reading-progress/highlights', json={'cluster_id': cid})
        assert saved.status_code == 200
        progress = saved.json()['progress']
        assert progress['cluster_id'] == cid
        assert progress['resolved_cluster_id'] == cid
        assert progress['resolution'] == 'exact'

        loaded = client.get('/api/reading-progress/highlights')
        assert loaded.status_code == 200
        assert loaded.json()['progress']['resolved_cluster_id'] == cid

        conn = db_mod.get_conn()
        try:
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM cluster_status WHERE user_id=? AND cluster_id=?",
                (clusters_env['user_id'], cid),
            ).fetchone()['n'] == 0
        finally:
            conn.close()

    def test_reading_progress_resolves_hidden_anchor_to_nearest_older(self, clusters_env):
        anchor, older = clusters_env['clusters'][:2]
        conn = db_mod.get_conn()
        try:
            conn.execute(
                """UPDATE clusters SET first_doc_at=datetime('now'),
                       last_updated_at=datetime('now'), published_at=datetime('now')
                     WHERE id=?""",
                (anchor,),
            )
            conn.execute(
                """UPDATE clusters SET first_doc_at=datetime('now','-1 hour'),
                       last_updated_at=datetime('now','-1 hour'),
                       published_at=datetime('now','-1 hour'), is_visible_in_feed=1
                     WHERE id=?""",
                (older,),
            )
            conn.commit()
        finally:
            conn.close()

        client = _client(clusters_env['app'])
        assert client.put('/api/reading-progress/highlights', json={'cluster_id': anchor}).status_code == 200
        conn = db_mod.get_conn()
        try:
            conn.execute('UPDATE clusters SET is_visible_in_feed=0 WHERE id=?', (anchor,))
            conn.commit()
        finally:
            conn.close()

        progress = client.get('/api/reading-progress/highlights').json()['progress']
        assert progress['cluster_id'] == anchor
        assert progress['resolved_cluster_id'] == older
        assert progress['resolution'] == 'nearest_older'


class TestClusterSources:
    def test_anonymous_can_read_public_cluster_sources(self, clusters_env):
        cid = clusters_env['clusters'][0]
        c = TestClient(clusters_env['app'])
        r = c.get(f'/api/clusters/{cid}/sources')
        assert r.status_code == 200
        assert len(r.json()['sources']) == 3

    def test_sources_list_returns_member_items(self, clusters_env):
        cid = clusters_env['clusters'][0]
        c = _client(clusters_env['app'])
        r = c.get(f'/api/clusters/{cid}/sources')
        assert r.status_code == 200
        sources = r.json()['sources']
        assert len(sources) == 3  # 3 items seeded
        # Source list is newest-first, with primary source as tie-breaker.
        assert sources[0]['item_id'] == 'itm_2'

    def test_logged_in_sources_do_not_expose_another_users_private_media(self, clusters_env):
        cid = clusters_env['clusters'][0]
        conn = db_mod.get_conn()
        try:
            conn.execute(
                """INSERT INTO items
                     (id,user_id,platform,source,fetched_at,title,content,url,media_json)
                   VALUES
                     ('private-other','other-user','manual','user-submit',datetime('now'),
                      'Private video','secret','https://example.com/private',
                      '[{"type":"video","url":"https://cdn.example/private.mp4"}]')"""
            )
            conn.execute(
                """INSERT INTO cluster_items(cluster_id,item_id,rank_in_cluster,is_primary_source)
                   VALUES (?, 'private-other', 99, 0)""",
                (cid,),
            )
            conn.commit()
        finally:
            conn.close()

        sources = _client(clusters_env['app']).get(
            f'/api/clusters/{cid}/sources'
        ).json()['sources']

        assert 'private-other' not in {source['item_id'] for source in sources}
        assert 'https://cdn.example/private.mp4' not in json.dumps(sources)

    def test_pagination_query(self, clusters_env):
        cid = clusters_env['clusters'][0]
        c = _client(clusters_env['app'])
        r = c.get(f'/api/clusters/{cid}/sources?limit=2&page=1')
        assert r.status_code == 200
        assert len(r.json()['sources']) == 2


class TestClusterClick:
    def test_anonymous_click_is_public_noop(self, clusters_env):
        cid = clusters_env['clusters'][0]
        c = TestClient(clusters_env['app'])
        r = c.post(f'/api/clusters/{cid}/click')
        assert r.status_code == 200
        assert r.json()['ok'] is True
        assert r.json()['last_seen_version'] == 0
        conn = db_mod.get_conn()
        try:
            cnt = conn.execute(
                "SELECT COUNT(*) AS n FROM cluster_status WHERE cluster_id=?",
                (cid,),
            ).fetchone()['n']
            assert cnt == 0
        finally:
            conn.close()

    def test_click_writes_cluster_status_only(self, clusters_env):
        cid = clusters_env['clusters'][0]
        c = _client(clusters_env['app'])
        r = c.post(f'/api/clusters/{cid}/click')
        assert r.status_code == 200

        conn = db_mod.get_conn()
        try:
            row = conn.execute(
                "SELECT clicked_at, last_seen_version FROM cluster_status "
                "WHERE user_id=? AND cluster_id=?",
                (clusters_env['user_id'], cid),
            ).fetchone()
            assert row is not None
            assert row['clicked_at'] is not None
            # Does NOT touch item_status for members
            members = conn.execute(
                "SELECT COUNT(*) AS n FROM item_status WHERE clicked_at IS NOT NULL"
            ).fetchone()['n']
            assert members == 0
        finally:
            conn.close()


class TestClusterFeedback:
    """v25.0 F-D — cluster 级质量反馈：只落库不改排序，幂等可撤销。"""

    def test_anonymous_feedback_requires_login_and_does_not_write(self, clusters_env):
        cid = clusters_env['clusters'][0]
        c = TestClient(clusters_env['app'])
        r = c.post(f'/api/clusters/{cid}/feedback', json={'kind': 'low_quality'})
        assert r.status_code == 401

        conn = db_mod.get_conn()
        try:
            cnt = conn.execute(
                "SELECT COUNT(*) AS n FROM cluster_status WHERE cluster_id=?",
                (cid,),
            ).fetchone()['n']
            assert cnt == 0
        finally:
            conn.close()

    def test_invalid_kind_rejected(self, clusters_env):
        cid = clusters_env['clusters'][0]
        c = _client(clusters_env['app'])
        r = c.post(f'/api/clusters/{cid}/feedback', json={'kind': 'meh'})
        assert r.status_code == 400

    @pytest.mark.parametrize('note', [501 * '字', 123, ['reason']])
    def test_invalid_feedback_note_rejected(self, clusters_env, note):
        cid = clusters_env['clusters'][0]
        c = _client(clusters_env['app'])
        r = c.post(
            f'/api/clusters/{cid}/feedback',
            json={'kind': 'should_feature', 'note': note},
        )
        assert r.status_code == 400

    def test_missing_cluster_404(self, clusters_env):
        c = _client(clusters_env['app'])
        r = c.post('/api/clusters/999999/feedback', json={'kind': 'irrelevant'})
        assert r.status_code == 404

    @pytest.mark.parametrize('kind', ['low_quality', 'should_feature'])
    def test_feedback_writes_then_same_kind_revokes(self, clusters_env, kind):
        cid = clusters_env['clusters'][0]
        c = _client(clusters_env['app'])

        first = c.post(f'/api/clusters/{cid}/feedback', json={'kind': kind})
        assert first.status_code == 200
        assert first.json()['ok'] is True
        assert first.json()['feedback_kind'] == kind

        conn = db_mod.get_conn()
        try:
            row = conn.execute(
                "SELECT feedback_kind, feedback_at, feedback_note, starred_at FROM cluster_status "
                "WHERE user_id=? AND cluster_id=?",
                (clusters_env['user_id'], cid),
            ).fetchone()
            assert row is not None
            assert row['feedback_kind'] == kind
            assert row['feedback_at'] is not None
            assert row['feedback_note'] is None
            assert row['starred_at'] is None
        finally:
            conn.close()

        # 再点同 kind = 撤销
        second = c.post(f'/api/clusters/{cid}/feedback', json={'kind': kind})
        assert second.status_code == 200
        assert second.json()['feedback_kind'] is None

        conn = db_mod.get_conn()
        try:
            row = conn.execute(
                "SELECT feedback_kind FROM cluster_status WHERE user_id=? AND cluster_id=?",
                (clusters_env['user_id'], cid),
            ).fetchone()
            assert row['feedback_kind'] is None
        finally:
            conn.close()

    def test_feedback_note_is_trimmed_exposed_and_cleared_on_revoke(self, clusters_env):
        cid = clusters_env['clusters'][0]
        c = _client(clusters_env['app'])

        first = c.post(
            f'/api/clusters/{cid}/feedback',
            json={'kind': 'should_feature', 'note': '  这条补充了关键上下文  '},
        )
        assert first.status_code == 200
        assert first.json()['feedback_note'] == '这条补充了关键上下文'

        conn = db_mod.get_conn()
        try:
            row = conn.execute(
                "SELECT feedback_kind, feedback_note FROM cluster_status "
                "WHERE user_id=? AND cluster_id=?",
                (clusters_env['user_id'], cid),
            ).fetchone()
            assert row['feedback_kind'] == 'should_feature'
            assert row['feedback_note'] == '这条补充了关键上下文'
        finally:
            conn.close()

        detail = c.get(f'/api/clusters/{cid}')
        assert detail.status_code == 200
        assert detail.json()['viewer_status']['feedback_note'] == '这条补充了关键上下文'

        revoked = c.post(
            f'/api/clusters/{cid}/feedback',
            json={'kind': 'should_feature'},
        )
        assert revoked.status_code == 200
        assert revoked.json() == {
            'ok': True,
            'feedback_kind': None,
            'feedback_note': None,
        }

        conn = db_mod.get_conn()
        try:
            row = conn.execute(
                "SELECT feedback_kind, feedback_note FROM cluster_status "
                "WHERE user_id=? AND cluster_id=?",
                (clusters_env['user_id'], cid),
            ).fetchone()
            assert row['feedback_kind'] is None
            assert row['feedback_note'] is None
        finally:
            conn.close()

    def test_feedback_switch_kind_updates_no_duplicate_rows(self, clusters_env):
        cid = clusters_env['clusters'][0]
        c = _client(clusters_env['app'])

        c.post(f'/api/clusters/{cid}/feedback', json={'kind': 'irrelevant'})
        r = c.post(f'/api/clusters/{cid}/feedback', json={'kind': 'low_quality'})
        assert r.status_code == 200
        assert r.json()['feedback_kind'] == 'low_quality'

        conn = db_mod.get_conn()
        try:
            rows = conn.execute(
                "SELECT COUNT(*) AS n FROM cluster_status WHERE user_id=? AND cluster_id=?",
                (clusters_env['user_id'], cid),
            ).fetchone()['n']
            assert rows == 1
        finally:
            conn.close()


class TestClusterStarAndLibrary:
    def test_anonymous_star_requires_login_and_does_not_write(self, clusters_env):
        cid = clusters_env['clusters'][0]
        c = TestClient(clusters_env['app'])
        r = c.post(f'/api/clusters/{cid}/star')
        assert r.status_code == 401

        conn = db_mod.get_conn()
        try:
            cnt = conn.execute(
                "SELECT COUNT(*) AS n FROM cluster_status WHERE cluster_id=?",
                (cid,),
            ).fetchone()['n']
            assert cnt == 0
        finally:
            conn.close()

    def test_star_toggles_cluster_status_without_touching_member_items(self, clusters_env):
        cid = clusters_env['clusters'][0]
        c = _client(clusters_env['app'])

        first = c.post(f'/api/clusters/{cid}/star')
        assert first.status_code == 200
        assert first.json()['ok'] is True
        assert first.json()['starred_at'] is not None

        conn = db_mod.get_conn()
        try:
            row = conn.execute(
                "SELECT starred_at, clicked_at, last_seen_version FROM cluster_status "
                "WHERE user_id=? AND cluster_id=?",
                (clusters_env['user_id'], cid),
            ).fetchone()
            assert row is not None
            assert row['starred_at'] is not None
            assert row['clicked_at'] is None
            assert row['last_seen_version'] == 0
            member_stars = conn.execute(
                "SELECT COUNT(*) AS n FROM item_status WHERE starred_at IS NOT NULL"
            ).fetchone()['n']
            assert member_stars == 0
        finally:
            conn.close()

        second = c.post(f'/api/clusters/{cid}/star')
        assert second.status_code == 200
        assert second.json() == {'ok': True, 'starred_at': None}

    def test_library_history_returns_items_and_clusters(self, clusters_env):
        cid = clusters_env['clusters'][0]
        conn = db_mod.get_conn()
        try:
            conn.execute(
                "INSERT INTO item_status (item_id, clicked_at) VALUES ('itm_1', '2026-05-25T08:30:00')"
            )
            conn.execute(
                """INSERT INTO cluster_status (user_id, cluster_id, clicked_at,
                                               last_seen_version)
                   VALUES (?, ?, '2026-05-25T09:00:00', 1)""",
                (clusters_env['user_id'], cid),
            )
            conn.commit()
        finally:
            conn.close()

        c = _client(clusters_env['app'])
        r = c.get('/api/library?view=history&limit=20')
        assert r.status_code == 200
        body = r.json()
        assert body['total'] == 2
        assert [entry['type'] for entry in body['entries']] == ['cluster', 'item']
        cluster_entry = body['entries'][0]
        assert cluster_entry['id'] == f'cluster:{cid}'
        assert cluster_entry['cluster']['id'] == cid
        assert 'why_read' in cluster_entry['cluster']
        assert cluster_entry['cluster']['viewer_status']['clicked_at'] == '2026-05-25T01:00:00Z'
        assert body['entries'][1]['item']['id'] == 'itm_1'

    def test_library_starred_returns_items_and_clusters(self, clusters_env):
        cid = clusters_env['clusters'][1]
        conn = db_mod.get_conn()
        try:
            conn.execute(
                "INSERT INTO item_status (item_id, starred_at) VALUES ('itm_0', '2026-05-25T08:00:00')"
            )
            conn.execute(
                """INSERT INTO cluster_status (user_id, cluster_id, starred_at)
                   VALUES (?, ?, '2026-05-25T10:00:00')""",
                (clusters_env['user_id'], cid),
            )
            conn.commit()
        finally:
            conn.close()

        c = _client(clusters_env['app'])
        r = c.get('/api/library?view=starred&limit=20')
        assert r.status_code == 200
        body = r.json()
        assert body['total'] == 2
        assert [entry['type'] for entry in body['entries']] == ['cluster', 'item']
        assert body['entries'][0]['cluster']['viewer_status']['starred_at'] == '2026-05-25T02:00:00Z'


class TestConfirmedEdgeFeedVisibility:
    """Confirmed-edge experiment: /api/feed/events trusts the pipeline-owned
    is_visible_in_feed bit instead of re-applying source-count gates."""

    def test_low_unique_sources_visible_when_pipeline_marks_visible(self, clusters_env):
        """The pipeline owns event validation; feed should not hide a visible
        cluster just because unique_source_count is below 2."""
        conn = db_mod.get_conn()
        try:
            cur = conn.execute(
                """INSERT INTO clusters (ai_title, ai_summary, ai_key_points,
                                         doc_count, unique_source_count,
                                         is_visible_in_feed, first_doc_at,
                                         last_doc_at, last_updated_at, published_at,
                                         live_version, platforms_json, cover_url)
                   VALUES ('5 reposts same source', 'echo', '[]',
                           5, 1, 1, '2026-04-25T10:00:00',
                           '2026-04-25T10:00:00', datetime('now'),
                           '2026-04-25T10:00:00', 1, '["x"]', NULL)"""
            )
            cid = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
        c = TestClient(clusters_env['app'])
        body = c.get('/api/feed/events').json()
        titles = [ev['ai_title'] for ev in body['events']]
        assert '5 reposts same source' in titles
        event = next(ev for ev in body['events']
                     if ev['ai_title'] == '5 reposts same source')
        assert event['unique_source_count'] == 1
        assert all('unique_source_count' in ev for ev in body['events'])

    def test_payload_includes_last_seen_version_field(self, clusters_env):
        c = _client(clusters_env['app'])
        body = c.get('/api/feed/events').json()
        # First-time viewer: last_seen_version=null for all events
        for ev in body['events']:
            assert 'last_seen_version' in ev
            assert ev['last_seen_version'] is None


class TestClusterSeen:
    """v15.1 §6.13 — POST /api/clusters/{id}/seen marks last_seen_version."""

    def test_anonymous_seen_is_public_noop(self, clusters_env):
        cid = clusters_env['clusters'][0]
        c = TestClient(clusters_env['app'])
        r = c.post(f'/api/clusters/{cid}/seen')
        assert r.status_code == 200
        assert r.json() == {'cluster_id': cid, 'last_seen_version': 0}
        conn = db_mod.get_conn()
        try:
            cnt = conn.execute(
                "SELECT COUNT(*) AS n FROM cluster_status WHERE cluster_id=?",
                (cid,),
            ).fetchone()['n']
            assert cnt == 0
        finally:
            conn.close()

    def test_anonymous_seen_missing_cluster_is_still_noop(self, clusters_env):
        c = TestClient(clusters_env['app'])
        r = c.post('/api/clusters/99999/seen')
        assert r.status_code == 200
        assert r.json() == {'cluster_id': 99999, 'last_seen_version': 0}

    def test_seen_404_when_cluster_missing(self, clusters_env):
        c = _client(clusters_env['app'])
        r = c.post('/api/clusters/99999/seen')
        assert r.status_code == 404

    def test_seen_writes_last_seen_version_at_current_live_version(self, clusters_env):
        cid = clusters_env['clusters'][0]
        # Bump live_version on this cluster so we can assert the marked value
        conn = db_mod.get_conn()
        try:
            conn.execute(
                "UPDATE clusters SET live_version = 7 WHERE id = ?", (cid,),
            )
            conn.commit()
        finally:
            conn.close()
        c = _client(clusters_env['app'])
        r = c.post(f'/api/clusters/{cid}/seen')
        assert r.status_code == 200
        body = r.json()
        assert body == {'cluster_id': cid, 'last_seen_version': 7}

        conn = db_mod.get_conn()
        try:
            row = conn.execute(
                "SELECT last_seen_version FROM cluster_status "
                "WHERE user_id=? AND cluster_id=?",
                (clusters_env['user_id'], cid),
            ).fetchone()
            assert row is not None
            assert row['last_seen_version'] == 7
        finally:
            conn.close()

    def test_seen_is_idempotent_upserts_existing_row(self, clusters_env):
        cid = clusters_env['clusters'][0]
        c = _client(clusters_env['app'])
        # Call twice
        c.post(f'/api/clusters/{cid}/seen').raise_for_status()
        c.post(f'/api/clusters/{cid}/seen').raise_for_status()
        # Then bump live_version and call once more
        conn = db_mod.get_conn()
        try:
            conn.execute("UPDATE clusters SET live_version = 4 WHERE id = ?", (cid,))
            conn.commit()
        finally:
            conn.close()
        r = c.post(f'/api/clusters/{cid}/seen')
        assert r.status_code == 200
        assert r.json()['last_seen_version'] == 4
        # cluster_status has exactly 1 row for (uid, cid) — UPSERT not duplicate
        conn = db_mod.get_conn()
        try:
            cnt = conn.execute(
                "SELECT COUNT(*) AS n FROM cluster_status "
                "WHERE user_id=? AND cluster_id=?",
                (clusters_env['user_id'], cid),
            ).fetchone()['n']
            assert cnt == 1
        finally:
            conn.close()


class TestClusterActions:
    def test_anonymous_actions_returns_empty(self, clusters_env):
        cid = clusters_env['clusters'][0]
        c = TestClient(clusters_env['app'])
        r = c.get(f'/api/clusters/{cid}/actions')
        assert r.status_code == 200
        assert r.json()['actions'] == []

    def test_get_actions_returns_empty_initially(self, clusters_env):
        cid = clusters_env['clusters'][0]
        c = _client(clusters_env['app'])
        r = c.get(f'/api/clusters/{cid}/actions')
        assert r.status_code == 200
        assert r.json()['actions'] == []

    def test_stale_action_flagged(self, clusters_env):
        cid = clusters_env['clusters'][0]
        conn = db_mod.get_conn()
        try:
            conn.execute(
                """INSERT INTO actions (id, source_type, source_id, cluster_version,
                                        user_id, title, action_type, prompt, is_stale)
                   VALUES ('act-s', 'cluster', ?, 1, ?, 't', 'research', 'p', 1)""",
                (str(cid), clusters_env['user_id']),
            )
            conn.commit()
        finally:
            conn.close()
        c = _client(clusters_env['app'])
        body = c.get(f'/api/clusters/{cid}/actions').json()
        assert len(body['actions']) == 1
        assert body['actions'][0]['is_stale'] == 1


class TestSearchContext:
    def test_recommend_context_returns_events_and_docs(self, clusters_env):
        c = _client(clusters_env['app'])
        r = c.get('/api/search?q=Cursor&context=recommend')
        assert r.status_code == 200
        b = r.json()
        assert 'events' in b
        assert b['events'] and 'why_read' in b['events'][0]
        assert 'docs' in b
        assert 'events_total' in b
        assert 'docs_total' in b

    def test_recommend_events_only_skips_docs_payload(self, clusters_env):
        c = _client(clusters_env['app'])
        r = c.get('/api/search?q=Cursor&context=recommend&events_only=1')
        assert r.status_code == 200
        b = r.json()
        assert 'events' in b
        assert 'events_total' in b
        assert b['docs'] == []
        assert b['docs_total'] == 0

    def test_channel_context_returns_docs_only(self, clusters_env):
        c = _client(clusters_env['app'])
        r = c.get('/api/search?q=Cursor&context=channel')
        assert r.status_code == 200
        b = r.json()
        # channel context SHALL NOT leak events (principle A)
        assert 'events' not in b or b.get('events') in (None, [])
        assert 'docs' in b

    def test_empty_query_returns_empty_results(self, clusters_env):
        c = _client(clusters_env['app'])
        r = c.get('/api/search?q=&context=recommend')
        assert r.status_code == 200
        b = r.json()
        assert b.get('events_total', 0) == 0
        assert b.get('docs_total', 0) == 0

    def test_search_excludes_high_doc_count_low_unique_sources_clusters(
        self, clusters_env
    ):
        """v15.1 review/v15.1: /api/search?context=recommend must mirror
        /api/feed/events visibility (unique_source_count >= 2).

        Without this gate, pre-cutover V1 clusters (unique_source_count=0 by
        DEFAULT) leak via search even though the feed correctly hides them.
        """
        # Seed a cluster with doc_count=5 but unique_source_count=0 (legacy
        # V1 shape). It must be invisible in /api/search recommend.
        conn = db_mod.get_conn()
        try:
            conn.execute(
                """INSERT INTO clusters (ai_title, ai_summary, ai_key_points,
                                         doc_count, unique_source_count,
                                         is_visible_in_feed, first_doc_at,
                                         last_doc_at, last_updated_at,
                                         published_at, live_version, platforms_json,
                                         cover_url)
                   VALUES ('Legacy v1 leak', 'pre-cutover', '[]',
                           5, 0, 1, '2026-04-24T09:00:00',
                           '2026-04-24T09:00:00', datetime('now'),
                           '2026-04-24T09:00:00', 1,
                           '[]', NULL)"""
            )
            conn.commit()
        finally:
            conn.close()
        c = _client(clusters_env['app'])
        r = c.get('/api/search?q=Legacy&context=recommend')
        assert r.status_code == 200
        b = r.json()
        # Legacy cluster must NOT show up in search events.
        titles = [e.get('ai_title') for e in (b.get('events') or [])]
        assert 'Legacy v1 leak' not in titles
        assert b.get('events_total', 0) == 0


@pytest.mark.parametrize("user", [False, True])
@pytest.mark.parametrize("future", ["2026-09-14T00:00:00Z", "2026-09-12T16:00:00Z"])
def test_future_events_excluded_before_pagination_and_counts(clusters_env, monkeypatch, user, future):
    from datetime import datetime, timezone
    import time_utils

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 12, 10, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(time_utils, 'datetime', FrozenDateTime)
    ids = clusters_env['clusters']
    conn = db_mod.get_conn()
    for cid, stamp in zip(ids, [future,
                                '2026-09-12T15:59:59Z',
                                '2026-09-11T07:00:00Z']):
        conn.execute("UPDATE clusters SET first_doc_at=?, last_doc_at=?, is_visible_in_feed=1 WHERE id=?", (stamp, stamp, cid))
    conn.commit()
    conn.close()
    client = _client(clusters_env['app']) if user else TestClient(clusters_env['app'])
    body = client.get('/api/feed/events?limit=1&since_version_snapshot=0').json()
    assert [e['id'] for e in body['events']] == [ids[1]]
    assert body['date_counts'] == {'2026-09-12': 1, '2026-09-11': 1}
    assert body['total_available_within_30d'] == 2
    assert body['new_since_last_fetch'] == 2
    assert body['next_cursor'] == 2
    second = client.get('/api/feed/events?limit=1&page=2').json()
    assert [e['id'] for e in second['events']] == [ids[2]]
    assert second['next_cursor'] is None
    seek = client.get('/api/feed/events?limit=1&target_date=2026-09-12').json()
    assert seek['date_seek']['anchor_event_id'] == ids[1]
    assert [e['id'] for e in seek['events']] == [ids[1]]
    assert seek['next_cursor'] == 2
    assert seek['date_counts'] == body['date_counts']
    future_day = datetime.fromisoformat(future).astimezone(time_utils.LOCAL_NAIVE_TZ).date().isoformat()
    missing = client.get(f'/api/feed/events?limit=1&target_date={future_day}').json()
    assert missing['date_seek'] == {
        'requested_date': future_day, 'status': 'not_found', 'anchor_event_id': None,
    }
    assert missing['events'] == [] and missing['next_cursor'] is None
    assert missing['date_counts'] == body['date_counts']


def test_beijing_rollover_releases_article_and_refreshes_public_cache(clusters_env, monkeypatch):
    from datetime import datetime, timezone
    import routes.clusters as route

    cutoff = [datetime(2026, 9, 12, 16, tzinfo=timezone.utc)]
    monkeypatch.setattr(route, 'highlights_published_before', lambda: cutoff[0])
    cid = clusters_env['clusters'][0]
    conn = db_mod.get_conn()
    conn.execute("UPDATE clusters SET first_doc_at='2026-09-12T16:00:00Z', last_doc_at='2026-09-12T16:00:00Z' WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    client = TestClient(clusters_env['app'])
    before = client.get('/api/feed/events').json()
    assert cid not in [e['id'] for e in before['events']]
    assert '2026-09-13' not in before['date_counts']
    cutoff[0] = datetime(2026, 9, 13, 16, tzinfo=timezone.utc)
    after = client.get('/api/feed/events').json()
    assert after['events'][0]['id'] == cid
    assert after['date_counts']['2026-09-13'] == 1
    assert after['total_available_within_30d'] == before['total_available_within_30d'] + 1


@pytest.mark.parametrize('save_future', [False, True])
def test_resume_excludes_future_articles_from_anchor_and_page(clusters_env, monkeypatch, save_future):
    from datetime import datetime, timezone
    import routes.clusters as route

    monkeypatch.setattr(route, 'highlights_published_before',
                        lambda: datetime(2026, 9, 12, 16, tzinfo=timezone.utc))
    conn = db_mod.get_conn()
    for idx in range(20):
        future_id = conn.execute("""INSERT INTO clusters(ai_title, is_visible_in_feed,
            first_doc_at, last_doc_at, last_updated_at, published_at)
            VALUES('future',1,'2026-09-14T00:00:00Z','2026-09-14T00:00:00Z',datetime('now'),datetime('now'))""").lastrowid
    saved_id = future_id if save_future else clusters_env['clusters'][0]
    anchor = conn.execute('SELECT first_doc_at FROM clusters WHERE id=?', (saved_id,)).fetchone()['first_doc_at']
    conn.execute("INSERT INTO reading_progress(user_id,surface,cluster_id,anchor_sort_at,updated_at) VALUES(?,'highlights',?,?,datetime('now'))",
                 (clusters_env['user_id'], saved_id, anchor))
    conn.commit()
    conn.close()
    client = _client(clusters_env['app'])
    progress = client.get('/api/reading-progress/highlights').json()['progress']
    assert progress['resolved_cluster_id'] == clusters_env['clusters'][0]
    assert progress['cursor'] == 1
    assert progress['resolution'] == ('nearest_older' if save_future else 'exact')
