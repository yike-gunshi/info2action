"""AI search HTTP and executor contracts; no live database or provider calls."""
import asyncio
import hashlib
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID
from urllib.error import HTTPError

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from remote_db.ai_search import get_job as db_get_job


@pytest.fixture
def api(monkeypatch):
    from routes import ai_search as route
    from routes import admin
    monkeypatch.setattr(admin.remote_db, 'app_state_to_remote', lambda: True)
    monkeypatch.setattr(route, 'load_project_env', lambda _: {})
    for key in ('AI_SEARCH_ENABLED', 'AI_SEARCH_WINDOW_DAYS', 'AI_SEARCH_SIM_MIN',
                'AI_SEARCH_QUALITY_MIN', 'AI_SEARCH_MAX_ITEMS', 'AI_SEARCH_USER_DAY',
                'AI_SEARCH_CONCURRENCY', 'AI_SEARCH_CAP_DAY', 'AI_SEARCH_HARD_TIMEOUT_MIN'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('AI_SEARCH_IP_SALT', 'test-salt')
    monkeypatch.setenv('GLM_CODING_API_KEY', 'fake-test-key')
    monkeypatch.setenv('AI_SEARCH_WORKER_ENABLED', '1')
    monkeypatch.setattr(route, '_AI_SEARCH_EXECUTOR', True, raising=False)
    monkeypatch.setattr(route.limiter, 'enabled', False)
    monkeypatch.setattr(route, '_github_cluster_display_min_stars', lambda: 50)
    items = [{'id': 'b', 'title': 'B', 'sim': .8, 'legs': ['vector'], 'hs10': 8},
             {'id': 'a', 'title': 'A', 'sim': .7, 'legs': ['lexical'], 'hs10': None}]
    jobs, created, started = {}, [], []
    def create(**kwargs):
        jid = f'job-{len(created) + 1}'
        created.append(kwargs)
        jobs[jid] = {'id': jid, 'status': 'queued', 'created_at': datetime.now(timezone.utc), **kwargs}
        return jid
    def claim_next(concurrency):
        queued = sorted((job for job in jobs.values() if job['status'] == 'queued'),
                        key=lambda job: (job['created_at'], job['id']))
        if not queued:
            return None
        queued[0]['status'] = 'running'
        return queued[0]['id']
    monkeypatch.setattr(route.search, 'create_job', create)
    monkeypatch.setattr(route.search, 'claim_job', Mock(side_effect=AssertionError('must claim FIFO')))
    monkeypatch.setattr(route.search, 'get_job', lambda jid: jobs.get(jid))
    def used_day(user_id=None):
        return sum(not job.get('quota_exempt', False) and
                   (user_id is None or job.get('user_id') == user_id)
                   for job in jobs.values() if job.get('user_id'))
    def exhausted(user_id, user_day, cap_day):
        return (user_day is not None and used_day(user_id) >= user_day or
                cap_day is not None and used_day() >= cap_day)
    monkeypatch.setattr(route.search, 'quota_exhausted', Mock(side_effect=exhausted), raising=False)
    monkeypatch.setattr(route.search, 'count_all_day', Mock(side_effect=used_day), raising=False)
    monkeypatch.setattr(route.search, 'count_all_month', Mock(return_value=37), raising=False)
    monkeypatch.setattr(route.search, 'queue_position', lambda _: 3)
    monkeypatch.setattr(route.search, 'get_candidates', Mock(side_effect=lambda ids, **kw: [dict(i) for id in ids for i in items if i['id'] == id]))
    monkeypatch.setattr(route.search, 'retrieve', Mock(return_value={
        'items': items, 'query_lexical': '"test"', 'lexical_hits': 1,
        'vector_hits': 2, 'vector_ok': True}))
    monkeypatch.setattr(route, '_spawn_job', started.append)
    monkeypatch.setattr(route.search, 'claim_next_queued', Mock(side_effect=claim_next))
    monkeypatch.setattr(route.search, 'save_job_input', Mock())
    monkeypatch.setattr(route.search, 'finish_job', Mock())
    monkeypatch.setattr(route.search, 'fail_job', Mock())
    monkeypatch.setattr(route.search, 'recover_stale', Mock())
    monkeypatch.setattr(route.search, 'list_jobs', Mock(return_value=[]))
    monkeypatch.setattr(route.search, 'count_jobs', Mock(return_value=9))
    def user_jobs(user_id):
        return [job for job in jobs.values() if job.get('user_id') == user_id]
    monkeypatch.setattr(route.search, 'list_user_jobs', Mock(side_effect=lambda user_id, limit:
        sorted(user_jobs(user_id), key=lambda job: job['created_at'], reverse=True)[:limit]), raising=False)
    monkeypatch.setattr(route.search, 'count_user_jobs', Mock(side_effect=lambda user_id:
        len(user_jobs(user_id))), raising=False)
    model_config = {
        'AI_SEARCH_MODEL': 'glm-5.3', 'AI_SEARCH_MAX_TOKENS': 131072,
        'AI_SEARCH_REASONING_EFFORT': 'high', 'AI_SEARCH_PROMPT_PATH': 'fake.md',
        'AI_SEARCH_JOB_TIMEOUT_S': 2400}
    monkeypatch.setattr(route.digest, 'settings', lambda: model_config)
    monkeypatch.setattr(route.digest, 'load_prompt', lambda _: ('v1', 'prompt'))
    monkeypatch.setattr(route.digest, 'build_input', lambda *a: {'user_message': 'full input', 'tiers': {'b': 'content', 'a': 'summary'}})
    monkeypatch.setattr(route.digest, 'run_digest', Mock(return_value={
        'input_text': 'full input', 'tiers': {'b': 'content', 'a': 'summary'},
        'raw': '{"content_md":"answer"}',
        'result': {'content_md': 'answer'}, 'content_md_final': 'answer [1]',
        'cited_ids': ['b'], 'usage': {'prompt_tokens': 10000}, 'elapsed_ms': 12,
        'model': 'glm-5.3', 'prompt_version': 'v1', 'request_params': {'max_tokens': 131072}}))
    app = FastAPI()
    app.state.limiter = route.limiter
    @app.middleware('http')
    async def user_context(request, call_next):
        role = request.headers.get('x-test-role')
        request.state.user = {'id': request.headers.get('x-test-user-id', 'user-1'), 'role': role} if role else None
        request.state.legacy_authenticated = request.headers.get('x-test-legacy') == '1'
        return await call_next(request)
    app.include_router(route.router)
    with TestClient(app) as client:
        client.headers['x-test-role'] = 'user'
        yield route, client, jobs, created, started


def test_post_normal(api):
    route, client, jobs, created, started = api
    response = client.post('/api/ai-search', json={'q': ' test '}, headers={'X-Forwarded-For': '1.2.3.4, 9.8.7.6'})
    assert response.status_code == 200
    data = response.json()
    assert set(data) >= {'job_id', 'query', 'query_lexical', 'window_days', 'lexical_hits', 'vector_hits', 'vector_ok', 'items', 'summary'}
    assert data['query'] == 'test' and data['summary']['status'] == 'running'
    assert started == ['job-1'] and created[0]['candidate_ids'] == ['b', 'a']
    assert created[0]['ip_hash'] == hashlib.sha256(b'test-salt1.2.3.4').hexdigest()[:16]
    assert created[0]['cap_day'] == 5 and created[0]['user_day'] == 2
    assert not created[0]['quota_exempt']
    assert created[0]['candidate_meta'][0]['sim'] == .8
    assert created[0]['user_id'] == 'user-1'
    assert 'client_ip' not in created[0]
    kwargs = route.search.retrieve.call_args.kwargs
    assert not kwargs['public_only'] and kwargs['user_id'] == 'user-1'
    assert kwargs['window_days'] == 90 and kwargs['quality_min'] is None


def test_post_queue_and_signed_in_visibility(api, monkeypatch):
    route, client, _, created, started = api
    route.search.claim_next_queued.side_effect = None
    route.search.claim_next_queued.return_value = None
    data = client.post('/api/ai-search', json={'q': 'test'}, headers={'x-test-role': 'user'}).json()
    assert data['summary'] == {'status': 'queued', 'position': 3}
    assert not started
    assert created[0]['user_id'] == 'user-1'
    kwargs = route.search.retrieve.call_args.kwargs
    assert not kwargs['public_only'] and kwargs['manual_owner_user_id'] == 'user-1'


def test_create_job_binds_identity_to_user_id_column(monkeypatch):
    from remote_db import ai_search as search

    class Connection:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=None):
            self.calls.append((' '.join(sql.split()), params))
            return self

        def fetchone(self):
            return {'id': 'persisted-job'}

    conn = Connection()

    @contextmanager
    def connect():
        yield conn

    monkeypatch.setattr(search._core, 'connect', connect)
    monkeypatch.setattr(search._core, 'remote_schema', lambda: 'test_search')
    user_id = "signed-in-user'1"
    assert search.create_job(
        query='identity test', query_lexical='identity test', ip_hash='test-ip-hash',
        user_id=user_id, candidate_ids=['item-1'], candidate_meta=[],
    ) == 'persisted-job'

    [(sql, params)] = [(sql, params) for sql, params in conn.calls if sql.startswith('INSERT ')]
    column_sql, value_sql = sql.split(' VALUES (', 1)
    assert column_sql.startswith('INSERT INTO test_search.ai_search_jobs ')
    columns = [value.strip() for value in column_sql.split('(', 1)[1].rsplit(')', 1)[0].split(',')]
    values = [value.strip() for value in value_sql.rsplit(') RETURNING ', 1)[0].split(',')]
    assert len(columns) == len(values)
    assert columns.count('user_id') == 1
    assert values[columns.index('user_id')] == '%(user_id)s'
    assert params['user_id'] == user_id
    assert user_id not in sql


def test_empty_does_not_create_or_count(api):
    route, client, _, created, started = api
    route.search.retrieve.return_value['items'] = []
    data = client.post('/api/ai-search', json={'q': 'test'}).json()
    assert data['job_id'] is None and data['items'] == [] and data['summary'] == {'status': 'none'}
    assert not created and not started
    route.search.quota_exhausted.assert_called_once_with('user-1', 2, 5)


def test_login_required_before_retrieval(api):
    route, client, _, created, _ = api
    response = client.post('/api/ai-search', json={'q': 'test'}, headers={'x-test-role': ''})
    assert response.status_code == 401 and response.json() == {'error': 'login_required'}
    route.search.retrieve.assert_not_called()
    route.search.quota_exhausted.assert_not_called()
    assert not created


@pytest.mark.parametrize('key,default', [('AI_SEARCH_USER_DAY', 2), ('AI_SEARCH_CAP_DAY', 5)])
@pytest.mark.parametrize('configured,expected', [('', None), ('  ', None), ('0', 0), ('-1', 0), ('7', 7), ('invalid', 'default')])
def test_daily_settings_support_unlimited_and_zero(api, monkeypatch, key, default, configured, expected):
    route = api[0]
    monkeypatch.setenv(key, configured)
    assert route.settings()['user_day' if key == 'AI_SEARCH_USER_DAY' else 'cap_day'] == (default if expected == 'default' else expected)


def test_empty_limits_allow_unlimited_search(api, monkeypatch):
    route, client, jobs, created, started = api
    monkeypatch.setenv('AI_SEARCH_USER_DAY', '')
    monkeypatch.setenv('AI_SEARCH_CAP_DAY', '')
    for _ in range(8):
        assert client.post('/api/ai-search', json={'q': 'test'}).status_code == 200
    assert len(created) == len(started) == 8
    assert created[0]['user_day'] is None and created[0]['cap_day'] is None


@pytest.mark.parametrize('key', ['AI_SEARCH_USER_DAY', 'AI_SEARCH_CAP_DAY'])
def test_zero_daily_limit_disables_search(api, monkeypatch, key):
    route, client, _, created, _ = api
    monkeypatch.setenv(key, '0')
    response = client.post('/api/ai-search', json={'q': 'test'})
    assert response.status_code == 429 and response.json() == {'error': 'quota_exhausted'}
    route.search.retrieve.assert_not_called()
    assert not created


def test_third_user_search_is_rejected_before_retrieval(api):
    route, client, _, created, started = api
    for _ in range(2):
        assert client.post('/api/ai-search', json={'q': 'test'}).status_code == 200
    route.search.retrieve.reset_mock()
    response = client.post('/api/ai-search', json={'q': 'test'})
    assert response.status_code == 429 and response.json() == {'error': 'quota_exhausted'}
    route.search.retrieve.assert_not_called()
    assert len(created) == len(started) == 2


def test_sixth_site_search_is_rejected_without_placeholder(api):
    route, client, _, created, started = api
    for user in ['a', 'a', 'b', 'b', 'c']:
        assert client.post('/api/ai-search', json={'q': 'test'}, headers={'x-test-user-id': user}).status_code == 200
    route.search.retrieve.reset_mock()
    response = client.post('/api/ai-search', json={'q': 'test'}, headers={'x-test-user-id': 'd'})
    assert response.status_code == 429 and response.json() == {'error': 'quota_exhausted'}
    route.search.retrieve.assert_not_called()
    assert len(created) == len(started) == 5


@pytest.mark.parametrize('headers', [{'x-test-role': 'admin'}, {'x-test-role': '', 'x-test-legacy': '1'}])
def test_admin_and_legacy_skip_limits_and_site_count(api, monkeypatch, headers):
    route, client, _, created, _ = api
    monkeypatch.setenv('AI_SEARCH_USER_DAY', '0')
    monkeypatch.setenv('AI_SEARCH_CAP_DAY', '0')
    for _ in range(7):
        assert client.post('/api/ai-search', json={'q': 'test'}, headers=headers).status_code == 200
    route.search.quota_exhausted.assert_not_called()
    assert all(job['quota_exempt'] for job in created)
    assert route.search.count_all_day() == 0


@pytest.mark.parametrize('status,error', [('failed', 'timeout'), ('failed', 'hard_timeout'), ('failed', 'parse_error'), ('unavailable', 'quota')])
def test_failed_and_unavailable_tasks_consume_daily_quota(api, status, error):
    _, client, jobs, created, _ = api
    for _ in range(2):
        data = client.post('/api/ai-search', json={'q': 'test'}).json()
        jobs[data['job_id']].update(status=status, error=error)
    response = client.post('/api/ai-search', json={'q': 'test'})
    assert response.status_code == 429 and response.json() == {'error': 'quota_exhausted'}
    assert len(created) == 2


@pytest.mark.parametrize('enabled,role,legacy,exhausted,expected', [
    ('0', '', False, False, (False, False, False, 'disabled')),
    ('0', 'user', False, True, (False, True, False, 'disabled')),
    ('1', '', False, False, (True, False, False, 'login_required')),
    ('1', 'user', False, False, (True, True, True, None)),
    ('1', 'user', False, True, (True, True, False, 'quota_exhausted')),
    ('1', 'admin', False, True, (True, True, True, None)),
    ('1', '', True, True, (True, True, True, None)),
])
def test_status_branches_and_no_used_or_remaining_counts(api, monkeypatch, enabled, role, legacy, exhausted, expected):
    route, client, _, _, _ = api
    monkeypatch.setenv('AI_SEARCH_ENABLED', enabled)
    route.search.quota_exhausted.side_effect = None
    route.search.quota_exhausted.return_value = exhausted
    response = client.get('/api/ai-search/status', headers={'x-test-role': role, 'x-test-legacy': '1' if legacy else '0'})
    assert response.status_code == 200
    assert response.json() == dict(zip(('enabled', 'logged_in', 'can_search', 'reason'), expected), user_day_limit=2)
    if enabled == '1' and role == 'user':
        route.search.quota_exhausted.assert_called_once_with('user-1', 2, 5)
    else:
        route.search.quota_exhausted.assert_not_called()


def test_status_unlimited_user_limit_is_null(api, monkeypatch):
    monkeypatch.setenv('AI_SEARCH_USER_DAY', '')
    response = api[1].get('/api/ai-search/status', headers={'x-test-role': ''})
    assert response.status_code == 200 and response.json()['user_day_limit'] is None


@pytest.mark.parametrize('body', [{'q': ''}, {'q': '  '}, {'q': 'x' * 201}, {'q': 12}, {}, []])
def test_bad_query(api, body):
    route, client, _, created, _ = api
    assert client.post('/api/ai-search', json=body).status_code == 400
    route.search.retrieve.assert_not_called()
    assert not created


def test_disabled(api, monkeypatch):
    route, client, _, _, _ = api
    monkeypatch.setenv('AI_SEARCH_ENABLED', '0')
    response = client.post('/api/ai-search', json={'q': 'test'})
    assert response.status_code == 404 and response.json() == {'error': 'ai_search_disabled'}
    assert client.get('/api/ai-search/job-1').status_code == 404
    assert client.post('/api/ai-search/job-1/retry').status_code == 404
    route.search.retrieve.assert_not_called()


def test_history_anonymous_returns_empty_without_querying_jobs(api):
    route, client, _, _, _ = api
    response = client.get('/api/ai-search/history?user_id=user-1', headers={'x-test-role': ''})
    assert response.status_code == 200
    assert response.json() == {'items': [], 'anonymous': True}
    route.search.list_user_jobs.assert_not_called()
    route.search.count_user_jobs.assert_not_called()
    route.search.retrieve.assert_not_called()


def test_history_signed_in_without_records(api):
    route, client, _, _, _ = api
    response = client.get('/api/ai-search/history', headers={'x-test-role': 'user'})
    assert response.status_code == 200
    assert response.json() == {'items': [], 'anonymous': False, 'total': 0}
    route.search.list_user_jobs.assert_called_once_with('user-1', 10)
    route.search.count_user_jobs.assert_called_once_with('user-1')


@pytest.mark.parametrize('role', ['user', 'admin'])
def test_history_only_lists_current_user_even_with_user_id_query(api, role):
    route, client, jobs, _, _ = api
    for user_id in ('user-1', 'user-2'):
        response = client.post('/api/ai-search', json={'q': f'{user_id} search'},
                               headers={'x-test-role': 'user', 'x-test-user-id': user_id})
        assert response.status_code == 200
    jobs['legacy'] = {'id': 'legacy', 'query': 'unowned search', 'status': 'done',
                      'created_at': datetime.now(timezone.utc)}
    for user_id, job_id in [('user-1', 'job-1'), ('user-2', 'job-2')]:
        response = client.get('/api/ai-search/history?user_id=someone-else',
                              headers={'x-test-role': role, 'x-test-user-id': user_id})
        assert response.status_code == 200
        assert response.json() == {'anonymous': False, 'total': 1, 'items': [{
            'job_id': job_id, 'query': f'{user_id} search', 'status': 'running',
            'created_at': jobs[job_id]['created_at'].isoformat()}]}
        route.search.list_user_jobs.assert_called_with(user_id, 10)
        route.search.count_user_jobs.assert_called_with(user_id)


@pytest.mark.parametrize('limit,expected_count', [(None, 10), (2, 2), (0, 1), (-7, 1), (50, 50), (100, 50)])
def test_history_orders_limits_and_returns_total_with_only_row_fields(api, limit, expected_count):
    route, client, jobs, _, _ = api
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    statuses = ['queued', 'running', 'done', 'failed', 'unavailable']
    for index in range(60):
        job_id = UUID(int=index + 1)
        jobs[str(job_id)] = {
            'id': job_id, 'query': f'search {index}', 'created_at': start + timedelta(minutes=index),
            'status': statuses[index % len(statuses)], 'user_id': 'user-1',
            'content_md': 'private', 'content_md_final': 'private', 'candidate_ids': ['a'],
            'candidate_meta': [{'id': 'a'}], 'input_text': 'private', 'usage': {'prompt_tokens': 7}}
    url = '/api/ai-search/history' + ('' if limit is None else f'?limit={limit}')
    response = client.get(url, headers={'x-test-role': 'user'})
    assert response.status_code == 200
    assert response.json() == {'anonymous': False, 'total': 60, 'items': [{
        'job_id': str(UUID(int=index + 1)), 'query': f'search {index}',
        'created_at': (start + timedelta(minutes=index)).isoformat(),
        'status': statuses[index % len(statuses)]}
        for index in range(59, 59 - expected_count, -1)]}
    route.search.list_user_jobs.assert_called_once_with('user-1', expected_count)
    route.search.count_user_jobs.assert_called_once_with('user-1')


@pytest.mark.parametrize('role', [None, 'user'])
def test_history_disabled_returns_404_without_querying_jobs(api, monkeypatch, role):
    route, client, _, _, _ = api
    monkeypatch.setenv('AI_SEARCH_ENABLED', '0')
    response = client.get('/api/ai-search/history', headers={'x-test-role': role} if role else {})
    assert response.status_code == 404
    assert response.json() == {'error': 'ai_search_disabled'}
    route.search.list_user_jobs.assert_not_called()
    route.search.count_user_jobs.assert_not_called()


@pytest.mark.parametrize('status', ['queued', 'running', 'done', 'failed', 'unavailable'])
def test_get_states(api, status):
    route, client, jobs, _, _ = api
    jobs['old'] = {'id': 'old', 'status': status, 'query': '怎么使用 seedance 2.5',
                   'candidate_ids': ['b', 'a'], 'candidate_meta': [{'id': 'b', 'sim': .8}],
                   'content_md_final': 'answer', 'cited_ids': ['b'], 'usage': {'prompt_tokens': 7}, 'model': 'glm-5.3',
                   'elapsed_ms': 99, 'error': 'quota' if status == 'unavailable' else 'oops', 'input_text': 'private'}
    response = client.get('/api/ai-search/old')
    assert response.status_code == 200
    data = response.json()
    assert data['job_id'] == 'old' and data['status'] == status
    assert data['query'] == jobs['old']['query']
    assert [x['id'] for x in data['items']] == ['b', 'a'] and data['items'][0]['sim'] == .8
    assert 'input_text' not in data
    if status == 'queued': assert data['position'] == 3
    if status == 'done': assert data['content_md'] == 'answer' and data['cited_count'] == 1 and data['cited_ids'] == ['b']
    else: assert data.get('content_md') is None
    if status == 'failed': assert data['error'] == 'internal'
    if status == 'unavailable': assert data['reason'] == 'quota'


def test_get_missing(api):
    assert api[1].get('/api/ai-search/missing').status_code == 404


def test_get_items_follow_citation_order_and_keep_uncited_candidates(api):
    route, client, jobs, _, _ = api
    candidates = [{'id': value, 'title': value} for value in ['a', 'b', 'c', 'd']]
    processed = route.digest.postprocess('{"content_md": "先[3]。后[1]。再引[3]"}', candidates)
    jobs['ordered'] = {'id': 'ordered', 'status': 'done', **processed,
                       'candidate_ids': ['a', 'b', 'c', 'd'],
                       'candidate_meta': [{'id': 'c', 'sim': .8, 'legs': ['vector'], 'tier': 'content'},
                                          {'id': 'b', 'sim': .7}]}
    route.search.get_candidates.side_effect = None
    route.search.get_candidates.return_value = list(reversed(candidates))
    response = client.get('/api/ai-search/ordered')
    assert response.status_code == 200
    data = response.json()
    assert [item['id'] for item in data['items']] == ['c', 'a', 'b', 'd']
    assert data['content_md'] == '先[1]。后[2]。再引[1]\n\n基于 2 条内容'
    for number, cited_id in enumerate(data['cited_ids'], 1):
        assert data['items'][number - 1]['id'] == cited_id
    assert data['items'][0]['sim'] == .8 and data['items'][0]['legs'] == ['vector']
    assert data['items'][2]['sim'] == .7
    assert 'tier' not in data['items'][0]
    assert jobs['ordered']['candidate_ids'] == ['a', 'b', 'c', 'd']


@pytest.mark.parametrize('visible_ids', [['a', 'b'], ['a', 'c'], []])
def test_reordered_job_hides_summary_when_any_candidate_is_invisible(api, visible_ids):
    route, client, jobs, _, _ = api
    jobs['ordered'] = {'id': 'ordered', 'status': 'done', 'candidate_ids': ['a', 'b', 'c'],
                       'cited_ids': ['c', 'a'], 'content_md_final': 'secret [1] public [2]'}
    route.search.get_candidates.side_effect = None
    route.search.get_candidates.return_value = [{'id': value} for value in visible_ids]
    response = client.get('/api/ai-search/ordered')
    assert response.status_code == 404
    assert response.json() == {'error': 'job_not_found'}


def test_private_candidate_blocks_summary(api):
    route, client, jobs, created, _ = api
    jobs['old'] = {'id': 'old', 'status': 'failed', 'candidate_ids': ['secret'], 'content_md_final': 'secret'}
    assert client.get('/api/ai-search/old').status_code == 404
    assert not created


@pytest.mark.parametrize('visible_ids', [['b', 'a'], ['b'], []])
def test_get_shared_job_uses_source_visibility_not_job_owner(api, visible_ids):
    route, client, jobs, _, _ = api
    jobs['shared'] = {'id': 'shared', 'user_id': 'another-user', 'status': 'done',
                      'query': 'shared search', 'candidate_ids': ['b', 'a'],
                      'content_md_final': 'shared answer', 'cited_ids': ['b']}
    route.search.get_candidates.side_effect = None
    route.search.get_candidates.return_value = [{'id': item_id} for item_id in visible_ids]
    response = client.get('/api/ai-search/shared', headers={'x-test-role': 'user'})
    assert route.search.get_candidates.call_args.kwargs['user_id'] == 'user-1'
    if len(visible_ids) == 2:
        assert response.status_code == 200
        assert response.json()['content_md'] == 'shared answer'
        assert 'user_id' not in response.json()
    else:
        assert response.status_code == 404
        assert response.json() == {'error': 'job_not_found'}


def test_retry_endpoint_removed(api):
    route, client, _, created, _ = api
    assert client.post('/api/ai-search/old/retry').status_code in (404, 405)
    assert not any(r.path.endswith('/retry') for r in route.router.routes)
    assert not created


@pytest.mark.parametrize('role', [None, 'user', 'admin'])
def test_admin(api, role):
    route, client, jobs, _, _ = api
    jobs['old'] = {'id': 'old', 'input_text': 'full input', 'result': {}, 'candidate_meta': [],
                   'request_params': {}, 'usage': {}, 'points_estimate': 2, 'query_lexical': 'test',
                   'model': 'glm-5.3', 'prompt_version': 'v1', 'content_md_final': 'answer', 'cited_ids': []}
    headers = {'x-test-role': role} if role else {}
    listing = client.get('/api/admin/ai-search/jobs?limit=2&offset=3&status=failed', headers=headers)
    detail = client.get('/api/admin/ai-search/jobs/old', headers=headers)
    if role != 'admin':
        assert listing.status_code == detail.status_code == 403
    else:
        assert listing.status_code == detail.status_code == 200
        data = listing.json()
        assert data['total'] == 9 and data['limit'] == 2 and data['offset'] == 3
        assert data['quota'] == {'cap_day': 5, 'used_day': 0, 'used_month': 37, 'user_day': 2, 'concurrency': 2}
        assert data['params'] == {'model': 'glm-5.3', 'max_tokens': 131072, 'reasoning_effort': 'high', 'sim_min': .45, 'quality_min': None, 'window_days': 90}
        assert detail.json() == jobs['old']
        route.search.count_jobs.assert_called_once_with('failed')
        route.search.count_all_month.assert_called_once_with()
        assert client.get('/api/admin/ai-search/jobs/missing', headers=headers).status_code == 404


@pytest.mark.parametrize('role', [None, 'user', 'admin'])
@pytest.mark.parametrize('enabled', ['0', '1'])
def test_admin_requires_remote_mode(api, monkeypatch, role, enabled):
    from routes import admin
    route, client, _, _, _ = api
    monkeypatch.setattr(admin.remote_db, 'app_state_to_remote', lambda: False)
    monkeypatch.setenv('AI_SEARCH_ENABLED', enabled)
    get_job = Mock()
    monkeypatch.setattr(route.search, 'get_job', get_job)
    headers = {'x-test-role': role} if role else {}
    for path in ('/api/admin/ai-search/jobs', '/api/admin/ai-search/jobs/old'):
        response = client.get(path, headers=headers)
        if role == 'admin':
            assert response.status_code == 501
            assert response.json() == {'reason': 'remote_required'}
        else:
            assert response.status_code == 403
    route.search.list_jobs.assert_not_called()
    route.search.count_jobs.assert_not_called()
    route.search.count_all_day.assert_not_called()
    route.search.count_all_month.assert_not_called()
    get_job.assert_not_called()


@pytest.mark.parametrize('error', [None, 'quota', 'parse', 'other'])
def test_run_job(api, error):
    route, _, jobs, _, _ = api
    jobs['old'] = {'id': 'old', 'status': 'running', 'query': 'test', 'candidate_ids': ['b', 'a'],
                   'candidate_meta': [{'id': 'b'}, {'id': 'a'}]}
    if error:
        route.digest.run_digest.side_effect = {'quota': route.digest.QuotaExhausted('429 quota'),
            'parse': route.digest.ParseError('bad raw'), 'other': RuntimeError('broken')}[error]
    asyncio.run(route._run_job('old'))
    saved = route.search.save_job_input.call_args.kwargs
    assert saved['input_text'] == 'full input' and saved['candidate_meta'][0]['tier'] == 'content'
    assert [i['id'] for i in route.digest.run_digest.call_args.args[1]] == ['b', 'a']
    if error is None:
        kw = route.search.finish_job.call_args.kwargs
        assert kw['input_text'] == 'full input' and kw['points_estimate'] == pytest.approx(6.9)
        assert kw['candidate_meta'][1]['tier'] == 'summary'
        route.search.fail_job.assert_not_called()
    else:
        kw = route.search.fail_job.call_args.kwargs
        assert kw['status'] == ('unavailable' if error == 'quota' else 'failed')
        assert kw['error'] == {'quota': 'quota', 'parse': 'parse_error', 'other': 'broken'}[error]
        if error == 'quota': assert kw['result']['error_detail'] == '429 quota'
        if error == 'parse': assert kw['raw'] == 'bad raw'
        route.search.finish_job.assert_not_called()
    route.search.claim_next_queued.assert_called()


def test_run_job_persists_first_appearance_numbers_before_serving(api):
    route, client, jobs, _, _ = api
    jobs['old'] = {'id': 'old', 'status': 'running', 'query': 'test',
                   'candidate_ids': ['b', 'a'], 'candidate_meta': []}
    processed = route.digest.postprocess('{"content_md": "先[2]。后[1]。再引[2]"}',
                                         [{'id': 'b'}, {'id': 'a'}])
    route.digest.run_digest.return_value.update(processed)
    asyncio.run(route._run_job('old'))
    saved = route.search.finish_job.call_args.kwargs
    assert saved['content_md_final'] == '先[1]。后[2]。再引[1]\n\n基于 2 条内容'
    assert saved['cited_ids'] == ['a', 'b']
    assert saved['result']['content_md'] == '先[2]。后[1]。再引[2]'
    jobs['old'].update(saved, status='done')
    response = client.get('/api/ai-search/old').json()
    assert response['content_md'] == saved['content_md_final']
    assert [item['id'] for item in response['items']] == saved['cited_ids']


def test_kick_next_fills_capacity(api):
    route, _, _, _, started = api
    route.search.claim_next_queued.side_effect = ['one', 'two', None]
    asyncio.run(route._kick_next())
    assert started == ['one', 'two']


def test_sweeper_startup_periodic_and_disabled(api, monkeypatch):
    route, _, _, _, _ = api
    real_sleep = asyncio.sleep
    now = [0.]
    monkeypatch.setattr(route, 'time', __import__('types').SimpleNamespace(monotonic=lambda: now[0]), raising=False)
    calls = []
    async def sleep(delay):
        calls.append(delay)
        now[0] += delay
        if len(calls) == 13:
            raise asyncio.CancelledError
        await real_sleep(0)
    monkeypatch.setattr(route.asyncio, 'sleep', sleep)
    async def run():
        task = route.start_ai_search_sweeper(is_leader=True)
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert calls == [5] * 13
    assert route.search.recover_stale.call_count == 2
    assert route.search.recover_stale.call_args_list[0].kwargs == {'requeue_before': route._PROCESS_STARTED_AT}
    assert route.search.recover_stale.call_args_list[1].kwargs == {'requeue_before': None}
    monkeypatch.setenv('AI_SEARCH_ENABLED', '0')
    assert route.start_ai_search_sweeper() is None


def test_real_auth_middleware_public_status_and_login_gate(api, monkeypatch):
    route, _, _, _, _ = api
    from middleware import auth
    monkeypatch.setattr(auth, '_AUTH_TOKEN', 'protected-token')
    monkeypatch.setattr(auth, '_try_jwt_auth', lambda request: None)
    app = FastAPI()
    app.state.limiter = route.limiter
    app.include_router(route.router)
    app.add_middleware(auth.AuthTokenMiddleware)
    with TestClient(app) as client:
        response = client.get('/api/ai-search/history')
        assert response.status_code == 200
        assert response.json() == {'items': [], 'anonymous': True}
        assert client.get('/api/ai-search/status').json()['reason'] == 'login_required'
        response = client.post('/api/ai-search', json={'q': 'test'})
        assert response.status_code == 401 and response.json() == {'error': 'login_required'}
        assert client.post('/api/ai-search/job-1/retry').status_code in (404, 405)
        assert client.get('/api/admin/ai-search/jobs').status_code == 403
        assert client.get('/api/admin/ai-search/jobs/job-1').status_code == 403
    assert not auth._is_public_api('/api/admin/ai-search/jobs/job-1/delete', 'GET')
    assert not auth._is_public_api('/api/ai-search-other', 'POST')


@pytest.mark.parametrize('leader', [True, False])
@pytest.mark.parametrize('worker', ['0', '1'])
def test_app_lifespan_leader_sweeper_and_shutdown(api, lifespan_app, monkeypatch, leader, worker):
    route, main = api[0], lifespan_app
    monkeypatch.setattr(main, '_acquire_web_leader_lock', lambda: leader)
    monkeypatch.setattr(main, '_web_leader_lock_handle', object() if leader else None)
    monkeypatch.setenv('AI_SEARCH_WORKER_ENABLED', worker)
    tasks = []
    real_start = route.start_ai_search_sweeper
    async def sweep():
        try:
            await asyncio.Event().wait()
        finally:
            assert route._AI_SEARCH_EXECUTOR is False
    monkeypatch.setattr(route, '_sweep', sweep)
    def start(*, is_leader):
        assert is_leader is leader
        task = real_start(is_leader=is_leader)
        if task is None:
            return None
        tasks.append(task)
        return task
    monkeypatch.setattr(route, 'start_ai_search_sweeper', start)
    async def run():
        async with main.lifespan(main.app):
            await asyncio.sleep(0)
            assert len(tasks) == int(leader and worker == '1')
            assert route._AI_SEARCH_EXECUTOR is (leader and worker == '1')
        assert route._AI_SEARCH_EXECUTOR is False
        if tasks:
            assert tasks[0].cancelled()
    asyncio.run(run())
    assert main.app.state.limiter is route.limiter
    paths = {r.path for r in main.app.routes}
    assert '/api/ai-search' in paths and '/api/admin/ai-search/jobs' in paths


@pytest.fixture
def lifespan_app(api, monkeypatch):
    import app as main
    monkeypatch.setattr(main, '_threadpool_tokens', lambda: 0)
    monkeypatch.setattr(main, '_sqlite_startup_summary', lambda: {'authority': 'test', 'database': 'stub', 'items': 0})
    monkeypatch.setattr(main.remote_db, 'remote_authority_enabled', lambda: False)
    monkeypatch.setattr(main.terminal, 'recover_tmux_sessions', lambda: None)
    monkeypatch.setattr(main.fetch, 'recover_orphaned_fetch_runs_from_previous_process', lambda: [])
    monkeypatch.setattr(main.fetch, 'has_local_active_fetch_runs', lambda: False)
    monkeypatch.setattr(main.fetch, 'interrupt_active_fetch_runs_for_shutdown', lambda: [])
    monkeypatch.setenv('INFO2ACTION_BACKEND_HOURLY_FETCH', '0')
    monkeypatch.setenv('INFO2ACTION_DYNAMIC_FETCH_ENABLED', '0')
    return main


def test_app_disabled_does_not_fall_through_to_spa(api, monkeypatch):
    import runpy
    from middleware import auth
    monkeypatch.setenv('AI_SEARCH_ENABLED', '0')
    monkeypatch.setattr(auth, '_AUTH_TOKEN', '')
    monkeypatch.setattr(auth, '_try_jwt_auth', lambda _: None)
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'src/app.py'), run_name='ai_search_disabled_app')
    client = TestClient(namespace['app'])  # No context manager: skip lifespan.
    response = client.get('/api/ai-search/status')
    assert response.status_code == 200
    assert response.json() == {'enabled': False, 'logged_in': False, 'can_search': False,
                               'reason': 'disabled', 'user_day_limit': 2}
    for method, path in [('POST', '/api/ai-search'), ('GET', '/api/ai-search/history'), ('GET', '/api/ai-search/old'),
                         ('POST', '/api/ai-search/old/retry'), ('GET', '/api/admin/ai-search/jobs')]:
        response = client.request(method, path)
        assert response.status_code == 404
        assert response.json() == {'error': 'ai_search_disabled'}
    assert not any(r.endpoint is api[0].post_search for r in namespace['app'].routes)


def test_atomic_quota_recheck_returns_429_without_cards(api, monkeypatch):
    route, client, jobs, created, started = api
    # Precheck passes; a competing request consumes quota before the insertion.
    assert hasattr(route.search, 'QuotaExceeded')
    monkeypatch.setattr(route.search, 'create_job', Mock(side_effect=route.search.QuotaExceeded()))
    response = client.post('/api/ai-search', json={'q': 'test'})
    assert response.status_code == 429 and response.json() == {'error': 'quota_exhausted'}
    assert not started and not created
    route.search.retrieve.assert_called_once()
    kwargs = route.search.create_job.call_args.kwargs
    assert kwargs['cap_day'] == 5 and kwargs['user_day'] == 2


def test_cf_client_ip_takes_priority(api):
    _, client, _, created, _ = api
    response = client.post('/api/ai-search', json={'q': 'test'}, headers={
        'CF-Connecting-IP': '5.6.7.8', 'X-Forwarded-For': '1.2.3.4, 9.8.7.6'})
    assert response.status_code == 200
    assert created[0]['ip_hash'] == hashlib.sha256(b'test-salt5.6.7.8').hexdigest()[:16]


def test_ip_salt_falls_back_to_jwt_secret(api, monkeypatch):
    route, client, _, created, _ = api
    monkeypatch.delenv('AI_SEARCH_IP_SALT')
    monkeypatch.setenv('JWT_SECRET', 'jwt-test-secret')
    monkeypatch.setenv('SECRET_KEY', 'unused-secret')
    assert route.settings()['ip_salt'] == 'jwt-test-secret'
    client.post('/api/ai-search', json={'q': 'test'}, headers={'CF-Connecting-IP': '1.2.3.4'})
    assert created[0]['ip_hash'] == hashlib.sha256(b'jwt-test-secret1.2.3.4').hexdigest()[:16]


@pytest.mark.parametrize('error,expected', [
    (None, None), ('', None), ('parse_error', 'parse_error'), ('hard_timeout', 'hard_timeout'),
    ('candidates_unavailable', 'candidates_unavailable'),
    ('quota', 'quota'), ('HTTP 429: secret provider body', 'internal'), ('provider COOLDOWN active', 'internal'),
    ('exceeded quota: secret', 'internal'), ('read timed out: private url', 'timeout'),
    ('TimeoutError: private host', 'timeout'), ('psycopg SQL secret', 'internal'),
])
@pytest.mark.parametrize('status', ['failed', 'unavailable'])
def test_public_errors_sanitized_admin_retains_raw(api, error, expected, status):
    route, client, jobs, _, _ = api
    jobs['old'] = {'id': 'old', 'status': status, 'candidate_ids': ['a'], 'error': error}
    data = client.get('/api/ai-search/old').json()
    assert data['error'] == expected
    if status == 'unavailable':
        assert data['reason'] == expected
    headers = {'x-test-role': 'admin'}
    assert client.get('/api/admin/ai-search/jobs/old', headers=headers).json()['error'] == error
    route.search.list_jobs.return_value = [jobs['old']]
    assert client.get('/api/admin/ai-search/jobs', headers=headers).json()['jobs'][0]['error'] == error


@pytest.mark.parametrize('configured,read_s,expected', [
    (None, 2400, 45), ('40', 2400, 45), ('1', 2401, 46), ('60', 2400, 60), ('45', 3600, 65),
])
def test_hard_timeout_lower_bound(api, monkeypatch, configured, read_s, expected):
    route = api[0]
    route.digest.settings()['AI_SEARCH_JOB_TIMEOUT_S'] = read_s
    if configured is not None:
        monkeypatch.setenv('AI_SEARCH_HARD_TIMEOUT_MIN', configured)
    assert route.settings()['hard_timeout'] == expected


@pytest.mark.parametrize('worker,leader', [('0', True), ('1', False), ('0', False)])
def test_nonexecutor_post_only_queues_and_never_claims_or_runs(api, monkeypatch, worker, leader):
    route, client, jobs, created, started = api
    monkeypatch.setenv('AI_SEARCH_WORKER_ENABLED', worker)
    monkeypatch.setattr(route, '_AI_SEARCH_EXECUTOR', leader and worker == '1')
    response = client.post('/api/ai-search', json={'q': 'test'})
    assert response.status_code == 200
    assert response.json()['job_id'] == 'job-1'
    assert response.json()['summary'] == {'status': 'queued', 'position': 3}
    assert jobs['job-1']['status'] == 'queued' and len(created) == 1
    assert not started and not route._AI_SEARCH_BG_TASKS
    route.search.claim_job.assert_not_called()
    route.search.claim_next_queued.assert_not_called()
    route.digest.run_digest.assert_not_called()
    asyncio.run(route._kick_next())
    asyncio.run(route._run_job('job-1'))
    route.search.claim_next_queued.assert_not_called()
    route.digest.run_digest.assert_not_called()


def test_executor_post_claims_oldest_job_instead_of_just_created_id(api):
    route, client, jobs, _, started = api
    now = datetime.now(timezone.utc)
    jobs['older'] = {'id': 'older', 'status': 'queued', 'created_at': now - timedelta(seconds=10)}
    response = client.post('/api/ai-search', json={'q': 'test'})
    assert response.status_code == 200
    assert started == ['older', 'job-1']
    route.search.claim_job.assert_not_called()
    assert route.search.claim_next_queued.call_args_list[0].args == (2,)


@pytest.mark.parametrize('step', ['claim_next_queued', 'get_job', 'queue_position'])
def test_post_after_paid_creation_returns_job_even_if_followup_fails(api, monkeypatch, step):
    route, client, jobs, created, _ = api
    if step == 'queue_position':
        route.search.claim_next_queued.side_effect = None
        route.search.claim_next_queued.return_value = None
    monkeypatch.setattr(route.search, step, Mock(side_effect=RuntimeError('DB temporarily unavailable')))
    response = client.post('/api/ai-search', json={'q': 'test'})
    assert response.status_code == 200
    assert response.json()['job_id'] == 'job-1'
    assert response.json()['summary'] == {'status': 'queued'}
    assert len(created) == len(jobs) == 1
    assert route.search.count_all_day() == 1


@pytest.mark.parametrize('key', ['', '   '])
def test_missing_key_disables_status_post_and_sweeper(api, monkeypatch, key):
    route, client, _, created, started = api
    monkeypatch.setenv('GLM_CODING_API_KEY', key)
    assert client.get('/api/ai-search/status').json() == {
        'enabled': False, 'logged_in': True, 'can_search': False,
        'reason': 'disabled', 'user_day_limit': 2}
    response = client.post('/api/ai-search', json={'q': 'test'})
    assert response.status_code == 404 and response.json() == {'error': 'ai_search_disabled'}
    async def run():
        assert route.start_ai_search_sweeper(is_leader=True) is None
    asyncio.run(run())
    assert not created and not started
    route.search.retrieve.assert_not_called()
    route.search.claim_next_queued.assert_not_called()
    route.search.recover_stale.assert_not_called()


@pytest.mark.parametrize('worker,leader', [('0', True), ('1', False)])
def test_sweeper_requires_both_worker_switch_and_leader(api, monkeypatch, worker, leader):
    route = api[0]
    monkeypatch.setenv('AI_SEARCH_WORKER_ENABLED', worker)
    async def run():
        assert route.start_ai_search_sweeper(is_leader=leader) is None
        await route._kick_next()
    asyncio.run(run())
    route.search.recover_stale.assert_not_called()
    route.search.claim_next_queued.assert_not_called()


def test_worker_switch_defaults_off_and_blank_enabled_uses_default(api, monkeypatch):
    route = api[0]
    monkeypatch.delenv('AI_SEARCH_WORKER_ENABLED')
    assert route.settings()['worker_enabled'] is False
    for value in ('', '  '):
        monkeypatch.setenv('AI_SEARCH_ENABLED', value)
        assert route.settings()['enabled'] is True


@pytest.mark.parametrize('job_id', ['not-a-uuid', '123', "x' OR true", '00000000-0000-0000-0000-00000000000z'])
@pytest.mark.parametrize('admin', [False, True])
def test_invalid_uuid_reads_return_not_found_without_connecting(api, monkeypatch, job_id, admin):
    route, client, _, _, _ = api
    monkeypatch.setattr(route.search, 'get_job', db_get_job)
    monkeypatch.setattr(route.search._core, 'connect', Mock(side_effect=AssertionError('invalid UUID must not reach DB')))
    path = '/api/admin/ai-search/jobs/' if admin else '/api/ai-search/'
    response = client.get(path + job_id, headers={'x-test-role': 'admin' if admin else 'user'})
    assert response.status_code == 404 and response.json() == {'error': 'job_not_found'}
    route.search._core.connect.assert_not_called()


@pytest.mark.parametrize('failures', [1, 2, 3, 4])
def test_finish_write_retries_and_preserves_generated_output_on_final_failure(api, monkeypatch, failures):
    route, _, jobs, _, _ = api
    jobs['old'] = {'id': 'old', 'status': 'running', 'query': 'test',
                   'candidate_ids': ['b', 'a'], 'candidate_meta': []}
    raw = '原始输出' * 60000
    route.digest.run_digest.return_value['raw'] = raw
    route.search.finish_job.side_effect = [RuntimeError('finish write failed')] * failures + [None]
    delays = []
    async def sleep(delay):
        delays.append(delay)
    monkeypatch.setattr(route.asyncio, 'sleep', sleep)
    asyncio.run(route._run_job('old'))
    assert route.search.finish_job.call_count == min(failures + 1, 4)
    assert delays == [1, 3, 9][:min(failures, 3)]
    route.digest.run_digest.assert_called_once()
    if failures < 4:
        route.search.fail_job.assert_not_called()
    else:
        kwargs = route.search.fail_job.call_args.kwargs
        assert kwargs['status'] == 'failed'
        assert kwargs['result']['content_md'] == 'answer'
        assert kwargs['result']['content_md_final'] == 'answer [1]'
        assert kwargs['result']['raw'] == raw


@pytest.mark.parametrize('kind', ['cooldown', 'wrapped_cooldown', 'http429', 'wrapped429', 'misleading_text'])
def test_quota_error_classified_by_exception_type_with_real_guard_message(api, kind):
    import ai_provider_guard as guard
    route, client, jobs, _, _ = api
    jobs['old'] = {'id': 'old', 'status': 'running', 'query': 'test',
                   'candidate_ids': ['b', 'a'], 'candidate_meta': []}
    until = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    message = guard._default_cooldown_message('glm-coding', until, 'ai_search')
    assert message.startswith('glm-coding provider is cooling down until ')
    errors = {
        'cooldown': guard.ProviderCooldown('glm-coding', until.isoformat(), message),
        'wrapped_cooldown': route.digest.QuotaExhausted(message),
        'http429': HTTPError('url', 429, 'arbitrary response', {}, None),
        'wrapped429': route.digest.QuotaExhausted('arbitrary response'),
        'misleading_text': RuntimeError('quota cooldown HTTP 429 is only a string'),
    }
    route.digest.run_digest.side_effect = errors[kind]
    asyncio.run(route._run_job('old'))
    failure = route.search.fail_job.call_args.kwargs
    jobs['old'].update(status=failure['status'], error=failure['error'])
    data = client.get('/api/ai-search/old').json()
    if kind == 'misleading_text':
        assert failure['status'] == 'failed' and data['error'] == 'internal'
    else:
        assert failure['status'] == 'unavailable'
        assert data['error'] == data['reason'] == 'quota'


def test_sweeper_retry_uses_fixed_process_start_boundary_and_five_second_interval(api, monkeypatch):
    route = api[0]
    now = [0.]
    monkeypatch.setattr(route, 'time', __import__('types').SimpleNamespace(monotonic=lambda: now[0]), raising=False)
    cutoff = datetime(2026, 10, 10, 11, tzinfo=timezone.utc)
    monkeypatch.setattr(route, '_PROCESS_STARTED_AT', cutoff, raising=False)
    route.search.recover_stale.side_effect = [RuntimeError('startup DB failure'), None, None]
    delays = []
    async def sleep(delay):
        delays.append(delay)
        now[0] += delay
        if len(delays) == 3:
            raise asyncio.CancelledError
    monkeypatch.setattr(route.asyncio, 'sleep', sleep)
    async def run():
        task = route.start_ai_search_sweeper(is_leader=True)
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert delays == [5, 5, 5]
    assert [call.kwargs for call in route.search.recover_stale.call_args_list] == [
        {'requeue_before': cutoff}, {'requeue_before': cutoff}]


@pytest.mark.parametrize('initial_worker', ['0', '1'])
def test_executor_identity_is_fixed_at_startup(api, monkeypatch, initial_worker):
    route = api[0]
    monkeypatch.setenv('AI_SEARCH_WORKER_ENABLED', initial_worker)
    async def idle():
        await asyncio.Event().wait()
    monkeypatch.setattr(route, '_sweep', idle)
    async def run():
        task = route.start_ai_search_sweeper(is_leader=True)
        expected = initial_worker == '1'
        assert route._is_executor() is expected
        monkeypatch.setenv('AI_SEARCH_WORKER_ENABLED', '0' if expected else '1')
        monkeypatch.setenv('AI_SEARCH_ENABLED', '0')
        monkeypatch.setenv('GLM_CODING_API_KEY', '')
        assert route._is_executor() is expected
        monkeypatch.setattr(route, 'settings', Mock(side_effect=AssertionError('identity must not read config')))
        assert route._is_executor() is expected
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_claimed_job_finishes_even_if_executor_has_been_stopped(api, monkeypatch):
    route, _, jobs, _, started = api
    jobs['claimed'] = {'id': 'claimed', 'status': 'running', 'query': 'test',
                       'candidate_ids': ['b', 'a'], 'candidate_meta': []}
    jobs['next'] = {'id': 'next', 'status': 'queued', 'created_at': datetime.now(timezone.utc)}
    monkeypatch.setattr(route, '_AI_SEARCH_EXECUTOR', False)
    asyncio.run(route._run_job('claimed'))
    route.digest.run_digest.assert_called_once()
    route.search.finish_job.assert_called_once()
    route.search.claim_next_queued.assert_not_called()
    assert started == []


def test_shutdown_disables_claiming_before_sweeper_cancellation_and_job_completion(api, lifespan_app, monkeypatch):
    route, _, jobs, _, started = api
    main = lifespan_app
    monkeypatch.setattr(main, '_acquire_web_leader_lock', lambda: True)
    monkeypatch.setattr(main, '_web_leader_lock_handle', object())
    jobs['claimed'] = {'id': 'claimed', 'status': 'running', 'query': 'test',
                       'candidate_ids': ['b', 'a'], 'candidate_meta': []}
    jobs['next'] = {'id': 'next', 'status': 'queued', 'created_at': datetime.now(timezone.utc)}
    async def sweep():
        try:
            await asyncio.Event().wait()
        finally:
            assert route._is_executor() is False
            # Simulate an existing GLM call completing during shutdown grace.
            await route._run_job('claimed')
    monkeypatch.setattr(route, '_sweep', sweep)
    async def run():
        async with main.lifespan(main.app):
            await asyncio.sleep(0)
            assert route._is_executor() is True
        assert route._is_executor() is False
    asyncio.run(run())
    route.search.finish_job.assert_called_once()
    route.search.claim_next_queued.assert_not_called()
    assert started == [] and jobs['next']['status'] == 'queued'


def test_stop_function_prevents_a_completed_job_from_draining_queue(api):
    route, _, jobs, _, started = api
    jobs['claimed'] = {'id': 'claimed', 'status': 'running', 'query': 'test',
                       'candidate_ids': ['b', 'a'], 'candidate_meta': []}
    route.stop_ai_search_sweeper()
    assert route._is_executor() is False
    asyncio.run(route._run_job('claimed'))
    route.search.finish_job.assert_called_once()
    route.search.claim_next_queued.assert_not_called()
    assert started == []


@pytest.mark.parametrize('fallback', ['lock_file_unavailable', 'fcntl_unavailable'])
def test_degraded_leader_keeps_other_scheduling_but_cannot_execute_ai_jobs(api, lifespan_app, monkeypatch, tmp_path, fallback):
    route, main = api[0], lifespan_app
    monkeypatch.setattr(main, '_web_leader_lock_handle', None)
    monkeypatch.setenv('INFO2ACTION_WEB_LEADER_LOCK', str(tmp_path / 'web-leader.lock'))
    if fallback == 'lock_file_unavailable':
        monkeypatch.setattr(main, 'open', Mock(side_effect=OSError('cannot create lock file')), raising=False)
    else:
        import builtins
        original_import = builtins.__import__
        def import_(name, *args, **kwargs):
            if name == 'fcntl':
                raise ImportError('no file locking')
            return original_import(name, *args, **kwargs)
        monkeypatch.setattr(builtins, '__import__', import_)
    other_scheduling = Mock()
    monkeypatch.setattr(main.terminal, 'recover_tmux_sessions', other_scheduling)
    async def run():
        assert main._acquire_web_leader_lock() is True
        assert main._web_leader_lock_handle is None
        async with main.lifespan(main.app):
            assert route._is_executor() is False
            await route._kick_next()
    asyncio.run(run())
    other_scheduling.assert_called_once()
    route.search.claim_next_queued.assert_not_called()
    route.search.recover_stale.assert_not_called()
    route.digest.run_digest.assert_not_called()


@pytest.mark.parametrize('leader,key,worker,warn', [
    (True, 'fake-test-key', '0', True), (False, 'fake-test-key', '0', False),
    (True, '', '0', False), (True, 'fake-test-key', '1', False),
])
def test_startup_warns_leader_when_enabled_jobs_will_remain_queued(api, monkeypatch, capsys, leader, key, worker, warn):
    route = api[0]
    monkeypatch.setenv('GLM_CODING_API_KEY', key)
    monkeypatch.setenv('AI_SEARCH_WORKER_ENABLED', worker)
    async def idle():
        await asyncio.Event().wait()
    monkeypatch.setattr(route, '_sweep', idle)
    async def run():
        task = route.start_ai_search_sweeper(is_leader=leader)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())
    output = capsys.readouterr().out
    if warn:
        assert '[warn]' in output and 'queued' in output
        assert 'AI_SEARCH_WORKER_ENABLED=1' in output and 'production .env' in output
    else:
        assert 'AI_SEARCH_WORKER_ENABLED=1' not in output


@pytest.mark.parametrize('error_kind', ['finish_write', 'parse'])
def test_failed_result_persistence_retries_once_without_content_and_reaches_terminal_state(api, monkeypatch, error_kind):
    route, _, jobs, _, _ = api
    jobs['old'] = {'id': 'old', 'status': 'running', 'query': 'test',
                   'candidate_ids': ['b', 'a'], 'candidate_meta': []}
    if error_kind == 'finish_write':
        route.search.finish_job.side_effect = RuntimeError('result too large')
    else:
        route.digest.run_digest.side_effect = route.digest.ParseError('bad raw')
    async def sleep(_):
        pass
    monkeypatch.setattr(route.asyncio, 'sleep', sleep)
    def fail(jid, *, error, status, raw, result):
        if result is not None or raw is not None:
            raise RuntimeError('cannot store result content')
        jobs[jid].update(status=status, error=error)
    route.search.fail_job.side_effect = fail
    asyncio.run(route._run_job('old'))
    assert route.search.fail_job.call_count == 2
    first, last = route.search.fail_job.call_args_list
    assert first.kwargs['result'] is not None or first.kwargs['raw'] is not None
    assert last.kwargs['result'] is None and last.kwargs['raw'] is None
    assert last.kwargs['error'] == first.kwargs['error']
    assert jobs['old']['status'] == 'failed'
    route.digest.run_digest.assert_called_once()


@pytest.mark.parametrize('flip_config', [False, True])
def test_sweeper_claims_every_five_seconds_but_scans_timeouts_every_sixty(api, monkeypatch, flip_config):
    from types import SimpleNamespace
    route = api[0]
    now, claims, scans = [0.], [], []
    monkeypatch.setattr(route, 'time', SimpleNamespace(monotonic=lambda: now[0]), raising=False)
    route.search.recover_stale.side_effect = lambda *args, **kwargs: scans.append((now[0], kwargs))
    async def kick():
        claims.append(now[0])
    monkeypatch.setattr(route, '_kick_next', kick)
    async def sleep(delay):
        assert delay == 5
        now[0] += delay
        if flip_config:
            monkeypatch.setenv('AI_SEARCH_WORKER_ENABLED', '0')
            monkeypatch.setenv('AI_SEARCH_ENABLED', '0')
        if now[0] == 65:
            raise asyncio.CancelledError
    monkeypatch.setattr(route.asyncio, 'sleep', sleep)
    async def run():
        with pytest.raises(asyncio.CancelledError):
            await route.start_ai_search_sweeper(is_leader=True)
    asyncio.run(run())
    assert claims == list(range(0, 65, 5))
    assert scans == [(0., {'requeue_before': route._PROCESS_STARTED_AT}), (60., {'requeue_before': None})]


def test_env_example_documents_empty_key_disables_admin_logs_too():
    text = (Path(__file__).resolve().parents[1] / '.env.example').read_text()
    comment = text.split('GLM_CODING_API_KEY=', 1)[0].rsplit('\n#', 1)[1]
    assert '留空即关闭 AI 搜索' in comment and 'admin 日志接口' in comment
