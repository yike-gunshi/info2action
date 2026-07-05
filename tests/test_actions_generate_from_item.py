"""Regression tests for manual single-item action generation.

PRD F29 says single-item manual generation has no threshold: a user click is
explicit intent, so the SSE result must contain an action and the detail panel
must be able to reload that action via /api/actions/by-item.
"""
import json
import os
import sys
import urllib.error
import uuid
from io import BytesIO

import bcrypt
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import db as db_mod  # noqa: E402
import generate_actions as generate_actions_mod  # noqa: E402

PASSWORD = 'password123'


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


@pytest.fixture()
def single_item_action_env(monkeypatch, tmp_path):
    monkeypatch.setenv('JWT_SECRET', 'single-item-action-test-secret-with-enough-entropy')
    monkeypatch.setenv('RATELIMIT_ENABLED', 'false')
    monkeypatch.setattr(db_mod, 'DB_PATH', str(tmp_path / 'single_item_actions.db'))
    db_mod._item_status_has_user_id = None

    conn = db_mod.get_conn()
    try:
        user_id = str(uuid.uuid4())
        hashed = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
        db_mod.create_user(conn, user_id, 'admin', 'admin@test.local', hashed, role='admin')
        db_mod.update_user(conn, user_id, email_verified=1)
        conn.execute(
            """INSERT INTO items (id, platform, source, fetched_at, title,
                                  content, ai_summary, ai_category)
               VALUES ('doc-low-value', 'x', 'following', datetime('now'),
                       'Weak Signal Item', 'short content',
                       'A weak signal summary', 'AI产品')"""
        )
        conn.commit()
    finally:
        conn.close()

    import app as app_mod
    import middleware.auth as auth_mw
    import routes.auth as auth_route

    monkeypatch.setattr(auth_route, 'JWT_SECRET', 'single-item-action-test-secret-with-enough-entropy')
    monkeypatch.setattr(auth_mw, '_AUTH_TOKEN', '')
    app_mod.app.state.limiter.enabled = False

    monkeypatch.setattr(generate_actions_mod, 'load_manifest', lambda: 'manifest')
    monkeypatch.setattr(generate_actions_mod, 'load_pulse', lambda: {})
    monkeypatch.setattr(generate_actions_mod, 'load_directions', lambda: ({}, 'directions'))
    monkeypatch.setattr(
        generate_actions_mod,
        'build_analysis_prompt',
        lambda manifest, context, directions, pulse_fields, user_guidance='': user_guidance or 'system',
    )

    return {'app': app_mod.app, 'user_id': user_id}


def _admin_client(app) -> TestClient:
    client = TestClient(app)
    resp = client.post('/api/auth/login', json={'login': 'admin@test.local', 'password': PASSWORD})
    assert resp.status_code == 200, resp.text
    return client


class TestGenerateFromItem:
    def test_generate_from_item_prefers_env_minimax_key(
        self,
        single_item_action_env,
        monkeypatch,
    ):
        monkeypatch.setenv('MINIMAX_API_KEY', 'env-test-key')

        def fake_process(item, api_key, api_base, model, system_prompt, on_thinking=None):
            assert api_key == 'env-test-key'
            return {
                'title': '使用 env key 生成',
                'action_type': 'investigate',
                'prompt': '确认 env key 优先级。',
                'reason': '避免 config 中的旧 key 继续触发 401。',
                'priority': 'medium',
            }, None, 'test-log.json'

        monkeypatch.setattr(generate_actions_mod, 'process_single_item_streaming', fake_process)

        client = _admin_client(single_item_action_env['app'])
        resp = client.post(
            '/api/actions/generate-from-item',
            json={'item_id': 'doc-low-value', 'action_type': 'investigate'},
        )

        assert resp.status_code == 200, resp.text
        events = _parse_sse(resp.text)
        result = next(e for e in events if e['type'] == 'result')
        assert result['action']['title'] == '使用 env key 生成'

    def test_minimax_401_does_not_create_fallback_action(
        self,
        single_item_action_env,
        monkeypatch,
    ):
        monkeypatch.setenv('MINIMAX_API_KEY', 'env-test-key')

        def fake_guarded_urlopen(*_args, **_kwargs):
            raise urllib.error.HTTPError(
                url='https://api.minimaxi.test/messages',
                code=401,
                msg='Unauthorized',
                hdrs=None,
                fp=BytesIO(b'{"error":"invalid api key"}'),
            )

        monkeypatch.setattr(
            generate_actions_mod.ai_provider_guard,
            'guarded_urlopen',
            fake_guarded_urlopen,
        )

        client = _admin_client(single_item_action_env['app'])
        resp = client.post(
            '/api/actions/generate-from-item',
            json={'item_id': 'doc-low-value', 'action_type': 'investigate'},
        )

        assert resp.status_code == 200, resp.text
        events = _parse_sse(resp.text)
        event_types = [e['type'] for e in events]
        assert 'error' in event_types
        assert 'result' not in event_types
        error = next(e for e in events if e['type'] == 'error')
        assert 'MiniMax authentication failed' in error['error']
        assert '401' in error['error']

        visible = client.get('/api/actions/by-item?item_id=doc-low-value').json()['actions']
        assert visible == []

    def test_empty_llm_result_falls_back_to_visible_action(self, single_item_action_env, monkeypatch):
        def fake_process(item, api_key, api_base, model, system_prompt, on_thinking=None):
            if on_thinking:
                on_thinking('模型没有直接给出行动点')
            return None, None, 'test-log.json'

        monkeypatch.setattr(generate_actions_mod, 'process_single_item_streaming', fake_process)

        client = _admin_client(single_item_action_env['app'])
        resp = client.post(
            '/api/actions/generate-from-item',
            json={'item_id': 'doc-low-value', 'action_type': 'investigate'},
        )

        assert resp.status_code == 200, resp.text
        events = _parse_sse(resp.text)
        result = next(e for e in events if e['type'] == 'result')
        action = result['action']

        assert action is not None
        assert action['id']
        assert action['title'] == '深入了解 Weak Signal Item'
        assert action['action_type'] == 'investigate'
        assert action['source_item_ids'] == ['doc-low-value']

        visible = client.get('/api/actions/by-item?item_id=doc-low-value').json()['actions']
        assert [a['id'] for a in visible] == [action['id']]
        assert visible[0]['title'] == '深入了解 Weak Signal Item'
        assert visible[0]['source_item_ids'] == ['doc-low-value']

    def test_generated_action_is_persisted_and_respects_user_selected_type(
        self,
        single_item_action_env,
        monkeypatch,
    ):
        def fake_process(item, api_key, api_base, model, system_prompt, on_thinking=None):
            return {
                'title': '模型生成的内容任务',
                'action_type': 'content',
                'prompt': '请输出一份内容草稿。',
                'reason': '用户明确要求生成。',
                'priority': 'high',
            }, {'relevance': 8, 'actionability': 7}, 'test-log.json'

        monkeypatch.setattr(generate_actions_mod, 'process_single_item_streaming', fake_process)

        client = _admin_client(single_item_action_env['app'])
        resp = client.post(
            '/api/actions/generate-from-item',
            json={'item_id': 'doc-low-value', 'action_type': 'implement', 'user_hint': '做成检查清单'},
        )

        assert resp.status_code == 200, resp.text
        result = next(e for e in _parse_sse(resp.text) if e['type'] == 'result')
        action = result['action']

        assert action['action_type'] == 'implement'
        assert action['source_item_ids'] == ['doc-low-value']
        visible = client.get('/api/actions/by-item?item_id=doc-low-value').json()['actions']
        assert [a['id'] for a in visible] == [action['id']]
        assert visible[0]['action_type'] == 'implement'
        assert visible[0]['source_item_ids'] == ['doc-low-value']
