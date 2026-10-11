"""Bound expensive feed searches before they consume database connections.

Admission is shared by workers on this deployment host via flock, not a
per-worker semaphore. A multi-host deployment must use a shared admission service.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
import fcntl
from functools import lru_cache, wraps
import json
import inspect
import math
import os
from pathlib import Path
import threading
import time

from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool


class SearchBusy(Exception):
    def __init__(self, retry_after=3):
        self.retry_after = retry_after


class SearchExpired(Exception):
    pass


@lru_cache(maxsize=1)
def search_settings():
    """Freeze admission settings per worker; invalid settings fail only searches."""
    capacity = int(os.getenv('INFO2ACTION_FEED_SEARCH_CONCURRENCY', '2'))
    queue_size = int(os.getenv('INFO2ACTION_FEED_SEARCH_QUEUE_SIZE') or str(capacity))
    wait_seconds = float(os.getenv('INFO2ACTION_FEED_SEARCH_QUEUE_WAIT_SECONDS', '3'))
    if not 1 <= capacity <= 64 or not 0 <= queue_size <= 64:
        raise ValueError('Invalid search admission capacity')
    if not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= 10:
        raise ValueError('Invalid search queue wait')
    return capacity, queue_size, wait_seconds


class SearchGate:
    def __init__(self, directory, capacity=None, per_minute=30,
                 queue_capacity=None, wait_seconds=None):
        configured_capacity, configured_queue, configured_wait = search_settings()
        self.directory = Path(directory)
        self.capacity = configured_capacity if capacity is None else capacity
        self.queue_capacity = (configured_queue if capacity is None else capacity) if queue_capacity is None else queue_capacity
        self.wait_seconds = configured_wait if wait_seconds is None else wait_seconds
        self.per_minute = per_minute
        if (not 1 <= self.capacity <= 64 or not 0 <= self.queue_capacity <= 64
                or self.per_minute < 1 or not math.isfinite(self.wait_seconds)
                or not 0 <= self.wait_seconds <= 10):
            raise ValueError('Invalid search gate settings')

    def _open(self, name):
        return os.fdopen(os.open(self.directory / name, os.O_CREAT | os.O_RDWR, 0o600), 'r+')

    def _try_lock(self, name):
        candidate = self._open(name)
        try:
            fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return candidate
        except BlockingIOError:
            candidate.close()
            return None
        except BaseException:
            candidate.close()
            raise

    def _replace_json(self, name, value):
        # Called only under rate.lock: keep its inode stable across replacements.
        with self._open(name + '.tmp') as pending:
            pending.write(json.dumps(value))
            pending.truncate()
            pending.flush()
        os.replace(self.directory / (name + '.tmp'), self.directory / name)

    def _ensure_config(self):
        configuration = dict(capacity=self.capacity, queue_capacity=self.queue_capacity,
                             wait_seconds=self.wait_seconds, per_minute=self.per_minute)
        path = self.directory / 'configuration.json'
        if path.exists():
            current = json.loads(path.read_text())
            if not isinstance(current, dict) or current.keys() != configuration.keys():
                raise ValueError('Invalid search gate configuration state')
            if current == configuration:
                return
            # Configuration can change between restarts, never while an old
            # worker holds execution or queue places under different settings.
            for pattern in ('slot-*', 'queue-*'):
                for entry in self.directory.glob(pattern):
                    probe = self._try_lock(entry.name)
                    if probe is None:
                        raise ValueError('Search worker admission settings differ')
                    probe.close()
        self._replace_json('configuration.json', configuration)

    def _waiters(self):
        waiting = []
        for index in range(self.queue_capacity):
            probe = self._try_lock(f'queue-{index}')
            if probe is not None:
                probe.close()  # A dead worker's registration is automatically free.
                continue
            ticket = json.loads((self.directory / f'queue-{index}').read_text())
            if not isinstance(ticket, int) or isinstance(ticket, bool) or ticket < 0:
                raise ValueError('Invalid search queue registration')
            waiting.append((ticket, index))
        return sorted(waiting)

    def _record_request(self):
        now = time.monotonic()
        path = self.directory / 'requests.json'
        arrivals = json.loads(path.read_text()) if path.exists() else []
        if not isinstance(arrivals, list) or any(
            not isinstance(t, (int, float)) or isinstance(t, bool) or not math.isfinite(t)
            for t in arrivals
        ):
            raise ValueError('Invalid search rate state')
        arrivals = [t for t in arrivals if now - 60 < t <= now]
        if len(arrivals) >= self.per_minute:
            raise SearchBusy(max(1, math.ceil(arrivals[0] + 60 - now)))
        arrivals.append(now)
        self._replace_json('requests.json', arrivals)

    def _attempt(self, queued=None, enqueue=False):
        self.directory.mkdir(parents=True, exist_ok=True)
        coordinator = self._try_lock('rate.lock')
        if coordinator is None:
            if queued is not None:
                return None, queued
            raise SearchBusy(1)
        try:
            self._ensure_config()
            waiting = self._waiters()
            is_first = not waiting if queued is None else waiting[0] == queued[1:]
            if is_first:
                for index in range(self.capacity):
                    slot = self._try_lock(f'slot-{index}')
                    if slot is not None:
                        try:
                            self._record_request()
                        except BaseException:
                            slot.close()
                            raise
                        if queued is not None:
                            queued[0].close()
                        return slot, None
            if queued is not None:
                return None, queued
            if enqueue and self.wait_seconds > 0:
                for index in range(self.queue_capacity):
                    place = self._try_lock(f'queue-{index}')
                    if place is not None:
                        ticket = max(time.monotonic_ns(), waiting[-1][0] + 1 if waiting else 0)
                        try:
                            place.write(json.dumps(ticket))
                            place.truncate()
                            place.flush()
                        except BaseException:
                            place.close()
                            raise
                        return None, (place, ticket, index)
            raise SearchBusy()
        finally:
            coordinator.close()

    @contextmanager
    def enter(self):
        """Immediate entry (also respects earlier asynchronous waiters)."""
        slot, _ = self._attempt()
        try:
            yield
        finally:
            slot.close()

    @asynccontextmanager
    async def wait(self, budget, request=None):
        """Bounded FIFO waiting with no borrowed connection or worker thread."""
        deadline = time.monotonic() + self.wait_seconds
        queued = slot = None
        try:
            while slot is None:
                budget.check()
                if request is not None and await request.is_disconnected():
                    budget.cancel('search_cancelled')  # No connections registered yet.
                    budget.check()
                if queued is not None and time.monotonic() >= deadline:
                    raise SearchBusy()
                slot, queued = self._attempt(queued, enqueue=True)
                if slot is None:
                    await asyncio.sleep(max(0, min(.025, deadline - time.monotonic(), budget.remaining())))
            budget.check()
            yield
        finally:
            if queued is not None:
                queued[0].close()
            if slot is not None:
                slot.close()


class SearchBudget:
    def __init__(self, timeout=120, max_queries=12, statement_ms=60000):
        self.deadline = time.monotonic() + timeout
        self.max_queries = max_queries
        self.statement_ms = statement_ms
        self.queries = 0
        self.reason = None
        self._stopped = False
        self._connections = []
        self._lock = threading.RLock()
        self._workers = 0
        self.workers_finished = threading.Event()
        self.workers_finished.set()

    def remaining(self):
        return self.deadline - time.monotonic()

    def check(self):
        if self._stopped or self.remaining() <= 0:
            self.reason = self.reason or 'search_timeout'
            raise SearchExpired(self.reason)

    def cancel(self, reason):
        # Registration and cancellation share a lock: never cancel a connection
        # after it has been returned to the pool and lent to another request.
        with self._lock:
            if self._stopped:
                return
            self.reason = reason
            self._stopped = True
            for conn in self._connections:
                try:
                    conn.cancel_safe(timeout=2)
                except Exception:
                    # SET LOCAL statement_timeout remains the server-side backstop.
                    pass

    @contextmanager
    def worker(self):
        with self._lock:
            self.check()
            self._workers += 1
            self.workers_finished.clear()
        try:
            yield
        finally:
            with self._lock:
                self._workers -= 1
                if self._workers == 0:
                    self.workers_finished.set()

    @contextmanager
    def connection(self, conn):
        with self._lock:
            self.check()
            self._connections.append(conn)
        try:
            yield SearchConnection(conn, self)
        finally:
            with self._lock:
                self._connections.remove(conn)


class SearchConnection:
    def __init__(self, conn, budget):
        self.conn, self.budget = conn, budget

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def execute(self, sql, params=None, **kwargs):
        command = str(sql).lstrip().upper()
        if command.startswith(('SAVEPOINT ', 'RELEASE ', 'ROLLBACK ')):
            return self.conn.execute(sql, params, **kwargs)
        self.budget.check()
        remaining_ms = max(1, int(self.budget.remaining() * 1000))
        timeout_ms = min(self.budget.statement_ms, remaining_ms)
        if command.startswith('SET LOCAL STATEMENT_TIMEOUT'):
            return self.conn.execute(f"SET LOCAL statement_timeout = '{timeout_ms}ms'")
        if command.startswith(('SET ', 'BEGIN ', 'COMMIT')):
            return self.conn.execute(sql, params, **kwargs)
        if self.budget.queries >= self.budget.max_queries:
            self.budget.reason = 'search_query_limit'
            raise SearchExpired(self.budget.reason)
        self.budget.queries += 1
        self.conn.execute(f"SET LOCAL statement_timeout = '{timeout_ms}ms'")
        try:
            return self.conn.execute(sql, params, **kwargs)
        except Exception as exc:
            if getattr(exc, 'sqlstate', None) == '57014':
                self.budget.reason = 'search_timeout'
            raise


current_search: ContextVar[SearchBudget | None] = ContextVar('feed_search', default=None)


@contextmanager
def optional_search_count(conn, enabled):
    """A failed count may roll back to its savepoint without losing search cards."""
    state = {'available': True}
    if not enabled:
        yield state
        return
    conn.execute('SAVEPOINT search_count')
    try:
        yield state
    except Exception:
        conn.execute('ROLLBACK TO SAVEPOINT search_count')
        state['available'] = False
    finally:
        conn.execute('RELEASE SAVEPOINT search_count')


def _track_search_worker(fn):
    if inspect.iscoroutinefunction(fn) or getattr(fn, '_search_worker_tracked', False):
        return fn
    @wraps(fn)
    def tracked(*args, **kwargs):
        budget = current_search.get()
        if budget is None:
            return fn(*args, **kwargs)
        with budget.worker():
            return fn(*args, **kwargs)
    # FastAPI resolves forward annotations using the callable's globals.
    # Resolve them before moving a routes/feed.py function into this module.
    tracked.__signature__ = inspect.signature(fn, eval_str=True)
    tracked._search_worker_tracked = True
    return tracked


def search_guarded(fn):
    worker = _track_search_worker(fn)
    @wraps(fn)
    async def guarded(*args, **kwargs):
        request = kwargs.get('request') or next((a for a in args if hasattr(a, 'query_params')), None)
        search = request.query_params.get('search', '') if request else ''
        if not search:
            return await fn(*args, **kwargs) if inspect.iscoroutinefunction(fn) else await run_in_threadpool(fn, *args, **kwargs)
        try:
            try:
                timeout = float(request.headers.get('X-Search-Budget-Ms', '120000')) / 1000
                if not math.isfinite(timeout) or timeout <= 0:
                    raise ValueError()
            except ValueError:
                return JSONResponse({'error': 'Invalid search budget'}, status_code=400)
            budget = SearchBudget(timeout=min(120, timeout))
            gate = SearchGate(Path(__file__).resolve().parents[1] / 'logs' / 'search-admission')
            async with gate.wait(budget, request=request):
                token = current_search.set(budget)
                task = asyncio.create_task(fn(*args, **kwargs) if inspect.iscoroutinefunction(fn) else run_in_threadpool(worker, *args, **kwargs), name='feed-search-work')
                try:
                    while not task.done():
                        await asyncio.wait({task}, timeout=.2)
                        if not task.done() and (budget.remaining() <= 0 or await request.is_disconnected()):
                            await asyncio.to_thread(budget.cancel, 'search_timeout' if budget.remaining() <= 0 else 'search_cancelled')
                    result = await task
                    budget.check()  # Normalization can finish between polling ticks.
                    payload = json.loads(result.body) if hasattr(result, 'body') else result
                    if budget.reason and (not isinstance(payload, dict) or payload.get('degraded') or getattr(result, 'status_code', 200) >= 400):
                        return JSONResponse({'error': '搜索超时，请重试', 'code': budget.reason}, status_code=504)
                    if hasattr(result, 'headers'):
                        result.headers['X-Search-Queries'] = str(budget.queries)
                    return result
                except asyncio.CancelledError:
                    async def stop_and_wait():
                        await asyncio.to_thread(budget.cancel, 'search_cancelled')
                        try:
                            await task
                        except (Exception, asyncio.CancelledError):
                            pass
                        # A cancelled asyncio Task can detach from a still-running
                        # synchronous endpoint during server shutdown.
                        while not budget.workers_finished.is_set():
                            await asyncio.sleep(.02)
                    # Repeated ASGI cancellation cannot interrupt cleanup and
                    # release capacity while a real database worker is running.
                    while True:
                        cleanup = asyncio.create_task(stop_and_wait())
                        while not cleanup.done():
                            try:
                                await asyncio.shield(cleanup)
                            except asyncio.CancelledError:
                                continue
                        try:
                            cleanup.result()
                        except asyncio.CancelledError:
                            # Server shutdown can cancel the cleanup child too.
                            # Retry cancellation and wait for actual worker exit.
                            continue
                        break
                    raise
                finally:
                    current_search.reset(token)
        except SearchExpired as exc:
            return JSONResponse({'error': '搜索超时，请重试', 'code': str(exc)}, status_code=504)
        except SearchBusy as exc:
            return JSONResponse({'error': '搜索繁忙，请稍后重试', 'code': 'search_busy', 'retry_after': exc.retry_after}, status_code=429, headers={'Retry-After': str(exc.retry_after)})
        except (OSError, ValueError, TypeError):
            return JSONResponse({'error': '搜索暂时不可用，请稍后重试', 'code': 'search_guard_unavailable'}, status_code=503)
    return guarded


class SearchRoute(APIRoute):
    def __init__(self, path, endpoint, **kwargs):
        if path in {'/api/feed', '/api/feed/sections', '/api/feed/sections/more', '/api/feed/platforms', '/api/feed/platforms/more'}:
            endpoint = _track_search_worker(endpoint)
        super().__init__(path, endpoint, **kwargs)

    def get_route_handler(self):
        handler = super().get_route_handler()
        if self.path in {'/api/feed', '/api/feed/sections', '/api/feed/sections/more', '/api/feed/platforms', '/api/feed/platforms/more'}:
            return search_guarded(handler)
        return handler
