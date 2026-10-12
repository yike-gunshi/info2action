"""Real PostgreSQL quota semantics; optional pgserver supplies a disposable DB.

Run with ``uv run --with pgserver --with pytest --with-requirements requirements.txt
python -m pytest tests/test_ai_search_*.py`` from a copy without production .env.
No external DSN is accepted: every connection uses this fixture's local socket.
"""
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))


@pytest.fixture(scope='module')
def postgres(tmp_path_factory):
    pgserver = pytest.importorskip('pgserver')
    with pgserver.get_server(tmp_path_factory.mktemp('ai-search-postgres'), cleanup_mode='delete') as server:
        yield server.get_uri()


@pytest.fixture
def quota_db(postgres, monkeypatch):
    import psycopg
    from psycopg.rows import dict_row
    from remote_db import ai_search as search

    schema = 'quota_test'
    with psycopg.connect(postgres) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
        conn.execute(f'CREATE SCHEMA {schema}')
        conn.execute(f'CREATE TABLE {schema}.users (id text PRIMARY KEY, role text NOT NULL)')
        conn.execute(f"INSERT INTO {schema}.users VALUES ('admin', 'admin'), ('one', 'user'), ('two', 'user'), ('three', 'user'), ('four', 'user')")
        conn.execute(f'''CREATE TABLE {schema}.ai_search_jobs (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            query text, query_lexical text, ip_hash text, user_id text,
            candidate_ids jsonb, candidate_meta jsonb, input_text text,
            window_days integer, lexical_hits integer, vector_hits integer, vector_ok boolean,
            model text, prompt_version text, request_params jsonb, status text, error text,
            created_at timestamptz DEFAULT '2026-10-10 15:59:59+00'::timestamptz,
            write_queued_at timestamptz)''')

    clock = {'now': '2026-10-10 15:59:59+00'}  # Beijing 23:59:59.
    barrier = {'value': None}

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, params=None):
            # Bind the transaction clock while executing the actual PostgreSQL SQL.
            sql = sql.replace('now()', f"TIMESTAMPTZ '{clock['now']}'")
            if 'pg_advisory_xact_lock' in sql and barrier['value'] is not None:
                barrier['value'].wait(timeout=10)
            return self.conn.execute(sql, params)

    @contextmanager
    def connect():
        with psycopg.connect(postgres, row_factory=dict_row, options='-c timezone=UTC') as conn:
            yield Connection(conn)

    monkeypatch.setattr(search._core, 'connect', connect)
    monkeypatch.setattr(search._core, 'remote_schema', lambda: schema)
    monkeypatch.setattr(search._core, '_runtime_env', lambda: {})

    def seed(user_id, status='done', created_at=None):
        with psycopg.connect(postgres) as conn:
            conn.execute(f'INSERT INTO {schema}.ai_search_jobs (user_id, status, created_at, write_queued_at) VALUES (%s, %s, %s, %s)',
                         (user_id, status, created_at or clock['now'], created_at or clock['now']))

    def create(user_id, *, user_day=2, cap_day=5, exempt=False):
        return search.create_job(query='q', query_lexical='q', ip_hash='shared-ip', user_id=user_id,
                                 candidate_ids=['item'], candidate_meta=[], user_day=user_day,
                                 cap_day=cap_day, quota_exempt=exempt)

    def total():
        with psycopg.connect(postgres) as conn:
            return conn.execute(f'SELECT count(*) FROM {schema}.ai_search_jobs').fetchone()[0]

    return search, clock, barrier, seed, create, total


@pytest.mark.parametrize('status', ['queued', 'running', 'done', 'failed', 'unavailable'])
def test_real_sql_every_task_state_counts_for_user_and_site(quota_db, status):
    search, _, _, seed, _, _ = quota_db
    assert hasattr(search, 'count_user_day') and hasattr(search, 'count_all_day')
    seed('one', status)
    assert search.count_user_day('one') == search.count_all_day() == 1


def test_real_sql_site_excludes_admin_and_legacy_but_personal_matches_owner(quota_db):
    search, _, _, seed, _, _ = quota_db
    assert hasattr(search, 'count_all_day')
    seed('one', 'failed')
    seed('admin', 'done')
    seed(None, 'unavailable')
    assert search.count_all_day() == 1
    assert search.count_user_day('one') == 1
    assert search.count_user_day('two') == 0


def test_real_sql_beijing_midnight_inclusive_and_resets_at_zero(quota_db):
    search, clock, _, seed, _, _ = quota_db
    assert hasattr(search, 'count_user_day')
    seed('one', 'done', '2026-10-09 15:59:59+00')  # Previous Beijing day.
    seed('one', 'failed', '2026-10-09 16:00:00+00')  # Exact today's midnight.
    seed('one', 'unavailable', '2026-10-10 15:59:59+00')
    assert search.count_user_day('one') == search.count_all_day() == 2
    clock['now'] = '2026-10-10 16:00:00+00'
    assert search.count_user_day('one') == search.count_all_day() == 0
    seed('one', 'running', '2026-10-10 16:00:00+00')
    assert search.count_user_day('one') == search.count_all_day() == 1


def test_real_sql_unavailable_jobs_hit_limit_without_placeholder(quota_db):
    search, _, _, seed, create, total = quota_db
    assert hasattr(search, 'QuotaExceeded')
    seed('one', 'failed')
    seed('one', 'unavailable')
    assert search.quota_exhausted('one', 2, 5)
    with pytest.raises(search.QuotaExceeded):
        create('one')
    assert total() == 2


@pytest.mark.parametrize('user_id', ['admin', None])
def test_real_sql_exempt_insert_skips_limits_and_site_count(quota_db, user_id):
    search, _, _, _, create, total = quota_db
    assert hasattr(search, 'count_all_day')
    for _ in range(7):
        create(user_id, user_day=0, cap_day=0, exempt=True)
    assert total() == 7 and search.count_all_day() == 0


@pytest.mark.parametrize('limit_kind,workers,expected', [('personal', 8, 2), ('site', 12, 5)])
def test_real_postgres_advisory_lock_atomically_serializes_competing_inserts(quota_db, limit_kind, workers, expected):
    search, _, barrier, _, create, total = quota_db
    assert hasattr(search, 'QuotaExceeded')
    barrier['value'] = threading.Barrier(workers)

    def submit(index):
        user_id = 'one' if limit_kind == 'personal' else ['one', 'two', 'three', 'four'][index % 4]
        try:
            return create(user_id, user_day=2 if limit_kind == 'personal' else None)
        except search.QuotaExceeded:
            return None

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(submit, range(workers)))
    assert sum(result is not None for result in results) == expected
    assert total() == search.count_all_day() == expected


def test_real_sql_same_ip_does_not_share_personal_allowance(quota_db):
    search, _, _, _, create, total = quota_db
    assert hasattr(search, 'QuotaExceeded')
    create('one')
    create('one')
    create('two')
    create('two')
    assert total() == 4
    assert search.count_user_day('one') == search.count_user_day('two') == 2


def test_real_sql_month_counts_all_tasks_from_beijing_calendar_month_start(quota_db):
    search, clock, _, seed, _, _ = quota_db
    clock['now'] = '2026-10-31 16:00:00+00'  # November starts in Beijing, still October in UTC.
    seed('one', 'done', '2026-10-31 15:59:59+00')
    seed('one', 'queued', '2026-10-31 16:00:00+00')
    seed('admin', 'failed', '2026-10-31 16:00:01+00')
    seed(None, 'unavailable', '2026-10-31 16:00:02+00')
    seed('two', 'running', '2026-10-31 16:00:03+00')
    assert search.count_all_month() == 4
