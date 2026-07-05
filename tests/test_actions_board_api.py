import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import db as db_mod  # noqa: E402


@pytest.fixture()
def actions_board_env(monkeypatch, tmp_path):
    monkeypatch.setenv('JWT_SECRET', 'actions-board-test-secret-with-enough-entropy')
    monkeypatch.setenv('AUTH_TOKEN', 'actions-board-token')
    monkeypatch.setenv('RATELIMIT_ENABLED', 'false')
    monkeypatch.setattr(db_mod, 'DB_PATH', str(tmp_path / 'actions_board.db'))
    db_mod._item_status_has_user_id = None

    conn = db_mod.get_conn()
    try:
        rows = [
            ('act-1', '["item-1"]', 'Build board API', 'implement', 'high', 'pending', 'implementation', '实施', '2026-05-24T08:00:00'),
            ('act-2', '[]', 'Build skeleton', 'implement', 'medium', 'pending', 'implementation', '实施', '2026-05-23T08:00:00'),
            ('act-3', '[]', 'Write QA probe', 'implement', 'low', 'pending', 'implementation', '实施', '2026-05-22T08:00:00'),
            ('act-4', '["item-4"]', 'Draft launch note', 'content', 'medium', 'confirmed', 'content', '内容', '2026-05-21T08:00:00'),
            ('act-5', '[]', 'Completed action', 'implement', 'medium', 'done', 'implementation', '实施', '2026-05-20T08:00:00'),
            ('act-6', '[]', 'Hidden failed action', 'implement', 'high', 'failed', 'implementation', '实施', '2026-05-19T08:00:00'),
        ]
        conn.executemany(
            """INSERT INTO actions
                 (id, source_type, source_item_ids, title, action_type, prompt,
                  priority, status, direction, direction_label, created_at)
               VALUES (?, 'item', ?, ?, ?, 'Do it', ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
    finally:
        conn.close()

    import app as app_mod
    import middleware.auth as auth_mw
    import routes.actions as actions_route

    monkeypatch.setattr(auth_mw, '_AUTH_TOKEN', 'actions-board-token')
    monkeypatch.setattr(actions_route.remote_db, 'app_state_to_remote', lambda: False)
    app_mod.app.state.limiter.enabled = False
    return app_mod.app


def test_actions_board_limits_each_status_lane_without_legacy_full_payload(actions_board_env):
    client = TestClient(actions_board_env)

    resp = client.get('/api/actions/board?limit_per_direction=2&token=actions-board-token')

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [d['slug'] for d in body['directions']] == ['pending', 'in_progress', 'done']
    pending = next(d for d in body['directions'] if d['slug'] == 'pending')
    in_progress = next(d for d in body['directions'] if d['slug'] == 'in_progress')
    done = next(d for d in body['directions'] if d['slug'] == 'done')
    assert pending['label'] == '待处理'
    assert pending['count'] == 3
    assert [item['id'] for item in pending['items']] == ['act-1', 'act-2']
    assert pending['has_more'] is True
    assert pending['next_offset'] == 2
    assert in_progress['count'] == 1
    assert [item['id'] for item in in_progress['items']] == ['act-4']
    assert done['count'] == 1
    assert [item['id'] for item in done['items']] == ['act-5']
    assert 'actions' not in body
    assert body['counts']['total'] == 5
    assert body['counts']['in_progress'] == 1
    assert body['meta']['limit_per_direction'] == 2


def test_actions_board_can_page_one_status_lane(actions_board_env):
    client = TestClient(actions_board_env)

    resp = client.get('/api/actions/board?status=pending&limit_per_direction=2&offset=2&token=actions-board-token')

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body['directions']) == 1
    pending = body['directions'][0]
    assert pending['slug'] == 'pending'
    assert pending['count'] == 3
    assert [item['id'] for item in pending['items']] == ['act-3']
    assert pending['has_more'] is False
    assert pending['next_offset'] is None


def test_actions_board_filters_source_presence_before_lane_limit(actions_board_env):
    client = TestClient(actions_board_env)

    resp = client.get('/api/actions/board?source_filter=with-source&limit_per_direction=2&token=actions-board-token')

    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = [
        item['id']
        for direction in body['directions']
        for item in direction['items']
    ]
    assert set(ids) == {'act-1', 'act-4'}
    assert body['counts']['total'] == 2


def test_actions_board_hides_failed_dismissed_ignored_from_main_lanes(actions_board_env):
    client = TestClient(actions_board_env)

    resp = client.get('/api/actions/board?limit_per_direction=20&token=actions-board-token')

    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = [
        item['id']
        for direction in body['directions']
        for item in direction['items']
    ]
    assert 'act-6' not in ids
    assert body['counts'].get('failed', 0) == 0


def test_actions_board_in_progress_status_groups_legacy_running_states(actions_board_env):
    client = TestClient(actions_board_env)

    resp = client.get('/api/actions/board?status=in_progress&limit_per_direction=2&token=actions-board-token')

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [d['slug'] for d in body['directions']] == ['in_progress']
    ids = [item['id'] for item in body['directions'][0]['items']]
    assert ids == ['act-4']
    assert body['counts']['confirmed'] == 1
    assert body['counts']['total'] == 1


def test_actions_board_remote_defaults_to_fast_base_cards(actions_board_env, monkeypatch):
    import routes.actions as actions_route

    observed: list[bool] = []

    def fake_remote_board(**kwargs):
        observed.append(kwargs['include_detail_payloads'])
        return {
            'counts': {'total': 0},
            'directions': [],
            'meta': {'detail_included': kwargs['include_detail_payloads']},
        }

    monkeypatch.setattr(actions_route.remote_db, 'app_state_to_remote', lambda: True)
    monkeypatch.setattr(actions_route.remote_db, 'get_actions_board_payload_remote', fake_remote_board)
    client = TestClient(actions_board_env)

    fast_resp = client.get('/api/actions/board?limit_per_direction=1&token=actions-board-token')
    detail_resp = client.get('/api/actions/board?limit_per_direction=1&include_detail=true&token=actions-board-token')

    assert fast_resp.status_code == 200
    assert detail_resp.status_code == 200
    assert observed == [False, True]
    assert fast_resp.json()['meta']['detail_included'] is False
    assert detail_resp.json()['meta']['detail_included'] is True
