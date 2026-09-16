"""Date seeking exercises actual window/order/page SQL on an isolated SQLite DB.

The adapter translates PostgreSQL syntax/day grouping, supplies version metadata,
and stubs the unrelated cover lookup. Production visibility/category joins,
filters, window functions, pagination and result mapping execute on SQLite.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3

import pytest

from tests.test_remote_event_backend import _isolate_remote_env


VERSION = '00000000-0000-0000-0000-00000000abcd'


class SqlConn:
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.create_function('split_part', 3, lambda value, separator, part: value.split(separator)[part - 1])
        self.sqls = []
        self.db.executescript('''
          CREATE TABLE highlights_scope_items(version_id TEXT, scope_key TEXT, rank INT,
            cluster_id INT, sort_at TEXT, card_json TEXT);
          CREATE TABLE clusters(id INT PRIMARY KEY, why_read TEXT);
          CREATE TABLE highlight_cluster_decisions(cluster_id INT, manual_display TEXT,
            score_inputs TEXT, highlight_score REAL, cluster_verdict TEXT,
            verdict_counts_json TEXT, deciding_item_id TEXT);
          CREATE TABLE items(id TEXT, highlight_value_path TEXT);
        ''')
        for index in range(45):
            stamp = datetime(2026, 9, 4, 15, 0, tzinfo=timezone.utc) - timedelta(hours=index)
            cluster_id = 100 + index
            card = json.dumps({'id': cluster_id, 'ai_title': f'event {index}',
                               'first_doc_at': stamp.isoformat(), 'live_version': 3})
            self.db.execute('INSERT INTO highlights_scope_items VALUES(?,?,?,?,?,?)',
                            (VERSION, 'all', 1000 - index, cluster_id, stamp.isoformat(), card))
            self.db.execute('INSERT INTO clusters VALUES(?, ?)', (cluster_id, 'worth reading'))
            self.db.execute('INSERT INTO highlight_cluster_decisions VALUES(?,?,?,?,?,?,?)',
                            (cluster_id, None, '{"max_flag_score10": 8}', 10, 'featured', '{}', None))
        self.db.commit()

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        params = params or {}
        if sql.startswith('SET LOCAL'):
            return self.db.execute('SELECT 1 WHERE 0')
        if 'highlights_read_model_versions v' in sql:
            if params.get('version_id', VERSION) != VERSION:
                return self.db.execute('SELECT 1 WHERE 0')
            return self.db.execute('SELECT ? AS version_id, ? AS scope_key, 45 AS total_count',
                                   (VERSION, params['scope_key']))
        if 'GROUP BY day' in sql:
            if 'FROM main.highlights_scope_items' in sql:
                sql = "SELECT COALESCE(date(h.sort_at, '+8 hours'), 'unknown') AS day, count(*) AS n " + sql[sql.index('FROM main.highlights_scope_items'):]
            else:
                sql = "SELECT COALESCE(date(COALESCE(c.first_doc_at, c.last_doc_at, c.last_updated_at), '+8 hours'), 'unknown') AS day, count(*) AS n " + sql[sql.index('FROM main.clusters'):]
        sql = re.sub(r'LEFT JOIN LATERAL \(.*?\) event_cover ON true', 'LEFT JOIN (SELECT NULL AS cover_url) event_cover ON true', sql, flags=re.S)
        sql = sql.replace("now() - interval '30 days'", "datetime('now', '-30 days')")
        sql = re.sub(r'::(?:text\[\]|uuid|timestamptz|integer|float|int|text)', '', sql)
        sql = re.sub(r'%\((\w+)\)s', r':\1', sql)
        sql = re.sub(r'= ANY\((:\w+)\)', r'IN (SELECT value FROM json_each(\1))', sql)
        sql = re.sub(r'OFFSET\s+([^\n]+)\s+LIMIT\s+(:\w+)', r'LIMIT \2 OFFSET \1', sql)
        params = {key: value.isoformat() if isinstance(value, datetime) else value for key, value in params.items()}
        params = {key: json.dumps(value) if isinstance(value, list) else value for key, value in params.items()}
        return self.db.execute(sql, params)

    def commit(self):
        self.db.commit()

    def rollback(self):
        self.db.rollback()


@pytest.fixture()
def read_model(monkeypatch):
    import remote_db
    _isolate_remote_env(monkeypatch, remote_db)
    monkeypatch.setenv('INFO2ACTION_HIGHLIGHTS_READ_MODEL', '1')
    monkeypatch.setenv('INFO2ACTION_HIGHLIGHTS_READ_MODEL_STALE_FALLBACK', '0')
    remote_db.clear_feed_cache_keys()
    conn = SqlConn()

    @contextmanager
    def connect():
        yield conn

    monkeypatch.setattr(remote_db, 'connect', connect)
    monkeypatch.setattr(remote_db, 'remote_schema', lambda: 'main')
    monkeypatch.setattr(remote_db, 'event_read_backend', lambda: 'test')
    for name in ('_read_local_read_cache', '_read_feed_snapshot'):
        monkeypatch.setattr(remote_db, name, lambda *args, **kwargs: None)
    for name in ('_write_feed_snapshot_async', '_write_local_read_cache_async'):
        monkeypatch.setattr(remote_db, name, lambda *args, **kwargs: None)
    yield remote_db, conn
    remote_db.clear_feed_cache_keys()
    conn.db.close()


def read_events(remote_db, **kwargs):
    params = dict(page=1, limit=20, public_only=True, min_github_stars=50, enabled=True)
    params.update(kwargs)
    return remote_db.fetch_events(**params)


def test_seek_read_model_uses_visible_order_and_original_page(read_model):
    remote_db, conn = read_model
    ordinary = read_events(remote_db)
    seek = read_events(remote_db, target_date='2026-09-03')
    assert seek.get('date_seek') == {'requested_date': '2026-09-03', 'status': 'found', 'anchor_event_id': 124}
    assert [event['id'] for event in seek['events']] == list(range(120, 140))
    assert seek['date_counts'] == ordinary['date_counts'] == {'2026-09-03': 21, '2026-09-04': 24}
    assert seek['next_cursor']['rank_after'] == 40
    last = read_events(remote_db, cursor=seek['next_cursor'])
    assert [event['id'] for event in seek['events'] + last['events']] == list(range(120, 145))
    assert 'date_seek' not in ordinary


def test_seek_rechecks_same_version_after_filter_change_and_expired_cursor(read_model):
    remote_db, conn = read_model
    first = read_events(remote_db, target_date='2026-09-03')
    assert first['date_seek']['anchor_event_id'] == 124
    conn.db.execute("UPDATE highlight_cluster_decisions SET manual_display='force_hide' WHERE cluster_id=124")
    conn.db.execute('DELETE FROM highlights_scope_items WHERE cluster_id < 110')
    conn.db.commit()
    second = read_events(remote_db, target_date='2026-09-03', cursor={
        'version_id': '00000000-0000-0000-0000-00000000dead', 'scope_key': 'all', 'rank_after': 999,
    })
    assert second['date_seek']['anchor_event_id'] == 125
    assert second['read_model_version_id'] == VERSION
    assert second['events'][0]['id'] == 110
    assert second['next_cursor']['rank_after'] == 20
    assert 124 not in [event['id'] for event in second['events']]


def test_seek_missing_date_retains_directory_and_no_unrelated_events(read_model):
    remote_db, _ = read_model
    response = read_events(remote_db, target_date='2026-08-01')
    assert response.get('date_seek') == {'requested_date': '2026-08-01', 'status': 'not_found', 'anchor_event_id': None}
    assert response['events'] == []
    assert response['next_cursor'] is None
    assert response['date_counts'] == {'2026-09-03': 21, '2026-09-04': 24}


def test_seek_rejects_read_model_page_whose_anchor_card_is_invalid(read_model):
    remote_db, conn = read_model
    conn.db.execute("UPDATE highlights_scope_items SET card_json='{}' WHERE cluster_id=124")
    conn.db.commit()
    params = dict(conn=conn, schema='main', page=2, limit=20, cursor=None,
                  since_version_snapshot=None, fetched_since=None, user_id=None,
                  public_only=True, min_github_stars=50, enabled=True,
                  categories=None, timezone_offset_minutes=-480)
    ordinary = remote_db._query_highlights_read_model_events(**params)
    assert ordinary is not None
    assert 124 not in [event['id'] for event in ordinary['events']]
    # None asks the existing caller to try the live query instead of lying about
    # a found anchor that the client cannot locate in the returned events.
    assert remote_db._query_highlights_read_model_events(**params, target_date='2026-09-03') is None


def test_seek_never_reads_or_writes_first_page_snapshots(read_model, monkeypatch):
    remote_db, _ = read_model
    for name in ('_read_local_read_cache', '_read_feed_snapshot', '_write_feed_snapshot_async', '_write_local_read_cache_async'):
        monkeypatch.setattr(remote_db, name, lambda *args, **kwargs: pytest.fail('date seek touched ordinary snapshot'))
    response = read_events(remote_db, target_date='2026-09-03')
    assert response['date_seek']['anchor_event_id'] == 124


def test_seek_applies_read_status_overlay(read_model):
    remote_db, conn = read_model
    conn.db.execute('CREATE TABLE cluster_status(cluster_id INT, user_id TEXT, last_seen_version INT)')
    conn.db.execute('INSERT INTO cluster_status VALUES(124, ?, 1)', ('reader',))
    conn.db.commit()
    response = read_events(remote_db, target_date='2026-09-03', user_id='reader', public_only=False)
    anchor = next(event for event in response['events'] if event['id'] == 124)
    assert anchor['has_update'] is True
    assert anchor['last_seen_version'] == 1


def test_seek_read_model_preserves_category_scope_and_quality_gate(read_model, monkeypatch):
    remote_db, conn = read_model
    monkeypatch.setenv('INFO2ACTION_HIGHLIGHTS_DISPLAY_THRESHOLD', '7')
    conn.db.execute("INSERT INTO highlights_scope_items SELECT version_id, 'category:coding', rank, cluster_id, sort_at, card_json FROM highlights_scope_items WHERE cluster_id % 2 = 0")
    conn.db.execute("UPDATE highlight_cluster_decisions SET score_inputs='{\"max_flag_score10\": 6}' WHERE cluster_id=124")
    conn.db.execute('UPDATE clusters SET why_read=NULL WHERE id=126')
    conn.db.commit()
    response = read_events(remote_db, target_date='2026-09-03', categories=['coding'])
    assert response['scope_key'] == 'category:coding'
    assert response['date_seek']['anchor_event_id'] == 128
    assert all(event['id'] % 2 == 0 for event in response['events'])
    assert {124, 126}.isdisjoint(event['id'] for event in response['events'])


@pytest.fixture()
def live_events(read_model, monkeypatch):
    remote_db, conn = read_model
    monkeypatch.setenv('INFO2ACTION_HIGHLIGHTS_READ_MODEL', '0')
    monkeypatch.setenv('INFO2ACTION_HIGHLIGHTS_DISPLAY_THRESHOLD', '7')
    monkeypatch.setattr(remote_db, '_fetch_event_source_metadata', lambda *args, **kwargs: {})
    for name, kind in [('ai_title', 'TEXT'), ('ai_summary', 'TEXT'), ('doc_count', 'INT'),
                       ('unique_source_count', 'INT'), ('first_doc_at', 'TEXT'), ('last_doc_at', 'TEXT'),
                       ('last_updated_at', 'TEXT'), ('published_at', 'TEXT'), ('cover_url', 'TEXT'),
                       ('platforms_json', 'TEXT'), ('live_version', 'INT'), ('is_visible_in_feed', 'INT'),
                       ('archived', 'INT'), ('merged_into', 'INT')]:
        conn.db.execute(f'ALTER TABLE clusters ADD COLUMN {name} {kind}')
    for name in ('user_id', 'platform', 'ai_category', 'fetched_at'):
        conn.db.execute(f'ALTER TABLE items ADD COLUMN {name} TEXT')
    conn.db.execute('CREATE TABLE cluster_items(cluster_id INT, item_id TEXT)')
    for row in conn.db.execute('SELECT cluster_id, sort_at FROM highlights_scope_items').fetchall():
        cluster_id, stamp = row['cluster_id'], row['sort_at']
        conn.db.execute('''UPDATE clusters SET ai_title = 'live event', ai_summary = 'summary',
          doc_count = 1, unique_source_count = 1, first_doc_at = ?, last_doc_at = ?,
          last_updated_at = datetime('now'), published_at = ?, platforms_json = '[]',
          live_version = 3, is_visible_in_feed = 1, archived = 0 WHERE id = ?''',
                        (stamp, stamp, stamp, cluster_id))
        conn.db.execute('INSERT INTO items(id, platform, ai_category) VALUES(?, ?, ?)',
                        (str(cluster_id), 'twitter', 'coding' if cluster_id % 2 == 0 else 'products'))
        conn.db.execute('INSERT INTO cluster_items VALUES(?, ?)', (cluster_id, str(cluster_id)))
    conn.db.commit()
    return remote_db, conn


def test_seek_live_fallback_preserves_or_filters_and_ordinary_page(live_events):
    remote_db, conn = live_events
    common = {'min_github_stars': 0, 'categories': ['coding', 'products']}
    first = read_events(remote_db, **common)
    seek = read_events(remote_db, target_date='2026-09-03', **common)
    assert seek['date_seek']['anchor_event_id'] == 124
    assert [event['id'] for event in seek['events']] == list(range(120, 140))
    assert seek['date_counts'] == first['date_counts']
    assert seek['next_cursor'] == 3
    last = read_events(remote_db, page=seek['next_cursor'], **common)
    assert [event['id'] for event in seek['events'] + last['events']] == list(range(120, 145))
    missing = read_events(remote_db, target_date='2026-08-01', **common)
    assert missing['date_seek']['status'] == 'not_found'
    assert missing['events'] == []
    assert missing['next_cursor'] is None


def test_seek_live_fallback_applies_public_and_quality_filters(live_events):
    remote_db, conn = live_events
    conn.db.execute("UPDATE items SET user_id='private' WHERE id='124'")
    conn.db.execute("UPDATE highlight_cluster_decisions SET score_inputs='{\"max_flag_score10\": 6}' WHERE cluster_id=125")
    conn.db.commit()
    seek = read_events(remote_db, target_date='2026-09-03', min_github_stars=0, categories=['coding', 'products'])
    assert seek['date_seek']['anchor_event_id'] == 126
    assert {124, 125}.isdisjoint(event['id'] for event in seek['events'])


@pytest.mark.parametrize('backend', ['read_model', 'live_events'])
def test_seek_filters_future_days_before_anchor_and_page(request, monkeypatch, backend):
    remote_db, conn = request.getfixturevalue(backend)

    def query(**overrides):
        if backend == 'live_events':
            return read_events(remote_db, min_github_stars=0, **overrides)
        params = dict(conn=conn, schema='main', page=1, limit=20, cursor=None,
                      since_version_snapshot=None, fetched_since=None, user_id=None,
                      public_only=True, min_github_stars=50, enabled=True,
                      categories=None, timezone_offset_minutes=-480)
        return remote_db._query_highlights_read_model_events(**(params | overrides))

    # Six later articles remain stored but cannot contribute to the visible
    # rank. The first 9/3 article shifts from position 24 to position 18.
    monkeypatch.setattr(remote_db, 'highlights_published_before',
                        lambda: datetime(2026, 9, 4, 10, tzinfo=timezone.utc))
    seek = query(target_date='2026-09-03')
    assert seek is not None
    assert seek['date_seek']['anchor_event_id'] == 124
    assert [event['id'] for event in seek['events']] == list(range(106, 126))
    assert seek['date_counts'] == {'2026-09-03': 21, '2026-09-04': 18}
    assert seek['total_available_within_30d'] == 39
    cursor = seek['next_cursor']
    after = query(**({'cursor': cursor} if isinstance(cursor, dict) else {'page': cursor}))
    assert [event['id'] for event in after['events']] == list(range(126, 145))
    assert after['next_cursor'] is None

    # Once the whole 9/4 issue is in the future, direct date seeking must
    # return not_found, not a plausible anchor outside the returned page.
    monkeypatch.setattr(remote_db, 'highlights_published_before',
                        lambda: datetime(2026, 9, 3, 16, tzinfo=timezone.utc))
    missing = query(target_date='2026-09-04')
    assert missing is not None
    assert missing['date_seek'] == {
        'requested_date': '2026-09-04', 'status': 'not_found', 'anchor_event_id': None,
    }
    assert missing['events'] == [] and missing['next_cursor'] is None
    assert missing['date_counts'] == {'2026-09-03': 21}
