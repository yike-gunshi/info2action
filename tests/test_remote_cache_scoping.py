"""BF-0515-cache-scoped-invalidation: prove that targeted invalidation
preserves other users' caches.

Multi-user safety property: when user A updates their profile / status / login,
user B's cached feed must remain intact (= cache hit rate is preserved).
"""
from __future__ import annotations

from contextlib import contextmanager
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import remote_db


@pytest.fixture(autouse=True)
def _reset_cache():
    remote_db._cache_clear_all()
    yield
    remote_db._cache_clear_all()


def _populate_caches():
    """Seed cache with realistic per-user keys."""
    # Anonymous feed snapshots
    remote_db._cache_set(("feed_sections_result", "remote_poc", 50, "", "", True, "", 50), {"sections": "anon"})
    remote_db._cache_set(("feed_platforms_result", "remote_poc", 50, "", "", True, "", 50), {"sections": "anon"})
    # User A specific
    remote_db._cache_set(("feed_sections_result", "remote_poc", 50, "", "user_a", False, "", 50), {"sections": "A"})
    remote_db._cache_set(("auth_session", "user_a", "jti_aaa"), {"session": "A"})
    # User B specific
    remote_db._cache_set(("feed_sections_result", "remote_poc", 50, "", "user_b", False, "", 50), {"sections": "B"})
    remote_db._cache_set(("auth_session", "user_b", "jti_bbb"), {"session": "B"})


def test_user_scoped_clear_preserves_others():
    _populate_caches()
    # user A logs out → only A's caches cleared
    removed = remote_db.clear_user_cache_keys("user_a")
    assert removed >= 2, f"expected to clear A's keys, removed={removed}"
    # User B still cached
    assert remote_db._cache_get(("feed_sections_result", "remote_poc", 50, "", "user_b", False, "", 50)) == {"sections": "B"}
    assert remote_db._cache_get(("auth_session", "user_b", "jti_bbb")) == {"session": "B"}
    # Anonymous still cached
    assert remote_db._cache_get(("feed_sections_result", "remote_poc", 50, "", "", True, "", 50)) == {"sections": "anon"}
    # User A gone
    assert remote_db._cache_get(("feed_sections_result", "remote_poc", 50, "", "user_a", False, "", 50)) is None
    assert remote_db._cache_get(("auth_session", "user_a", "jti_aaa")) is None


def test_clear_feed_keys_preserves_auth():
    _populate_caches()
    removed = remote_db.clear_feed_cache_keys()
    assert removed >= 3, f"expected feed keys cleared, removed={removed}"
    # Auth still there
    assert remote_db._cache_get(("auth_session", "user_a", "jti_aaa")) == {"session": "A"}
    assert remote_db._cache_get(("auth_session", "user_b", "jti_bbb")) == {"session": "B"}
    # Feed gone for everyone
    assert remote_db._cache_get(("feed_sections_result", "remote_poc", 50, "", "user_a", False, "", 50)) is None
    assert remote_db._cache_get(("feed_sections_result", "remote_poc", 50, "", "", True, "", 50)) is None


def test_clear_feed_keys_can_clear_remote_feed_snapshots(monkeypatch):
    executed: list[tuple[str, tuple[str, ...] | None]] = []
    committed = {"value": False}

    class FakeCursor:
        rowcount = 2

    class FakeConn:
        def execute(self, sql, params=None):
            executed.append((" ".join(sql.split()), params))
            return FakeCursor()

        def commit(self):
            committed["value"] = True

    @contextmanager
    def fake_connect():
        yield FakeConn()

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")

    removed = remote_db.clear_feed_cache_keys(clear_remote_snapshots=True)

    assert removed == 2
    assert committed["value"] is True
    assert executed == [
        (
            "DELETE FROM remote_poc.feed_snapshots WHERE snapshot_key LIKE %s OR snapshot_key LIKE %s",
            ("events:%", "sections:%"),
        )
    ]


def test_user_scoped_clear_with_none_user_id_noops():
    _populate_caches()
    initial_size = len(remote_db._CACHE)
    removed = remote_db.clear_user_cache_keys(None)
    assert removed == 0
    assert len(remote_db._CACHE) == initial_size


def test_user_scoped_clear_with_empty_user_id_noops():
    _populate_caches()
    initial_size = len(remote_db._CACHE)
    removed = remote_db.clear_user_cache_keys("")
    assert removed == 0
    assert len(remote_db._CACHE) == initial_size


def test_clear_remote_query_cache_alias_only_clears_feed():
    """The deprecated function should now behave like clear_feed_cache_keys
    (preserves auth) rather than wiping everything."""
    _populate_caches()
    remote_db.clear_remote_query_cache()  # deprecated path
    # Auth survives
    assert remote_db._cache_get(("auth_session", "user_a", "jti_aaa")) is not None
    assert remote_db._cache_get(("auth_session", "user_b", "jti_bbb")) is not None
    # Feed gone
    assert remote_db._cache_get(("feed_sections_result", "remote_poc", 50, "", "", True, "", 50)) is None


def test_user_id_substring_does_not_falsely_match():
    """Ensure user_id matching is exact-string per element, not substring."""
    remote_db._cache_set(("feed_sections_result", "remote_poc", 50, "", "12", False, "", 50), {"sections": "12"})
    remote_db._cache_set(("feed_sections_result", "remote_poc", 50, "", "123", False, "", 50), {"sections": "123"})
    # Clearing user "12" should not clear user "123"
    removed = remote_db.clear_user_cache_keys("12")
    assert removed == 1
    assert remote_db._cache_get(("feed_sections_result", "remote_poc", 50, "", "123", False, "", 50)) == {"sections": "123"}
    assert remote_db._cache_get(("feed_sections_result", "remote_poc", 50, "", "12", False, "", 50)) is None


def test_update_item_asr_fields_clears_matching_item_detail_caches(monkeypatch):
    executed = []

    class FakeConn:
        def execute(self, sql, params=None):
            executed.append((" ".join(sql.split()), params))

        def commit(self):
            pass

    @contextmanager
    def fake_connect():
        yield FakeConn()

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")

    remote_db._cache_set(("feed_item_detail", "remote_poc", "item-1", True, False, "", 50), {"id": "item-1"})
    remote_db._cache_set(("feed_items_detail_batch", "remote_poc", ("item-1", "item-2"), True, False, "", 50), [{"id": "item-1"}])
    remote_db._cache_set(("feed_item_detail", "remote_poc", "item-12", True, False, "", 50), {"id": "item-12"})
    remote_db._cache_set(("feed_sections_result", "remote_poc", 50, "", "", True, "", 50), {"sections": "kept"})

    remote_db.update_item_asr_fields_remote("item-1", asr_status="running")

    assert executed == [
        (
            "UPDATE remote_poc.items SET asr_status = %(v0)s WHERE id = %(item_id)s",
            {"item_id": "item-1", "v0": "running"},
        )
    ]
    assert remote_db._cache_get(("feed_item_detail", "remote_poc", "item-1", True, False, "", 50)) is None
    assert remote_db._cache_get(("feed_items_detail_batch", "remote_poc", ("item-1", "item-2"), True, False, "", 50)) is None
    assert remote_db._cache_get(("feed_item_detail", "remote_poc", "item-12", True, False, "", 50)) == {"id": "item-12"}
    assert remote_db._cache_get(("feed_sections_result", "remote_poc", 50, "", "", True, "", 50)) == {"sections": "kept"}
