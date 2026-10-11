"""Searches slower than ordinary feed reads must still return matching cards."""
from contextlib import contextmanager
from pathlib import Path
import re
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import remote_db


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class BudgetedConnection:
    def __init__(self, query_ms):
        self.query_ms = query_ms
        self.timeout_ms = 0
        self.params = []

    def execute(self, sql, params=None):
        if sql.startswith("SET LOCAL statement_timeout"):
            self.timeout_ms = int(re.search(r"'(\d+)ms'", sql).group(1))
            return Result([])
        if sql.startswith(('SAVEPOINT ', 'ROLLBACK ', 'RELEASE ')):
            return Result([])
        self.params.append(params or {})
        if self.timeout_ms < self.query_ms:
            raise TimeoutError("search canceled before matching cards could return")
        if "count(" in sql.lower():
            if "i.ai_categories IS NULL" in sql:
                return Result([])
            return Result([dict(section_category="coding", category="coding",
                                platform="twitter", source="following", cnt=1)])
        return Result([dict(id="design-hit", title="设计工具", platform="twitter",
                            source="following", section_category="coding",
                            ai_category="coding", ai_categories=["coding"],
                            visible=1)])

    def rollback(self):
        pass


@pytest.fixture(autouse=True)
def isolated_feed(monkeypatch):
    remote_db.clear_feed_cache_keys()
    monkeypatch.setattr(remote_db, "_REMOTE_FEED_LIVE_CIRCUIT_OPEN_UNTIL", 0.0)
    monkeypatch.setattr(remote_db, "_runtime_env", lambda: {
        remote_db.INFO_READ_MODEL_ENV: "0",
        remote_db.REMOTE_FEED_LIVE_TIMEOUT_MS_ENV: "2500",
        remote_db.REMOTE_FEED_SEARCH_TIMEOUT_MS_ENV: "10000",
    })
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_read_local_read_cache", lambda *a, **k: None)
    monkeypatch.setattr(remote_db, "_write_local_read_cache_async", lambda *a, **k: None)
    yield
    remote_db.clear_feed_cache_keys()


def install_connection(monkeypatch, conn):
    @contextmanager
    def connect():
        yield conn
    monkeypatch.setattr(remote_db, "connect", connect)


def query(view, search):
    if view == "category":
        return remote_db.query_feed_sections(search=search, public_only=True)
    return remote_db.query_feed_platforms(search=search, per_platform=None, public_only=True)


@pytest.mark.parametrize("view", ["category", "platform"])
@pytest.mark.parametrize("search,query_ms,expected_timeout", [
    ("设计", 3000, 10000),
    (None, 1000, 2500),
])
def test_search_returns_matches_without_relaxing_ordinary_feed_budget(
    monkeypatch, view, search, query_ms, expected_timeout,
):
    conn = BudgetedConnection(query_ms)
    install_connection(monkeypatch, conn)
    result = query(view, search)
    assert result.get("degraded") is not True
    cards = [item for items in result["sections"].values() for item in items]
    assert [item["id"] for item in cards] == ["design-hit"]
    assert conn.timeout_ms == expected_timeout
    if search:
        assert all(params.get("search_like") == "%设计%" for params in conn.params)


@pytest.mark.parametrize("view", ["category", "platform"])
def test_search_still_has_a_finite_timeout(monkeypatch, view):
    conn = BudgetedConnection(11000)
    install_connection(monkeypatch, conn)
    result = query(view, "设计")
    assert result["degraded"] is True
    assert result["sections"] == {}

@pytest.mark.parametrize('view', ['category', 'platform'])
def test_count_failure_preserves_search_cards(monkeypatch, view):
    class FailingCountConnection(BudgetedConnection):
        def execute(self, sql, params=None):
            if sql.startswith(('SAVEPOINT ', 'ROLLBACK ', 'RELEASE ')):
                return Result([])
            if 'count(' in sql.lower():
                raise TimeoutError('count took too long')
            return super().execute(sql, params)
    conn = FailingCountConnection(3000)
    install_connection(monkeypatch, conn)
    result = query(view, '设计')
    cards = [item for items in result['sections'].values() for item in items]
    assert [item['id'] for item in cards] == ['design-hit']
    assert result['counts_available'] is False
    assert result.get('total') is None

@pytest.mark.parametrize('view', ['category', 'platform'])
def test_search_timeout_does_not_trip_shared_feed_circuit(monkeypatch, view):
    conn = BudgetedConnection(11000)
    install_connection(monkeypatch, conn)
    from feed_search import SearchBudget, current_search
    budget = SearchBudget()
    budget.reason = 'search_timeout'
    token = current_search.set(budget)
    try:
        assert query(view, '设计')['degraded'] is True
        assert remote_db._REMOTE_FEED_LIVE_CIRCUIT_OPEN_UNTIL == 0
    finally:
        current_search.reset(token)

@pytest.mark.parametrize('view', ['category', 'platform', 'category_more', 'platform_more', 'items'])
@pytest.mark.parametrize('failure', ['busy', 'expired'])
def test_search_control_failure_is_not_empty_feed_or_shared_outage(monkeypatch, view, failure):
    from feed_search import SearchBudget, SearchBusy, SearchExpired, current_search
    error = SearchBusy() if failure == 'busy' else SearchExpired('search_cancelled')
    @contextmanager
    def unavailable():
        raise error
        yield
    monkeypatch.setattr(remote_db, 'connect', unavailable)
    token = current_search.set(SearchBudget())
    try:
        with pytest.raises(type(error)):
            if view in ('category', 'platform'):
                query(view, '设计')
            elif view == 'category_more':
                remote_db.query_feed_by_category(category='coding', search='设计', public_only=True)
            elif view == 'platform_more':
                remote_db.query_feed_by_platform(platform='twitter', search='设计', public_only=True)
            else:
                remote_db.query_feed(search='设计', public_only=True)
        assert remote_db._REMOTE_FEED_LIVE_CIRCUIT_OPEN_UNTIL == 0
    finally:
        current_search.reset(token)
