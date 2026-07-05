"""Test v12.2 ASR schema migration on items table."""
import sqlite3
import pytest
from src import db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db_path = tmp_path / "test_feed.db"
    monkeypatch.setattr(db, "DB_PATH", str(db_path))
    c = db.get_conn()
    yield c
    c.close()


def _column_names(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _index_names(conn, table):
    return {row[1] for row in conn.execute(
        f"SELECT * FROM sqlite_master WHERE type='index' AND tbl_name='{table}'"
    ).fetchall()}


def test_legacy_cluster_status_migrates_starred_at_before_indexes(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy_feed.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE cluster_status (
          user_id TEXT NOT NULL,
          cluster_id INTEGER NOT NULL,
          clicked_at TIMESTAMP,
          last_seen_version INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (user_id, cluster_id)
        );
        """
    )
    legacy.close()

    monkeypatch.setattr(db, "DB_PATH", str(db_path))
    conn = db.get_conn()
    try:
        assert "starred_at" in _column_names(conn, "cluster_status")
        indexes = _index_names(conn, "cluster_status")
        assert "idx_cluster_status_user_clicked" in indexes
        assert "idx_cluster_status_user_starred" in indexes
    finally:
        conn.close()


class TestAsrSchemaMigration:
    """v12.2: items 表新增 7 个 ASR 字段 + 1 个部分索引."""

    def test_asr_text_column_exists(self, conn):
        assert 'asr_text' in _column_names(conn, 'items')

    def test_asr_status_column_exists(self, conn):
        assert 'asr_status' in _column_names(conn, 'items')

    def test_asr_duration_sec_column_exists(self, conn):
        assert 'asr_duration_sec' in _column_names(conn, 'items')

    def test_asr_cost_yuan_column_exists(self, conn):
        assert 'asr_cost_yuan' in _column_names(conn, 'items')

    def test_asr_attempted_at_column_exists(self, conn):
        assert 'asr_attempted_at' in _column_names(conn, 'items')

    def test_asr_failed_reason_column_exists(self, conn):
        assert 'asr_failed_reason' in _column_names(conn, 'items')

    def test_asr_provider_column_has_default(self, conn):
        # 插入一条不带 asr_provider 的 item,默认值应为 'doubao-seedasr-bigmodel'
        conn.execute("""
            INSERT INTO items(id, platform, source, fetched_at) VALUES ('t1', 'twitter', 'following', '2026-04-18T00:00:00')
        """)
        row = conn.execute("SELECT asr_provider FROM items WHERE id='t1'").fetchone()
        assert row[0] == 'doubao-seedasr-bigmodel'

    def test_idx_items_asr_status_exists(self, conn):
        assert 'idx_items_asr_status' in _index_names(conn, 'items')

    def test_asr_fields_are_nullable_by_default(self, conn):
        """未触发 ASR 的 item 其 asr_* 字段应为 NULL."""
        conn.execute("INSERT INTO items(id, platform, source, fetched_at) VALUES ('t2','twitter','following','2026-04-18T00:00:00')")
        row = conn.execute(
            "SELECT asr_text, asr_status, asr_duration_sec, asr_cost_yuan, "
            "asr_attempted_at, asr_failed_reason FROM items WHERE id='t2'"
        ).fetchone()
        assert all(v is None for v in row)

    def test_can_write_and_read_asr_fields(self, conn):
        """基本 CRUD:写入 ASR 数据后能读回."""
        conn.execute("INSERT INTO items(id, platform, source, fetched_at) VALUES ('t3','twitter','following','2026-04-18T00:00:00')")
        conn.execute("""
            UPDATE items SET
              asr_text='hello world',
              asr_status='success',
              asr_duration_sec=90,
              asr_cost_yuan=0.0435,
              asr_attempted_at='2026-04-18T15:00:00',
              asr_failed_reason=NULL,
              asr_provider='doubao-seedasr-bigmodel'
            WHERE id='t3'
        """)
        row = conn.execute("""
            SELECT asr_text, asr_status, asr_duration_sec, asr_cost_yuan,
                   asr_attempted_at, asr_provider FROM items WHERE id='t3'
        """).fetchone()
        assert tuple(row) == ('hello world', 'success', 90, 0.0435,
                               '2026-04-18T15:00:00', 'doubao-seedasr-bigmodel')

    def test_partial_index_only_covers_nonnull(self, conn):
        """部分索引 WHERE asr_status IS NOT NULL: 对 NULL 行不占索引空间."""
        row = conn.execute("""
            SELECT sql FROM sqlite_master
            WHERE type='index' AND name='idx_items_asr_status'
        """).fetchone()
        assert row is not None
        assert 'WHERE' in row[0].upper() and 'asr_status' in row[0]
