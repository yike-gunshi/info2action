"""T3 HTTP/worker contracts. Every database and model boundary is stubbed."""
import asyncio
import hashlib
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests.test_ai_search_api import api as base_api


@pytest.fixture
def real_functions():
    from routes import ai_search as route
    return SimpleNamespace(build_input=route.digest.build_input,
                           run_digest=route.digest.run_digest, spawn=route._spawn_job)


@pytest.fixture
def api(real_functions, base_api):
    # Capture originals before base_api replaces them, during setup only.
    return base_api


@pytest.fixture
def v2_api(api, monkeypatch):
    route = api[0]
    monkeypatch.setenv('AI_SEARCH_PIPELINE', 'v2')
    for key in ('AI_SEARCH_RETRIEVAL_USER_DAY', 'AI_SEARCH_RETRIEVAL_CONCURRENCY'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(route, '_AI_SEARCH_SCHEMA_ERROR_LOGGED', False)
    monkeypatch.setattr(route.search._core, 'connect', Mock(side_effect=AssertionError('live DB forbidden')))
    monkeypatch.setattr(route.search, 'get_job', Mock(side_effect=lambda jid, **kw: api[2].get(jid)))
    monkeypatch.setattr(route.search, 'create_retrieval_job', Mock(return_value='retrieval-1'))
    monkeypatch.setattr(route.search, 'count_user_retrieval_day', Mock(return_value=0))
    monkeypatch.setattr(route.search, 'update_retrieval_progress', Mock())
    monkeypatch.setattr(route.search, 'finish_retrieval', Mock(return_value='queued'))
    monkeypatch.setattr(route.retrieval, 'settings', lambda: {
        'AI_SEARCH_RETRIEVAL_TIMEOUT_S': 60, 'AI_SEARCH_INPUT_MAX_TOKENS': 300000,
        'AI_SEARCH_CHARS_PER_TOKEN': 1.7})
    monkeypatch.setattr(route.retrieval, 'run', Mock(return_value=retrieval_result()))
    monkeypatch.setattr(route.digest, 'call_glm', Mock(side_effect=AssertionError('live GLM forbidden')))
    def spawned(coroutine):
        values = coroutine.cr_frame.f_locals
        api[4].append(values.get('job_id') or values['job']['id'])
        coroutine.close()
    monkeypatch.setattr(route, '_spawn_job', spawned)
    return api


def retrieval_result(outcome='ok'):
    return dict(outcome=outcome, candidate_ids=['b', 'a'],
                candidate_meta=[{'id': 'b', 'score': .9}, {'id': 'a', 'score': .8}],
                retrieval={'stage': 'searching', 'keywords': ['Muse', 'Cue']},
                lexical_hits=3, vector_hits=4, vector_ok=True, window_days=7,
                query_lexical='Muse | Cue')


def retrieval_job(scope=None):
    return dict(id='retrieval-1', query='question', status='retrieving',
                retrieval={'scope': scope} if scope is not None else {})


def test_post_v2_only_creates_durable_job(v2_api):
    route, client, _, created, _ = v2_api
    response = client.post('/api/ai-search', json={'q': ' question '},
                           headers={'X-Forwarded-For': '1.2.3.4'})
    assert response.status_code == 200
    assert response.json() == {'job_id': 'retrieval-1', 'query': 'question',
                               'summary': {'status': 'retrieving'}}
    route.search.create_retrieval_job.assert_called_once_with(
        query='question', ip_hash=hashlib.sha256(b'test-salt1.2.3.4').hexdigest()[:16],
        user_id='user-1', scope=dict(public_only=False, user_id='user-1',
            manual_owner_user_id='user-1', min_github_stars=50, quota_exempt=False),
        user_day=2, cap_day=5, retrieval_user_day=10, quota_exempt=False)
    assert not created
    route.search.retrieve.assert_not_called()
    route.search.quota_exhausted.assert_not_called()
    route.search.claim_next_retrieving.assert_called_once_with(2)


@pytest.mark.parametrize('kind,status,error', [
    ('writing_quota', 429, 'quota_exhausted'), ('retrieval_quota', 429, 'quota_exhausted'),
    ('in_progress', 409, 'retrieval_in_progress')])
def test_post_v2_atomic_rejections(v2_api, kind, status, error):
    route, client, _, created, started = v2_api
    route.search.create_retrieval_job.side_effect = (
        route.search.RetrievalInProgress() if kind == 'in_progress' else route.search.QuotaExceeded())
    response = client.post('/api/ai-search', json={'q': 'question'})
    assert response.status_code == status and response.json() == {'error': error}
    assert not created and not started
    route.search.claim_next_retrieving.assert_not_called()
    route.search.retrieve.assert_not_called()
    route.search.quota_exhausted.assert_not_called()


@pytest.mark.parametrize('identity', ['admin', 'legacy'])
def test_post_v2_admin_and_legacy_exempt(v2_api, identity):
    route, client, _, _, _ = v2_api
    client.headers.clear()
    headers = {'x-test-role': 'admin'} if identity == 'admin' else {'x-test-legacy': '1'}
    assert client.post('/api/ai-search', json={'q': 'q'}, headers=headers).status_code == 200
    kwargs = route.search.create_retrieval_job.call_args.kwargs
    assert kwargs['quota_exempt'] is True and kwargs['scope']['quota_exempt'] is True
    assert kwargs['scope']['manual_owner_user_id'] is None


@pytest.mark.parametrize('executor,claim_error', [(True, False), (False, False), (True, True)])
def test_post_v2_claims_without_waiting_and_preserves_id(v2_api, monkeypatch, executor, claim_error):
    route, client, _, _, started = v2_api
    monkeypatch.setattr(route, '_AI_SEARCH_EXECUTOR', executor)
    route.search.claim_next_retrieving.side_effect = (
        RuntimeError('claim unavailable') if claim_error else [retrieval_job(), None])
    response = client.post('/api/ai-search', json={'q': 'q'})
    assert response.status_code == 200 and response.json()['job_id'] == 'retrieval-1'
    assert started == (['retrieval-1'] if executor and not claim_error else [])
    assert route.search.claim_next_retrieving.call_count == (1 if claim_error else 2 if executor else 0)
    route.retrieval.run.assert_not_called()


@pytest.mark.parametrize('pipeline,identity,v2', [
    ('v1', 'user', False), ('v1', 'admin', False), ('v1', 'legacy', False),
    ('v2_admin', 'user', False), ('v2_admin', 'admin', True), ('v2_admin', 'legacy', True),
    ('v2', 'user', True), ('v2', 'admin', True), ('v2', 'legacy', True),
    ('invalid', 'admin', False)])
def test_pipeline_dispatch(v2_api, monkeypatch, capsys, pipeline, identity, v2):
    route, client, _, _, _ = v2_api
    monkeypatch.setenv('AI_SEARCH_PIPELINE', pipeline)
    client.headers.clear()
    headers = {'x-test-role': identity} if identity != 'legacy' else {'x-test-legacy': '1'}
    assert client.post('/api/ai-search', json={'q': 'q'}, headers=headers).status_code == 200
    assert route.search.create_retrieval_job.called is v2
    assert route.search.retrieve.called is not v2
    if pipeline == 'invalid':
        assert '[warn]' in capsys.readouterr().out and route.settings()['pipeline'] == 'v1'


def test_pipeline_defaults_and_configuration_bounds(v2_api, monkeypatch):
    route = v2_api[0]
    monkeypatch.delenv('AI_SEARCH_PIPELINE')
    assert route.settings()['pipeline'] == 'v2_admin'
    assert route.settings()['retrieval_concurrency'] == 2
    assert route.settings()['retrieval_user_day'] == 10
    monkeypatch.setenv('AI_SEARCH_RETRIEVAL_CONCURRENCY', '0')
    monkeypatch.setenv('AI_SEARCH_RETRIEVAL_USER_DAY', '-1')
    assert route.settings()['retrieval_concurrency'] == 1
    assert route.settings()['retrieval_user_day'] == 0
    monkeypatch.setenv('AI_SEARCH_RETRIEVAL_CONCURRENCY', 'bad')
    monkeypatch.setenv('AI_SEARCH_RETRIEVAL_USER_DAY', 'bad')
    assert route.settings()['retrieval_concurrency'] == 2
    assert route.settings()['retrieval_user_day'] == 10


@pytest.mark.parametrize('value', ['', '   '])
def test_pipeline_empty_value_uses_default_without_warning(v2_api, monkeypatch, capsys, value):
    route = v2_api[0]
    monkeypatch.setenv('AI_SEARCH_PIPELINE', value)
    assert route.settings()['pipeline'] == 'v2_admin'
    assert '[warn]' not in capsys.readouterr().out


@pytest.mark.parametrize('logged_in,q,status,error', [
    (False, '', 401, 'login_required'), (True, '', 400, 'invalid_query'),
    (True, 'x' * 201, 400, 'invalid_query')])
def test_v2_login_and_query_validation_before_creation(v2_api, logged_in, q, status, error):
    route, client, _, _, _ = v2_api
    if not logged_in:
        client.headers.clear()
    response = client.post('/api/ai-search', json={'q': q})
    assert response.status_code == status and response.json() == {'error': error}
    route.search.create_retrieval_job.assert_not_called()


@pytest.mark.parametrize('quota_exempt', [False, True])
@pytest.mark.parametrize('progress_failure', [False, True])
@pytest.mark.parametrize('final_status', ['queued', 'insufficient', 'failed'])
def test_retrieval_success_scope_progress_and_handoff(v2_api, monkeypatch, quota_exempt, progress_failure, final_status):
    route = v2_api[0]
    scope = dict(public_only=False, user_id='owner', manual_owner_user_id='owner',
                 min_github_stars=12, quota_exempt=quota_exempt)
    job, result = retrieval_job(scope), retrieval_result()
    main_thread, callbacks = threading.get_ident(), []
    def run(question, **kwargs):
        assert threading.get_ident() != main_thread
        assert question == 'question' and kwargs['timeout_s'] == 60
        assert {key: kwargs[key] for key in scope if key != 'quota_exempt'} == {
            key: value for key, value in scope.items() if key != 'quota_exempt'}
        assert 'quota_exempt' not in kwargs
        kwargs['progress']({'stage': 'searching', 'keywords': ['Muse']})
        return result
    def progress(jid, patch):
        callbacks.append((jid, patch, threading.get_ident()))
        if progress_failure:
            raise RuntimeError('progress unavailable')
    route.retrieval.run.side_effect = run
    route.search.update_retrieval_progress.side_effect = progress
    route.search.finish_retrieval.return_value = final_status
    kick_write, kick_retrieval = AsyncMock(), AsyncMock()
    monkeypatch.setattr(route, '_kick_next', kick_write)
    monkeypatch.setattr(route, '_kick_retrieval', kick_retrieval)
    asyncio.run(route._run_retrieval(job))
    assert callbacks[0][:2] == ('retrieval-1', {'stage': 'searching', 'keywords': ['Muse']})
    assert callbacks[0][2] != main_thread
    route.search.finish_retrieval.assert_called_once_with(
        'retrieval-1', 'ok', **{key: value for key, value in result.items() if key != 'outcome'},
        user_day=2, cap_day=5, quota_exempt=quota_exempt)
    assert kick_write.await_count == (1 if final_status == 'queued' else 0)
    kick_retrieval.assert_awaited_once()
    route.search.fail_job.assert_not_called()
    assert job['retrieval']['scope'] == scope


def test_retrieval_missing_scope_defaults_to_public_and_not_exempt(v2_api):
    route = v2_api[0]
    asyncio.run(route._run_retrieval(retrieval_job()))
    kwargs = route.retrieval.run.call_args.kwargs
    assert kwargs['public_only'] is True and kwargs['user_id'] is None
    assert kwargs['manual_owner_user_id'] is None and kwargs['min_github_stars'] == 50
    assert route.search.finish_retrieval.call_args.kwargs['quota_exempt'] is False


@pytest.mark.parametrize('kind,code', [
    ('timeout', 'retrieval_timeout'), ('retrieval', 'retrieval_failed'),
    ('other', 'retrieval_failed'), ('outer_timeout', 'retrieval_timeout')])
def test_retrieval_failures_persist_partial_record_before_failure(v2_api, monkeypatch, kind, code):
    route = v2_api[0]
    partial = {'preview': {'titles': ['Muse']}, 'stage': 'searching'}
    if kind == 'timeout':
        exc = route.retrieval.RetrievalTimeout(retrieval=partial)
    elif kind == 'retrieval':
        exc = route.retrieval.RetrievalError('failure ' + 'x' * 600, retrieval=partial)
    else:
        exc = RuntimeError('unexpected failure')
    route.retrieval.run.side_effect = exc
    events = []
    route.search.update_retrieval_progress.side_effect = lambda jid, patch: events.append(('progress', patch))
    route.search.fail_job.side_effect = lambda jid, **kw: events.append(('fail', kw['error']))
    if kind == 'outer_timeout':
        async def timeout(awaitable, *, timeout):
            assert timeout == 75
            awaitable.close()
            raise asyncio.TimeoutError
        monkeypatch.setattr(route.asyncio, 'wait_for', timeout)
    asyncio.run(route._run_retrieval(retrieval_job()))
    assert events[-1] == ('fail', code)
    route.search.finish_retrieval.assert_not_called()
    if kind == 'outer_timeout':
        assert events == [('fail', code)]
    else:
        assert events[0][0] == 'progress' and len(events[0][1]['error']) <= 500
        if kind in ('timeout', 'retrieval'):
            assert events[0][1] == {**partial, 'error': str(exc)[:500]}


def test_retrieval_failure_progress_write_error_still_fails_job(v2_api):
    route = v2_api[0]
    route.retrieval.run.side_effect = route.retrieval.RetrievalError('failure', retrieval={'preview': {}})
    route.search.update_retrieval_progress.side_effect = RuntimeError('DB unavailable')
    asyncio.run(route._run_retrieval(retrieval_job()))
    route.search.fail_job.assert_called_once_with('retrieval-1', error='retrieval_failed')


@pytest.mark.parametrize('failures', [1, 2, 3, 4])
def test_retrieval_finish_retries_and_leaves_job_for_recovery(v2_api, monkeypatch, failures):
    route = v2_api[0]
    route.search.finish_retrieval.side_effect = [RuntimeError('write failed')] * failures + ['queued']
    delays = []
    async def sleep(delay):
        delays.append(delay)
    monkeypatch.setattr(route.asyncio, 'sleep', sleep)
    kick = AsyncMock()
    monkeypatch.setattr(route, '_kick_next', kick)
    asyncio.run(route._run_retrieval(retrieval_job()))
    assert delays == [1, 3, 9][:min(failures, 3)]
    assert route.search.finish_retrieval.call_count == min(failures + 1, 4)
    route.retrieval.run.assert_called_once()
    route.search.fail_job.assert_not_called()
    assert kick.await_count == (0 if failures == 4 else 1)


def test_retrieval_shutdown_cancellation_does_not_write_or_drain(v2_api, monkeypatch):
    route = v2_api[0]
    entered = asyncio.Event()
    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(route, 'run_in_threadpool', blocked)
    kick = AsyncMock()
    monkeypatch.setattr(route, '_kick_retrieval', kick)
    async def run():
        task = asyncio.create_task(route._run_retrieval(retrieval_job()))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    route.search.finish_retrieval.assert_not_called()
    route.search.fail_job.assert_not_called()
    route.search.update_retrieval_progress.assert_not_called()
    kick.assert_not_awaited()


@pytest.mark.parametrize('claimed,expected', [(0, 1), (1, 2), (2, 2)])
def test_retrieval_claim_loop_sql_limits_and_stops_at_empty(v2_api, claimed, expected):
    route, _, _, _, started = v2_api
    route.search.claim_next_retrieving.side_effect = [retrieval_job()] * claimed + [None]
    asyncio.run(route._kick_retrieval())
    assert route.search.claim_next_retrieving.call_count == expected
    assert started == ['retrieval-1'] * claimed
    assert all(call.args == (2,) for call in route.search.claim_next_retrieving.call_args_list)


def test_spawn_tracks_both_coroutines_in_shared_task_set(v2_api, monkeypatch, real_functions):
    route = v2_api[0]
    tracked = set()
    monkeypatch.setattr(route, '_AI_SEARCH_BG_TASKS', tracked)
    async def work():
        await asyncio.sleep(0)
    async def run():
        real_functions.spawn(work())
        real_functions.spawn(work())
        assert len(tracked) == 2
        await asyncio.gather(*tracked)
        assert not tracked
    asyncio.run(run())


def test_sweeper_schema_recovery_retrieval_then_writing(v2_api, monkeypatch):
    route = v2_api[0]
    events, now = [], [0.]
    route.search.schema_ready.side_effect = lambda: events.append('schema') or True
    route.search.recover_stale.side_effect = lambda *a, **kw: events.append(('recover', kw))
    monkeypatch.setattr(route, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    async def retrieval():
        events.append('retrieval')
    async def writing():
        events.append('writing')
    async def sleep(delay):
        assert delay == 5
        now[0] += 5
        if now[0] == 65:
            raise asyncio.CancelledError
    monkeypatch.setattr(route, '_kick_retrieval', retrieval)
    monkeypatch.setattr(route, '_kick_next', writing)
    monkeypatch.setattr(route.asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(route._sweep())
    assert events[:4] == ['schema', ('recover', {'requeue_before': route._PROCESS_STARTED_AT}), 'retrieval', 'writing']
    assert events[4:7] == ['schema', 'retrieval', 'writing']
    assert events[-4:] == ['schema', ('recover', {'requeue_before': None}), 'retrieval', 'writing']


def test_sweeper_unavailable_schema_skips_and_logs_once_per_process(v2_api, monkeypatch, capsys):
    route = v2_api[0]
    route.search.schema_ready.return_value = False
    ticks = [0]
    async def sleep(delay):
        assert delay == 5
        ticks[0] += 1
        if ticks[0] % 3 == 0:
            raise asyncio.CancelledError
    monkeypatch.setattr(route.asyncio, 'sleep', sleep)
    for _ in range(2):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(route._sweep())
    output = capsys.readouterr().out
    assert output.count('[error]') == 1 and 'migration' in output and 'disabled' in output
    assert '[warn]' not in output
    route.search.recover_stale.assert_not_called()
    route.search.claim_next_retrieving.assert_not_called()
    route.search.claim_next_queued.assert_not_called()


def test_sweeper_schema_query_exception_logs_actual_error_and_skips_claims(v2_api, monkeypatch, capsys):
    route = v2_api[0]
    route.search.schema_ready.side_effect = RuntimeError('temporary DB outage')
    async def sleep(delay):
        assert delay == 5
        raise asyncio.CancelledError
    monkeypatch.setattr(route.asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(route._sweep())
    output = capsys.readouterr().out
    assert '[warn]' in output and 'temporary DB outage' in output
    assert 'migration not applied' not in output and not route._AI_SEARCH_SCHEMA_ERROR_LOGGED
    route.search.recover_stale.assert_not_called()
    route.search.claim_next_retrieving.assert_not_called()
    route.search.claim_next_queued.assert_not_called()


def test_sweeper_schema_recovery_resets_missing_migration_warning(v2_api, monkeypatch, capsys):
    route = v2_api[0]
    route.search.schema_ready.side_effect = [False, False, True, False, False]
    ticks = []
    async def sleep(delay):
        ticks.append(delay)
        if len(ticks) == 5:
            raise asyncio.CancelledError
    monkeypatch.setattr(route.asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(route._sweep())
    assert capsys.readouterr().out.count('migration not applied') == 2
    route.search.recover_stale.assert_called_once()
    route.search.claim_next_retrieving.assert_called_once()
    route.search.claim_next_queued.assert_called_once()


@pytest.mark.parametrize('stage', ['queued', 'understanding', 'searching', None])
def test_get_retrieving_light_read_stage_and_keywords(v2_api, stage):
    route, client, jobs, _, _ = v2_api
    jobs['r'] = dict(id='r', status='retrieving', candidate_ids=[], vector_ok=False,
                     user_id='user-1', retrieval={'stage': stage, 'keywords': ['Cue', 'Muse']})
    response = client.get('/api/ai-search/r')
    assert response.status_code == 200
    data = response.json()
    route.search.get_job.assert_called_once_with('r', include_input=False)
    assert data['stage'] == ('searching' if stage == 'searching' else 'understanding')
    assert 'vector_ok' not in data and data['items'] == []
    if stage == 'searching':
        assert data['keywords'] == ['Cue', 'Muse']
    else:
        assert 'keywords' not in data


@pytest.mark.parametrize('identity,owner,visible', [
    ('owner', 'user-1', True), ('admin', 'user-1', True), ('legacy', 'user-1', True),
    ('other', 'user-1', False), ('anonymous', 'user-1', False), ('anonymous', None, False),
    ('owner', None, False)])
def test_retrieving_keywords_only_visible_to_owner_or_access_all(v2_api, identity, owner, visible):
    _, client, jobs, _, _ = v2_api
    jobs['r'] = dict(id='r', status='retrieving', user_id=owner, candidate_ids=[],
                     retrieval={'stage': 'searching', 'keywords': ['私有项目']})
    client.headers.clear()
    headers = {'x-test-role': 'user'} if identity in ('owner', 'other') else (
        {'x-test-role': 'admin'} if identity == 'admin' else {'x-test-legacy': '1'} if identity == 'legacy' else {})
    if identity == 'other':
        headers['x-test-user-id'] = 'another-user'
    response = client.get('/api/ai-search/r', headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data['stage'] == ('searching' if visible else 'understanding')
    assert data.get('keywords') == (['私有项目'] if visible else None)
    if not visible:
        assert 'keywords' not in data and '私有项目' not in response.text


@pytest.mark.parametrize('status,error,vector_ok', [
    ('failed', 'retrieval_failed', None), ('failed', 'retrieval_timeout', None),
    ('failed', 'quota_exhausted', False), ('failed', 'timeout', False),
    ('queued', None, True), ('running', None, False), ('done', None, True),
    ('insufficient', None, False)])
def test_get_vector_flag_only_after_retrieval_and_new_error_codes(v2_api, status, error, vector_ok):
    _, client, jobs, _, _ = v2_api
    jobs['r'] = dict(id='r', status=status, error=error, candidate_ids=['b', 'a'],
                     vector_ok=bool(vector_ok), retrieval={'stage': 'searching', 'keywords': ['Muse']})
    data = client.get('/api/ai-search/r').json()
    assert data['error'] == error
    assert 'stage' not in data and 'keywords' not in data
    if vector_ok is None:
        assert 'vector_ok' not in data
    else:
        assert data['vector_ok'] is vector_ok
    assert [item['id'] for item in data['items']] == ['b', 'a']


@pytest.mark.parametrize('missing_kind,status', [('private', 404), ('offline', 200), ('offline_cited', 404), ('none', 200)])
def test_get_visibility_vs_offline_and_single_query_without_missing(v2_api, missing_kind, status):
    route, client, jobs, _, _ = v2_api
    jobs['r'] = dict(id='r', status='done', candidate_ids=['b', 'a'],
                     cited_ids=['b', 'a'] if missing_kind == 'offline_cited' else ['b'],
                     content_md_final='answer [1]')
    route.search.get_candidates.side_effect = (
        [[{'id': 'b'}, {'id': 'a'}]] if missing_kind == 'none' else
        [[{'id': 'b'}], [{'id': 'a'}] if missing_kind == 'private' else []])
    response = client.get('/api/ai-search/r')
    assert response.status_code == status
    calls = route.search.get_candidates.call_args_list
    assert len(calls) == (1 if missing_kind == 'none' else 2)
    if len(calls) == 2:
        assert calls[1].args == (['a'],)
        assert calls[1].kwargs == dict(public_only=False, manual_owner_user_id=None, min_github_stars=0)
    if status == 200:
        assert [i['id'] for i in response.json()['items']] == (['b', 'a'] if missing_kind == 'none' else ['b'])
        assert response.json()['content_md'] == 'answer [1]'
    else:
        assert response.json() == {'error': 'job_not_found'}
    assert jobs['r']['candidate_ids'] == ['b', 'a']


@pytest.mark.parametrize('pipeline,identity,quota', [
    ('v2', 'user', True), ('v1', 'user', False), ('v2_admin', 'user', False),
    ('v2', 'admin', False), ('v2_admin', 'admin', False), ('v2', 'legacy', False)])
def test_status_retrieval_daily_limit_dispatch_and_exemption(v2_api, monkeypatch, pipeline, identity, quota):
    route, client, _, _, _ = v2_api
    monkeypatch.setenv('AI_SEARCH_PIPELINE', pipeline)
    route.search.count_user_retrieval_day.return_value = 10
    client.headers.clear()
    headers = {'x-test-role': identity} if identity != 'legacy' else {'x-test-legacy': '1'}
    data = client.get('/api/ai-search/status', headers=headers).json()
    assert data['enabled'] is True and data['can_search'] is not quota
    assert data['reason'] == ('quota_exhausted' if quota else None)
    assert set(data) == {'enabled', 'logged_in', 'can_search', 'reason', 'user_day_limit'}
    assert route.search.count_user_retrieval_day.called is quota
    if identity in ('admin', 'legacy'):
        route.search.quota_exhausted.assert_not_called()


def test_status_v2_writing_quota_short_circuits_retrieval_count(v2_api):
    route, client, _, _, _ = v2_api
    route.search.quota_exhausted.side_effect = None
    route.search.quota_exhausted.return_value = True
    data = client.get('/api/ai-search/status').json()
    assert data['reason'] == 'quota_exhausted' and data['can_search'] is False
    route.search.count_user_retrieval_day.assert_not_called()


@pytest.mark.parametrize('path,method', [('/api/ai-search', 'post'), ('/api/ai-search/r', 'get'),
    ('/api/ai-search/history', 'get'), ('/api/ai-search/status', 'get'),
    ('/api/admin/ai-search/jobs', 'get'), ('/api/admin/ai-search/jobs/r', 'get')])
def test_schema_unavailable_is_disabled_not_500(v2_api, path, method):
    route, client, _, _, _ = v2_api
    route.search.schema_ready.return_value = False
    client.headers.clear()
    response = getattr(client, method)(path, headers={'x-test-role': 'admin'},
                                      **({'json': {'q': ''}} if method == 'post' else {}))
    if path.endswith('/status'):
        assert response.status_code == 200
        data = response.json()
        assert data['enabled'] is False and data['can_search'] is False and data['reason'] == 'disabled'
    else:
        assert response.status_code == 404 and response.json() == {'error': 'ai_search_disabled'}
    route.search.get_job.assert_not_called()
    route.search.retrieve.assert_not_called()
    route.search.create_retrieval_job.assert_not_called()
    route.search.list_user_jobs.assert_not_called()
    route.search.list_jobs.assert_not_called()
    route.search.count_all_day.assert_not_called()
    route.search.count_all_month.assert_not_called()


def test_admin_checks_availability_and_keeps_full_details(v2_api):
    route, client, jobs, _, _ = v2_api
    jobs['r'] = {'id': 'r', 'input_text': 'full input', 'retrieval': {}}
    assert client.get('/api/admin/ai-search/jobs', headers={'x-test-role': 'admin'}).status_code == 200
    assert client.get('/api/admin/ai-search/jobs/r', headers={'x-test-role': 'admin'}).json()['input_text'] == 'full input'
    assert route.search.schema_ready.call_count == 2
    route.search.get_job.assert_called_once_with('r')


@pytest.mark.parametrize('last_result', [None, True, False])
def test_schema_exception_uses_last_success_or_defaults_ready(v2_api, capsys, last_result):
    route, client, jobs, _, _ = v2_api
    jobs['r'] = dict(id='r', status='retrieving', user_id='user-1', candidate_ids=[], retrieval={})
    if last_result is not None:
        route.search.schema_ready.return_value = last_result
        assert asyncio.run(route._schema_ready()) is last_result
    route.search.schema_ready.side_effect = RuntimeError('temporary schema query error')
    response = client.get('/api/ai-search/r')
    if last_result is False:
        assert response.status_code == 404 and response.json() == {'error': 'ai_search_disabled'}
        route.search.get_job.assert_not_called()
    else:
        assert response.status_code == 200 and response.json()['stage'] == 'understanding'
        route.search.get_job.assert_called_once_with('r', include_input=False)
    assert '[warn]' in capsys.readouterr().out


@pytest.mark.parametrize('path', ['/api/ai-search/r', '/api/admin/ai-search/jobs', '/api/admin/ai-search/jobs/r'])
def test_schema_exception_allows_real_query_failure_instead_of_disabled(v2_api, path):
    route, client, _, _, _ = v2_api
    route.search.schema_ready.side_effect = RuntimeError('schema query unavailable')
    route.search.get_job.side_effect = RuntimeError('job query unavailable')
    route.search.list_jobs.side_effect = RuntimeError('jobs query unavailable')
    with pytest.raises(RuntimeError, match='query unavailable'):
        client.get(path, headers={'x-test-role': 'admin'})


def test_history_preserves_retrieving_and_insufficient(v2_api):
    route, client, _, _, _ = v2_api
    route.search.list_user_jobs.side_effect = None
    route.search.list_user_jobs.return_value = [dict(id=str(i), status=state, query='q', created_at='today')
        for i, state in enumerate(['retrieving', 'insufficient'])]
    assert [item['status'] for item in client.get('/api/ai-search/history').json()['items']] == ['retrieving', 'insufficient']


@pytest.mark.parametrize('v2', [False, True])
def test_writer_partial_offline_contiguous_input_and_exact_saved_message(v2_api, monkeypatch, real_functions, v2):
    route, _, jobs, _, _ = v2_api
    job = dict(id='writing', query='question', status='running', candidate_ids=['b', 'offline', 'a'],
               candidate_meta=[{'id': 'b', 'score': .9}, {'id': 'offline', 'score': .85}, {'id': 'a', 'score': .8}])
    if v2:
        job['retrieval'] = {}
    jobs['writing'] = job
    candidates = [dict(id=i, title=i, content='c' * 200, ai_key_points='k', ai_summary='s') for i in ['a', 'b']]
    route.search.get_candidates.side_effect = None
    route.search.get_candidates.return_value = candidates
    config = {**route.digest.settings(), 'AI_SEARCH_ITEM_CHARS': 4000, 'AI_SEARCH_INPUT_BUDGET_TOKENS': 1}
    monkeypatch.setattr(route.digest, 'settings', lambda: config)
    monkeypatch.setattr(route.digest, 'build_input', real_functions.build_input)
    messages = []
    def call(system, message, **kwargs):
        messages.append(message)
        return '{"content_md":"answer [2] [1]"}', {}, 'glm-5.3'
    monkeypatch.setattr(route.digest, 'call_glm', call)
    monkeypatch.setattr(route.digest, 'run_digest', real_functions.run_digest)
    asyncio.run(route._run_job('writing'))
    saved = route.search.save_job_input.call_args.kwargs
    finished = route.search.finish_job.call_args.kwargs
    assert saved['input_text'] == messages[0] == finished['input_text']
    assert '[1]' in messages[0] and '[2]' in messages[0] and '[3]' not in messages[0]
    assert messages[0].index('· b') < messages[0].index('· a')
    assert 'c' * 200 in messages[0] if v2 else 'c' * 200 not in messages[0]
    expected = [{'id': 'b', 'score': .9, 'tier': 'content' if v2 else 'summary'},
                {'id': 'offline', 'score': .85, 'tier': None, 'offline': True},
                {'id': 'a', 'score': .8, 'tier': 'content' if v2 else 'summary'}]
    assert saved['candidate_meta'] == finished['candidate_meta'] == expected
    assert finished['cited_ids'] == ['a', 'b']
    assert job['candidate_ids'] == ['b', 'offline', 'a']
    route.search.fail_job.assert_not_called()


@pytest.mark.parametrize('v2', [False, True])
def test_writer_all_candidates_offline_fails(v2_api, v2):
    route, _, jobs, _, _ = v2_api
    jobs['writing'] = dict(id='writing', query='q', status='running', candidate_ids=['missing'])
    if v2:
        jobs['writing']['retrieval'] = {}
    route.search.get_candidates.side_effect = None
    route.search.get_candidates.return_value = []
    asyncio.run(route._run_job('writing'))
    assert route.search.fail_job.call_args.kwargs['error'] == 'candidates_unavailable'
    route.search.save_job_input.assert_not_called()
    route.digest.run_digest.assert_not_called()


@pytest.mark.parametrize('v2', [False, True])
def test_writer_rechecks_grown_content_and_saves_exact_sent_candidates(v2_api, monkeypatch, real_functions, v2):
    route, _, jobs, _, _ = v2_api
    candidates = [dict(id=i, title=i, content='正文' * 500, ai_summary='摘要') for i in ['b', 'a', 'tail']]
    job = dict(id='writing', query='question', status='running', candidate_ids=['b', 'a', 'tail'],
               candidate_meta=[{'id': i, 'score': .9, 'offline': True, 'dropped': 'input_budget'}
                               for i in ['b', 'a', 'tail']])
    if v2:
        job['retrieval'] = {}
    jobs['writing'] = job
    route.search.get_candidates.side_effect = None
    route.search.get_candidates.return_value = list(reversed(candidates))
    config = {**route.digest.settings(), 'AI_SEARCH_ITEM_CHARS': 4000, 'AI_SEARCH_INPUT_BUDGET_TOKENS': 10**9}
    monkeypatch.setattr(route.digest, 'settings', lambda: config)
    monkeypatch.setattr(route.digest, 'build_input', real_functions.build_input)
    monkeypatch.setattr(route.digest, 'run_digest', real_functions.run_digest)
    monkeypatch.setattr(route.digest, 'load_prompt', lambda _: ('test', '{query}|{today}|{item_count}'))
    monkeypatch.setattr(route.retrieval, 'settings', lambda: {
        'AI_SEARCH_INPUT_MAX_TOKENS': 1700, 'AI_SEARCH_CHARS_PER_TOKEN': 1.7})
    original_trim = route.digest.trim_input_candidates
    trim = Mock(wraps=original_trim)
    monkeypatch.setattr(route.digest, 'trim_input_candidates', trim)
    calls = []
    def call(system, message, **kwargs):
        calls.append((system, message))
        return '{"content_md":"answer [1]"}', {}, 'glm-5.3'
    monkeypatch.setattr(route.digest, 'call_glm', call)
    asyncio.run(route._run_job('writing'))
    route.search.fail_job.assert_not_called()
    saved = route.search.save_job_input.call_args.kwargs
    finished = route.search.finish_job.call_args.kwargs
    assert saved['input_text'] == calls[0][1] == finished['input_text']
    assert saved['candidate_meta'] == finished['candidate_meta']
    assert '正文' * 500 in calls[0][1]
    assert len(saved['candidate_meta']) == 3
    assert saved['candidate_meta'][:2] == [{'id': i, 'score': .9, 'tier': 'content'} for i in ['b', 'a']]
    if v2:
        trim.assert_called_once()
        assert len(calls[0][0]) + len(calls[0][1]) <= 1700 * 1.7
        assert '[3]' not in calls[0][1] and '· tail' not in calls[0][1]
        assert saved['candidate_meta'][2] == {'id': 'tail', 'score': .9, 'tier': None, 'dropped': 'input_budget'}
        # These candidates fit before their fetched content grew.
        old = [{**item, 'content': '短正文'} for item in candidates]
        assert original_trim(job['query'], '2026-10-11', old, config) == (old, [])
    else:
        trim.assert_not_called()
        assert '[3]' in calls[0][1] and '· tail' in calls[0][1]
        assert saved['candidate_meta'][2] == {'id': 'tail', 'score': .9, 'tier': 'content'}


def test_writer_recovery_replaces_offline_and_dropped_flags(v2_api, monkeypatch, real_functions):
    route, _, jobs, _, _ = v2_api
    job = dict(id='writing', query='q', status='running', candidate_ids=['b', 'a'], retrieval={},
               candidate_meta=[{'id': 'b', 'offline': True}, {'id': 'a', 'dropped': 'input_budget'}])
    jobs['writing'] = job
    route.search.get_candidates.side_effect = None
    route.search.get_candidates.return_value = [dict(id='a', content='正文')]
    config = {**route.digest.settings(), 'AI_SEARCH_ITEM_CHARS': 4000, 'AI_SEARCH_INPUT_BUDGET_TOKENS': 120000}
    monkeypatch.setattr(route.digest, 'settings', lambda: config)
    monkeypatch.setattr(route.digest, 'build_input', real_functions.build_input)
    monkeypatch.setattr(route.digest, 'run_digest', real_functions.run_digest)
    monkeypatch.setattr(route.digest, 'call_glm', lambda *a, **kw: ('{"content_md":"answer [1]"}', {}, 'glm'))
    asyncio.run(route._run_job('writing'))
    first = route.search.save_job_input.call_args.kwargs['candidate_meta']
    assert first == [{'id': 'b', 'tier': None, 'offline': True}, {'id': 'a', 'tier': 'content'}]
    # Startup recovery uses the previous attempt's persisted metadata.
    job['candidate_meta'] = first
    route.search.get_candidates.return_value = [dict(id=i, content='正文') for i in ['a', 'b']]
    asyncio.run(route._run_job('writing'))
    second = route.search.save_job_input.call_args.kwargs['candidate_meta']
    assert second == [{'id': 'b', 'tier': 'content'}, {'id': 'a', 'tier': 'content'}]
    assert route.search.finish_job.call_args.kwargs['candidate_meta'] == second
    route.search.fail_job.assert_not_called()
