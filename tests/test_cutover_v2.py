"""
Tests for scripts/cutover_v2.py

CRITICAL constraints (mirroring feature-spec.md R9.1/R9.2/R9.3):
  - --dry-run never touches the DB.
  - --execute requires backup before DELETE; backup failure aborts before DELETE.
  - Missing --yes + non-YES stdin → abort, no DELETE.
  - cluster_judge_log table missing → preflight blocks.
  - Successful cutover writes one cluster_v15_1_executed event to logs/cluster_events.jsonl.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "cutover_v2.py"


def _load_cutover_module(monkeypatch, tmp_log_dir: Path, tmp_backup_dir: Path):
    """
    Import cutover_v2 fresh and rebind module-level paths so tests can't
    accidentally write to repo's data/backups or logs/.
    """
    spec = importlib.util.spec_from_file_location("cutover_v2_test_mod", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    monkeypatch.setattr(mod, "BACKUP_DIR", tmp_backup_dir, raising=True)
    monkeypatch.setattr(mod, "LOG_DIR", tmp_log_dir, raising=True)
    monkeypatch.setattr(mod, "LOG_FILE", tmp_log_dir / "cluster_events.jsonl", raising=True)
    return mod


# ---------- fixture: a populated v15.1-shaped DB ----------

def _create_test_db(db_path: Path, *, with_cluster_judge_log: bool = True) -> None:
    """
    Build a minimal but realistic schema:
      clusters / cluster_items / cluster_status / items / actions
      + (optionally) cluster_judge_log
    Populated with 2 clusters, 2 cluster_items, 1 cluster_status, 3 items,
    2 actions (1 cluster + 1 item).
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE clusters (
              id INTEGER PRIMARY KEY,
              ai_title TEXT,
              live_version INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE cluster_items (
              cluster_id INTEGER NOT NULL,
              item_id TEXT NOT NULL,
              PRIMARY KEY (cluster_id, item_id)
            );
            CREATE TABLE cluster_status (
              user_id INTEGER NOT NULL,
              cluster_id INTEGER NOT NULL,
              last_seen_version INTEGER,
              PRIMARY KEY (user_id, cluster_id)
            );
            CREATE TABLE items (
              id TEXT PRIMARY KEY,
              title TEXT,
              embedding BLOB,
              embedding_provider TEXT,
              cluster_id INTEGER,
              cluster_locked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE actions (
              id TEXT PRIMARY KEY,
              source_type TEXT NOT NULL,
              source_id TEXT,
              status TEXT
            );
            """
        )

        if with_cluster_judge_log:
            conn.executescript(
                """
                CREATE TABLE cluster_judge_log (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  item_id TEXT NOT NULL,
                  decision_model TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                INSERT INTO cluster_judge_log (item_id, decision_model)
                VALUES ('item-x', 'MiniMax-test');
                """
            )

        conn.executescript(
            """
            INSERT INTO clusters (id, ai_title) VALUES
              (1, 'cluster one'),
              (2, 'cluster two');

            INSERT INTO cluster_items (cluster_id, item_id) VALUES
              (1, 'item-1'),
              (2, 'item-2');

            INSERT INTO cluster_status (user_id, cluster_id, last_seen_version)
              VALUES (1, 1, 1);

            INSERT INTO items (id, title, embedding, embedding_provider, cluster_id, cluster_locked)
              VALUES
                ('item-1', 't1', X'00112233', 'minimax', 1, 1),
                ('item-2', 't2', X'aabbccdd', 'doubao',   2, 0),
                ('item-3', 't3', NULL, NULL, NULL, 0);

            INSERT INTO actions (id, source_type, source_id, status) VALUES
              ('act-c1', 'cluster', '1', 'pending'),
              ('act-i1', 'item',    'item-3', 'done');
            """
        )

        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def populated_db(tmp_path: Path) -> Path:
    db = tmp_path / "feed.db"
    _create_test_db(db)
    return db


@pytest.fixture()
def cutover_mod(monkeypatch, tmp_path: Path):
    log_dir = tmp_path / "logs"
    backup_dir = tmp_path / "backups"
    return _load_cutover_module(monkeypatch, log_dir, backup_dir)


# ---------- tests: dry-run ----------

class TestDryRun:
    def test_dry_run_default_when_no_flag(self, cutover_mod, populated_db, capsys):
        rc = cutover_mod.main(["--db-path", str(populated_db)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "DRY RUN" in out
        assert "DELETE FROM cluster_items" in out
        assert "DELETE FROM cluster_status" in out
        assert "DELETE FROM clusters" in out
        assert "UPDATE items SET cluster_id = NULL" in out
        assert "DELETE FROM actions WHERE source_type = 'cluster'" in out

    def test_dry_run_explicit_flag(self, cutover_mod, populated_db, capsys):
        rc = cutover_mod.main(["--dry-run", "--db-path", str(populated_db)])
        assert rc == 0
        assert "DRY RUN" in capsys.readouterr().out

    def test_dry_run_does_not_modify_db(self, cutover_mod, populated_db):
        before = self._counts(populated_db)
        cutover_mod.main(["--dry-run", "--db-path", str(populated_db)])
        after = self._counts(populated_db)
        assert before == after

    def test_dry_run_does_not_create_backup(self, cutover_mod, populated_db, tmp_path):
        cutover_mod.main(["--dry-run", "--db-path", str(populated_db)])
        backups = list((tmp_path / "backups").glob("*.db"))
        assert backups == []

    def test_dry_run_prints_pre_cutover_stats(self, cutover_mod, populated_db, capsys):
        cutover_mod.main(["--dry-run", "--db-path", str(populated_db)])
        out = capsys.readouterr().out
        assert "Pre-cutover statistics" in out
        # 2 clusters, 2 cluster_items, 1 cluster_status seeded
        assert "clusters" in out and "2" in out

    @staticmethod
    def _counts(db: Path) -> dict:
        with sqlite3.connect(str(db)) as conn:
            return {
                "clusters": conn.execute("SELECT COUNT(*) FROM clusters").fetchone()[0],
                "cluster_items": conn.execute("SELECT COUNT(*) FROM cluster_items").fetchone()[0],
                "cluster_status": conn.execute("SELECT COUNT(*) FROM cluster_status").fetchone()[0],
                "items_emb": conn.execute(
                    "SELECT COUNT(*) FROM items WHERE embedding IS NOT NULL"
                ).fetchone()[0],
                "actions_cluster": conn.execute(
                    "SELECT COUNT(*) FROM actions WHERE source_type='cluster'"
                ).fetchone()[0],
            }


# ---------- tests: preflight ----------

class TestPreflight:
    def test_db_path_missing_returns_nonzero(self, cutover_mod, tmp_path, capsys):
        missing = tmp_path / "does-not-exist.db"
        rc = cutover_mod.main(["--dry-run", "--db-path", str(missing)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "does not exist" in err

    def test_missing_cluster_judge_log_blocks(self, cutover_mod, tmp_path, capsys):
        db = tmp_path / "feed.db"
        _create_test_db(db, with_cluster_judge_log=False)
        rc = cutover_mod.main(["--dry-run", "--db-path", str(db)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "cluster_judge_log" in err

    def test_already_cutover_warns_in_dry_run(self, cutover_mod, tmp_path, capsys):
        db = tmp_path / "feed.db"
        _create_test_db(db)
        with sqlite3.connect(str(db)) as conn:
            conn.execute("DELETE FROM cluster_items")
            conn.execute("DELETE FROM clusters")
            conn.commit()
        rc = cutover_mod.main(["--dry-run", "--db-path", str(db)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "already empty" in out or "already" in out


# ---------- tests: execute ----------

class TestExecute:
    def test_execute_yes_full_happy_path(
        self, cutover_mod, populated_db, tmp_path, capsys
    ):
        rc = cutover_mod.main(
            ["--execute", "--yes", "--db-path", str(populated_db)]
        )
        assert rc == 0

        # backup created
        backups = list((tmp_path / "backups").glob("feed-pre-v15.1-*.db"))
        assert len(backups) == 1
        assert backups[0].stat().st_size > 0

        # DB invariants (R9.3)
        with sqlite3.connect(str(populated_db)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM clusters").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM cluster_items").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM cluster_status").fetchone()[0] == 0
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM items WHERE cluster_id IS NOT NULL"
                ).fetchone()[0]
                == 0
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM items WHERE embedding IS NOT NULL"
                ).fetchone()[0]
                == 0
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM items WHERE embedding_provider IS NOT NULL"
                ).fetchone()[0]
                == 0
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM items WHERE cluster_locked != 0"
                ).fetchone()[0]
                == 0
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM actions WHERE source_type='cluster'"
                ).fetchone()[0]
                == 0
            )
            # non-cluster actions preserved
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM actions WHERE source_type='item'"
                ).fetchone()[0]
                == 1
            )

        # audit log written
        log_file = tmp_path / "logs" / "cluster_events.jsonl"
        assert log_file.exists()
        lines = [
            json.loads(l) for l in log_file.read_text().splitlines() if l.strip()
        ]
        assert len(lines) == 1
        ev = lines[0]
        assert ev["event"] == "cluster_v15_1_executed"
        assert ev["executed_by"] == "cli"
        assert ev["invariants_ok"] is True
        assert ev["before_clusters_count"] == 2
        assert ev["before_cluster_items_count"] == 2
        assert ev["before_items_with_embedding"] == 2
        assert ev["after_clusters_count"] == 0
        assert ev["after_items_with_embedding"] == 0
        assert ev["after_actions_cluster_source"] == 0

    def test_execute_without_yes_and_non_YES_stdin_aborts(
        self, cutover_mod, populated_db, tmp_path, monkeypatch, capsys
    ):
        # simulate user typing "no"
        monkeypatch.setattr("builtins.input", lambda *a, **k: "no")

        rc = cutover_mod.main(["--execute", "--db-path", str(populated_db)])
        assert rc == 1

        # DB unchanged on cluster tables (backup did happen, but no DELETE/UPDATE)
        with sqlite3.connect(str(populated_db)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM clusters").fetchone()[0] == 2
            assert conn.execute("SELECT COUNT(*) FROM cluster_items").fetchone()[0] == 2
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM items WHERE embedding IS NOT NULL"
                ).fetchone()[0]
                == 2
            )

    def test_execute_backup_failure_aborts_before_delete(
        self, cutover_mod, populated_db, tmp_path, monkeypatch, capsys
    ):
        # Force `sqlite3 .backup` to fail by mocking subprocess.run
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="disk full"
            )

        monkeypatch.setattr(cutover_mod.subprocess, "run", fake_run)

        rc = cutover_mod.main(
            ["--execute", "--yes", "--db-path", str(populated_db)]
        )
        assert rc == 1

        # absolutely nothing should have changed
        with sqlite3.connect(str(populated_db)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM clusters").fetchone()[0] == 2
            assert conn.execute("SELECT COUNT(*) FROM cluster_items").fetchone()[0] == 2
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM items WHERE embedding IS NOT NULL"
                ).fetchone()[0]
                == 2
            )

        # No log entry written
        log_file = tmp_path / "logs" / "cluster_events.jsonl"
        # log file may exist but must not contain cluster_v15_1_executed
        if log_file.exists():
            text = log_file.read_text()
            assert "cluster_v15_1_executed" not in text

    def test_execute_backup_missing_file_aborts(
        self, cutover_mod, populated_db, tmp_path, monkeypatch
    ):
        # subprocess returns 0 but backup file is never created
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )

        monkeypatch.setattr(cutover_mod.subprocess, "run", fake_run)

        rc = cutover_mod.main(
            ["--execute", "--yes", "--db-path", str(populated_db)]
        )
        assert rc == 1

        with sqlite3.connect(str(populated_db)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM clusters").fetchone()[0] == 2

    def test_execute_already_cutover_with_yes_proceeds_safely(
        self, cutover_mod, populated_db, tmp_path
    ):
        # Empty cluster tables but keep a valid items.embedding row to verify reset still happens
        with sqlite3.connect(str(populated_db)) as conn:
            conn.execute("DELETE FROM cluster_items")
            conn.execute("DELETE FROM cluster_status")
            conn.execute("DELETE FROM clusters")
            conn.commit()

        rc = cutover_mod.main(
            ["--execute", "--yes", "--db-path", str(populated_db)]
        )
        assert rc == 0

        with sqlite3.connect(str(populated_db)) as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM items WHERE embedding IS NOT NULL"
                ).fetchone()[0]
                == 0
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM actions WHERE source_type='cluster'"
                ).fetchone()[0]
                == 0
            )


# ---------- tests: argparse safety ----------

class TestArgparse:
    def test_dry_run_and_execute_mutually_exclusive(self, cutover_mod):
        with pytest.raises(SystemExit):
            cutover_mod.main(["--dry-run", "--execute"])

    def test_no_no_backup_flag_exists(self, cutover_mod):
        # Hard guarantee: there must NOT be a --no-backup bypass.
        parser = cutover_mod.build_arg_parser()
        all_flags = {
            flag
            for act in parser._actions
            for flag in act.option_strings
        }
        assert "--no-backup" not in all_flags
        # also verify both modes are present
        assert "--dry-run" in all_flags
        assert "--execute" in all_flags
        assert "--yes" in all_flags
