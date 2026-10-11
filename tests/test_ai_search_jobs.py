import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture
def db(monkeypatch):
    from remote_db import ai_search as search

    class Connection:
        def __init__(self):
            self.calls = []
            self.checkouts = 0
            self.row = {"id": "job-1", "n": 3}

        def execute(self, sql, params=None):
            self.calls.append((" ".join(sql.split()), params))
            return self

        def fetchone(self):
            return self.row

        def fetchall(self):
            return [self.row] if self.row else []

    conn = Connection()

    @contextmanager
    def connect():
        conn.checkouts += 1
        yield conn

    monkeypatch.setattr(search._core, "connect", connect)
    monkeypatch.setattr(search._core, "remote_schema", lambda: "test_search")
    monkeypatch.setattr(search._core, "_runtime_env", lambda: {})
    return search, conn


@pytest.mark.parametrize(("usage", "expected"), [
    ({}, 0.),
    ({"prompt_tokens": 10000, "completion_tokens": 1000}, 9.3),
    ({"prompt_tokens": 10000, "prompt_tokens_details": {"cached_tokens": 2000}, "completion_tokens": 1000}, 9.64),
])
def test_points_estimate(usage, expected):
    from remote_db.ai_search import points_estimate

    assert points_estimate(usage) == pytest.approx(expected)


@pytest.mark.parametrize("row", [{"id": "job-1"}, None])
def test_claim_job_atomic_condition_and_serialization(db, row):
    search, conn = db
    conn.row = row
    assert search.claim_job("job-1", 2) is bool(row)
    sql, params = next((s, p) for s, p in conn.calls if s.startswith("UPDATE"))
    assert "status='queued'" in sql and "status='running'" in sql and "RETURNING id" in sql
    assert "(SELECT count(*) FROM test_search.ai_search_jobs WHERE status='running') < %s" in sql
    assert params == ("job-1", 2)
    lock_idx = next(i for i, (s, _) in enumerate(conn.calls) if "pg_advisory_xact_lock" in s or s.startswith("LOCK TABLE"))
    assert lock_idx < conn.calls.index((sql, params))


def test_claim_next_queued_atomic_fifo(db):
    search, conn = db
    assert search.claim_next_queued(2) == "job-1"
    sql = next(s for s, _ in conn.calls if "UPDATE" in s)
    assert "ORDER BY created_at" in sql and "FOR UPDATE SKIP LOCKED" in sql
    assert "status='queued'" in sql and "count(*)" in sql and "RETURNING id" in sql
    conn.row = None
    assert search.claim_next_queued(2) is None


@pytest.mark.parametrize("method,args", [("count_user_day", ("user'1",)), ("count_all_day", ())])
def test_daily_quota_counts_all_statuses_since_beijing_midnight(db, method, args):
    search, conn = db
    assert hasattr(search, method)
    assert getattr(search, method)(*args) == 3
    sql, params = next((s, p) for s, p in conn.calls if "count(*)" in s)
    assert "status" not in sql and "ip_hash" not in sql
    assert "created_at >= (date_trunc('day', now() AT TIME ZONE 'Asia/Shanghai')) AT TIME ZONE 'Asia/Shanghai'" in sql
    assert "created_at <= now()" not in sql
    if args:
        assert "user_id = %s" in sql and params == args and "user'1" not in sql
    else:
        assert "user_id IS NOT NULL" in sql
        assert "user_id NOT IN (SELECT id FROM test_search.users WHERE role = 'admin')" in sql


def test_month_count_includes_all_statuses_and_ips_from_calendar_month_start(db):
    search, conn = db
    conn.row = {"n": 37}
    assert search.count_all_month() == 37
    assert conn.checkouts == 1
    sql, params = next((s, p) for s, p in conn.calls if "count(*)" in s)
    assert "FROM test_search.ai_search_jobs" in sql
    assert "WHERE created_at >= date_trunc('month', now() AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai'" in sql
    assert "status" not in sql and "ip_hash" not in sql
    assert params in (None, ())
    assert any(s.startswith("SET LOCAL statement_timeout") for s, _ in conn.calls)
    assert not any(s.startswith("SET ") and not s.startswith("SET LOCAL ") for s, _ in conn.calls)


def test_recover_stale_marks_timeouts_then_requeues_only_prestartup_jobs(db):
    from datetime import datetime, timezone
    search, conn = db
    cutoff = datetime(2026, 10, 10, 10, tzinfo=timezone.utc)
    search.recover_stale(45, requeue_before=cutoff)
    updates = [(s, p) for s, p in conn.calls if s.startswith("UPDATE")]
    assert len(updates) == 2
    assert "status='failed'" in updates[0][0] and "hard_timeout" in updates[0][0]
    assert "finished_at=now()" in updates[0][0] and "started_at <" in updates[0][0]
    assert updates[0][1] == (45,)
    assert "status='queued'" in updates[1][0] and "started_at=NULL" in updates[1][0]
    assert "WHERE status='running' AND started_at < %s" in updates[1][0]
    assert updates[1][1] == (cutoff,)


def test_periodic_recovery_does_not_requeue_live_jobs(db):
    search, conn = db
    search.recover_stale()
    assert len([s for s, _ in conn.calls if s.startswith("UPDATE")]) == 1


def test_create_job_parameterizes_all_input_and_json(db):
    search, conn = db
    assert search.create_job(query="q'", query_lexical='"q"', ip_hash="hash", candidate_ids=["a"],
                             candidate_meta=[{"id": "a", "tier": "content"}], input_text="原文",
                             window_days=90, lexical_hits=1, vector_hits=0, vector_ok=False) == "job-1"
    sql, params = next((s, p) for s, p in conn.calls if s.startswith("INSERT"))
    assert "test_search.ai_search_jobs" in sql and "q'" not in sql and "原文" not in sql
    assert params["query"] == "q'" and params["input_text"] == "原文"
    assert json.loads(params["candidate_ids"]) == ["a"]
    assert json.loads(params["candidate_meta"])[0]["tier"] == "content"


@pytest.mark.parametrize("user_id", ["user'1", None])
def test_create_job_persists_optional_user_id(db, user_id):
    search, conn = db
    assert search.create_job(query="q", query_lexical="q", ip_hash="hash", candidate_ids=[],
                             candidate_meta=[], user_id=user_id) == "job-1"
    sql, params = next((s, p) for s, p in conn.calls if s.startswith("INSERT"))
    columns = sql.split("(", 1)[1].split(")", 1)[0]
    assert "user_id" in [column.strip() for column in columns.split(",")]
    assert "%(user_id)s" in sql and params["user_id"] == user_id
    if user_id is not None:
        assert user_id not in sql


def test_create_job_defaults_user_id_to_null(db):
    search, conn = db
    search.create_job(query="q", query_lexical="q", ip_hash="hash", candidate_ids=[],
                      candidate_meta=[])
    params = next(p for sql, p in conn.calls if sql.startswith("INSERT"))
    assert params["user_id"] is None


@pytest.mark.parametrize("limit,expected_limit", [(10, 10), (2, 2), (0, 1), (-3, 1), (51, 50)])
def test_list_user_jobs_selects_light_fields_for_only_one_user_in_newest_order(db, limit, expected_limit):
    search, conn = db
    conn.row = {"id": "job-1", "query": "q", "created_at": "2026-09-21T10:00:00Z", "status": "done"}
    assert search.list_user_jobs("user'1", limit) == [conn.row]
    sql, params = next((s, p) for s, p in conn.calls if s.startswith("SELECT"))
    columns = sql.removeprefix("SELECT ").split(" FROM ")[0]
    assert {column.strip() for column in columns.split(",")} == {"id", "query", "created_at", "status"}
    assert "FROM test_search.ai_search_jobs WHERE user_id = %s" in sql
    assert "ORDER BY created_at DESC" in sql and "LIMIT %s" in sql
    assert "user'1" not in sql and params == ("user'1", expected_limit)
    assert conn.checkouts == 1
    assert any(s.startswith("SET LOCAL statement_timeout") for s, _ in conn.calls)
    assert all(s.startswith(("SELECT ", "SET LOCAL ")) for s, _ in conn.calls)


def test_list_user_jobs_returns_empty_history(db):
    search, conn = db
    conn.row = None
    assert search.list_user_jobs("user-1", 10) == []


@pytest.mark.parametrize("total", [0, 71])
def test_count_user_jobs_includes_all_history_statuses_without_limit(db, total):
    search, conn = db
    conn.row = {"n": total}
    assert search.count_user_jobs("user'1") == total
    sql, params = next((s, p) for s, p in conn.calls if s.startswith("SELECT"))
    assert sql == "SELECT count(*) AS n FROM test_search.ai_search_jobs WHERE user_id = %s"
    assert params == ("user'1",)
    assert conn.checkouts == 1
    assert any(s.startswith("SET LOCAL statement_timeout") for s, _ in conn.calls)
    assert all(s.startswith(("SELECT ", "SET LOCAL ")) for s, _ in conn.calls)


def test_finish_job_stores_full_result_and_only_finishes_running(db):
    search, conn = db
    search.finish_job("job-1", result={"content_md": "原文"}, content_md_final="final", cited_ids=["a"],
                      usage={"prompt_tokens": 1}, elapsed_ms=20, points_estimate=0.1,
                      model="glm-5.3", prompt_version="v1", request_params={"max_tokens": 100})
    sql, params = next((s, p) for s, p in conn.calls if s.startswith("UPDATE"))
    assert "status='done'" in sql and "finished_at=now()" in sql and "AND status='running'" in sql
    assert json.loads(params["result"])["content_md"] == "原文"
    assert params["content_md_final"] == "final" and params["job_id"] == "job-1"


def test_fail_job_stores_raw_bounded_utf8(db):
    search, conn = db
    search.fail_job("job-1", "parse_error", raw="中" * 100000)
    sql, params = next((s, p) for s, p in conn.calls if s.startswith("UPDATE"))
    assert "finished_at=now()" in sql and params["status"] == "failed"
    assert json.loads(params['result'])['raw'] == ('中' * 100000).encode()[:200 * 1024].decode(errors='ignore')
    with pytest.raises(ValueError):
        search.fail_job("job-1", "bad", status="done")


def test_get_list_and_queue_position(db):
    from uuid import UUID
    search, conn = db
    assert search.get_job(str(UUID(int=1)))["id"] == "job-1"
    assert search.queue_position("job-1") == 3
    position_sql = conn.calls[-1][0]
    assert "(older.created_at, older.id) <= (target.created_at, target.id)" in position_sql
    assert search.list_jobs(10, 5, status="failed") == [conn.row]
    sql, params = conn.calls[-1]
    assert "input_text" not in sql and "result" not in sql
    assert "candidate_count" in sql and "ORDER BY created_at DESC" in sql
    assert params == ("failed", 10, 5)
    conn.row = None
    assert search.get_job("missing") is None


def test_create_job_rejects_placeholder_status(db):
    search, conn = db
    with pytest.raises((TypeError, ValueError)):
        search.create_job(query="q", query_lexical="q", ip_hash="hash", candidate_ids=["a"],
                          candidate_meta=[], status="unavailable", error="quota_exhausted")
    assert not any(sql.startswith('INSERT') for sql, _ in conn.calls)


@pytest.mark.parametrize("with_input", [True, False])
def test_finish_job_updates_optional_input_without_erasing_saved_values(db, with_input):
    search, conn = db
    optional = {"input_text": "assembled", "candidate_meta": [{"id": "a"}]} if with_input else {}
    search.finish_job("job-1", result={}, content_md_final="answer", cited_ids=[], usage={},
                      elapsed_ms=1, points_estimate=0, model="glm", prompt_version="v1",
                      request_params={}, **optional)
    sql, params = next((s, p) for s, p in conn.calls if s.startswith("UPDATE"))
    assert "input_text=COALESCE(%(input_text)s, input_text)" in sql
    assert "candidate_meta=COALESCE(%(candidate_meta)s::jsonb, candidate_meta)" in sql
    assert params["input_text"] == ("assembled" if with_input else None)
    assert params["candidate_meta"] == (json.dumps([{"id": "a"}]) if with_input else None)


@pytest.mark.parametrize("status", [None, "failed", ""])
def test_count_jobs_matches_list_filter(db, status):
    search, conn = db
    assert search.count_jobs(status=status) == 3
    sql, params = next((s, p) for s, p in conn.calls if "count(*)" in s)
    assert ("WHERE status = %s" in sql) is bool(status)
    assert params == ((status,) if status else ())


def test_save_job_input_only_updates_running_job(db):
    search, conn = db
    search.save_job_input("job-1", input_text="source text", candidate_meta=[{"id": "a"}],
                          model="glm", prompt_version="v1", request_params={"max_tokens": 123})
    sql, params = next((s, p) for s, p in conn.calls if s.startswith("UPDATE"))
    assert "WHERE id=%(job_id)s AND status='running'" in sql
    assert "status=" not in sql.split("WHERE")[0]
    assert params["input_text"] == "source text" and params["model"] == "glm"
    assert params["prompt_version"] == "v1"
    assert json.loads(params["candidate_meta"]) == [{"id": "a"}]
    assert json.loads(params["request_params"]) == {"max_tokens": 123}


def test_get_candidates_filters_visibility_and_preserves_candidate_order(db, monkeypatch):
    search, conn = db
    monkeypatch.setattr(conn, "fetchall", lambda: [
        {"id": "b", "ai_key_points": '["point"]'}, {"id": "a", "embedding": [1]}])
    items = search.get_candidates(["a", "missing", "b"], public_only=False,
                                  user_id="viewer", manual_owner_user_id="owner", min_github_stars=7)
    assert [i["id"] for i in items] == ["a", "b"]
    assert "embedding" not in items[0] and "ai_key_points" not in items[1]
    queries = [(s, p) for s, p in conn.calls if s.startswith("SELECT")]
    assert len(queries) == 1
    sql, params = queries[0]
    assert "i.id = ANY(%(ids)s)" in sql and "i.visible = 1" in sql
    assert "item_status s" in sql and params["status_user_id"] == "viewer"
    assert params["ids"] == ["a", "missing", "b"]
    where, scope_params = search._base_item_where(public_only=False, manual_owner_user_id="owner", min_github_stars=7)
    assert all(" ".join(clause.split()) in sql for clause in where)
    assert all(params[k] == v for k, v in scope_params.items())
    assert any(s.startswith("SET LOCAL") for s, _ in conn.calls)


def test_get_candidates_full_text_includes_each_source_column_once(db):
    search, conn = db
    conn.row = {"id": "a", "content": "full source", "ai_key_points": '["point"]', "ai_summary": "summary"}
    items = search.get_candidates(["a"], public_only=False, min_github_stars=0, full_text=True)
    sql, _ = next((s, p) for s, p in conn.calls if s.startswith("SELECT"))
    columns = sql.split(" FROM ")[0]
    assert [column.strip() for column in columns.removeprefix("SELECT ").split(",")].count("i.content") == 1
    assert columns.count("i.ai_key_points") == 1
    assert columns.count("i.ai_summary") == 1
    assert "NULL::text AS ai_key_points" not in columns
    assert items[0]["content"] == "full source" and items[0]["ai_summary"] == "summary"
    assert items[0]["ai_key_points"] == ["point"]


def test_get_candidates_empty_does_not_connect(db):
    search, conn = db
    assert search.get_candidates([]) == []
    assert conn.calls == []


@pytest.mark.parametrize("cap,user", [(10, 5), (10, None), (None, 5), (None, None)])
def test_create_counts_after_lock_on_same_connection(db, cap, user):
    search, conn = db
    assert search.create_job(query="q", query_lexical="q", ip_hash="hash", candidate_ids=[],
                             candidate_meta=[], user_id='user-1', cap_day=cap, user_day=user) == "job-1"
    assert conn.checkouts == 1
    lock = next(i for i, (sql, _) in enumerate(conn.calls) if "pg_advisory_xact_lock" in sql)
    insert = next(i for i, (sql, _) in enumerate(conn.calls) if sql.startswith("INSERT"))
    counts = [(i, sql, params) for i, (sql, params) in enumerate(conn.calls) if "count(*)" in sql]
    assert len(counts) == int(cap is not None) + int(user is not None)
    for i, sql, params in counts:
        assert lock < i < insert
        assert "AT TIME ZONE 'Asia/Shanghai'" in sql
        assert "created_at <= now()" not in sql and "status" not in sql
        assert params == (("user-1",) if "user_id = %s" in sql else ())
    assert any("user_id = %s" in sql for _, sql, _ in counts) is (user is not None)
    assert conn.calls[insert][1]["status"] == "queued"


@pytest.mark.parametrize("cap,user", [(None, 3), (3, None), (0, 10), (10, 0)])
def test_create_daily_limit_raises_without_inserting(db, cap, user):
    search, conn = db
    assert hasattr(search, 'QuotaExceeded')
    with pytest.raises(search.QuotaExceeded):
        search.create_job(query="q", query_lexical="q", ip_hash="hash", candidate_ids=[],
                          candidate_meta=[], user_id='user-1', cap_day=cap, user_day=user)
    assert conn.checkouts == 1
    assert not any(sql.startswith("INSERT") for sql, _ in conn.calls)


def test_create_exempt_jobs_skip_both_counts(db):
    search, conn = db
    search.create_job(query="q", query_lexical="q", ip_hash="hash", candidate_ids=[],
                      candidate_meta=[], user_id='admin-1', cap_day=0, user_day=0, quota_exempt=True)
    params = next(p for sql, p in conn.calls if sql.startswith("INSERT"))
    assert params['status'] == 'queued' and params['error'] is None
    assert not any('count(*)' in sql for sql, _ in conn.calls)


@pytest.mark.parametrize('limited', [True, False])
def test_create_preserves_quota_exception_through_core_connection_wrapper(db, monkeypatch, limited):
    search, conn = db
    @contextmanager
    def wrapped_connect():
        try:
            yield conn
        except Exception as exc:
            # Real _core.connect wraps application exceptions after rollback.
            raise search._core._wrap_remote_db_exception(exc) from exc
    monkeypatch.setattr(search._core, 'connect', wrapped_connect)
    assert hasattr(search, 'QuotaExceeded')
    if not limited:
        def fail(*args, **kwargs):
            raise RuntimeError('database unavailable')
        monkeypatch.setattr(conn, 'execute', fail)
    expected = search.QuotaExceeded if limited else search._core.RemoteDBError
    with pytest.raises(expected) as exc:
        search.create_job(query='q', query_lexical='q', ip_hash='hash', candidate_ids=[],
                          candidate_meta=[], user_id='user-1', user_day=3)
    if limited:
        assert not any(sql.startswith('INSERT') for sql, _ in conn.calls)


def test_fail_job_stores_generated_body_and_bounded_raw_in_existing_result_column(db):
    search, conn = db
    result = {'content_md': '原始正文', 'content_md_final': '已处理正文', 'raw': '中' * 100000}
    search.fail_job('job-1', 'finish write failed', result=result)
    sql, params = next((s, p) for s, p in conn.calls if s.startswith('UPDATE'))
    assert json.loads(params['result']) == {**result, 'raw': result['raw'].encode()[:200 * 1024].decode(errors='ignore')}
    assert "status IN ('queued', 'running')" in sql


@pytest.fixture(scope='module')
def jobs_postgres(tmp_path_factory):
    pgserver = pytest.importorskip('pgserver')
    with pgserver.get_server(tmp_path_factory.mktemp('ai-search-jobs-postgres'), cleanup_mode='delete') as server:
        yield server.get_uri()


@pytest.fixture
def real_jobs_db(jobs_postgres, monkeypatch):
    import psycopg
    from psycopg.rows import dict_row
    from remote_db import ai_search as search
    schema = 'jobs_test'
    with psycopg.connect(jobs_postgres) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
        conn.execute(f'CREATE SCHEMA {schema}')
        conn.execute(f'''CREATE TABLE {schema}.ai_search_jobs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            created_at timestamptz NOT NULL DEFAULT now(),
            started_at timestamptz, finished_at timestamptz,
            status text NOT NULL DEFAULT 'queued', error text, result jsonb,
            content_md_final text, cited_ids jsonb, usage jsonb, elapsed_ms integer,
            points_estimate numeric, model text, prompt_version text, request_params jsonb,
            input_text text, candidate_meta jsonb)''')
    barrier = {'value': None}

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, params=None):
            if 'pg_advisory_xact_lock' in sql and barrier['value'] is not None:
                barrier['value'].wait(timeout=10)
            return self.conn.execute(sql, params)

    @contextmanager
    def connect():
        with psycopg.connect(jobs_postgres, row_factory=dict_row, options='-c timezone=UTC') as conn:
            yield Connection(conn)

    monkeypatch.setattr(search._core, 'connect', connect)
    monkeypatch.setattr(search._core, 'remote_schema', lambda: schema)
    monkeypatch.setattr(search._core, '_runtime_env', lambda: {})

    def seed(*, job_id=None, status='queued', created_at=None, started_at=None):
        with psycopg.connect(jobs_postgres) as conn:
            return str(conn.execute(f'''INSERT INTO {schema}.ai_search_jobs
                (id, status, created_at, started_at)
                VALUES (COALESCE(%s::uuid, gen_random_uuid()), %s, COALESCE(%s, now()), %s)
                RETURNING id''', (job_id, status, created_at, started_at)).fetchone()[0])

    def running_count():
        with psycopg.connect(jobs_postgres) as conn:
            return conn.execute(f"SELECT count(*) FROM {schema}.ai_search_jobs WHERE status='running'").fetchone()[0]

    return search, seed, barrier, running_count


def test_real_postgres_concurrent_claims_never_exceed_two_running(real_jobs_db):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    search, seed, barrier, running_count = real_jobs_db
    ids = [seed() for _ in range(12)]
    barrier['value'] = threading.Barrier(12)
    with ThreadPoolExecutor(max_workers=12) as executor:
        claims = list(executor.map(lambda _: search.claim_next_queued(2), range(12)))
    barrier['value'] = None
    claimed = [job_id for job_id in claims if job_id is not None]
    assert len(claimed) == len(set(claimed)) == running_count() == 2
    assert set(claimed).issubset(ids)
    assert search.claim_next_queued(2) is None


def test_real_postgres_late_finish_cannot_revive_hard_timed_out_job(real_jobs_db):
    from datetime import datetime, timedelta, timezone
    search, seed, _, _ = real_jobs_db
    jid = seed(status='running', started_at=datetime.now(timezone.utc) - timedelta(minutes=46))
    search.recover_stale(45)
    search.finish_job(jid, result={'content_md': 'late answer'}, content_md_final='late answer',
                      cited_ids=[], usage={}, elapsed_ms=1, points_estimate=0, model='glm',
                      prompt_version='v1', request_params={})
    job = search.get_job(jid)
    assert job['status'] == 'failed' and job['error'] == 'hard_timeout'
    assert job['content_md_final'] is None and job['result'] is None
    assert job['finished_at'] is not None


def test_real_postgres_startup_recovery_never_requeues_poststartup_claims(real_jobs_db):
    from datetime import datetime, timedelta, timezone
    search, seed, _, running_count = real_jobs_db
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=2)
    old = seed(status='running', started_at=cutoff - timedelta(seconds=1))
    exact = seed(status='running', started_at=cutoff)
    recent = seed()
    assert search.claim_next_queued(3) == recent
    assert search.get_job(recent)['started_at'] > cutoff
    for _ in range(2):  # A startup retry retains the same cutoff.
        search.recover_stale(45, requeue_before=cutoff)
        assert search.get_job(old)['status'] == 'queued'
        assert search.get_job(old)['started_at'] is None
        assert search.get_job(exact)['status'] == 'running'
        assert search.get_job(recent)['status'] == 'running'
    assert running_count() == 2


def test_real_postgres_queue_rank_is_one_based_and_fifo_breaks_time_ties_by_id(real_jobs_db):
    from datetime import datetime, timezone
    from uuid import UUID
    search, seed, _, _ = real_jobs_db
    created = datetime(2026, 10, 10, 10, tzinfo=timezone.utc)
    ids = [seed(job_id=str(UUID(int=n)), created_at=created) for n in (3, 1, 2)]
    ids.sort()
    assert [search.queue_position(jid) for jid in ids] == [1, 2, 3]
    assert search.claim_next_queued(2) == ids[0]
    assert search.queue_position(ids[0]) == 0
    assert [search.queue_position(jid) for jid in ids[1:]] == [1, 2]


def test_real_postgres_failure_fallback_preserves_generated_body_and_bounded_raw(real_jobs_db):
    search, seed, _, _ = real_jobs_db
    jid = seed()
    assert search.claim_next_queued(2) == jid
    generated = {'content_md': '生成正文', 'content_md_final': '后处理正文', 'raw': '原始' * 100000}
    search.fail_job(jid, 'finish write failed', result=generated)
    job = search.get_job(jid)
    assert job['status'] == 'failed'
    assert job['result'] == {**generated, 'raw': generated['raw'].encode()[:200 * 1024].decode(errors='ignore')}


def test_finish_job_strips_nuls_from_text_and_nested_json_before_writing(db):
    search, conn = db
    literal = r'\u0000'
    search.finish_job('job-1', result={'content_md': '正文\x00', 'nested': [{'k\x00': 'v\x00'}], 'literal': literal},
                      content_md_final='成\x00文', cited_ids=['a'], usage={'note': '\x00'},
                      elapsed_ms=1, points_estimate=0, model='glm\x00', prompt_version='v\x001',
                      request_params={'note': '\x00'}, input_text='输\x00入',
                      candidate_meta=[{'id': 'a', 'title': '标\x00题'}])
    _, params = next((s, p) for s, p in conn.calls if s.startswith('UPDATE'))
    assert params['content_md_final'] == '成文' and params['input_text'] == '输入'
    assert params['model'] == 'glm' and params['prompt_version'] == 'v1'
    assert json.loads(params['result']) == {'content_md': '正文', 'nested': [{'k': 'v'}], 'literal': literal}
    assert json.loads(params['usage']) == {'note': ''}
    assert json.loads(params['request_params']) == {'note': ''}
    assert json.loads(params['candidate_meta']) == [{'id': 'a', 'title': '标题'}]


def test_failure_fallback_strips_nuls_and_bounds_raw_by_utf8_bytes(db):
    search, conn = db
    raw = ('原\x00始' * 100000)
    result = {'content_md': '正\x00文', 'content_md_final': '后\x00处理', 'raw': raw}
    search.fail_job('job-1', '错\x00误', result=result)
    _, params = next((s, p) for s, p in conn.calls if s.startswith('UPDATE'))
    saved = json.loads(params['result'])
    assert params['error'] == '错误'
    assert saved['content_md'] == '正文' and saved['content_md_final'] == '后处理'
    expected = raw.replace('\x00', '').encode()[:200 * 1024].decode(errors='ignore')
    assert saved['raw'] == expected and len(saved['raw'].encode()) <= 200 * 1024
    assert result['raw'] == raw  # Sanitizing must not mutate the caller's result.


def test_input_write_strips_nuls_before_model_inference(db):
    search, conn = db
    search.save_job_input('job-1', input_text='原\x00文', candidate_meta=[{'title': '标\x00题'}],
                          model='glm', prompt_version='v1', request_params={})
    _, params = next((s, p) for s, p in conn.calls if s.startswith('UPDATE'))
    assert params['input_text'] == '原文'
    assert json.loads(params['candidate_meta']) == [{'title': '标题'}]


@pytest.mark.parametrize('next_action', ['finish_again', 'fail_after_done'])
def test_real_postgres_terminal_job_writes_are_idempotent(real_jobs_db, next_action):
    search, seed, _, _ = real_jobs_db
    jid = seed()
    assert search.claim_next_queued(2) == jid
    fields = dict(result={'content_md': 'original'}, content_md_final='original', cited_ids=['a'],
                  usage={'prompt_tokens': 1}, elapsed_ms=10, points_estimate=1, model='glm',
                  prompt_version='v1', request_params={'max_tokens': 10})
    search.finish_job(jid, **fields)  # Commits before the second call's transaction.
    before = search.get_job(jid)
    assert before['status'] == 'done'
    if next_action == 'finish_again':
        search.finish_job(jid, **{**fields, 'result': {'content_md': 'replacement'},
                                'content_md_final': 'replacement', 'usage': {'prompt_tokens': 99}, 'elapsed_ms': 999})
    else:
        search.fail_job(jid, 'late failure', status='unavailable', raw='late raw', result={'content_md': 'replacement'})
    assert search.get_job(jid) == before


def test_real_postgres_generated_nuls_are_cleaned_before_jsonb_and_text_writes(real_jobs_db):
    search, seed, _, _ = real_jobs_db
    jid = seed()
    assert search.claim_next_queued(2) == jid
    search.finish_job(jid, result={'content_md': '正\x00文'}, content_md_final='正\x00文', cited_ids=[],
                      usage={}, elapsed_ms=1, points_estimate=0, model='glm', prompt_version='v1',
                      request_params={}, input_text='原\x00文')
    job = search.get_job(jid)
    assert job['status'] == 'done' and job['content_md_final'] == '正文'
    assert job['result'] == {'content_md': '正文'} and job['input_text'] == '原文'
