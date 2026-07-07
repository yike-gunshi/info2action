"""Source failure tracking behavior tests."""
import os
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    import db

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "feed.db"))
    db._item_status_has_user_id = None
    yield


def _insert_source(conn, status="active"):
    cur = conn.execute(
        "INSERT INTO sources(platform, source_key, status) VALUES('rss', ?, ?)",
        (f"https://example.com/{status}.xml", status),
    )
    conn.commit()
    return cur.lastrowid


def _source_row(conn, source_id):
    return conn.execute(
        """SELECT status, consecutive_failures, last_success_at, last_error
           FROM sources WHERE id = ?""",
        (source_id,),
    ).fetchone()


def test_consecutive_failures_increment_below_default_threshold(tmp_db):
    import db

    conn = db.get_conn()
    try:
        source_id = _insert_source(conn)
        for _ in range(3):
            db.record_source_fetch_result(conn, source_id, ok=False, error="timeout")

        row = _source_row(conn, source_id)
        assert row["consecutive_failures"] == 3
        assert row["status"] == "active"
    finally:
        conn.close()


def test_failure_at_threshold_marks_active_source_broken(tmp_db):
    import db

    conn = db.get_conn()
    try:
        source_id = _insert_source(conn)
        for _ in range(2):
            db.record_source_fetch_result(
                conn, source_id, ok=False, error="HTTP 500", broken_after=2
            )

        row = _source_row(conn, source_id)
        assert row["consecutive_failures"] == 2
        assert row["status"] == "broken"
        assert row["last_error"] == "HTTP 500"
    finally:
        conn.close()


def test_success_resets_failures_and_restores_broken_source(tmp_db):
    import db

    conn = db.get_conn()
    try:
        source_id = _insert_source(conn, status="broken")
        conn.execute(
            """UPDATE sources
               SET consecutive_failures = 5, last_error = 'HTTP 500'
               WHERE id = ?""",
            (source_id,),
        )
        conn.commit()

        db.record_source_fetch_result(conn, source_id, ok=True)

        row = _source_row(conn, source_id)
        assert row["consecutive_failures"] == 0
        assert row["status"] == "active"
        assert row["last_success_at"]
        assert row["last_error"] is None
    finally:
        conn.close()


def test_paused_and_deleted_sources_are_unchanged(tmp_db):
    import db

    conn = db.get_conn()
    try:
        for status in ("paused", "deleted"):
            source_id = _insert_source(conn, status=status)
            db.record_source_fetch_result(conn, source_id, ok=False, error="timeout")
            row = _source_row(conn, source_id)
            assert row["consecutive_failures"] == 0
            assert row["status"] == status
    finally:
        conn.close()


def test_unknown_and_none_source_id_are_noops(tmp_db):
    import db

    conn = db.get_conn()
    try:
        db.record_source_fetch_result(conn, None, ok=False, error="timeout")
        db.record_source_fetch_result(conn, 999, ok=False, error="timeout")
    finally:
        conn.close()
