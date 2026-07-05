from contextlib import contextmanager
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import remote_db


def test_inserted_run_queue_uses_fetch_run_items_join(monkeypatch):
    executed = []

    class FakeResult:
        def fetchall(self):
            return []

    class FakeConn:
        def execute(self, sql, params=None):
            executed.append((" ".join(sql.split()), params))
            return FakeResult()

    @contextmanager
    def fake_connect():
        yield FakeConn()

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")

    rows = remote_db.query_pending_enrichment_items_remote(
        run_id=3011,
        run_items_scope="inserted",
    )

    assert rows == []
    sql, params = next(
        call for call in executed
        if "FROM remote_poc.fetch_run_items fri" in call[0]
    )
    assert (
        "FROM remote_poc.fetch_run_items fri "
        "JOIN remote_poc.items i ON i.id = fri.item_id"
    ) in sql
    assert "EXISTS (" not in sql
    assert "fri.run_id = %s" in sql
    assert "fri.was_inserted = 1" in sql
    assert "ORDER BY i.fetched_at DESC" in sql
    assert params == (3011,)


def test_tagged_run_queue_keeps_items_fetch_run_filter(monkeypatch):
    executed = []

    class FakeResult:
        def fetchall(self):
            return []

    class FakeConn:
        def execute(self, sql, params=None):
            executed.append((" ".join(sql.split()), params))
            return FakeResult()

    @contextmanager
    def fake_connect():
        yield FakeConn()

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")

    rows = remote_db.query_pending_enrichment_items_remote(
        run_id=3011,
        run_items_scope="tagged",
    )

    assert rows == []
    sql, params = next(
        call for call in executed
        if "FROM remote_poc.items" in call[0]
    )
    assert "FROM remote_poc.items" in sql
    assert "fetch_run_items fri" not in sql
    assert "items.fetch_run_id = %s" in sql
    assert "ORDER BY items.fetched_at DESC" in sql
    assert params == (3011,)
