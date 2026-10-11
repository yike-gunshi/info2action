"""Search cancellation covers the real pool check/retry loop without a DB."""
from contextlib import contextmanager
from pathlib import Path
import re
import sys
import threading
import time

import psycopg
from psycopg_pool import ConnectionPool, PoolTimeout
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import remote_db
from feed_search import SearchBudget, SearchBusy, SearchExpired, current_search


class Connection:
    def __init__(self, block=False, rollback_fails=False):
        self.autocommit = False
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.block = block
        self.cancelled = 0
        self.rollbacks = 0
        self.registered_during_probe = False
        self.rollback_fails = rollback_fails
        self.closed = False

    def execute(self, sql, params=None):
        self.calls.append(str(sql))
        if str(sql) in ("", "SELECT 1"):
            budget = current_search.get()
            self.registered_during_probe = budget is not None and self in budget._connections
            self.started.set()
            if self.block:
                assert self.release.wait(2), "test did not release probe"
                if self.cancelled:
                    raise psycopg.errors.QueryCanceled("canceling connection probe")
        return self

    def cancel_safe(self, timeout):
        self.cancelled += 1
        self.release.set()

    def rollback(self):
        self.rollbacks += 1
        if self.rollback_fails:
            raise psycopg.OperationalError("probe rollback failed")

    def close(self):
        self.closed = True

    def __enter__(self):
        if self.closed:
            raise psycopg.OperationalError("closed connection")
        return self

    def __exit__(self, *args):
        pass


@pytest.fixture
def pool_factory(monkeypatch):
    """Capture the app callback, then exercise the installed driver retry loop."""
    callbacks = {}

    class CapturePool:
        check_connection = staticmethod(ConnectionPool.check_connection)

        def __init__(self, **kwargs):
            callbacks.update(kwargs)

    monkeypatch.setattr("psycopg_pool.ConnectionPool", CapturePool)
    monkeypatch.setattr(remote_db, "_POOL", None)
    monkeypatch.setattr(remote_db, "_POOL_DSN", None)
    monkeypatch.setattr(remote_db, "_runtime_env", lambda: {})
    monkeypatch.setattr(remote_db, "database_url", lambda: "postgresql://unused.invalid/db")
    remote_db._get_pool(psycopg, None)

    def build(conn, timeout=2, legacy_loop=False):
        pool = ConnectionPool(
            conninfo="postgresql://unused.invalid/db", min_size=0, max_size=1,
            open=False, check=callbacks["check"], timeout=timeout,
        )
        state = {"taken": 0, "returned": 0, "timeout": None}

        def take(unused_timeout):
            state["taken"] += 1
            return conn

        original_getconn = pool.getconn

        def getconn(timeout=None):
            state["timeout"] = timeout
            return original_getconn(timeout=timeout)

        monkeypatch.setattr(pool, "_check_open_getconn", lambda: None)
        monkeypatch.setattr(pool, "_getconn_unchecked", take)
        monkeypatch.setattr(pool, "_putconn", lambda *a, **kw: state.__setitem__("returned", state["returned"] + 1))
        monkeypatch.setattr(pool, "putconn", lambda *a: state.__setitem__("returned", state["returned"] + 1))
        monkeypatch.setattr(pool, "getconn", getconn)
        if legacy_loop:
            # psycopg_pool 3.3.1 has no BaseException cleanup in this loop.
            # Keep the installed pool's getconn()/connection() lifecycle, with
            # that older check-loop exception boundary as a compatibility shim.
            def check_loop(deadline):
                checked_conn = pool._getconn_unchecked(deadline - time.monotonic())
                try:
                    pool._check_connection(checked_conn)
                except Exception:
                    pool._putconn(checked_conn, from_getconn=True)
                    raise PoolTimeout("failed legacy health check")
                return checked_conn
            monkeypatch.setattr(pool, "_getconn_with_check_loop", check_loop)
        monkeypatch.setattr(remote_db, "_get_pool", lambda *a: pool)
        monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
        return pool, state

    return build, callbacks


@pytest.mark.parametrize("legacy_loop", [False, True], ids=["installed-driver", "pool-3.3.1-boundary"])
@pytest.mark.parametrize("rollback_fails", [False, True], ids=["rollback-ok", "discard-broken"])
def test_cancelled_health_check_stops_pool_retry_and_returns_connection_once(pool_factory, legacy_loop, rollback_fails):
    build, _ = pool_factory
    conn = Connection(block=True, rollback_fails=rollback_fails)
    _, state = build(conn, legacy_loop=legacy_loop)
    budget = SearchBudget(timeout=5)
    errors = []
    done = threading.Event()

    def worker():
        token = current_search.set(budget)
        try:
            with remote_db.connect():
                pytest.fail("cancelled probe reached a search query")
        except BaseException as exc:
            errors.append(exc)
        finally:
            current_search.reset(token)
            done.set()

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert conn.started.wait(1)
        budget.cancel("search_cancelled")
        stopped_promptly = done.wait(.3)
    finally:
        conn.release.set()
        thread.join(3)
    assert stopped_promptly, "cancelled health check continued waiting/retrying"
    assert conn.registered_during_probe, "pool health check bypassed cancellation registration"
    assert conn.cancelled == 1
    assert len(errors) == 1 and isinstance(errors[0], SearchExpired)
    assert str(errors[0]) == "search_cancelled"
    assert state["taken"] == state["returned"] == 1
    assert conn.rollbacks == 1
    assert conn.closed is rollback_fails
    assert budget._connections == []
    budget.cancel("search_timeout")
    assert conn.cancelled == 1, "late cancellation reached returned connection"


def test_health_check_uses_local_read_only_transaction_and_checkout_deadline(pool_factory):
    build, _ = pool_factory
    conn = Connection()
    _, state = build(conn, timeout=.2)
    budget = SearchBudget(timeout=2)
    token = current_search.set(budget)
    try:
        with remote_db.connect():
            pass
    finally:
        current_search.reset(token)
    assert "SELECT 1" in conn.calls, "search probe has no bounded read-only transaction"
    probe = conn.calls[:conn.calls.index("SELECT 1") + 1]
    # autocommit=False makes psycopg send its implicit BEGIN before the first
    # execute(). A second BEGIN READ ONLY would leave that transaction unchanged.
    assert probe[0] == "SET TRANSACTION READ ONLY"
    assert not any(s.startswith("BEGIN") for s in probe)
    timeout_sql = next(s for s in probe if s.startswith("SET LOCAL statement_timeout"))
    timeout_ms = int(re.search(r"'(\d+)ms'", timeout_sql).group(1))
    assert 0 < timeout_ms <= 200
    assert conn.registered_during_probe
    assert budget.queries == 0, "health checks must not consume the read-query allowance"
    assert conn.rollbacks == 2, "probe transaction must end before the query transaction"
    assert all(not s.startswith("SET ") or s.startswith(("SET LOCAL ", "SET TRANSACTION ")) for s in conn.calls)
    assert state["taken"] == state["returned"] == 1


def test_pool_checkout_is_bounded_by_remaining_action_budget(pool_factory):
    build, _ = pool_factory
    _, state = build(Connection(), timeout=2)
    budget = SearchBudget(timeout=.2)
    token = current_search.set(budget)
    try:
        with remote_db.connect():
            pass
    finally:
        current_search.reset(token)
    assert state["timeout"] is not None and 0 < state["timeout"] <= .2


def test_pool_timeout_is_search_busy_and_does_not_become_db_failure(monkeypatch):
    class Pool:
        timeout = 2

        @contextmanager
        def connection(self, timeout=None):
            raise PoolTimeout("pool occupied")
            yield

    monkeypatch.setattr(remote_db, "_get_pool", lambda *a: Pool())
    token = current_search.set(SearchBudget(timeout=2))
    try:
        with pytest.raises(SearchBusy):
            with remote_db.connect():
                pass
    finally:
        current_search.reset(token)


def test_query_budget_exception_survives_connect_cleanup(pool_factory):
    build, _ = pool_factory
    conn = Connection()
    _, state = build(conn)
    budget = SearchBudget()
    token = current_search.set(budget)
    try:
        with pytest.raises(SearchExpired, match="search_query_limit"):
            with remote_db.connect():
                raise SearchExpired("search_query_limit")
    finally:
        current_search.reset(token)
    assert state["taken"] == state["returned"] == 1
    assert conn.rollbacks == 2


def test_non_search_health_check_keeps_driver_liveness_probe(pool_factory):
    _, callbacks = pool_factory
    conn = Connection()
    callbacks["check"](conn)
    assert conn.calls == [""]
    assert conn.autocommit is False
    assert conn.rollbacks == 0
