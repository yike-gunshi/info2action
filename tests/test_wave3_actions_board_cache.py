from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import remote_db  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_cache():
    remote_db._cache_clear_all()
    yield
    remote_db._cache_clear_all()


class _InvalidationConn:
    def __init__(self):
        self.executed: list[tuple[str, object]] = []
        self.commits = 0

    def execute(self, sql, params=None):
        self.executed.append((str(sql), params))

    def commit(self):
        self.commits += 1


def _read_model_payload(marker: str) -> dict:
    return {
        "counts": {"total": 1, "pending": 1, "in_progress": 0},
        "directions": [
            {
                "slug": "pending",
                "label": "Pending",
                "count": 1,
                "items": [{"id": marker, "title": f"Action {marker}"}],
                "has_more": False,
                "next_offset": None,
            },
        ],
        "meta": {
            "limit_per_direction": 20,
            "offset": 0,
            "degraded": False,
            "detail_degraded": False,
            "detail_included": False,
            "read_model": remote_db.ACTION_BOARD_READ_MODEL_NAME,
            "read_model_version_id": f"version-{marker}",
            "scope_key": "date:all|priority:all",
            "query_strategy": "action_board_read_model",
        },
    }


def _get_board(**kwargs):
    params = {
        "status": None,
        "priority": None,
        "action_type": None,
        "direction": None,
        "source_filter": None,
        "date_filter": None,
        "user_id": "user-a",
        "can_view_all": False,
        "limit_per_direction": 20,
        "offset": 0,
        "include_detail_payloads": False,
    }
    params.update(kwargs)
    return remote_db.get_actions_board_payload_remote(**params)


def test_actions_board_read_model_result_is_cached(monkeypatch):
    calls = []

    def fake_query(**kwargs):
        calls.append(kwargs)
        return _read_model_payload("cached")

    monkeypatch.setattr(remote_db, "_query_actions_board_read_model_remote", fake_query)

    first = _get_board()
    first["directions"][0]["items"][0]["title"] = "mutated by caller"
    second = _get_board()

    assert len(calls) == 1
    assert second["directions"][0]["items"][0]["title"] == "Action cached"


def test_actions_board_cache_key_separates_user_and_filter(monkeypatch):
    calls = []

    def fake_query(**kwargs):
        calls.append(kwargs)
        marker = f"{kwargs.get('user_id')}:{kwargs.get('priority') or 'all'}"
        return _read_model_payload(marker)

    monkeypatch.setattr(remote_db, "_query_actions_board_read_model_remote", fake_query)

    first = _get_board(user_id="user-a", priority="high")
    second = _get_board(user_id="user-b", priority="high")
    third = _get_board(user_id="user-a", priority="low")
    fourth = _get_board(user_id="user-a", priority="high")

    assert len(calls) == 3
    assert first["directions"][0]["items"][0]["id"] == "user-a:high"
    assert second["directions"][0]["items"][0]["id"] == "user-b:high"
    assert third["directions"][0]["items"][0]["id"] == "user-a:low"
    assert fourth["directions"][0]["items"][0]["id"] == "user-a:high"


def test_actions_board_cache_is_cleared_by_read_model_invalidation(monkeypatch):
    calls = []

    def fake_query(**kwargs):
        calls.append(kwargs)
        return _read_model_payload(str(len(calls)))

    monkeypatch.setattr(remote_db, "_query_actions_board_read_model_remote", fake_query)

    first = _get_board()
    second = _get_board()

    remote_db.invalidate_action_board_read_model_remote(_InvalidationConn())
    third = _get_board()

    assert len(calls) == 2
    assert first["directions"][0]["items"][0]["id"] == "1"
    assert second["directions"][0]["items"][0]["id"] == "1"
    assert third["directions"][0]["items"][0]["id"] == "2"


def test_actions_board_result_cache_ttl_default_and_env_override(monkeypatch):
    monkeypatch.delenv("INFO2ACTION_ACTIONS_BOARD_CACHE_TTL_SEC", raising=False)
    assert remote_db._actions_board_result_cache_ttl_sec() == 300

    monkeypatch.setenv("INFO2ACTION_ACTIONS_BOARD_CACHE_TTL_SEC", "45")
    assert remote_db._actions_board_result_cache_ttl_sec() == 45

    monkeypatch.setenv("INFO2ACTION_ACTIONS_BOARD_CACHE_TTL_SEC", "-5")
    assert remote_db._actions_board_result_cache_ttl_sec() == 0
