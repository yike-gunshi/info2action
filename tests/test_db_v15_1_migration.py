"""Tests for v15.1 event-aggregation V2 DB migration (PRD §5.17).

Covers:
- clusters: unique_source_count / last_summary_warnings_json / event_embedding
- cluster_items: source_identity / join_decision_id
- cluster_judge_log table + 2 indexes
- Migration is idempotent (safe to call get_conn() repeatedly)
- event_embedding BLOB round-trip
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import db as db_mod  # noqa: E402


@pytest.fixture()
def tmp_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / 'test_v15_1.db')
    monkeypatch.setattr(db_mod, 'DB_PATH', db_path)
    conn = db_mod.get_conn()
    yield conn
    conn.close()


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _table_exists(conn, name):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _index_exists(conn, name):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone()
    return row is not None


class TestClustersV15_1Columns:
    def test_unique_source_count_added(self, tmp_db):
        cols = _columns(tmp_db, 'clusters')
        assert 'unique_source_count' in cols

    def test_last_summary_warnings_json_added(self, tmp_db):
        cols = _columns(tmp_db, 'clusters')
        assert 'last_summary_warnings_json' in cols

    def test_event_embedding_added(self, tmp_db):
        cols = _columns(tmp_db, 'clusters')
        assert 'event_embedding' in cols

    def test_unique_source_count_default_zero(self, tmp_db):
        tmp_db.execute(
            "INSERT INTO clusters (first_doc_at) VALUES (?)",
            ('2026-04-27T10:00:00',),
        )
        row = tmp_db.execute(
            "SELECT unique_source_count, last_summary_warnings_json, event_embedding "
            "FROM clusters LIMIT 1"
        ).fetchone()
        assert row['unique_source_count'] == 0
        assert row['last_summary_warnings_json'] is None
        assert row['event_embedding'] is None


class TestClusterItemsV15_1Columns:
    def test_source_identity_added(self, tmp_db):
        cols = _columns(tmp_db, 'cluster_items')
        assert 'source_identity' in cols

    def test_join_decision_id_added(self, tmp_db):
        cols = _columns(tmp_db, 'cluster_items')
        assert 'join_decision_id' in cols


class TestClusterJudgeLogTable:
    def test_table_exists(self, tmp_db):
        assert _table_exists(tmp_db, 'cluster_judge_log')

    def test_required_columns(self, tmp_db):
        cols = _columns(tmp_db, 'cluster_judge_log')
        required = {
            'id', 'item_id', 'candidate_cluster_ids',
            'llm_input_tokens', 'llm_output_tokens',
            'matches_json', 'selected_cluster_id', 'selection_reason',
            'possible_merge_candidates', 'decision_model', 'created_at',
        }
        missing = required - cols
        assert not missing, f"cluster_judge_log missing columns: {missing}"

    def test_idx_item(self, tmp_db):
        assert _index_exists(tmp_db, 'idx_cluster_judge_log_item')

    def test_idx_selected_partial(self, tmp_db):
        assert _index_exists(tmp_db, 'idx_cluster_judge_log_selected')

    def test_insert_and_read(self, tmp_db):
        tmp_db.execute(
            """INSERT INTO cluster_judge_log
                 (item_id, candidate_cluster_ids, llm_input_tokens, llm_output_tokens,
                  matches_json, selected_cluster_id, selection_reason,
                  possible_merge_candidates, decision_model)
               VALUES ('item-1', '[1,2,3]', 1200, 400,
                       '[{"cluster_id":1,"same_event":true}]', 1, 'top-confidence-match',
                       '[2]', 'MiniMax-M2.7')""",
        )
        tmp_db.commit()
        row = tmp_db.execute(
            "SELECT * FROM cluster_judge_log WHERE item_id='item-1'"
        ).fetchone()
        assert row['selected_cluster_id'] == 1
        assert row['selection_reason'] == 'top-confidence-match'
        assert row['decision_model'] == 'MiniMax-M2.7'
        assert row['llm_input_tokens'] == 1200
        assert row['created_at']  # auto-filled


class TestEventEmbeddingBlobRoundtrip:
    def test_blob_write_read(self, tmp_db):
        # Insert a cluster, then UPDATE event_embedding with raw bytes
        tmp_db.execute(
            "INSERT INTO clusters (first_doc_at) VALUES (?)",
            ('2026-04-27T10:00:00',),
        )
        cid = tmp_db.execute("SELECT id FROM clusters LIMIT 1").fetchone()['id']
        payload = b'\x00\x01\x02fakeembed\xff\xfe'
        tmp_db.execute(
            "UPDATE clusters SET event_embedding = ? WHERE id = ?",
            (payload, cid),
        )
        tmp_db.commit()
        row = tmp_db.execute(
            "SELECT event_embedding FROM clusters WHERE id = ?", (cid,)
        ).fetchone()
        assert row['event_embedding'] == payload


class TestIdempotency:
    def test_init_db_repeatable(self, monkeypatch, tmp_path):
        """Calling get_conn() twice on the same path must not raise."""
        db_path = str(tmp_path / 'test_repeat.db')
        monkeypatch.setattr(db_mod, 'DB_PATH', db_path)
        conn1 = db_mod.get_conn()
        conn1.close()
        # Second call: should be a no-op for all v15.1 ALTER / CREATE statements.
        conn2 = db_mod.get_conn()
        try:
            cols = _columns(conn2, 'clusters')
            assert 'unique_source_count' in cols
            assert _table_exists(conn2, 'cluster_judge_log')
            assert _index_exists(conn2, 'idx_cluster_judge_log_item')
            assert _index_exists(conn2, 'idx_cluster_judge_log_selected')
        finally:
            conn2.close()
