"""Bounded search waiting must happen before any database work or worker thread."""
import asyncio
import fcntl
import multiprocessing
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from feed_search import SearchGate, SearchBudget, SearchBusy, SearchExpired


def test_search_configuration_is_frozen_and_blank_queue_follows_capacity(monkeypatch):
    from feed_search import search_settings
    search_settings.cache_clear()
    monkeypatch.setenv('INFO2ACTION_FEED_SEARCH_CONCURRENCY', '3')
    monkeypatch.setenv('INFO2ACTION_FEED_SEARCH_QUEUE_SIZE', '')
    monkeypatch.setenv('INFO2ACTION_FEED_SEARCH_QUEUE_WAIT_SECONDS', '.5')
    try:
        assert search_settings() == (3, 3, .5)
        monkeypatch.setenv('INFO2ACTION_FEED_SEARCH_CONCURRENCY', '5')
        assert search_settings() == (3, 3, .5), 'live workers adopted different settings'
    finally:
        search_settings.cache_clear()


@pytest.mark.parametrize('key,value', [
    ('CONCURRENCY', '0'), ('CONCURRENCY', '65'),
    ('QUEUE_SIZE', '-1'), ('QUEUE_SIZE', '65'),
    ('QUEUE_WAIT_SECONDS', 'nan'), ('QUEUE_WAIT_SECONDS', '11'),
])
def test_invalid_search_configuration_rejects_search_but_not_normal_browsing(monkeypatch, key, value):
    from feed_search import search_guarded, search_settings
    search_settings.cache_clear()
    monkeypatch.setenv('INFO2ACTION_FEED_SEARCH_' + key, value)
    class Request:
        headers = {}
        def __init__(self, search):
            self.query_params = {'search': search}
    @search_guarded
    def work(request):
        return {'ok': True}
    try:
        response = asyncio.run(work(Request('设计')))
        assert response.status_code == 503
        assert asyncio.run(work(Request(''))) == {'ok': True}
    finally:
        search_settings.cache_clear()


def test_queue_is_bounded_and_fifo(tmp_path):
    async def scenario():
        gate = SearchGate(tmp_path, capacity=1, queue_capacity=2, wait_seconds=.5)
        order = []
        active = gate.enter()
        active.__enter__()
        async def worker(number):
            async with gate.wait(SearchBudget(timeout=2)):
                order.append(number)
                await asyncio.sleep(.03)
        first = asyncio.create_task(worker(1))
        await asyncio.sleep(.05)
        second = asyncio.create_task(worker(2))
        await asyncio.sleep(.05)
        try:
            with pytest.raises(SearchBusy):
                async with gate.wait(SearchBudget(timeout=2)):
                    pytest.fail('overflow started work')
            active.__exit__(None, None, None)
            # A fresh arrival cannot bypass the already queued first request.
            with pytest.raises(SearchBusy):
                with gate.enter():
                    pytest.fail('new arrival jumped the queue')
            await asyncio.gather(first, second)
            assert order == [1, 2]
        finally:
            active.__exit__(None, None, None)
            await asyncio.gather(first, second, return_exceptions=True)
    asyncio.run(scenario())


def test_queue_timeout_releases_waiting_place(tmp_path):
    async def scenario():
        gate = SearchGate(tmp_path, capacity=1, queue_capacity=1, wait_seconds=.1)
        with gate.enter():
            start = time.monotonic()
            with pytest.raises(SearchBusy):
                async with gate.wait(SearchBudget(timeout=2)):
                    pytest.fail('occupied execution slot was reused')
            assert .08 <= time.monotonic() - start < .5
        async with gate.wait(SearchBudget(timeout=2)):
            pass
    asyncio.run(scenario())


def test_queue_checks_action_deadline_and_disconnect(tmp_path):
    class Request:
        async def is_disconnected(self):
            return True
    async def scenario():
        gate = SearchGate(tmp_path, capacity=1, queue_capacity=1, wait_seconds=.5)
        with gate.enter():
            with pytest.raises(SearchExpired, match='search_timeout'):
                async with gate.wait(SearchBudget(timeout=.04)):
                    pytest.fail('expired queue reached worker')
            budget = SearchBudget(timeout=2)
            with pytest.raises(SearchExpired, match='search_cancelled'):
                async with gate.wait(budget, request=Request()):
                    pytest.fail('disconnected request reached worker')
        async with gate.wait(SearchBudget(timeout=2)):
            pass
    asyncio.run(scenario())


def test_task_cancel_releases_queue_and_execution_once(tmp_path):
    async def scenario():
        gate = SearchGate(tmp_path, capacity=1, queue_capacity=1, wait_seconds=.5)
        async def worker(entered=None):
            async with gate.wait(SearchBudget(timeout=2)):
                if entered:
                    entered.set()
                await asyncio.sleep(2)
        with gate.enter():
            waiting = asyncio.create_task(worker())
            await asyncio.sleep(.05)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            another = asyncio.create_task(worker())
            await asyncio.sleep(.05)
            assert not another.done(), 'cancelled queue leaked its place'
            another.cancel()
            with pytest.raises(asyncio.CancelledError):
                await another
        entered = asyncio.Event()
        executing = asyncio.create_task(worker(entered))
        await entered.wait()
        executing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await executing
        with gate.enter():
            pass
    asyncio.run(scenario())


def test_corrupt_live_queue_fails_closed(tmp_path):
    async def scenario():
        gate = SearchGate(tmp_path, capacity=1, queue_capacity=1, wait_seconds=.2)
        with gate.enter():
            with (tmp_path / 'queue-0').open('w+') as broken:
                fcntl.flock(broken, fcntl.LOCK_EX | fcntl.LOCK_NB)
                broken.write('{broken')
                broken.flush()
                with pytest.raises(ValueError):
                    async with gate.wait(SearchBudget()):
                        pytest.fail('corrupt live registration bypassed protection')
    asyncio.run(scenario())


def queue_in_process(path, ready):
    async def scenario():
        gate = SearchGate(path, capacity=1, queue_capacity=1, wait_seconds=3)
        async def waiter():
            async with gate.wait(SearchBudget(timeout=10)):
                pytest.fail('parent still owns execution')
        task = asyncio.create_task(waiter())
        await asyncio.sleep(.1)
        assert not task.done()
        ready.set()
        await task
    asyncio.run(scenario())


def test_queued_process_exit_recovers_place_across_workers(tmp_path):
    ctx = multiprocessing.get_context('spawn')
    ready = ctx.Event()
    gate = SearchGate(tmp_path, capacity=1, queue_capacity=1, wait_seconds=3)
    with gate.enter():
        child = ctx.Process(target=queue_in_process, args=(tmp_path, ready))
        child.start()
        try:
            assert ready.wait(5)
            async def full():
                with pytest.raises(SearchBusy):
                    async with gate.wait(SearchBudget()):
                        pytest.fail('different worker bypassed queue capacity')
            asyncio.run(full())
            child.terminate()
            child.join(5)
            async def reclaimed():
                budget = SearchBudget(timeout=.06)
                with pytest.raises(SearchExpired):
                    async with gate.wait(budget):
                        pytest.fail('occupied execution started')
            asyncio.run(reclaimed())
        finally:
            if child.is_alive():
                child.terminate()
            child.join(5)
    with gate.enter():
        pass


def test_five_execution_five_waiting_rejects_eleventh_without_work(tmp_path):
    async def scenario():
        gate = SearchGate(tmp_path, capacity=5, queue_capacity=5, wait_seconds=.3)
        from contextlib import ExitStack
        called = []
        async def worker(number):
            async with gate.wait(SearchBudget()):
                called.append(number)
        with ExitStack() as stack:
            for _ in range(5):
                stack.enter_context(gate.enter())
            waiting = [asyncio.create_task(worker(i)) for i in range(5)]
            await asyncio.sleep(.06)
            with pytest.raises(SearchBusy):
                async with gate.wait(SearchBudget()):
                    pytest.fail('11th request started')
            assert called == []
        await asyncio.gather(*waiting)
        assert sorted(called) == list(range(5))
    asyncio.run(scenario())


def test_worker_configuration_mismatch_fails_closed_and_restart_can_change(tmp_path):
    gate = SearchGate(tmp_path, capacity=1, queue_capacity=1)
    with gate.enter():
        with pytest.raises(ValueError, match='settings differ'):
            with SearchGate(tmp_path, capacity=2, queue_capacity=2).enter():
                pytest.fail('worker configuration multiplied capacity')
    with SearchGate(tmp_path, capacity=2, queue_capacity=2).enter():
        pass


@pytest.mark.parametrize("cancel_internal_worker", [False, True])
@pytest.mark.parametrize("cancel_cleanup", [False, True])
def test_guard_holds_slot_until_worker_ends_despite_repeated_cancel(tmp_path, monkeypatch, cancel_internal_worker, cancel_cleanup):
    import threading
    import feed_search
    class Request:
        query_params = {'search': '设计'}
        headers = {}
        async def is_disconnected(self):
            return False
    gate = SearchGate(tmp_path, capacity=1)
    monkeypatch.setattr(feed_search, 'SearchGate', lambda *a: gate)
    started, cancelled, release, finished = [threading.Event() for _ in range(4)]
    class Connection:
        def cancel_safe(self, timeout):
            cancelled.set()
            release.wait(1)
    @feed_search.search_guarded
    def worker(request):
        budget = feed_search.current_search.get()
        with budget.connection(Connection()):
            started.set()
            assert release.wait(2)
        finished.set()
        return {'ok': True}
    async def scenario():
        task = asyncio.create_task(worker(Request()))
        while not started.is_set():
            await asyncio.sleep(.005)
        if cancel_internal_worker:
            children = [t for t in asyncio.all_tasks() if t.get_name() == "feed-search-work"]
            assert len(children) == 1
            children[0].cancel()
        else:
            task.cancel()
        while not cancelled.is_set():
            await asyncio.sleep(.005)
        if cancel_cleanup:
            cleanup = [t for t in asyncio.all_tasks()
                       if t.get_coro().__qualname__.endswith('.stop_and_wait')]
            assert len(cleanup) == 1
            cleanup[0].cancel()
        task.cancel()
        await asyncio.sleep(.02)
        try:
            assert not task.done(), 'repeat cancellation released admission before real work ended'
            with pytest.raises(SearchBusy):
                with gate.enter():
                    pytest.fail('old worker still owns capacity')
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        with gate.enter():
            pass
    asyncio.run(scenario())
