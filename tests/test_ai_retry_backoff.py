import os
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_ai_retry_columns_are_added(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "feed.db"))
    conn = db.get_conn()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()}

    assert {"ai_error_count", "ai_last_error", "ai_last_error_at", "ai_retry_after"} <= cols


def test_retry_after_filter_excludes_future_retry(tmp_path, monkeypatch):
    import db
    import generate_summaries

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "feed.db"))
    conn = db.get_conn()
    conn.execute(
        """INSERT INTO items (
               id, platform, source, title, content, fetched_at, ai_retry_after
           ) VALUES (?, ?, ?, ?, ?, ?, datetime('now', '+1 hour'))""",
        ("x1", "twitter", "test", "title", "long enough content", "2026-04-24T00:00:00"),
    )
    conn.commit()

    rows = generate_summaries.query_pending_items(conn, limit=10)

    assert rows == []
