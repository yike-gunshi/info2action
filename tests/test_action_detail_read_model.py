import json
import os
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import db as db_mod
import action_detail_read_model
import remote_db
import routes.actions as actions_route


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeOneResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _ActionReadModelFreshnessConn:
    def __init__(self):
        self.sqls = []
        self.params = []

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        self.params.append(params or {})
        normalized = " ".join(sql.split())
        if normalized.startswith("SET LOCAL"):
            return _FakeResult([])
        if "SELECT created_at FROM remote_poc.actions" in normalized:
            return _FakeOneResult({"created_at": "2026-05-24T12:00:00+00:00"})
        if "WITH top_actions AS" in normalized:
            return _FakeResult([
                {
                    "id": "missing-detail",
                    "created_at": "2026-05-24T12:00:00+00:00",
                    "confirmed_at": None,
                    "executed_at": None,
                    "completed_at": None,
                    "dismissed_at": None,
                    "dispatched_at": None,
                    "project_context_updated_at": None,
                    "source_updated_at": None,
                },
                {
                    "id": "stale-detail",
                    "created_at": "2026-05-24T10:00:00+00:00",
                    "confirmed_at": None,
                    "executed_at": None,
                    "completed_at": "2026-05-24T11:00:00+00:00",
                    "dismissed_at": None,
                    "dispatched_at": None,
                    "project_context_updated_at": None,
                    "source_updated_at": "2026-05-24T10:00:00+00:00",
                },
                {
                    "id": "fresh-detail",
                    "created_at": "2026-05-24T09:00:00+00:00",
                    "confirmed_at": None,
                    "executed_at": None,
                    "completed_at": None,
                    "dismissed_at": None,
                    "dispatched_at": None,
                    "project_context_updated_at": None,
                    "source_updated_at": "2026-05-24T09:00:00+00:00",
                },
            ])
        raise AssertionError(f"unexpected SQL: {normalized}")


def test_sqlite_action_detail_read_model_round_trips_complete_payload(monkeypatch, tmp_path):
    monkeypatch.setattr(db_mod, 'DB_PATH', str(tmp_path / 'feed.db'))
    conn = db_mod.get_conn()
    try:
        action_id = db_mod.create_action(
            conn,
            source_type='manual',
            title='完整行动详情',
            action_type='implementation',
            prompt='- 第一步\n- 第二步',
            source_item_ids=['source-1'],
            reason='完整决策理由',
            priority='high',
            user_id='user-1',
        )
        payload = {
            'id': action_id,
            'title': '完整行动详情',
            'type': 'implementation',
            'action_type': 'implementation',
            'status': 'pending',
            'priority': 'high',
            'steps': ['第一步', '第二步'],
            'reason': '完整决策理由',
            'source_item_ids': ['source-1'],
            'source_items': [
                {
                    'id': 'source-1',
                    'platform': 'twitter',
                    'title': '真实来源标题',
                    'ai_summary': '真实来源摘要',
                    'url': 'https://example.com/source',
                    'referenced_urls': [],
                }
            ],
            'source_item_count': 1,
            'created_at': '2026-05-30T01:20:00Z',
        }

        db_mod.upsert_action_detail_read_model(
            conn,
            action_id=action_id,
            viewer_scope='owner',
            owner_user_id='user-1',
            payload=payload,
            source_item_ids=['source-1'],
            source_updated_at='2026-05-30T01:19:00Z',
        )

        row = conn.execute(
            "SELECT payload_json, source_item_ids FROM action_detail_read_models WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        assert row is not None
        assert json.loads(row['source_item_ids']) == ['source-1']

        stored = db_mod.get_action_detail_read_model(
            conn,
            action_id,
            viewer_scope='owner',
            owner_user_id='user-1',
        )
        assert stored == payload
    finally:
        conn.close()


def test_sqlite_actions_list_can_merge_detail_read_model(monkeypatch, tmp_path):
    monkeypatch.setattr(db_mod, 'DB_PATH', str(tmp_path / 'feed.db'))
    conn = db_mod.get_conn()
    try:
        action_id = db_mod.create_action(
            conn,
            source_type='manual',
            title='列表预载详情',
            action_type='implementation',
            prompt='- 先拿完整详情\n- 再打开弹窗',
            source_item_ids=['source-2'],
            reason='列表已经携带决策理由',
            priority='high',
            user_id='user-2',
        )
        payload = db_mod.build_action_detail_read_model(
            conn,
            action_id,
            request_user_id='user-2',
            can_view_all=False,
            owner_user_id='user-2',
            persist=True,
        )

        actions = db_mod.get_actions(conn, user_id='user-2')
        payloads = db_mod.get_action_detail_read_models(
            conn,
            [action['id'] for action in actions],
            viewer_scope='owner',
            owner_user_id='user-2',
        )
        merged = [
            action_detail_read_model.merge_action_with_detail_payload(
                action,
                payloads.get(action['id']),
            )
            for action in actions
        ]

        assert payload is not None
        assert merged[0]['id'] == action_id
        assert merged[0]['steps'] == ['先拿完整详情', '再打开弹窗']
        assert merged[0]['source_items'] == []
        assert merged[0]['source_item_count'] == 0
        assert merged[0]['reason'] == '列表已经携带决策理由'
    finally:
        conn.close()


def test_sqlite_action_detail_read_model_misses_when_action_source_is_newer(monkeypatch, tmp_path):
    monkeypatch.setattr(db_mod, 'DB_PATH', str(tmp_path / 'feed.db'))
    conn = db_mod.get_conn()
    try:
        action_id = db_mod.create_action(
            conn,
            source_type='manual',
            title='执行状态会更新',
            action_type='implementation',
            prompt='- 先确认\n- 再执行',
            source_item_ids=[],
            reason='状态更新后详情 payload 不能继续用旧的',
            priority='high',
            user_id='user-3',
        )
        payload = db_mod.build_action_detail_read_model(
            conn,
            action_id,
            request_user_id='user-3',
            can_view_all=False,
            owner_user_id='user-3',
            persist=True,
        )
        assert payload is not None

        db_mod.update_action(
            conn,
            action_id,
            owner_user_id='user-3',
            status='done',
            completed_at='2999-01-01T00:00:00Z',
        )

        assert db_mod.get_action_detail_read_model(
            conn,
            action_id,
            viewer_scope='owner',
            owner_user_id='user-3',
        ) is None
    finally:
        conn.close()


def test_remote_action_detail_read_model_freshness_helper_detects_stale_source():
    row = {
        'source_updated_at': '2026-05-24T10:00:00Z',
        'created_at': '2026-05-24T09:00:00Z',
        'completed_at': '2026-05-24T11:00:00Z',
    }

    assert remote_db._action_detail_read_model_fresh(row) is False

    row['source_updated_at'] = '2026-05-24T11:00:00Z'
    assert remote_db._action_detail_read_model_fresh(row) is True


def test_action_detail_read_model_freshness_remote_reports_missing_and_stale(monkeypatch):
    conn = _ActionReadModelFreshnessConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")

    result = remote_db.action_detail_read_model_freshness_remote(limit=3)

    assert result["stale"] is True
    assert result["latest_action_created_at"] == "2026-05-24T12:00:00Z"
    assert result["sampled_actions"] == 3
    assert result["prefetch_missing_count"] == 1
    assert result["prefetch_stale_count"] == 1
    assert result["prefetch_unfresh_count"] == 2
    assert result["stale_action_ids_sample"] == ["missing-detail", "stale-detail"]
    assert any("LEFT JOIN remote_poc.action_detail_read_models" in sql for sql in conn.sqls)


def test_actions_payload_stale_fallback_is_age_bounded(monkeypatch):
    monkeypatch.setenv(actions_route._ACTIONS_PAYLOAD_STALE_FALLBACK_MAX_SEC_ENV, "60")
    cached = (100.0, {"actions": [{"id": "cached"}], "counts": {}, "directions": []})

    fresh_enough = actions_route._copy_actions_payload_if_stale_fallback_allowed(cached, now=130.0)
    too_old = actions_route._copy_actions_payload_if_stale_fallback_allowed(cached, now=161.0)

    assert fresh_enough["stale_cache"] is True
    assert fresh_enough["stale_cache_age_sec"] == 30
    assert too_old is None


def test_list_prefetch_action_ids_are_capped_to_first_screen_budget():
    actions = [
        {'id': f'act-{idx}', 'direction': f'dir-{idx % 5}'}
        for idx in range(80)
    ]

    selected = action_detail_read_model.select_list_prefetch_action_ids(actions)

    assert len(selected) == action_detail_read_model.LIST_PREFETCH_TOTAL
    assert selected == [f'act-{idx}' for idx in range(action_detail_read_model.LIST_PREFETCH_TOTAL)]


def test_list_prefetch_action_ids_cover_multiple_direction_lanes():
    actions = [
        *({'id': f'ai-{idx}', 'direction': 'ai-infra'} for idx in range(30)),
        *({'id': f'content-{idx}', 'direction': 'content-creation'} for idx in range(30)),
        *({'id': f'agent-{idx}', 'direction': 'agent-ecosystem'} for idx in range(30)),
    ]

    selected = action_detail_read_model.select_list_prefetch_action_ids(actions, total=9)

    assert selected == [
        'ai-0', 'content-0', 'agent-0',
        'ai-1', 'content-1', 'agent-1',
        'ai-2', 'content-2', 'agent-2',
    ]
