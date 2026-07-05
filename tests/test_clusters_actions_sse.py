"""BF-0424-CLUSTER-SSE regression tests.

The cluster action endpoint must behave like the v10.1 generate-from-item flow:
multi-stage SSE, live thinking-ai events, result envelope, and DB persistence.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import db as db_mod  # noqa: E402
import generate_actions as generate_actions_mod  # noqa: E402
from tests.test_clusters_api import _client, clusters_env  # noqa: E402,F401


def _parse_sse(text: str) -> list[dict]:
    events: list[dict] = []
    for block in text.split('\n\n'):
        block = block.strip()
        if not block:
            continue
        event_type = 'message'
        payload = {}
        for line in block.splitlines():
            if line.startswith('event: '):
                event_type = line[7:].strip()
            elif line.startswith('data: '):
                payload = json.loads(line[6:])
        events.append({'type': event_type, **payload})
    return events


class TestClusterActionSSE:
    def test_streams_llm_thinking_result_and_persists_cluster_action(
        self,
        clusters_env,
        monkeypatch,
    ):
        cid = clusters_env['clusters'][0]

        def fake_stream(api_key, api_base, model, system_prompt, content,
                        max_tokens=2048, on_thinking=None):
            assert api_key == 'test-key'
            assert api_base == 'https://api.minimaxi.com/anthropic/v1'
            assert model
            assert '事件聚合上下文' in content
            assert '用户指定行动类型' in content
            assert '必须有可验收产出物' in content
            assert '不要只写“调研/深入了解/关注/分析某事件”' in content
            if on_thinking:
                on_thinking('正在比较多源信息的差异')
                on_thinking('生成一个实施型行动点')
            return json.dumps({
                'title': '验证多源事件机会',
                'action_type': 'investigate',
                'prompt': '基于这些来源输出一份机会评估和执行建议。',
                'priority': 'high',
                'reason': '多源同时报道，值得快速判断是否需要跟进。',
            }, ensure_ascii=False)

        monkeypatch.setenv('MINIMAX_API_KEY', 'test-key')
        monkeypatch.setattr(generate_actions_mod, 'call_minimax_streaming', fake_stream)

        client = _client(clusters_env['app'])
        resp = client.post(
            f'/api/clusters/{cid}/actions',
            json={'user_hint': '关注工程机会', 'action_type': 'implement'},
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers.get('connection') == 'close'

        events = _parse_sse(resp.text)
        event_types = [e['type'] for e in events]
        assert event_types.count('thinking') >= 4
        assert 'thinking-ai' in event_types
        assert {'type': 'stage', 'index': 1, 'status': 'active'} in events
        assert {'type': 'stage', 'index': 3, 'status': 'done'} in events
        assert event_types[-2:] == ['result', 'done']

        result = next(e for e in events if e['type'] == 'result')
        action = result['action']
        assert action['title'] == '验证多源事件机会'
        assert action['action_type'] == 'implement'
        assert action['source_type'] == 'cluster'
        assert action['cluster_version'] == 1

        conn = db_mod.get_conn()
        try:
            row = conn.execute(
                """SELECT source_type, source_id, cluster_version, source_item_ids,
                          title, action_type, priority, is_stale, user_id
                   FROM actions
                   WHERE id = ?""",
                (action['id'],),
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        assert row['source_type'] == 'cluster'
        assert row['source_id'] == str(cid)
        assert row['cluster_version'] == 1
        assert json.loads(row['source_item_ids']) == ['itm_0', 'itm_1', 'itm_2']
        assert row['title'] == '验证多源事件机会'
        assert row['action_type'] == 'implement'
        assert row['priority'] == 'high'
        assert row['is_stale'] == 0
        assert row['user_id'] == clusters_env['user_id']

    def test_llm_failure_stops_without_cluster_fallback(self, clusters_env, monkeypatch):
        cid = clusters_env['clusters'][0]

        def fake_stream(*args, **kwargs):
            on_thinking = kwargs.get('on_thinking')
            if on_thinking:
                on_thinking('开始综合，但模型调用即将失败')
            raise RuntimeError('provider unavailable')

        monkeypatch.setenv('MINIMAX_API_KEY', 'test-key')
        monkeypatch.setattr(generate_actions_mod, 'call_minimax_streaming', fake_stream)

        client = _client(clusters_env['app'])
        resp = client.post(f'/api/clusters/{cid}/actions', json={})
        assert resp.status_code == 200, resp.text

        events = _parse_sse(resp.text)
        event_types = [e['type'] for e in events]
        assert 'thinking-ai' in event_types
        assert 'error' in event_types
        assert 'result' not in event_types
        assert 'done' not in event_types

        conn = db_mod.get_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM actions WHERE source_type = 'cluster' AND source_id = ?",
                (str(cid),),
            ).fetchone()
        finally:
            conn.close()
        assert row['n'] == 0

    def test_unparseable_llm_result_stops_without_cluster_fallback(self, clusters_env, monkeypatch):
        cid = clusters_env['clusters'][0]

        def fake_stream(*args, **kwargs):
            return '这不是 JSON，也不应该被保存成 fallback action'

        monkeypatch.setenv('MINIMAX_API_KEY', 'test-key')
        monkeypatch.setattr(generate_actions_mod, 'call_minimax_streaming', fake_stream)

        client = _client(clusters_env['app'])
        resp = client.post(f'/api/clusters/{cid}/actions', json={})
        assert resp.status_code == 200, resp.text

        events = _parse_sse(resp.text)
        event_types = [e['type'] for e in events]
        assert 'error' in event_types
        assert 'result' not in event_types
        assert 'done' not in event_types
        assert any('未能解析' in e.get('error', '') for e in events if e['type'] == 'error')

        conn = db_mod.get_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM actions WHERE source_type = 'cluster' AND source_id = ?",
                (str(cid),),
            ).fetchone()
        finally:
            conn.close()
        assert row['n'] == 0

    def test_minimax_auth_error_stops_without_cluster_fallback(self, clusters_env, monkeypatch):
        cid = clusters_env['clusters'][0]

        def fake_stream(*args, **kwargs):
            raise generate_actions_mod.ProviderAuthenticationError(
                'MiniMax authentication failed (HTTP 401): invalid api key'
            )

        monkeypatch.setenv('MINIMAX_API_KEY', 'bad-key')
        monkeypatch.setattr(generate_actions_mod, 'call_minimax_streaming', fake_stream)

        client = _client(clusters_env['app'])
        resp = client.post(f'/api/clusters/{cid}/actions', json={})
        assert resp.status_code == 200, resp.text

        events = _parse_sse(resp.text)
        event_types = [e['type'] for e in events]
        assert 'error' in event_types
        assert 'result' not in event_types
        assert 'done' not in event_types
        assert any('HTTP 401' in e.get('error', '') for e in events if e['type'] == 'error')

        conn = db_mod.get_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM actions WHERE source_type = 'cluster' AND source_id = ?",
                (str(cid),),
            ).fetchone()
        finally:
            conn.close()
        assert row['n'] == 0

    def test_missing_cluster_returns_json_404_not_sse(self, clusters_env):
        client = _client(clusters_env['app'])
        resp = client.post('/api/clusters/999999/actions', json={})
        assert resp.status_code == 404
        assert resp.json() == {'error': 'Cluster not found'}


class TestMiniMaxChatConfig:
    def test_embedding_base_url_does_not_override_chat_api_base(self, monkeypatch):
        monkeypatch.setenv('MINIMAX_API_KEY', 'env-key')
        monkeypatch.setenv('MINIMAX_BASE_URL', 'https://api.minimax.chat')
        monkeypatch.delenv('MINIMAX_API_BASE', raising=False)

        api_key, api_base, model = generate_actions_mod.resolve_minimax_chat_config({
            'api_key': 'config-key',
            'api_base': 'https://api.minimaxi.com/anthropic/v1',
            'model': 'MiniMax-M2.7',
        })

        assert api_key == 'env-key'
        assert api_base == 'https://api.minimaxi.com/anthropic/v1'
        assert model == 'MiniMax-M2.7'
