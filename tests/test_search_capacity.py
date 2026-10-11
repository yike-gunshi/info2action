"""Search admission must bound database work across threads and workers."""
import importlib
import multiprocessing
import sys
from pathlib import Path

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

@pytest.fixture
def search_module():
    try:
        module = importlib.import_module('feed_search')
    except ModuleNotFoundError:
        module = None
    assert module is not None, 'Search has no admission/deadline protection'
    return module


def test_capacity_is_fail_fast_and_released_on_exception(search_module, tmp_path):
    gate = search_module.SearchGate(tmp_path, capacity=2, per_minute=30)
    with gate.enter(), gate.enter():
        with pytest.raises(search_module.SearchBusy):
            with gate.enter():
                pytest.fail('overflow reached database work')
    with pytest.raises(ValueError):
        with gate.enter():
            raise ValueError('query failed')
    with gate.enter():
        pass


def test_rolling_request_limit_survives_new_gate_instances(search_module, tmp_path):
    with search_module.SearchGate(tmp_path, capacity=2, per_minute=2).enter():
        pass
    with search_module.SearchGate(tmp_path, capacity=2, per_minute=2).enter():
        pass
    with pytest.raises(search_module.SearchBusy):
        with search_module.SearchGate(tmp_path, capacity=2, per_minute=2).enter():
            pytest.fail('rate limit reset with worker instance')


def hold_slot(path, ready, release):
    from feed_search import SearchGate
    with SearchGate(path, capacity=2, per_minute=30).enter():
        ready.set()
        release.wait(5)


def test_capacity_is_shared_between_processes(search_module, tmp_path):
    ctx = multiprocessing.get_context('spawn')
    ready, release = ctx.Event(), ctx.Event()
    child = ctx.Process(target=hold_slot, args=(tmp_path, ready, release))
    child.start()
    try:
        assert ready.wait(5)
        with search_module.SearchGate(tmp_path, capacity=2, per_minute=30).enter():
            with pytest.raises(search_module.SearchBusy):
                with search_module.SearchGate(tmp_path, capacity=2, per_minute=30).enter():
                    pytest.fail('worker multiplier bypassed capacity')
    finally:
        release.set()
        child.join(5)
        if child.is_alive():
            child.terminate()
            child.join()
    assert child.exitcode == 0


class Connection:
    def __init__(self):
        self.calls = []
        self.cancelled = 0
    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return self
    def cancel_safe(self, timeout):
        self.cancelled += 1


def test_sql_budget_bounds_work_and_cancels_active_query(search_module):
    budget = search_module.SearchBudget(timeout=120, max_queries=2)
    conn = Connection()
    with budget.connection(conn) as guarded:
        guarded.execute('SELECT 1')
        guarded.execute('SELECT 2')
        with pytest.raises(search_module.SearchExpired):
            guarded.execute('SELECT 3')
        assert len([s for s, _ in conn.calls if s.startswith('SELECT')]) == 2
        budget.cancel('search_timeout')
        assert conn.cancelled == 1
    budget.cancel('search_timeout')
    assert conn.cancelled == 1, 'late cancellation reached returned connection'


def test_statement_timeout_uses_remaining_action_budget(search_module):
    budget = search_module.SearchBudget(timeout=.15)
    conn = Connection()
    with budget.connection(conn) as guarded:
        guarded.execute('SELECT 1')
    import re
    timeout = int(re.search(r"'(\d+)ms'", conn.calls[0][0]).group(1))
    assert 0 < timeout <= 150


def test_40_http_requests_admit_only_two_database_reads(search_module, tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import feed
    gate_class = search_module.SearchGate
    monkeypatch.setattr(search_module, 'SearchGate', lambda *a: gate_class(tmp_path))
    monkeypatch.setattr(feed.remote_db, 'feed_read_from_remote', lambda: True)
    monkeypatch.setattr(feed, '_github_display_min_stars', lambda: 50)
    monkeypatch.setattr(feed, '_manual_owner_user_id', lambda req: None)
    count, lock, release = [0], threading.Lock(), threading.Event()
    def database_read(**kwargs):
        with lock:
            count[0] += 1
        assert search_module.current_search.get() is not None
        assert release.wait(8)
        return {'sections': {}, 'total': 0}
    monkeypatch.setattr(feed.remote_db, 'query_feed_sections', database_read)
    app = FastAPI()
    app.include_router(feed.router)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=40) as executor:
        futures = [executor.submit(client.get, '/api/feed/sections?search=设计') for _ in range(40)]
        import time
        end = time.monotonic() + 6
        while sum(f.done() for f in futures) < 38 and time.monotonic() < end:
            time.sleep(.02)
        assert count[0] == 2
        assert sum(f.done() for f in futures) == 38
        release.set()
        responses = [f.result() for f in futures]
    assert [r.status_code for r in responses].count(200) == 2
    assert [r.status_code for r in responses].count(429) == 38
    assert all(r.headers.get('Retry-After') for r in responses if r.status_code == 429)


def test_deadline_really_cancels_worker_and_releases_admission(search_module, tmp_path, monkeypatch):
    import threading
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import feed
    gate_class = search_module.SearchGate
    monkeypatch.setattr(search_module, 'SearchGate', lambda *a: gate_class(tmp_path))
    monkeypatch.setattr(feed.remote_db, 'feed_read_from_remote', lambda: True)
    monkeypatch.setattr(feed, '_github_display_min_stars', lambda: 50)
    monkeypatch.setattr(feed, '_manual_owner_user_id', lambda req: None)
    stopped = threading.Event()
    class SlowConnection(Connection):
        def cancel_safe(self, timeout):
            stopped.set()
    def read(**kwargs):
        budget = search_module.current_search.get()
        with budget.connection(SlowConnection()):
            assert stopped.wait(3), 'HTTP timeout left database worker running'
        return {'sections': {}, 'degraded': True}
    monkeypatch.setattr(feed.remote_db, 'query_feed_sections', read)
    app = FastAPI()
    app.include_router(feed.router)
    with TestClient(app) as client:
        response = client.get('/api/feed/sections?search=设计', headers={'X-Search-Budget-Ms':'50'})
    assert response.status_code == 504
    assert stopped.is_set()
    with gate_class(tmp_path, capacity=2).enter(), gate_class(tmp_path, capacity=2).enter():
        pass


def test_rate_window_recovers_after_one_minute(search_module, tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(search_module.time, 'monotonic', lambda: clock[0])
    gate = search_module.SearchGate(tmp_path, capacity=2, per_minute=1)
    with gate.enter():
        pass
    with pytest.raises(search_module.SearchBusy):
        with gate.enter():
            pass
    clock[0] += 61
    with gate.enter():
        pass


def test_count_savepoint_recovers_aborted_transaction(search_module):
    class AbortedConnection:
        def __init__(self):
            self.aborted = False
        def execute(self, sql):
            if sql == 'bad count':
                self.aborted = True
                raise TimeoutError('count timeout')
            if sql.startswith('ROLLBACK TO'):
                self.aborted = False
            elif self.aborted:
                pytest.fail('count rollback did not recover transaction')
            return self
    conn = AbortedConnection()
    with search_module.optional_search_count(conn, True) as state:
        conn.execute('bad count')
    assert state['available'] is False
    conn.execute('SELECT matching cards remain usable')


def test_worker_crash_does_not_leak_admission(search_module, tmp_path):
    ctx = multiprocessing.get_context('spawn')
    ready, release = ctx.Event(), ctx.Event()
    child = ctx.Process(target=hold_slot, args=(tmp_path, ready, release))
    child.start()
    try:
        assert ready.wait(5)
        child.terminate()
        child.join(5)
        gate = search_module.SearchGate(tmp_path)
        with gate.enter(), gate.enter():
            pass
    finally:
        if child.is_alive():
            child.terminate()
            child.join()


def test_corrupt_admission_state_fails_closed(search_module, tmp_path):
    (tmp_path / 'requests.json').write_text('{incomplete')
    with pytest.raises(ValueError):
        with search_module.SearchGate(tmp_path).enter():
            pytest.fail('corrupt rate state admitted a query')


def test_pooled_search_uses_read_only_local_state_and_rolls_back(search_module, monkeypatch):
    from contextlib import contextmanager
    import remote_db
    class PooledConnection(Connection):
        rollbacks = 0
        def rollback(self):
            self.rollbacks += 1
    conn = PooledConnection()
    class Pool:
        @contextmanager
        def connection(self, timeout=None):
            yield conn
    monkeypatch.setattr(remote_db, '_get_pool', lambda *a: Pool())
    monkeypatch.setattr(remote_db, 'remote_schema', lambda: 'remote_poc')
    monkeypatch.setattr(remote_db, '_force_writable_on_connect', lambda: pytest.fail('search leaked session state'))
    token = search_module.current_search.set(search_module.SearchBudget())
    try:
        with remote_db.connect() as guarded:
            guarded.execute('SELECT 1')
        sqls = [sql for sql, _ in conn.calls]
        assert 'SET TRANSACTION READ ONLY' in sqls
        assert 'SET LOCAL search_path TO remote_poc, extensions, public' in sqls
        assert not any(s.startswith('SET search_path') or 'default_transaction_read_only' in s for s in sqls)
        assert conn.rollbacks == 1
    finally:
        search_module.current_search.reset(token)


def test_http_queue_serves_waiters_and_keeps_normal_requests_responsive(search_module, tmp_path, monkeypatch):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import feed
    gate_class = search_module.SearchGate
    monkeypatch.setattr(search_module, 'SearchGate', lambda *a: gate_class(tmp_path))
    monkeypatch.setattr(feed.remote_db, 'feed_read_from_remote', lambda: True)
    monkeypatch.setattr(feed, '_github_display_min_stars', lambda: 50)
    monkeypatch.setattr(feed, '_manual_owner_user_id', lambda req: None)
    reads, lock, release = [0], threading.Lock(), threading.Event()
    def database_read(**kwargs):
        assert kwargs['search'] == '设计'
        with lock:
            reads[0] += 1
        assert release.wait(5)
        return {'sections': {}, 'total': 0}
    monkeypatch.setattr(feed.remote_db, 'query_feed_sections', database_read)
    app = FastAPI()
    app.include_router(feed.router)
    @app.get('/health')
    async def healthy():
        return {'ok': True}
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(client.get, '/api/feed/sections?search=设计') for _ in range(6)]
        try:
            end = time.monotonic() + 2
            while (reads[0] < 2 or sum(f.done() for f in futures) < 2) and time.monotonic() < end:
                time.sleep(.01)
            assert reads[0] == 2
            assert sum(f.done() for f in futures) == 2
            start = time.monotonic()
            assert client.get('/health').status_code == 200
            assert time.monotonic() - start < .5
        finally:
            release.set()
        responses = [f.result() for f in futures]
    assert reads[0] == 4, 'short waiters never got a newly freed execution place'
    assert [r.status_code for r in responses].count(200) == 4
    assert [r.status_code for r in responses].count(429) == 2


def test_invalid_action_budget_does_not_consume_admission_quota(search_module, tmp_path, monkeypatch):
    import asyncio
    gate = search_module.SearchGate(tmp_path, capacity=1, per_minute=1)
    monkeypatch.setattr(search_module, 'SearchGate', lambda *a: gate)
    class Request:
        query_params = {'search': '设计'}
        headers = {'X-Search-Budget-Ms': 'nan'}
    @search_module.search_guarded
    def work(request):
        pytest.fail('invalid budget reached work')
    for _ in range(3):
        response = asyncio.run(work(Request()))
        assert response.status_code == 400
    with gate.enter():
        pass


@pytest.mark.parametrize('timeout_ms,delay,count_timeout,expected_status', [
    (20, .08, False, 504),
    (1000, 0, True, 200),
])
def test_completed_result_respects_action_deadline_but_preserves_optional_count_failure(
    search_module, tmp_path, monkeypatch, timeout_ms, delay, count_timeout, expected_status,
):
    import asyncio
    import time
    from fastapi.responses import JSONResponse
    gate = search_module.SearchGate(tmp_path, capacity=1)
    monkeypatch.setattr(search_module, 'SearchGate', lambda *a: gate)
    class Request:
        query_params = {'search': '设计'}
        headers = {'X-Search-Budget-Ms': str(timeout_ms)}
        async def is_disconnected(self):
            return False
    @search_module.search_guarded
    def work(request):
        if count_timeout:
            search_module.current_search.get().reason = 'search_timeout'
        time.sleep(delay)  # Post-query normalization/serialization can finish late.
        return JSONResponse({'items': [{'title': '设计'}], 'counts_available': not count_timeout})
    response = asyncio.run(work(Request()))
    assert response.status_code == expected_status
    with gate.enter():
        pass
