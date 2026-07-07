"""v21.0 action-revival (D3/E4): per-user Discord 派发频道 + forum/text 分支路由。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import routes.actions as actions_route  # noqa: E402

_ACTION = {'title': 'T', 'prompt': 'p', 'id': 'a1b2c3d4', 'priority': 'medium', 'action_type': 'investigate'}


@pytest.fixture(autouse=True)
def _reset_tag_cache(monkeypatch):
    monkeypatch.setattr(actions_route, '_discord_tags_cache', None)
    # 隔离掉全局 config 的 tag 解析,聚焦频道路由
    monkeypatch.setattr(actions_route, '_resolve_tag_ids', lambda action: [])
    monkeypatch.setattr(actions_route, '_load_discord_config', lambda: {})


def test_dispatch_forum_creates_thread(monkeypatch):
    calls = []

    def fake_api(method, endpoint, payload=None, bot_token=None):
        calls.append((method, endpoint))
        if method == 'GET':
            return {'type': 15}  # forum channel
        return {'id': 'thread-1', 'guild_id': 'g1'}

    monkeypatch.setattr(actions_route, '_discord_api', fake_api)
    tid, url = actions_route._dispatch_to_discord(_ACTION, channel_id='123')
    assert ('POST', '/channels/123/threads') in calls
    assert tid == 'thread-1'
    assert url == 'https://discord.com/channels/g1/thread-1'


def test_dispatch_text_channel_posts_message(monkeypatch):
    calls = []

    def fake_api(method, endpoint, payload=None, bot_token=None):
        calls.append((method, endpoint))
        if method == 'GET':
            return {'type': 0}  # text channel
        return {'id': 'msg-1', 'guild_id': 'g1'}

    monkeypatch.setattr(actions_route, '_discord_api', fake_api)
    mid, url = actions_route._dispatch_to_discord(_ACTION, channel_id='456')
    assert ('POST', '/channels/456/messages') in calls
    assert ('POST', '/channels/456/threads') not in calls
    assert mid == 'msg-1'
    assert url == 'https://discord.com/channels/g1/456/msg-1'


def test_dispatch_requires_a_channel(monkeypatch):
    # 无 per-user channel + 全局 config 空 → 明确报错
    monkeypatch.setattr(actions_route, '_discord_api', lambda *a, **k: {})
    with pytest.raises(ValueError):
        actions_route._dispatch_to_discord(_ACTION)
