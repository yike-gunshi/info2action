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
                               '2026-04-25T10:00:00Z',
                               '2026-04-25T10:00:00Z',
                               '2026-04-25T10:00:00Z',
                               '2026-04-25T10:00:00Z',
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
                               'author', '2026-04-25T10:00:00Z',
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
                           4, 4, 1, '2026-04-20T09:00:00',
                           '2026-05-15T09:00:00', '2026-05-15T09:00:00',
                           3, '["x"]', NULL, '2026-05-15T09:05:00')"""
            ).lastrowid
            fresh = conn.execute(
                """INSERT INTO clusters (ai_title, ai_summary, ai_key_points,
                                         doc_count, unique_source_count,
                                         is_visible_in_feed, first_doc_at,
                                         last_doc_at, last_updated_at,
                                         live_version, platforms_json, cover_url,
                                         published_at)
                   VALUES ('新首发事件', 'fresh', '[]',
                           2, 2, 1, '2026-04-25T09:00:00',
                           '2026-04-25T09:15:00', '2026-04-25T09:15:00',
                           1, '["reddit"]', NULL, '2026-04-25T09:20:00')"""
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
                  'platforms', 'first_doc_at', 'live_version'):
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
        }


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
