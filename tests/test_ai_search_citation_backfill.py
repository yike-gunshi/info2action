import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def job(job_id="job-1", content="先[3]，后[1]，又[3]。", **overrides):
    return {
        "id": job_id,
        "status": "done",
        "content_md_final": content,
        "candidate_ids": ["item-1", "item-2", "item-3"],
        "cited_ids": ["item-3", "item-1"],
        **overrides,
    }


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class Connection:
    def __init__(self, rows, events, conflict_id=None):
        self.rows = rows
        self.events = events
        self.conflict_id = conflict_id
        self.calls = []
        self.pending = []
        self.persisted = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def execute(self, sql, params=None):
        sql = " ".join(sql.split())
        self.calls.append((sql, params))
        self.events.append(sql.split()[0])
        if sql.startswith("SELECT"):
            return Cursor(self.rows)
        if sql.startswith("UPDATE"):
            if params["id"] == self.conflict_id:
                return Cursor([])
            self.pending.append(params)
            return Cursor([{"id": params["id"]}])
        return Cursor([])

    def commit(self):
        self.commits += 1
        self.persisted.extend(self.pending)
        self.pending.clear()

    def rollback(self):
        self.rollbacks += 1
        self.pending.clear()

    def close(self):
        self.closed = True


@pytest.fixture
def backfill(monkeypatch):
    module = importlib.import_module("backfill_ai_search_citation_numbers")
    monkeypatch.setattr(module, "load_project_env", lambda root: {})
    monkeypatch.delenv("SUPABASE_DB_DIRECT_URL", raising=False)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://user:password@pool.example:6543/postgres")

    def forbid_database(**kwargs):
        raise AssertionError("Tests must explicitly install a mock database")

    monkeypatch.setattr(module.psycopg, "connect", forbid_database)
    return module


def mock_connection(backfill, monkeypatch, rows, conflict_id=None):
    events = []
    conn = Connection(rows, events, conflict_id)
    connections = []

    def connect(**kwargs):
        connections.append(kwargs)
        return conn

    monkeypatch.setattr(backfill.psycopg, "connect", connect)
    return conn, events, connections


def test_plan_uses_shared_helper_and_preserves_sources(backfill):
    from ai_search_digest import renumber_citations

    assert backfill.renumber_citations is renumber_citations
    original = job()
    changes = backfill.plan_changes([original])
    assert len(changes) == 1
    assert changes[0]["old_content_md_final"] == original["content_md_final"]
    assert changes[0]["new_content_md_final"] == "先[1]，后[2]，又[1]。"
    assert changes[0]["candidate_ids"] == original["candidate_ids"]
    assert changes[0]["cited_ids"] == original["cited_ids"]
    assert original["content_md_final"] == "先[3]，后[1]，又[3]。"


def test_plan_is_idempotent_even_when_cited_order_differs_from_candidates(backfill):
    original = job()
    change = backfill.plan_changes([original])[0]
    renumbered = {**original, "content_md_final": change["new_content_md_final"]}
    assert backfill.plan_changes([renumbered]) == []


@pytest.mark.parametrize("row", [
    job(status="running"), job(status="failed"), job(content=""),
    job(content=None), job(content=" \n "), job(content="没有引用"),
    job(content="已连续[1]，再次[2][1]，未知[99]"),
])
def test_plan_skips_ineligible_or_unchanged_rows(backfill, row):
    assert backfill.plan_changes([row]) == []


@pytest.mark.parametrize("overrides", [
    {"cited_ids": ["item-1", "item-3"]},
    {"cited_ids": None}, {"candidate_ids": {"bad": "shape"}},
])
def test_plan_rejects_unknown_provenance(backfill, overrides):
    with pytest.raises(ValueError, match="job-1"):
        backfill.plan_changes([job(**overrides)])


def test_dry_run_is_read_only_prints_diff_and_never_backs_up_or_writes(backfill, monkeypatch, tmp_path, capsys):
    conn, _, connections = mock_connection(backfill, monkeypatch, [job()])
    backup = tmp_path / "backup.json"
    assert backfill.run_backfill(backup_path=backup) == 1
    assert conn.calls[0][0] == "BEGIN READ ONLY"
    sql, params = next((s, p) for s, p in conn.calls if s.startswith("SELECT"))
    assert "FROM remote_poc.ai_search_jobs" in sql
    assert "status = %s" in sql and "content_md_final IS NOT NULL" in sql
    assert "content_md_final <> %s" in sql and params == ("done", "")
    assert not any(s.startswith(("SET ", "UPDATE", "INSERT", "DELETE")) for s, _ in conn.calls)
    assert conn.commits == 0 and conn.rollbacks == 1 and conn.closed
    assert connections[0]["prepare_threshold"] is None
    assert connections[0]["autocommit"] is True
    assert not backup.exists()
    output = capsys.readouterr().out
    assert "job-1" in output and "先[3]" in output and "先[1]" in output
    assert "dry-run" in output


def test_apply_fsyncs_backup_before_any_update_and_preserves_other_fields(backfill, monkeypatch, tmp_path):
    original = job()
    conn, events, connections = mock_connection(backfill, monkeypatch, [original])
    backup = tmp_path / "backup.json"
    real_fsync = backfill.os.fsync

    def fsync(fd):
        data = json.loads(backup.read_text())
        assert data["table"] == "remote_poc.ai_search_jobs"
        saved = data["rows"][0]
        assert saved["id"] == "job-1"
        assert saved["old_content_md_final"] == original["content_md_final"]
        assert saved["new_content_md_final"] == "先[1]，后[2]，又[1]。"
        assert saved["candidate_ids"] == original["candidate_ids"]
        assert saved["cited_ids"] == original["cited_ids"]
        real_fsync(fd)
        events.append("FSYNC")

    monkeypatch.setattr(backfill.os, "fsync", fsync)
    assert backfill.run_backfill(apply=True, backup_path=backup) == 1
    assert events.index("FSYNC") < events.index("UPDATE")
    assert conn.commits == 1 and conn.rollbacks == 0 and conn.closed
    assert len(conn.persisted) == 1
    sql, params = next((s, p) for s, p in conn.calls if s.startswith("UPDATE"))
    assert sql.startswith("UPDATE remote_poc.ai_search_jobs SET content_md_final = ")
    assert "SET content_md_final = %(new_content_md_final)s WHERE" in sql
    for field in ("id", "status", "old_content_md_final", "candidate_ids", "cited_ids"):
        assert f"%({field})s" in sql
    assert params["status"] == "done"
    assert params["old_content_md_final"] == original["content_md_final"]
    assert json.loads(params["candidate_ids"]) == original["candidate_ids"]
    assert json.loads(params["cited_ids"]) == original["cited_ids"]
    assert connections[0]["port"] == "5432"
    assert not any(s.startswith("SET ") for s, _ in conn.calls)
    assert backup.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("failure", ["exists", "fsync"])
def test_failed_backup_prevents_all_updates(backfill, monkeypatch, tmp_path, failure):
    conn, _, _ = mock_connection(backfill, monkeypatch, [job()])
    backup = tmp_path / "backup.json"
    if failure == "exists":
        backup.write_text("keep me")
    else:
        def fail_fsync(fd):
            raise OSError("disk sync failed")
        monkeypatch.setattr(backfill.os, "fsync", fail_fsync)
    with pytest.raises(OSError):
        backfill.run_backfill(apply=True, backup_path=backup)
    assert not any(s.startswith("UPDATE") for s, _ in conn.calls)
    assert conn.commits == 0 and conn.rollbacks == 1 and conn.closed
    if failure == "exists":
        assert backup.read_text() == "keep me"


def test_conflicting_second_row_rolls_back_whole_batch_and_keeps_backup(backfill, monkeypatch, tmp_path):
    conn, _, _ = mock_connection(backfill, monkeypatch, [job(), job("job-2")], conflict_id="job-2")
    backup = tmp_path / "backup.json"
    with pytest.raises(RuntimeError, match="job-2"):
        backfill.run_backfill(apply=True, backup_path=backup)
    assert len([s for s, _ in conn.calls if s.startswith("UPDATE")]) == 2
    assert conn.commits == 0 and conn.rollbacks == 1 and conn.closed
    assert conn.persisted == [] and conn.pending == []
    assert len(json.loads(backup.read_text())["rows"]) == 2


def test_apply_with_no_changes_does_not_write_backup(backfill, monkeypatch, tmp_path):
    conn, _, _ = mock_connection(backfill, monkeypatch, [job(content="已编号[1][2]")])
    backup = tmp_path / "backup.json"
    assert backfill.run_backfill(apply=True, backup_path=backup) == 0
    assert not backup.exists()
    assert not any(s.startswith("UPDATE") for s, _ in conn.calls)
    assert conn.commits == 0 and conn.closed


def test_connection_enforces_session_port_and_connection_options(backfill, monkeypatch):
    config = backfill.connection_kwargs(apply=True)
    assert config["port"] == "5432" and config["host"] == "pool.example"
    assert config["prepare_threshold"] is None and config["autocommit"] is True
    assert "-c statement_timeout=" in config["options"]
    assert "-c lock_timeout=" in config["options"]
    assert "-c default_transaction_read_only=off" in config["options"]
    assert config["connect_timeout"] > 0
    monkeypatch.setenv("SUPABASE_DB_DIRECT_URL", "postgresql://user:password@direct.example:5432/postgres")
    assert backfill.connection_kwargs(apply=True)["host"] == "direct.example"


@pytest.mark.parametrize("suffix", ["?port=6543", "?port=5432", "?%70ort=6543", "?port=5432&port=6543"])
def test_connection_rejects_query_port_overrides(backfill, monkeypatch, suffix):
    monkeypatch.setenv("SUPABASE_DB_DIRECT_URL", "postgresql://user:password@direct.example:5432/postgres" + suffix)
    with pytest.raises(ValueError, match="port"):
        backfill.connection_kwargs(apply=True)


def test_connection_rejects_non_session_port_for_apply(backfill, monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_DIRECT_URL", "postgresql://user:password@direct.example:7777/postgres")
    with pytest.raises(ValueError, match="5432"):
        backfill.connection_kwargs(apply=True)


def test_connection_loads_project_env_without_mutating_environ(backfill, monkeypatch):
    monkeypatch.delenv("SUPABASE_DB_URL")
    monkeypatch.setattr(backfill, "load_project_env", lambda root: {
        "SUPABASE_DB_DIRECT_URL": "postgresql://user:password@from-file.example:5432/postgres",
    })
    assert backfill.connection_kwargs(apply=True)["host"] == "from-file.example"
    assert "SUPABASE_DB_DIRECT_URL" not in backfill.os.environ
