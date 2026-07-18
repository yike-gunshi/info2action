from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import re
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import highlight_score_v26  # noqa: E402
from clustering import pipeline as pl  # noqa: E402


@pytest.fixture()
def qualify_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE clusters (
            id INTEGER PRIMARY KEY,
            pending_is_visible_in_feed INTEGER,
            pending_summary_warnings_json TEXT,
            last_touched_run_id INTEGER
        );
        CREATE TABLE items (
            id TEXT PRIMARY KEY,
            highlight_include_in_highlights INTEGER
        );
        CREATE TABLE cluster_items (
            cluster_id INTEGER NOT NULL,
            item_id TEXT NOT NULL
        );
        """
    )
    yield conn
    conn.close()


def _insert_cluster(conn, cluster_id, includes):
    conn.execute("INSERT INTO clusters (id) VALUES (?)", (cluster_id,))
    for offset, include in enumerate(includes):
        item_id = f"{cluster_id}-{offset}"
        conn.execute(
            "INSERT INTO items (id, highlight_include_in_highlights) VALUES (?, ?)",
            (item_id, include),
        )
        conn.execute(
            "INSERT INTO cluster_items (cluster_id, item_id) VALUES (?, ?)",
            (cluster_id, item_id),
        )
    conn.commit()


def test_clusters_meeting_bar_returns_empty_set_for_empty_input(qualify_db):
    assert pl._clusters_meeting_bar(qualify_db, []) == set()


def test_clusters_meeting_bar_returns_all_qualified_clusters(qualify_db):
    _insert_cluster(qualify_db, 1, [1])
    _insert_cluster(qualify_db, 2, [0, 1])

    assert pl._clusters_meeting_bar(qualify_db, {2, 1}) == {1, 2}


def test_clusters_meeting_bar_returns_only_qualified_clusters(qualify_db):
    _insert_cluster(qualify_db, 1, [0, 1])
    _insert_cluster(qualify_db, 2, [0])

    assert pl._clusters_meeting_bar(qualify_db, [1, 2]) == {1}


def test_clusters_meeting_bar_treats_all_null_verdicts_as_unqualified(qualify_db):
    _insert_cluster(qualify_db, 1, [None, None])

    assert pl._clusters_meeting_bar(qualify_db, [1]) == set()


def test_qualify_gate_defaults_to_disabled_without_filtering_or_hiding(
    monkeypatch,
    qualify_db,
):
    monkeypatch.delenv("INFO2ACTION_HIGHLIGHTS_QUALIFY_GATE", raising=False)

    def fail_if_queried(*_args, **_kwargs):
        raise AssertionError("disabled gate must not query qualification")

    monkeypatch.setattr(pl, "_clusters_meeting_bar", fail_if_queried)

    assert pl._apply_highlights_qualify_gate(
        qualify_db,
        [2, 1],
        run_id=42,
        remote_cluster_backend=False,
    ) == [2, 1]


def test_enabled_local_gate_hides_and_removes_unqualified_cluster(
    monkeypatch,
    qualify_db,
):
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_QUALIFY_GATE", "1")
    monkeypatch.setattr(pl.remote_db, "cluster_to_remote", lambda: False)
    _insert_cluster(qualify_db, 1, [1])
    _insert_cluster(qualify_db, 2, [0, None])

    result = pl._apply_highlights_qualify_gate(
        qualify_db,
        [2, 1],
        run_id=42,
        remote_cluster_backend=False,
    )

    assert result == [1]
    hidden = qualify_db.execute(
        """SELECT pending_is_visible_in_feed,
                  pending_summary_warnings_json,
                  last_touched_run_id
             FROM clusters
            WHERE id = 2"""
    ).fetchone()
    assert hidden["pending_is_visible_in_feed"] == 0
    assert json.loads(hidden["pending_summary_warnings_json"]) == ["未达标：无举旗成员"]
    assert hidden["last_touched_run_id"] == 42


def test_enabled_remote_gate_uses_remote_hidden_writer(monkeypatch):
    calls = []
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_QUALIFY_GATE", "1")
    monkeypatch.setattr(pl, "_clusters_meeting_bar_remote", lambda _ids: {2})
    monkeypatch.setattr(
        pl.remote_db,
        "mark_cluster_hidden_remote",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    result = pl._apply_highlights_qualify_gate(
        None,
        [3, 2],
        run_id=None,
        remote_cluster_backend=True,
    )

    assert result == [2]
    assert calls == [
        (
            (None, 3),
            {
                "warning": "未达标：无举旗成员",
                "publish_immediately": True,
                "run_id": None,
            },
        )
    ]


def test_clusters_meeting_bar_remote_uses_exists_and_schema_prefix(monkeypatch):
    captured = {}

    class FakeResult:
        def fetchall(self):
            return [{"id": 9}]

    class FakeConn:
        def execute(self, sql, params):
            captured["sql"] = sql
            captured["params"] = params
            return FakeResult()

    class FakeConnect:
        def __enter__(self):
            return FakeConn()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(pl.remote_db, "connect", lambda: FakeConnect())
    monkeypatch.setattr(pl.remote_db, "remote_schema", lambda: "test_schema")

    assert pl._clusters_meeting_bar_remote([9, 4]) == {9}
    normalized_sql = " ".join(captured["sql"].split())
    assert "FROM test_schema.clusters c" in normalized_sql
    assert "EXISTS (" in normalized_sql
    assert "JOIN test_schema.items i" in normalized_sql
    assert "i.highlight_include_in_highlights IS TRUE" in normalized_sql
    assert captured["params"] == (4, 9)


def test_prompt_version_matches_v26_prompt_header():
    prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "15_item_score_v26.md"
    header = prompt_path.read_text(encoding="utf-8")[:500]
    match = re.search(r"`(item_score_v26[^`]+)`", header)
    assert match, "prompt header must declare a version"
    assert highlight_score_v26.PROMPT_VERSION == match.group(1)
