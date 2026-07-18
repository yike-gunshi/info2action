"""Regression tests for submit URL concurrency and task retention limits."""
import asyncio
import hashlib
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import remote_db
import routes.submit as submit_route


class FakeRequest:
    def __init__(self, user_id: str, body: dict):
        self.state = SimpleNamespace(user={'id': user_id})
        self._body = body

    async def json(self):
        return self._body


@pytest.fixture(autouse=True)
def reset_submit_state(monkeypatch):
    submit_route._submit_tasks.clear()
    monkeypatch.setattr(submit_route, '_submit_active_total', 0, raising=False)
    monkeypatch.setattr(submit_route, '_submit_active_by_user', {}, raising=False)
    monkeypatch.setattr(submit_route, '_SUBMIT_MAX_GLOBAL', 12, raising=False)
    monkeypatch.setattr(submit_route, '_SUBMIT_MAX_PER_USER', 3, raising=False)
    monkeypatch.setattr(submit_route, '_SUBMIT_TASKS_MAX', 256, raising=False)
    monkeypatch.setattr(submit_route, '_SUBMIT_TASK_TTL_SEC', 900, raising=False)
    yield
    submit_route._submit_tasks.clear()


def _prepare_new_submit(monkeypatch):
    monkeypatch.setattr(submit_route, '_is_blocked_submit_target', lambda _host: False)
    monkeypatch.setattr(remote_db, 'app_state_to_remote', lambda: True)
    monkeypatch.setattr(remote_db, 'get_submit_existing_item_remote', lambda *_args: None)


def _post(user_id: str, url: str):
    return asyncio.run(submit_route.post_submit_url(FakeRequest(user_id, {'url': url})))


def test_invalid_submit_limit_env_uses_default(monkeypatch):
    parser = getattr(submit_route, '_submit_env_int', None)
    assert parser is not None
    monkeypatch.setenv('TEST_SUBMIT_LIMIT', 'invalid')
    assert parser('TEST_SUBMIT_LIMIT', 7) == 7
    monkeypatch.setenv('TEST_SUBMIT_LIMIT', '0')
    assert parser('TEST_SUBMIT_LIMIT', 7) == 7


def test_evict_submit_tasks_removes_expired_terminal_tasks_only(monkeypatch):
    evict = getattr(submit_route, '_evict_stale_submit_tasks', None)
    assert evict is not None
    monkeypatch.setattr(submit_route.time, 'time', lambda: 2_000)
    monkeypatch.setattr(submit_route, '_SUBMIT_TASK_TTL_SEC', 900)
    submit_route._submit_tasks.update({
        'active-old': {'status': 'processing', 'created_at': 1},
        'terminal-old': {'status': 'done', 'created_at': 1_000},
        'terminal-new': {'status': 'error', 'created_at': 1_500},
    })

    evict()

    assert set(submit_route._submit_tasks) == {'active-old', 'terminal-new'}


def test_evict_submit_tasks_drops_oldest_terminal_until_below_capacity(monkeypatch):
    evict = getattr(submit_route, '_evict_stale_submit_tasks', None)
    assert evict is not None
    monkeypatch.setattr(submit_route.time, 'time', lambda: 2_000)
    monkeypatch.setattr(submit_route, '_SUBMIT_TASK_TTL_SEC', 10_000)
    monkeypatch.setattr(submit_route, '_SUBMIT_TASKS_MAX', 3)
    submit_route._submit_tasks.update({
        'active-old': {'status': 'fetching', 'created_at': 1},
        'terminal-oldest': {'status': 'done', 'created_at': 1_000},
        'terminal-middle': {'status': 'failed', 'created_at': 1_100},
        'terminal-newest': {'status': 'ready', 'created_at': 1_200},
    })

    evict()

    assert set(submit_route._submit_tasks) == {'active-old', 'terminal-newest'}


def test_duplicate_active_url_returns_before_quota(monkeypatch):
    user_id = 'user-duplicate'
    url = 'https://example.com/already-running'
    item_id = hashlib.md5(url.encode()).hexdigest()
    submit_route._submit_active_total = 12
    submit_route._submit_active_by_user[user_id] = 3
    submit_route._submit_tasks[item_id] = {
        'status': 'processing',
        'url': url,
        'title': '',
        'error': '',
        'user_id': user_id,
    }

    response = _post(user_id, url)

    assert response == {'ok': True, 'task_id': item_id, 'status': 'processing'}
    assert submit_route._submit_active_total == 12
    assert submit_route._submit_active_by_user[user_id] == 3


def test_submit_rejects_when_global_limit_is_full(monkeypatch):
    _prepare_new_submit(monkeypatch)
    submit_route._submit_active_total = 12
    started = {'value': False}

    class DummyThread:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            started['value'] = True

    monkeypatch.setattr(submit_route.threading, 'Thread', DummyThread)

    response = _post('user-global', 'https://example.com/global-limit')

    assert response.status_code == 429
    assert json.loads(response.body) == {
        'error': '服务器提交任务已满,请稍后重试',
        'code': 'submit_busy',
    }
    assert started['value'] is False
    assert submit_route._submit_tasks == {}


def test_submit_rejects_when_user_limit_is_full(monkeypatch):
    _prepare_new_submit(monkeypatch)
    submit_route._submit_active_by_user['user-full'] = 3
    started = {'value': False}

    class DummyThread:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            started['value'] = True

    monkeypatch.setattr(submit_route.threading, 'Thread', DummyThread)

    response = _post('user-full', 'https://example.com/user-limit')

    assert response.status_code == 429
    assert json.loads(response.body) == {
        'error': '你有太多提交任务在进行中,请等待完成后再试',
        'code': 'submit_user_limit',
    }
    assert started['value'] is False
    assert submit_route._submit_tasks == {}


def test_submit_thread_start_failure_rolls_back_slot_and_task(monkeypatch):
    _prepare_new_submit(monkeypatch)
    url = 'https://example.com/start-failure'
    item_id = hashlib.md5(url.encode()).hexdigest()

    class FailingThread:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            raise RuntimeError('thread start failed')

    monkeypatch.setattr(submit_route.threading, 'Thread', FailingThread)

    with pytest.raises(RuntimeError, match='thread start failed'):
        _post('user-start-failure', url)

    assert submit_route._submit_active_total == 0
    assert 'user-start-failure' not in submit_route._submit_active_by_user
    assert item_id not in submit_route._submit_tasks


def test_submit_background_releases_slot_after_completion(monkeypatch):
    import fetch_url

    _prepare_new_submit(monkeypatch)
    monkeypatch.setattr(fetch_url, 'fetch_url', lambda _url: {
        'title': 'A complete submitted page title',
        'content': 'x' * 80,
    })
    monkeypatch.setattr(remote_db, 'upsert_item_remote', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(remote_db, 'set_status', lambda **_kwargs: None)
    monkeypatch.setattr(remote_db, 'get_feed_item', lambda **_kwargs: {'title': 'Submitted title'})
    monkeypatch.setattr(
        submit_route.subprocess,
        'run',
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )
    observed = {}

    class ImmediateThread:
        def __init__(self, target, args, daemon):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            observed['active_total_at_start'] = submit_route._submit_active_total
            observed['active_user_at_start'] = submit_route._submit_active_by_user.get('user-complete')
            self.target(*self.args)

    monkeypatch.setattr(submit_route.threading, 'Thread', ImmediateThread)

    response = _post('user-complete', 'https://example.com/completes')

    assert response['status'] == 'fetching'
    assert observed == {'active_total_at_start': 1, 'active_user_at_start': 1}
    assert submit_route._submit_active_total == 0
    assert 'user-complete' not in submit_route._submit_active_by_user
    task = submit_route._submit_tasks[response['task_id']]
    assert task['status'] == 'done'
    assert isinstance(task['created_at'], float)
