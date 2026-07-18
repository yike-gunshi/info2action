"""Tests for v15.0 event-aggregation DB migration.

Feature Spec: .features/event-aggregation-v15/feature-spec.md §2.5
PRD: docs/PRD.md §5.12 / §5.13 / §5.14 / §5.15 / §5.16

Covers:
- Three new tables: clusters / cluster_items / cluster_status
- items table extended: embedding, embedding_provider, cluster_id, cluster_locked
- actions table extended: source_id, cluster_version, is_stale
- Indexes: idx_clusters_visible_first_doc, idx_clusters_last_updated,
           idx_clusters_merged_into, idx_items_cluster_id
- Migration is idempotent (safe to call get_conn() twice)
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import db as db_mod


@pytest.fixture()
def tmp_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / 'test_v15.db')
    monkeypatch.setattr(db_mod, 'DB_PATH', db_path)
    conn = db_mod.get_conn()
    yield conn
    conn.close()


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _indexes(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA index_list({table})").fetchall()}


class TestClustersTable:
    def test_clusters_table_exists(self, tmp_db):
        row = tmp_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='clusters'"
        ).fetchone()
        assert row is not None, "clusters table must exist after init"

    def test_clusters_has_core_columns(self, tmp_db):
        cols = _columns(tmp_db, 'clusters')
        required = {
            'id', 'ai_title', 'ai_summary', 'ai_key_points',
            'ai_summary_draft', 'ai_title_draft', 'ai_key_points_draft',
            'live_version', 'doc_count', 'platforms_json', 'cover_url',
            'first_doc_at', 'last_doc_at', 'last_updated_at',
            'is_visible_in_feed', 'merged_into', 'archived',
            'prompt_version', 'representative_vector', 'created_at',
        }
        missing = required - cols
        assert not missing, f"clusters table missing columns: {missing}"

    def test_clusters_indexes(self, tmp_db):
        indexes = _indexes(tmp_db, 'clusters')
        assert 'idx_clusters_visible_first_doc' in indexes
        assert 'idx_clusters_last_updated' in indexes

    def test_clusters_defaults(self, tmp_db):
        tmp_db.execute(
            "INSERT INTO clusters (first_doc_at) VALUES (?)",
            ('2026-04-24T10:00:00',),
        )
        row = tmp_db.execute("SELECT * FROM clusters LIMIT 1").fetchone()
        assert row['live_version'] == 0
        assert row['doc_count'] == 0
        assert row['is_visible_in_feed'] == 0
        assert row['archived'] == 0


class TestClusterItemsTable:
    def test_cluster_items_table_exists(self, tmp_db):
        row = tmp_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='cluster_items'"
        ).fetchone()
        assert row is not None

    def test_cluster_items_columns(self, tmp_db):
        cols = _columns(tmp_db, 'cluster_items')
        required = {'cluster_id', 'item_id', 'rank_in_cluster',
                    'added_at', 'is_primary_source'}
        missing = required - cols
        assert not missing

    def test_cluster_items_pk_is_composite(self, tmp_db):
        tmp_db.execute(
            "INSERT INTO clusters (first_doc_at) VALUES (?)", ('2026-04-24T10:00:00',)
        )
        cluster_id = tmp_db.execute("SELECT id FROM clusters LIMIT 1").fetchone()[0]
        # Use a real item (FK is ON).
        tmp_db.execute(
            "INSERT INTO items (id, platform, source, fetched_at) "
            "VALUES ('item-a', 'x', 's', datetime('now'))"
        )
        tmp_db.execute(
            "INSERT INTO cluster_items (cluster_id, item_id) VALUES (?, ?)",
            (cluster_id, 'item-a'),
        )
        # Second insert with same (cluster_id, item_id) should fail (composite PK).
        with pytest.raises(sqlite3.IntegrityError):
            tmp_db.execute(
                "INSERT INTO cluster_items (cluster_id, item_id) VALUES (?, ?)",
                (cluster_id, 'item-a'),
            )


class TestClusterStatusTable:
    def test_cluster_status_table_exists(self, tmp_db):
        row = tmp_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='cluster_status'"
        ).fetchone()
        assert row is not None

    def test_cluster_status_columns(self, tmp_db):
        cols = _columns(tmp_db, 'cluster_status')
        required = {
            'user_id', 'cluster_id', 'clicked_at', 'last_seen_version',
            'feedback_note',
        }
        missing = required - cols
        assert not missing


class TestItemsExtendedColumns:
    def test_items_has_embedding_columns(self, tmp_db):
        cols = _columns(tmp_db, 'items')
        assert 'embedding' in cols, "items needs embedding BLOB for cluster vectors"
        assert 'embedding_provider' in cols
        assert 'cluster_id' in cols
        assert 'cluster_locked' in cols

    def test_items_cluster_id_index(self, tmp_db):
        indexes = _indexes(tmp_db, 'items')
        assert 'idx_items_cluster_id' in indexes


class TestActionsExtendedColumns:
    def test_actions_has_cluster_linkage_columns(self, tmp_db):
        cols = _columns(tmp_db, 'actions')
        assert 'source_id' in cols, "actions needs source_id (item_id or cluster_id)"
        assert 'cluster_version' in cols
        assert 'is_stale' in cols

    def test_actions_is_stale_default_zero(self, tmp_db):
        # A minimal action insert; existing schema has many required fields.
        tmp_db.execute(
            """INSERT INTO actions (id, source_type, title, action_type, prompt)
               VALUES ('a1', 'cluster', 't', 'research', 'p')"""
        )
        row = tmp_db.execute("SELECT is_stale FROM actions WHERE id='a1'").fetchone()
        assert row['is_stale'] == 0


class TestIdempotency:
    def test_init_twice_is_safe(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / 'twice.db')
        monkeypatch.setattr(db_mod, 'DB_PATH', db_path)
        conn1 = db_mod.get_conn()
        conn1.close()
        conn2 = db_mod.get_conn()
        # If non-idempotent, second call would raise "duplicate column" or similar.
        assert conn2 is not None
        cols = _columns(conn2, 'clusters')
        assert 'live_version' in cols
        conn2.close()


class TestBumpClusterVersionHelper:
    """R6.2: When cluster live_version bumps, existing actions become stale."""

    def test_bump_sets_matching_actions_stale(self, tmp_db):
        # Arrange: one cluster, two actions (one cluster, one doc).
        tmp_db.execute(
            "INSERT INTO clusters (id, live_version, first_doc_at) VALUES (10, 3, ?)",
            ('2026-04-24T10:00:00',),
        )
        tmp_db.execute(
            """INSERT INTO actions (id, source_type, source_id, cluster_version, title, action_type, prompt)
               VALUES ('ac1', 'cluster', 10, 3, 't', 'research', 'p')"""
        )
        tmp_db.execute(
            """INSERT INTO actions (id, source_type, source_id, cluster_version, title, action_type, prompt)
               VALUES ('ac2', 'doc', 999, NULL, 't', 'research', 'p')"""
        )
        tmp_db.commit()

        # Act: bump
        db_mod.bump_cluster_version_and_stale_actions(tmp_db, 10, 4)

        # Assert: cluster bumped, cluster-sourced action stale, doc action untouched.
        row = tmp_db.execute("SELECT live_version FROM clusters WHERE id=10").fetchone()
        assert row['live_version'] == 4
        c_action = tmp_db.execute("SELECT is_stale FROM actions WHERE id='ac1'").fetchone()
        d_action = tmp_db.execute("SELECT is_stale FROM actions WHERE id='ac2'").fetchone()
        assert c_action['is_stale'] == 1
        assert d_action['is_stale'] == 0
