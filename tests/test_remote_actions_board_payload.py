import os
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import remote_db  # noqa: E402


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _BoardConn:
    def __init__(self):
        self.queries: list[str] = []
        self.params: list[object] = []

    def execute(self, sql, params=None):
        self.queries.append(str(sql))
        self.params.append(params)
        if str(sql).startswith('SET LOCAL statement_timeout'):
            return _Rows([])
        if 'GROUP BY status' in str(sql):
            return _Rows([
                {'status': 'pending', 'cnt': 3},
                {'status': 'confirmed', 'cnt': 1},
            ])
        if 'LEFT JOIN LATERAL' in str(sql):
            return _Rows([
                {
                    'board_lane': 'pending',
                    'board_lane_label': '待处理',
                    'lane_total': 3,
                    'id': 'act-1',
                    'source_item_ids': ['item-1'],
                    'title': 'Build board',
                    'action_type': 'implement',
                    'prompt': 'Do it',
                    'priority': 'high',
                    'status': 'pending',
                    'direction': 'implementation',
                    'direction_label': '实施',
                    'created_at': '2026-05-24T08:00:00Z',
                },
                {
                    'board_lane': 'pending',
                    'board_lane_label': '待处理',
                    'lane_total': 3,
                    'id': 'act-2',
                    'source_item_ids': [],
                    'title': 'Build skeleton',
                    'action_type': 'implement',
                    'prompt': 'Do it',
                    'priority': 'medium',
                    'status': 'pending',
                    'direction': 'implementation',
                    'direction_label': '实施',
                    'created_at': '2026-05-23T08:00:00Z',
                },
                {
                    'board_lane': 'in_progress',
                    'board_lane_label': '执行中',
                    'lane_total': 1,
                    'id': 'act-4',
                    'source_item_ids': [],
                    'title': 'Draft note',
                    'action_type': 'content',
                    'prompt': 'Do it',
                    'priority': 'medium',
                    'status': 'confirmed',
                    'direction': 'content',
                    'direction_label': '内容',
                    'created_at': '2026-05-21T08:00:00Z',
                },
                {
                    'board_lane': 'done',
                    'board_lane_label': '已完成',
                    'lane_total': 0,
                    'id': None,
                    'source_item_ids': None,
                    'title': None,
                    'action_type': None,
                    'prompt': None,
                    'priority': None,
                    'status': None,
                    'direction': None,
                    'direction_label': None,
                    'created_at': None,
                },
            ])
        raise AssertionError(f'unexpected SQL: {sql}')


class _ReadModelBoardConn:
    def __init__(self):
        self.queries: list[str] = []
        self.params: list[object] = []

    def execute(self, sql, params=None):
        text = str(sql)
        self.queries.append(text)
        self.params.append(params)
        if text.startswith('SET LOCAL statement_timeout'):
            return _Rows([])
        if 'FROM remote_poc.action_board_read_model_state' in text:
            return _Rows([
                {
                    'version_id': '00000000-0000-0000-0000-0000000000ab',
                    'generated_at': '2026-05-25T00:00:00Z',
                    'completed_at': '2026-05-25T00:00:01Z',
                    'payload_version': 1,
                },
            ])
        if 'FROM remote_poc.action_board_scopes' in text:
            return _Rows([
                {
                    'scope_key': 'date:all|priority:low',
                    'total_count': 2,
                    'status_counts_json': {'pending': 2},
                },
            ])
        if 'FROM remote_poc.action_board_scope_lanes' in text:
            return _Rows([
                {'lane_slug': 'pending', 'lane_label': '待处理', 'total_count': 2},
                {'lane_slug': 'in_progress', 'lane_label': '执行中', 'total_count': 0},
                {'lane_slug': 'done', 'lane_label': '已完成', 'total_count': 0},
            ])
        if 'FROM remote_poc.action_board_scope_items' in text:
            return _Rows([
                {
                    'lane_slug': 'pending',
                    'rank': 1,
                    'action_id': 'act-p2-1',
                    'created_at': '2026-05-25T08:00:00Z',
                    'card_json': {
                        'id': 'act-p2-1',
                        'source_item_ids': [],
                        'title': 'P2 read model card',
                        'action_type': 'implement',
                        'prompt': 'Do it',
                        'priority': 'low',
                        'status': 'pending',
                        'direction': 'implementation',
                        'direction_label': '实施',
                        'created_at': '2026-05-25T08:00:00Z',
                    },
                },
            ])
        raise AssertionError(f'unexpected SQL: {sql}')


class _LegacyActionsConn:
    def __init__(self):
        self.queries: list[str] = []
        self.params: list[object] = []

    def execute(self, sql, params=None):
        text = str(sql)
        self.queries.append(text)
        self.params.append(params)
        if text.startswith('SET LOCAL statement_timeout'):
            return _Rows([])
        if 'SELECT id, source_item_ids, title, action_type, prompt' in text:
            return _Rows([
                {
                    'id': 'act-legacy-1',
                    'source_item_ids': [],
                    'title': 'Legacy action',
                    'action_type': 'implement',
                    'prompt': 'Do it',
                    'priority': 'medium',
                    'status': 'pending',
                    'direction': 'implementation',
                    'direction_label': '实施',
                    'created_at': '2026-05-28T08:00:00Z',
                },
            ])
        if 'GROUP BY status' in text:
            return _Rows([{'status': 'pending', 'cnt': 3}])
        if 'GROUP BY direction, direction_label' in text:
            return _Rows([{'direction': 'implementation', 'direction_label': '实施', 'cnt': 3}])
        raise AssertionError(f'unexpected SQL: {sql}')


class _ReadModelRefreshProbeConn:
    def __init__(self):
        self.queries: list[str] = []

    def execute(self, sql, params=None):
        text = str(sql)
        self.queries.append(text)
        if text.startswith('SET LOCAL statement_timeout'):
            return _Rows([])
        if 'INSERT INTO remote_poc.action_board_read_model_versions' in text:
            raise RuntimeError('stop after version insert')
        return _Rows([])

    def rollback(self):
        pass

    def commit(self):
        pass


def _install_fake_board_conn(monkeypatch):
    conn = _BoardConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, 'connect', fake_connect)
    return conn


def test_action_board_refresh_casts_read_model_name_parameter(monkeypatch):
    monkeypatch.setenv('INFO2ACTION_ACTION_BOARD_READ_MODEL', '1')
    conn = _ReadModelRefreshProbeConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, 'connect', fake_connect)

    try:
        remote_db.refresh_action_board_read_model_remote(can_view_all=True)
    except remote_db.RemoteDBError:
        pass

    joined_sql = '\n'.join(conn.queries)
    assert "jsonb_build_object('read_model', %(read_model_name)s::text)" in joined_sql


def test_remote_actions_board_prefers_action_board_read_model(monkeypatch):
    monkeypatch.setenv('INFO2ACTION_ACTION_BOARD_READ_MODEL', '1')
    conn = _ReadModelBoardConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, 'connect', fake_connect)

    payload = remote_db.get_actions_board_payload_remote(
        priority='low',
        limit_per_direction=2,
        include_detail_payloads=False,
    )

    joined_sql = '\n'.join(conn.queries)
    assert 'LEFT JOIN LATERAL' not in joined_sql
    assert payload['meta']['read_model'] == 'action_board_v1'
    assert payload['meta']['query_strategy'] == 'action_board_read_model'
    assert payload['meta']['read_model_version_id'] == '00000000-0000-0000-0000-0000000000ab'
    assert payload['counts']['total'] == 2
    assert payload['counts']['pending'] == 2
    assert [d['slug'] for d in payload['directions']] == ['pending', 'in_progress', 'done']
    assert payload['directions'][0]['items'][0]['id'] == 'act-p2-1'
    assert any(
        isinstance(params, dict) and params.get('scope_key') == 'date:all|priority:low'
        for params in conn.params
    )


def test_remote_actions_board_uses_status_lane_queries_without_window_sql(monkeypatch):
    conn = _install_fake_board_conn(monkeypatch)

    payload = remote_db.get_actions_board_payload_remote(
        limit_per_direction=2,
        include_detail_payloads=False,
    )

    joined_sql = '\n'.join(conn.queries).upper()
    assert 'ROW_NUMBER() OVER' not in joined_sql
    assert 'COUNT(*) OVER' not in joined_sql
    assert 'GROUPING SETS' not in joined_sql
    assert 'GROUP BY STATUS' in joined_sql
    assert payload['counts']['total'] == 4
    assert payload['counts']['in_progress'] == 1
    assert payload['counts']['done'] == 0
    assert [d['slug'] for d in payload['directions']] == ['pending', 'in_progress', 'done']
    assert len(payload['directions'][0]['items']) == 2
    assert payload['directions'][0]['has_more'] is True
    assert payload['directions'][0]['next_offset'] == 2
    assert payload['directions'][2]['count'] == 0
    assert payload['directions'][2]['items'] == []
    assert payload['meta']['detail_included'] is False
    assert payload['meta']['query_strategy'] == 'status_lanes_lateral'


def test_remote_actions_board_in_progress_status_expands_to_legacy_states(monkeypatch):
    conn = _install_fake_board_conn(monkeypatch)

    remote_db.get_actions_board_payload_remote(
        status='in_progress',
        limit_per_direction=2,
        include_detail_payloads=False,
    )

    assert any(
        isinstance(params, dict) and params.get('statuses') == ['confirmed', 'executing', 'dispatched']
        for params in conn.params
    )


def test_remote_actions_board_detail_failure_degrades_to_base_cards(monkeypatch):
    _install_fake_board_conn(monkeypatch)
    observed = {}

    def fail_detail(*args, **kwargs):
        observed['statement_timeout_ms'] = kwargs.get('statement_timeout_ms')
        raise RuntimeError('detail timeout')

    monkeypatch.setattr(remote_db, '_get_action_list_detail_payloads_remote', fail_detail)

    payload = remote_db.get_actions_board_payload_remote(
        limit_per_direction=2,
        include_detail_payloads=True,
    )

    assert payload['meta']['detail_degraded'] is True
    assert payload['meta']['detail_included'] is False
    assert observed['statement_timeout_ms'] is not None
    assert payload['directions'][0]['items'][0]['id'] == 'act-1'
    assert payload['directions'][0]['items'][0]['title'] == 'Build board'


def test_remote_actions_board_merges_slim_list_detail_payload(monkeypatch):
    _install_fake_board_conn(monkeypatch)

    def list_detail(_conn, action_ids, **kwargs):
        return {
            action_ids[0]: {
                'steps': ['第一步'],
                'source_items': [{'id': 'item-1', 'title': '来源标题'}],
                'source_item_count': 1,
                '_list_payload': True,
            },
        }

    monkeypatch.setattr(remote_db, '_get_action_list_detail_payloads_remote', list_detail)

    payload = remote_db.get_actions_board_payload_remote(
        limit_per_direction=2,
        include_detail_payloads=True,
    )

    first = payload['directions'][0]['items'][0]
    assert first['steps'] == ['第一步']
    assert first['source_item_count'] == 1
    assert first['_list_payload'] is True
    assert payload['meta']['detail_degraded'] is False
    assert payload['meta']['detail_included'] is True


def test_remote_legacy_actions_payload_is_paginated_and_timeout_guarded(monkeypatch):
    conn = _LegacyActionsConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, 'connect', fake_connect)

    payload = remote_db.get_actions_payload_remote(
        priority='medium',
        include_detail_payloads=False,
        limit=25,
        offset=50,
    )

    joined_sql = '\n'.join(conn.queries)
    assert 'SET LOCAL statement_timeout' in joined_sql
    assert 'LIMIT %(limit)s OFFSET %(offset)s' in joined_sql
    assert any(
        isinstance(params, dict)
        and params.get('limit') == 25
        and params.get('offset') == 50
        and params.get('priority') == 'medium'
        for params in conn.params
    )
    assert payload['actions'][0]['id'] == 'act-legacy-1'
    assert payload['meta']['query_strategy'] == 'legacy_actions_paginated'
    assert payload['meta']['limit'] == 25
    assert payload['meta']['offset'] == 50
