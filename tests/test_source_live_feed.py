"""Source pages use bounded reads, snapshot counts and scoped live cursors."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import pytest
import remote_db


class Result:
    def __init__(self, rows): self.rows = rows
    def fetchall(self): return self.rows


def card(item_id, *, published_at='2026-07-01T00:00:00Z', fetched_at='2026-07-02T00:00:00Z', score=1):
    return dict(id=item_id, platform='twitter', source='following', visible=1,
                user_id=None, ai_categories=['products'], ai_category='products',
                published_at=published_at, fetched_at=fetched_at, relevance_score=score)


class Conn:
    def __init__(self, cards=None, counts=True):
        self.cards = cards or [card('c'), card('b'), card('a')]
        self.counts = counts
        self.calls = []
    def execute(self, sql, params=None):
        self.calls.append((sql, params or {}))
        if sql.startswith('SET LOCAL'): return Result([])
        if 'info_pill_counts' in sql:
            if not self.counts: return Result([])
            return Result([
                dict(kind='platform_meta', value='50', n=1, computed_at='2026-07-02T00:00:00Z'),
                dict(kind='platform:50', value=json.dumps(['public', 'twitter']), n=90, computed_at='2026-07-02T00:00:00Z'),
                dict(kind='platform_category:50', value=json.dumps(['public', 'twitter', 'products']), n=80, computed_at='2026-07-02T00:00:00Z'),
            ])
        if 'row_number()' in sql.lower() or 'count(' in sql.lower():
            raise AssertionError('request must not rank/count all items')
        return Result(self.cards)
    def rollback(self): pass


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    remote_db.clear_feed_cache_keys()
    monkeypatch.setattr(remote_db, '_REMOTE_FEED_LIVE_CIRCUIT_OPEN_UNTIL', 0.0)
    monkeypatch.setattr(remote_db, 'remote_schema', lambda: 'remote_poc')
    monkeypatch.setattr(remote_db, 'feed_read_backend', lambda: 'supabase_poc')
    monkeypatch.setattr(remote_db, '_read_local_read_cache', lambda *a, **k: None)
    monkeypatch.setattr(remote_db, '_write_local_read_cache_async', lambda *a, **k: None)
    monkeypatch.setenv(remote_db.INFO_READ_MODEL_ENV, '0')
    yield
    remote_db.clear_feed_cache_keys()


def install(monkeypatch, conn):
    @contextmanager
    def connect(): yield conn
    monkeypatch.setattr(remote_db, 'connect', connect)


def test_overview_bounded_reads_and_snapshot_totals(monkeypatch):
    def forbid_legacy_count_prewarm(**kwargs):
        pytest.fail('live source pages use snapshot counts directly')
    monkeypatch.setattr(remote_db, '_warm_platform_page_count_cache', forbid_legacy_count_prewarm)
    conn = Conn()
    install(monkeypatch, conn)
    result = remote_db.query_feed_platforms(per_platform=2, public_only=True)
    assert result.get('degraded') is not True
    assert result['platform_counts']['twitter'] == 90
    assert len(result['sections']['twitter']) == 2
    assert result['platform_next_cursors']['twitter']['kind'] == 'live'
    assert result['counts_generated_at']
    assert any('LATERAL' in sql and 'LIMIT' in sql for sql, _ in conn.calls)
    assert not any('row_number()' in sql.lower() or 'count(' in sql.lower() for sql, _ in conn.calls)


def test_missing_snapshot_keeps_cards_and_unknown_counts(monkeypatch):
    conn = Conn(counts=False)
    install(monkeypatch, conn)
    result = remote_db.query_feed_platforms(per_platform=2, public_only=True)
    assert result.get('degraded') is not True
    assert result['sections']['twitter']
    assert result['platform_counts'] == {}
    assert result['counts_generated_at'] is None


def test_null_cursor_compares_nulls_last_explicitly():
    where, params = [], {}
    remote_db._add_platform_live_after(where, params, dict(sort_at=None, fetched_at=None, relevance_score=None, id='b'))
    sql = ' '.join(where)
    assert 'IS NULL' in sql
    assert 'i.id < %(cursor_id)s' in sql
    assert params['cursor_id'] == 'b'


def test_source_cursor_preserves_postgres_microseconds(monkeypatch):
    conn = Conn(cards=[card('c', published_at='2026-07-01T00:00:00.123456Z'), card('b')])
    install(monkeypatch, conn)
    result = remote_db.query_feed_platforms(per_platform=1, public_only=True)
    cursor = result['platform_next_cursors']['twitter']
    assert cursor['after']['sort_at'] == '2026-07-01T00:00:00.123456Z'
    assert remote_db._normalize_item(card('c', published_at='2026-07-01T00:00:00.123456Z'))['published_at'] == '2026-07-01T00:00:00Z'


def test_prewarm_reports_degraded_as_failure(monkeypatch):
    monkeypatch.setattr(remote_db, '_info_read_model_enabled', lambda *a: False)
    monkeypatch.setattr(remote_db, '_highlights_read_model_enabled', lambda *a: False)
    monkeypatch.setattr(remote_db, 'query_feed_sections', lambda **k: {'sections': {}})
    monkeypatch.setattr(remote_db, 'query_feed_platforms', lambda **k: {'sections': {}, 'degraded': True})
    result = remote_db.prewarm_platforms(refresh_read_model=False, refresh_highlights_read_model=False)
    assert result['query_ok'] is False
    assert result['query_error']


def test_snapshot_audiences_do_not_leak_another_users_private_totals(monkeypatch):
    class AudienceConn(Conn):
        def execute(self, sql, params=None):
            if 'info_pill_counts' not in sql:
                return super().execute(sql, params)
            return Result([
                dict(kind='platform_meta', value='50', n=1, computed_at='2026-07-02T00:00:00Z'),
                *[dict(kind='platform:50', value=json.dumps([audience, platform]), n=n)
                  for audience, platform, n in [('public', 'twitter', 90),
                    ('user:me', 'manual', 2), ('user:someone_else', 'manual', 10),
                    ('admin', 'twitter', 90), ('admin', 'manual', 12)]],
            ])
    conn = AudienceConn()
    public = remote_db._platform_snapshot_counts(conn, 'remote_poc', public_only=True,
                manual_owner_user_id=None, min_github_stars=50)
    own = remote_db._platform_snapshot_counts(conn, 'remote_poc', public_only=False,
                manual_owner_user_id='me', min_github_stars=50)
    admin = remote_db._platform_snapshot_counts(conn, 'remote_poc', public_only=False,
                manual_owner_user_id=None, min_github_stars=50)
    assert public['platform_counts'] == {'twitter': 90}
    assert own['platform_counts'] == {'twitter': 90, 'manual': 2}
    assert admin['platform_counts'] == {'twitter': 90, 'manual': 12}


def test_stale_fallback_rehydrates_only_currently_allowed_ids(monkeypatch):
    conn = Conn(cards=[card('visible_now')])
    install(monkeypatch, conn)
    cached = dict(sections={'twitter': [card('visible_now'), card('hidden_now')]},
                  platform_counts={'twitter': 100}, source_counts={}, category_counts={},
                  overview_generated_at=datetime.now(timezone.utc).isoformat())
    fallback = remote_db._validated_platforms_fallback(cached, schema='remote_poc',
        where=['i.visible = 1', "i.fetched_at >= now() - interval '90 days'", 'i.user_id IS NULL'],
        params={}, user_id=None)
    assert [i['id'] for i in fallback['sections']['twitter']] == ['visible_now']
    assert fallback['stale'] is True and fallback['degraded'] is True
    assert fallback['platform_counts'] == {}
    assert cached['platform_counts'] == {'twitter': 100}  # no mutation
    query, params = next((s,p) for s,p in conn.calls if 'items i' in s)
    assert 'visible = 1' in query and '90 days' in query and 'user_id IS NULL' in query
    assert params['fallback_ids'] == ['visible_now', 'hidden_now']
    conn.calls.clear()
    cached['overview_generated_at'] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    assert remote_db._validated_platforms_fallback(cached, schema='remote_poc', where=[], params={}, user_id=None) is None
    assert conn.calls == []


def test_stale_fallback_fails_closed_when_visibility_cannot_be_checked(monkeypatch):
    @contextmanager
    def failed_connect():
        raise remote_db.RemoteDBError('unavailable')
        yield
    monkeypatch.setattr(remote_db, 'connect', failed_connect)
    cached = dict(sections={'twitter': [card('private_after_snapshot')]},
                  overview_generated_at=datetime.now(timezone.utc).isoformat())
    assert remote_db._validated_platforms_fallback(cached, schema='remote_poc', where=[], params={}, user_id=None) is None


def test_platform_failure_not_public_cached_and_refresh_bypasses_cache(monkeypatch):
    from types import SimpleNamespace
    import routes.feed as route
    from routes.public_response_cache import clear_public_response_cache
    clear_public_response_cache()
    request = SimpleNamespace(method='GET', headers={},
        state=SimpleNamespace(user=None, legacy_authenticated=False))
    calls = []
    def query(**kwargs):
        calls.append(kwargs)
        if len(calls) <= 2:
            return {'sections': {}, 'degraded': True}
        return {'sections': {'twitter': [card('a')]}, 'degraded': False}
    monkeypatch.setattr(route.remote_db, 'feed_read_from_remote', lambda: True)
    monkeypatch.setattr(route.remote_db, 'query_feed_platforms', query)
    monkeypatch.setattr(route, '_github_display_min_stars', lambda: 50)
    first = route.get_feed_platforms(request, search=None, refresh=False)
    second = route.get_feed_platforms(request, search=None, refresh=False)
    assert first.headers['cache-control'] == second.headers['cache-control'] == 'no-store'
    route.get_feed_platforms(request, search=None, refresh=False)
    route.get_feed_platforms(request, search=None, refresh=False)
    assert len(calls) == 3  # only the successful response is cached
    route.get_feed_platforms(request, search=None, refresh=True)
    assert len(calls) == 4 and calls[-1]['force_refresh'] is True
    clear_public_response_cache()


@pytest.mark.parametrize('sections', [None, [], {'twitter': None}, {'twitter': ['bad-card']}])
def test_malformed_success_cache_fails_closed(monkeypatch, sections):
    cached = {'sections': sections, 'overview_generated_at': datetime.now(timezone.utc).isoformat()}
    assert remote_db._validated_platforms_fallback(cached, schema='remote_poc', where=[], params={}, user_id=None) is None


def test_source_snapshot_uses_configured_stars_without_changing_type_snapshot(monkeypatch):
    import db
    monkeypatch.setattr(db, 'github_min_stars_for_display', lambda default=50: 75)
    monkeypatch.setattr(remote_db, 'remote_authority_enabled', lambda: True)
    class RefreshConn:
        def __init__(self): self.calls = []; self.commits = 0
        def execute(self, sql, params=None): self.calls.append((sql, params or {})); return Result([])
        def commit(self): self.commits += 1
    conn = RefreshConn()
    install(monkeypatch, conn)
    assert remote_db.refresh_info_pill_counts()['ok']
    section_sql, _ = next((s,p) for s,p in conn.calls if "SELECT 'section_category'" in s)
    source_sql, params = next((s,p) for s,p in conn.calls if 'platform_meta' in s)
    assert '>= 50' in section_sql
    assert params['stars'] == '75' and '>= 75' in source_sql
    assert conn.commits == 1


def test_source_prewarm_uses_configured_stars_type_prewarm_stays_50(monkeypatch):
    import db
    monkeypatch.setattr(db, 'github_min_stars_for_display', lambda default=50: 75)
    calls = []
    monkeypatch.setattr(remote_db, '_info_read_model_enabled', lambda *a: False)
    monkeypatch.setattr(remote_db, '_highlights_read_model_enabled', lambda *a: False)
    monkeypatch.setattr(remote_db, 'query_feed_sections', lambda **k: calls.append(('type', k)) or {})
    monkeypatch.setattr(remote_db, 'query_feed_platforms', lambda **k: calls.append(('source', k)) or {})
    remote_db.prewarm_platforms(refresh_read_model=False, refresh_highlights_read_model=False)
    assert calls[0][1]['min_github_stars'] == 50
    assert calls[1][1]['min_github_stars'] == 75


@pytest.mark.parametrize('counts_only', [False, True])
def test_postfetch_updates_counts_even_with_all_prewarm_models_disabled(monkeypatch, counts_only):
    import routes.fetch as fetch
    calls = []
    for key in ['INFO2ACTION_CACHE_PREWARM', 'INFO2ACTION_INFO_READ_MODEL', 'INFO2ACTION_HIGHLIGHTS_READ_MODEL']:
        monkeypatch.setenv(key, '1' if counts_only else '0')
    monkeypatch.setattr(fetch, '_platform_prewarm_enabled', lambda: counts_only)
    monkeypatch.setattr(fetch, '_remote_db_pressure_skip_reason', lambda: None)
    monkeypatch.setattr(fetch.remote_db, 'remote_authority_enabled', lambda: True)
    monkeypatch.setattr(fetch.remote_db, 'refresh_info_pill_counts', lambda: calls.append('counts') or {'ok': True})
    def forbid(**k): pytest.fail('read models/prewarm should stay disabled')
    monkeypatch.setattr(fetch.remote_db, 'prewarm_platforms', forbid)
    monkeypatch.setattr(fetch.remote_db, 'refresh_info_read_model_if_stale', forbid)
    monkeypatch.setattr(fetch.remote_db, 'refresh_highlights_read_model_if_stale', forbid)
    class InlineThread:
        def __init__(self, *, target, **k): self.target = target
        def start(self): self.target()
    monkeypatch.setattr(fetch.threading, 'Thread', InlineThread)
    fetch._schedule_post_fetch_read_model_refresh(123, counts_only=counts_only)
    assert calls == ['counts']
