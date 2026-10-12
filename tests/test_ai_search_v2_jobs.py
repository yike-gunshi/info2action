"""v2 jobs against disposable PostgreSQL; never accept an external DSN."""
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
MIGRATION = ROOT / 'supabase/migrations/2026101100_ai_search_v2.sql'
SCOPE = {'public_only': True, 'manual_owner_user_id': None, 'min_github_stars': 50}
FINISH = dict(candidate_ids=['a', 'b'], candidate_meta=[{'id': 'a', 'score': .9}],
              retrieval={'stage': 'queued', 'rerank_model': 'voyage'},
              lexical_hits=7, vector_hits=11, vector_ok=True, window_days=7,
              query_lexical='"关键词"')


@pytest.fixture(scope='module')
def postgres(tmp_path_factory):
    pgserver = pytest.importorskip('pgserver')
    with pgserver.get_server(tmp_path_factory.mktemp('v2-jobs-postgres'), cleanup_mode='delete') as server:
        yield server.get_uri()


@pytest.fixture
def old_db(postgres, monkeypatch):
    import psycopg
    from psycopg.rows import dict_row
    from remote_db import ai_search as search

    # Execute the MVP CREATE TABLE verbatim, including its unnamed status CHECK.
    mvp = (ROOT / 'supabase/migrations/202609191900_ai_search.sql').read_text()
    table_sql = mvp[mvp.index('CREATE TABLE IF NOT EXISTS remote_poc.ai_search_jobs'):].split(';')[0]
    with psycopg.connect(postgres) as conn:
        conn.execute('DROP SCHEMA IF EXISTS remote_poc CASCADE')
        conn.execute('CREATE SCHEMA remote_poc')
        conn.execute("CREATE TABLE remote_poc.users (id text PRIMARY KEY, role text NOT NULL)")
        conn.execute("INSERT INTO remote_poc.users VALUES ('one', 'user'), ('two', 'user'), ('admin', 'admin')")
        conn.execute(table_sql)
        # Identity was added after the original MVP migration.
        conn.execute('ALTER TABLE remote_poc.ai_search_jobs ADD COLUMN user_id text')
        conn.execute("""ALTER TABLE remote_poc.ai_search_jobs ALTER COLUMN created_at
                         SET DEFAULT '2026-10-10 15:59:59+00'::timestamptz""")

    clock = {'now': '2026-10-10 15:59:59+00'}
    barrier = {'value': None}
    calls = []

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, params=None):
            calls.append((sql, params))
            if 'pg_advisory_xact_lock' in sql and barrier['value'] is not None:
                barrier['value'].wait(timeout=15)
            return self.conn.execute(sql.replace('now()', f"TIMESTAMPTZ '{clock['now']}'"), params)

    @contextmanager
    def connect():
        with psycopg.connect(postgres, row_factory=dict_row, options='-c timezone=UTC') as conn:
            yield Connection(conn)

    monkeypatch.setattr(search._core, 'connect', connect)
    monkeypatch.setattr(search._core, 'remote_schema', lambda: 'remote_poc')
    monkeypatch.setattr(search._core, '_runtime_env', lambda: {})
    # Prevent both success and failure cache entries from crossing fixture DBs.
    monkeypatch.setattr(search, '_schema_ready_cache', {}, raising=False)

    def sql(statement, params=None):
        with psycopg.connect(postgres, row_factory=dict_row) as conn:
            cursor = conn.execute(statement, params)
            return cursor.fetchall() if cursor.description else []

    def migrate():
        assert MIGRATION.is_file(), 'the v2 migration must exist'
        with psycopg.connect(postgres, autocommit=True) as conn:
            conn.execute(MIGRATION.read_text())

    def seed(user_id='one', *, status='retrieving', created_at=None, write_queued_at=None,
             retrieval=None, started_at=None, job_id=None):
        return str(sql('''INSERT INTO remote_poc.ai_search_jobs
            (id, query, query_lexical, ip_hash, user_id, status, created_at,
             write_queued_at, retrieval, started_at)
            VALUES (COALESCE(%s::uuid, gen_random_uuid()), 'q', 'q', 'ip', %s, %s,
                    %s, %s, %s::jsonb, %s) RETURNING id''',
            (job_id, user_id, status, created_at or clock['now'], write_queued_at,
             json.dumps(retrieval) if retrieval is not None else None, started_at))[0]['id'])

    def create(user_id='one', **kwargs):
        return search.create_retrieval_job(query='原问题', ip_hash='same-ip', user_id=user_id,
                                          scope=SCOPE, **({'user_day': 2, 'cap_day': 5,
                                                           'retrieval_user_day': 10} | kwargs))

    return SimpleNamespace(search=search, sql=sql, migrate=migrate, seed=seed, create=create,
                           clock=clock, barrier=barrier, calls=calls)


@pytest.fixture
def db(old_db):
    old_db.migrate()
    return old_db


def test_migration_old_table_dynamic_constraints_backfill_and_schema_cache(old_db, monkeypatch):
    import psycopg
    db = old_db
    timer = {'now': 1000.}
    monkeypatch.setattr(db.search.time, 'monotonic', lambda: timer['now'])
    assert db.search.schema_ready() is False
    db.sql('ALTER TABLE remote_poc.ai_search_jobs RENAME CONSTRAINT ai_search_jobs_status_check TO arbitrary_mvp_name')
    db.sql("ALTER TABLE remote_poc.ai_search_jobs ADD CONSTRAINT second_status_check CHECK (status <> 'invalid' AND window_days >= 0)")
    db.sql('ALTER TABLE remote_poc.ai_search_jobs ADD CONSTRAINT keep_window_check CHECK (window_days >= 0)')
    old = str(db.sql("""INSERT INTO remote_poc.ai_search_jobs (query, query_lexical, ip_hash, user_id)
                         VALUES ('old', 'old', 'ip', 'one') RETURNING id""")[0]['id'])
    db.migrate()
    assert db.search.schema_ready() is False  # Negative results are cached too.
    timer['now'] += 29
    assert db.search.schema_ready() is False
    timer['now'] += 1
    assert db.search.schema_ready() is True
    before = len(db.calls)
    assert db.search.schema_ready() is True
    assert len(db.calls) == before
    job = db.search.get_job(old)
    assert job['write_queued_at'] == job['created_at']
    constraints = {r['conname'] for r in db.sql("""SELECT conname FROM pg_constraint
                      WHERE conrelid='remote_poc.ai_search_jobs'::regclass AND contype='c'""")}
    assert constraints == {'ai_search_jobs_status_check', 'keep_window_check'}
    jid = db.create()
    assert db.search.get_job(jid)['query_lexical'] is None
    with pytest.raises(psycopg.errors.CheckViolation):
        db.sql("""INSERT INTO remote_poc.ai_search_jobs (query, ip_hash, status)
                  VALUES ('invalid', 'ip', 'invalid')""")
    assert db.search.get_job(jid)['status'] == 'retrieving'


@pytest.mark.parametrize('ready,ttl', [(False, 30), (True, 600)])
def test_schema_cache_ttl_depends_on_successful_result(monkeypatch, ready, ttl):
    from remote_db import ai_search as search
    timer, calls = [1000.], []
    class Connection:
        def execute(self, sql, params=None):
            calls.append(sql)
            return self
        def fetchone(self):
            return {'available': ready}
    @contextmanager
    def connect():
        yield Connection()
    monkeypatch.setattr(search._core, 'connect', connect)
    monkeypatch.setattr(search._core, 'remote_schema', lambda: 'ttl-test')
    monkeypatch.setattr(search, '_schema_ready_cache', {})
    monkeypatch.setattr(search, 'time', SimpleNamespace(monotonic=lambda: timer[0]))
    assert search.schema_ready() is ready
    before = len(calls)
    timer[0] += ttl - 1
    assert search.schema_ready() is ready and len(calls) == before
    timer[0] += 1
    assert search.schema_ready() is ready and len(calls) > before


def test_schema_query_exception_does_not_cache_false(monkeypatch):
    from remote_db import ai_search as search
    @contextmanager
    def connect():
        raise RuntimeError('temporary checkout error')
        yield
    monkeypatch.setattr(search._core, 'connect', connect)
    monkeypatch.setattr(search._core, 'remote_schema', lambda: 'exception-test')
    monkeypatch.setattr(search, '_schema_ready_cache', {})
    with pytest.raises(RuntimeError, match='temporary checkout error'):
        search.schema_ready()
    assert search._schema_ready_cache == {}


def test_migration_header_matches_session_port_and_sql_is_transaction_local():
    text = MIGRATION.read_text()
    header, sql = text.split('BEGIN;', 1)
    assert 'SUPABASE_DB_DIRECT_URL' in header and 'SUPABASE_DB_URL' in header
    assert '5432' in header and '6543' in header
    assert not re.search(r'^\s*SET\s+(?!LOCAL\b)', sql, re.I | re.M)


@pytest.mark.parametrize('missing', ['column', 'status', 'table'])
def test_schema_ready_requires_column_and_retrieving_constraint(db, missing):
    if missing == 'column':
        db.sql('ALTER TABLE remote_poc.ai_search_jobs DROP COLUMN write_queued_at')
    elif missing == 'status':
        db.sql('ALTER TABLE remote_poc.ai_search_jobs DROP CONSTRAINT ai_search_jobs_status_check')
        db.sql("ALTER TABLE remote_poc.ai_search_jobs ADD CHECK (status IN ('queued', 'done'))")
    else:
        db.sql('DROP TABLE remote_poc.ai_search_jobs')
    assert db.search.schema_ready() is False


def test_create_initial_fields_scope_and_same_ip_independent_users(db):
    jid = db.create()
    job = db.search.get_job(jid)
    assert job['status'] == 'retrieving' and job['write_queued_at'] is None
    assert job['candidate_ids'] == [] and job['candidate_meta'] == []
    assert job['retrieval'] == {'scope': SCOPE, 'stage': 'queued'}
    assert job['query'] == '原问题' and job['user_id'] == 'one'
    assert db.create('two') != jid
    assert db.search.count_user_retrieval_day('one') == 1
    assert db.search.count_user_day('one') == db.search.count_all_day() == 0


@pytest.mark.parametrize('reason', ['personal', 'site', 'retrieval', 'in_progress'])
def test_create_checks_limits_before_in_progress_without_inserting(db, reason):
    if reason == 'personal':
        for _ in range(2):
            db.seed(status='failed', write_queued_at=db.clock['now'])
    elif reason == 'site':
        for _ in range(5):
            db.seed('two', status='done', write_queued_at=db.clock['now'])
    elif reason == 'retrieval':
        for _ in range(9):
            db.seed(status='insufficient')
    db.seed()  # All quota cases also have an in-progress job; quota wins.
    before = db.sql('SELECT count(*) AS n FROM remote_poc.ai_search_jobs')[0]['n']
    expected = db.search.RetrievalInProgress if reason == 'in_progress' else db.search.QuotaExceeded
    with pytest.raises(expected):
        db.create()
    assert db.sql('SELECT count(*) AS n FROM remote_poc.ai_search_jobs')[0]['n'] == before


@pytest.mark.parametrize('user_id', ['admin', None])
def test_exempt_create_and_finish_skip_all_three_limits(db, user_id):
    for _ in range(11):
        jid = db.create(user_id, user_day=0, cap_day=0, retrieval_user_day=0, quota_exempt=True)
    assert db.search.count_all_day() == 0
    assert db.search.finish_retrieval(jid, 'ok', **FINISH, user_day=0, cap_day=0,
                                      quota_exempt=True) == 'queued'
    assert db.search.get_job(jid)['write_queued_at'] is not None
    assert db.search.count_all_day() == 0


def test_competing_submissions_only_create_one_retrieval(db):
    db.barrier['value'] = threading.Barrier(8)

    def submit(_):
        try:
            return db.create()
        except db.search.RetrievalInProgress:
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(submit, range(8)))
    db.barrier['value'] = None
    assert sum(jid is not None for jid in results) == 1
    assert db.search.count_user_retrieval_day('one') == 1


def test_claim_fifo_ties_only_started_retrievals_count_and_independent_writing(db):
    db.seed('two', status='running', started_at=db.clock['now'])
    ids = [db.seed(job_id=str(UUID(int=n)), retrieval={'scope': SCOPE, 'stage': 'queued'}) for n in (3, 1, 2)]
    assert db.search.claim_next_retrieving(0) is None
    first = db.search.claim_next_retrieving(2)
    second = db.search.claim_next_retrieving(2)
    assert [first['id'], second['id']] == sorted(ids)[:2]
    assert first['query'] == 'q' and first['user_id'] == 'one'
    assert first['retrieval']['scope'] == SCOPE and first['retrieval']['stage'] == 'understanding'
    assert first['retrieval']['started_at'] is not None
    assert db.search.get_job(first['id'])['started_at'] is None
    assert db.search.claim_next_retrieving(2) is None
    db.search.fail_job(first['id'], 'retrieval_failed')
    assert db.search.claim_next_retrieving(2)['id'] == sorted(ids)[2]
    assert db.search.claim_next_retrieving(10) is None


def test_competing_claims_respect_retrieval_concurrency(db):
    ids = [db.seed(job_id=str(UUID(int=n))) for n in range(1, 13)]
    db.barrier['value'] = threading.Barrier(12)
    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(lambda _: db.search.claim_next_retrieving(2), range(12)))
    db.barrier['value'] = None
    claimed = [job['id'] for job in results if job is not None]
    assert len(claimed) == len(set(claimed)) == 2
    assert set(claimed) == set(ids[:2])


@pytest.mark.parametrize('status', ['retrieving', 'insufficient', 'queued', 'running', 'done', 'failed', 'unavailable'])
def test_progress_merges_only_retrieving_jobs(db, status):
    jid = db.seed(status=status, retrieval={'scope': SCOPE, 'stage': 'understanding'})
    before = db.search.get_job(jid)
    db.search.update_retrieval_progress(jid, {'stage': 'searching', 'keywords': ['关\x00键词']})
    job = db.search.get_job(jid)
    if status == 'retrieving':
        assert job['retrieval'] == {'scope': SCOPE, 'stage': 'searching', 'keywords': ['关键词']}
    else:
        assert job == before


@pytest.mark.parametrize('outcome,expected', [('insufficient', 'insufficient'), ('ok', 'queued'), ('exhausted', 'failed')])
def test_finish_persists_all_fields_and_three_outcomes(db, outcome, expected):
    jid = db.create()
    if outcome == 'exhausted':
        for _ in range(5):
            db.seed('two', status='failed', write_queued_at=db.clock['now'])
    result = db.search.finish_retrieval(jid, 'ok' if outcome == 'exhausted' else outcome,
                                        **FINISH, user_day=2, cap_day=5)
    assert result == expected
    job = db.search.get_job(jid)
    for key in ('candidate_ids', 'candidate_meta', 'lexical_hits', 'vector_hits', 'vector_ok', 'window_days', 'query_lexical'):
        assert job[key] == FINISH[key]
    assert job['retrieval'] == {'scope': SCOPE, **FINISH['retrieval']}
    assert (job['write_queued_at'] is not None) == (expected == 'queued')
    assert (job['finished_at'] is not None) == (expected != 'queued')
    assert job['error'] == ('quota_exhausted' if expected == 'failed' else None)
    assert db.search.count_user_day('one') == (1 if expected == 'queued' else 0)
    assert db.search.count_user_retrieval_day('one') == 1
    # Late finish and progress cannot change any fields, including quota usage.
    assert db.search.finish_retrieval(jid, 'ok', **{**FINISH, 'candidate_ids': ['late']}) == expected
    db.search.update_retrieval_progress(jid, {'stage': 'late'})
    if expected != 'queued':
        db.search.fail_job(jid, 'retrieval_timeout')
    assert db.search.get_job(jid) == job


def test_finish_insufficient_empty_or_five_candidates_does_not_check_quota(db):
    for candidates in ([], ['a', 'b', 'c', 'd', 'e']):
        jid = db.create()
        assert db.search.finish_retrieval(jid, 'insufficient', **{**FINISH, 'candidate_ids': candidates},
                                          user_day=0, cap_day=0) == 'insufficient'
        assert db.search.get_job(jid)['candidate_ids'] == candidates
    assert db.search.count_user_day('one') == 0


def test_finish_rechecks_personal_quota(db):
    jid = db.create()
    for _ in range(2):
        db.seed(status='unavailable', write_queued_at=db.clock['now'])
    assert db.search.finish_retrieval(jid, 'ok', **FINISH, user_day=2, cap_day=100) == 'failed'
    assert db.search.get_job(jid)['error'] == 'quota_exhausted'


def test_competing_finishes_take_last_site_slot_once(db):
    ids = [db.create(user) for user in ('one', 'two')]
    for _ in range(4):
        db.seed('other', status='done', write_queued_at=db.clock['now'])
    db.barrier['value'] = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda jid: db.search.finish_retrieval(jid, 'ok', **FINISH,
                                                                           user_day=2, cap_day=5), ids))
    db.barrier['value'] = None
    assert sorted(outcomes) == ['failed', 'queued']
    assert db.search.count_all_day() == 5
    assert sum(db.search.get_job(jid)['write_queued_at'] is not None for jid in ids) == 1


@pytest.mark.parametrize('error', ['retrieval_failed', 'retrieval_timeout'])
def test_retrieval_failures_are_finished_without_writing_charge(db, error):
    jid = db.create()
    db.search.fail_job(jid, error)
    job = db.search.get_job(jid)
    assert job['status'] == 'failed' and job['error'] == error and job['finished_at'] is not None
    assert job['write_queued_at'] is None
    assert db.search.count_user_day('one') == 0 and db.search.count_user_retrieval_day('one') == 1


def test_v1_counts_writing_and_retrieval_attempts_even_after_failure(db):
    jid = db.search.create_job(query='v1', query_lexical='v1', ip_hash='same-ip', user_id='one',
                               candidate_ids=['a'], candidate_meta=[], user_day=2, cap_day=5)
    assert db.search.get_job(jid)['write_queued_at'] is not None
    db.search.fail_job(jid, 'parse_error')
    assert db.search.count_user_day('one') == db.search.count_all_day() == 1
    assert db.search.count_user_retrieval_day('one') == 1
    assert db.search.quota_exhausted('one', 1, 5) is True
    with pytest.raises(db.search.QuotaExceeded):
        db.search.create_job(query='v1', query_lexical='v1', ip_hash='ip', user_id='one',
                             candidate_ids=[], candidate_meta=[], user_day=1)


@pytest.mark.parametrize('status', ['queued', 'running', 'done', 'failed', 'unavailable'])
def test_every_postwriting_state_counts_by_write_time_not_creation(db, status):
    db.seed(status=status, created_at='2026-10-09 15:59:59+00', write_queued_at='2026-10-09 16:00:00+00')
    assert db.search.count_user_day('one') == db.search.count_all_day() == 1
    assert db.search.count_user_retrieval_day('one') == 0


def test_daily_and_monthly_counts_owner_admin_legacy_and_beijing_midnight(db):
    db.seed(status='done', created_at='2026-10-09 15:59:59+00', write_queued_at='2026-10-09 16:00:00+00')
    db.seed(status='done', created_at='2026-10-09 16:00:00+00', write_queued_at='2026-10-09 15:59:59+00')
    db.seed(status='failed', write_queued_at='2026-10-10 15:59:59+00')
    db.seed('admin', status='done', write_queued_at=db.clock['now'])
    db.seed(None, status='unavailable', write_queued_at=db.clock['now'])
    assert db.search.count_user_day('one') == db.search.count_all_day() == 2
    assert db.search.count_user_day('two') == 0 and db.search.count_user_day('admin') == 1
    assert db.search.count_user_retrieval_day('one') == 2
    db.clock['now'] = '2026-10-10 16:00:00+00'
    assert db.search.count_user_day('one') == db.search.count_all_day() == 0
    assert db.search.count_user_retrieval_day('one') == 0
    db.seed(status='failed', write_queued_at=db.clock['now'])
    assert db.search.count_user_day('one') == db.search.count_user_retrieval_day('one') == 1
    db.clock['now'] = '2026-10-31 16:00:00+00'
    db.seed(status='done', write_queued_at='2026-10-31 15:59:59+00')
    for user in ('one', 'admin', None):
        db.seed(user, status='failed', write_queued_at=db.clock['now'])
    db.seed(status='insufficient')
    assert db.search.count_all_month() == 3


def test_recover_retrieval_timeout_created_time_strict_boundary_and_timeout_wins(db):
    old = db.seed(created_at='2026-10-10 15:54:58+00', retrieval={'started_at': '2026-10-10 15:59:00+00'})
    waiting = db.seed(created_at='2026-10-10 15:54:58+00')
    exact = db.seed(created_at='2026-10-10 15:54:59+00')
    recent = db.seed(created_at='2026-10-10 15:55:00+00')
    db.search.recover_stale(requeue_before='2026-10-10 15:59:30+00')
    for jid in (old, waiting):
        job = db.search.get_job(jid)
        assert job['status'] == 'failed' and job['error'] == 'retrieval_timeout'
        assert job['finished_at'] is not None and job['write_queued_at'] is None
        assert db.search.finish_retrieval(jid, 'ok', **FINISH) == 'failed'
    assert db.search.get_job(exact)['status'] == db.search.get_job(recent)['status'] == 'retrieving'


def test_recover_prestartup_retrieval_only_preserves_fields_and_running_rules(db):
    cutoff = '2026-10-10 15:59:30+00'
    old = db.seed(created_at='2026-10-10 15:58:00+00',
                  retrieval={'scope': SCOPE, 'stage': 'searching', 'keywords': ['a'],
                             'started_at': '2026-10-10 15:59:29+00'})
    exact = db.seed(retrieval={'stage': 'understanding', 'started_at': cutoff})
    recent = db.seed(retrieval={'started_at': '2026-10-10 15:59:31+00'})
    queued = db.seed(retrieval={'stage': 'queued'})
    running_old = db.seed(status='running', started_at='2026-10-10 15:59:29+00')
    running_recent = db.seed(status='running', started_at='2026-10-10 15:59:31+00')
    running_timeout = db.seed(status='running', started_at='2026-10-10 15:13:00+00')
    db.search.recover_stale()  # Periodic recovery does not reset active retrieval.
    assert db.search.get_job(old)['retrieval']['stage'] == 'searching'
    for _ in range(2):
        db.search.recover_stale(requeue_before=cutoff)
        assert db.search.get_job(old)['retrieval'] == {'scope': SCOPE, 'stage': 'queued', 'keywords': ['a']}
        assert db.search.get_job(old)['status'] == 'retrieving'
        assert db.search.get_job(exact)['retrieval']['started_at'] == cutoff
        assert db.search.get_job(recent)['retrieval']['started_at'] == '2026-10-10 15:59:31+00'
        assert db.search.get_job(queued)['retrieval'] == {'stage': 'queued'}
        assert db.search.get_job(running_old)['status'] == 'queued'
        assert db.search.get_job(running_recent)['status'] == 'running'
        assert db.search.get_job(running_timeout)['error'] == 'hard_timeout'
    assert db.search.claim_next_retrieving(3)['id'] == old


def test_light_job_projection_never_selects_input_text(db):
    jid = db.create()
    db.sql('UPDATE remote_poc.ai_search_jobs SET input_text=%s WHERE id=%s', ('x' * 1_500_000, jid))
    full = db.search.get_job(jid)
    db.calls.clear()
    light = db.search.get_job(jid, include_input=False)
    assert 'input_text' not in light
    assert light == {key: value for key, value in full.items() if key != 'input_text'}
    select = next(sql for sql, _ in db.calls if sql.startswith('SELECT'))
    assert '*' not in select and 'input_text' not in select
    assert db.search.get_job(str(UUID(int=999)), include_input=False) is None
    before = len(db.calls)
    assert db.search.get_job('bad', include_input=False) is None
    assert len(db.calls) == before


def test_light_projection_without_postgres(monkeypatch):
    from remote_db import ai_search as search
    statements = []

    class Connection:
        def execute(self, sql, params=None):
            statements.append(sql)
            return self

        def fetchone(self):
            return {'id': str(UUID(int=1)), 'retrieval': {'stage': 'searching'}}

    @contextmanager
    def connect():
        yield Connection()

    monkeypatch.setattr(search._core, 'connect', connect)
    monkeypatch.setattr(search._core, 'remote_schema', lambda: 'test_search')
    job = search.get_job(str(UUID(int=1)), include_input=False)
    assert job['retrieval']['stage'] == 'searching'
    select = next(sql for sql in statements if sql.startswith('SELECT'))
    assert '*' not in select and 'input_text' not in select
    assert 'retrieval' in select and 'write_queued_at' in select


def test_new_sql_settings_are_transaction_local(db):
    jid = db.create()
    db.search.claim_next_retrieving(2)
    db.search.update_retrieval_progress(jid, {'keywords': ['a']})
    db.search.finish_retrieval(jid, 'ok', **FINISH)
    db.search.schema_ready()
    db.search.recover_stale(requeue_before=db.clock['now'])
    assert not any(re.match(r'\s*SET\s+(?!LOCAL\b)', sql, re.I) for sql, _ in db.calls)
    assert not re.search(r'^\s*SET\s+(?!LOCAL\b)', MIGRATION.read_text(), re.I | re.M)


@pytest.mark.parametrize('reason', ['quota', 'in_progress', 'database'])
def test_create_application_errors_survive_remote_db_wrapper(db, monkeypatch, reason):
    connect = db.search._core.connect

    @contextmanager
    def wrapped_connect():
        try:
            with connect() as conn:
                if reason == 'database':
                    raise RuntimeError('database unavailable')
                yield conn
        except Exception as exc:
            raise db.search._core._wrap_remote_db_exception(exc) from exc

    db.seed()
    monkeypatch.setattr(db.search._core, 'connect', wrapped_connect)
    expected = {'quota': db.search.QuotaExceeded, 'in_progress': db.search.RetrievalInProgress,
                'database': db.search._core.RemoteDBError}[reason]
    with pytest.raises(expected):
        db.create(user_day=0 if reason == 'quota' else 2)
