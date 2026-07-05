from __future__ import annotations

import sqlite3
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "scripts"))

import run_ready_backfill_windows as runner  # noqa: E402


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE items (
             id TEXT PRIMARY KEY,
             published_at TEXT,
             cluster_id INTEGER,
             embedding BLOB,
             ai_summary TEXT,
             ai_category TEXT,
             ai_categories TEXT,
             ai_quality_score INTEGER
           )"""
    )
    conn.execute(
        """CREATE TABLE clusters (
             id INTEGER PRIMARY KEY,
             last_touched_run_id INTEGER,
             published_run_id INTEGER,
             archived INTEGER DEFAULT 0,
             merged_into INTEGER,
             ai_title TEXT,
             ai_summary TEXT,
             ai_key_points TEXT,
             ai_title_draft TEXT,
             ai_summary_draft TEXT,
             ai_key_points_draft TEXT,
             pending_is_visible_in_feed INTEGER,
             unique_source_count INTEGER DEFAULT 1
           )"""
    )
    conn.execute("CREATE TABLE cluster_items (cluster_id INTEGER, item_id TEXT)")
    return conn


def _insert_ready(conn, item_id: str, published_at: str, *, cluster_id=None, category="products"):
    conn.execute(
        """INSERT INTO items (
             id, published_at, cluster_id, embedding, ai_summary, ai_category,
             ai_categories, ai_quality_score
           ) VALUES (?, ?, ?, ?, 'summary', ?, ?, 80)""",
        (item_id, published_at, cluster_id, b"vec", category, f'["{category}"]'),
    )


def test_build_window_plan_splits_large_six_hour_bucket_into_hours():
    conn = _conn()
    _insert_ready(conn, "recent-small", "2026-05-10T23:10:00+00:00")
    for idx in range(4):
        _insert_ready(conn, f"busy-{idx}", "2026-05-10T17:30:00+00:00")
    conn.commit()

    plan = runner.build_window_plan(
        conn,
        since=datetime(2026, 5, 10, 12, tzinfo=timezone.utc),
        until=datetime(2026, 5, 11, 0, tzinfo=timezone.utc),
        window_hours=6,
        split_threshold=3,
        split_hours=1,
    )

    assert plan[0].ready_count == 1
    split_windows = [window for window in plan if window.split_from_hours == 6]
    assert len(split_windows) == 6
    assert any(window.ready_count == 4 for window in split_windows)


def test_count_ready_candidates_excludes_other_category():
    conn = _conn()
    _insert_ready(conn, "product", "2026-05-10T17:30:00+00:00")
    _insert_ready(conn, "other", "2026-05-10T17:35:00+00:00", category="other")
    conn.commit()

    count = runner.count_ready_candidates(
        conn,
        datetime(2026, 5, 10, 17, tzinfo=timezone.utc),
        datetime(2026, 5, 10, 18, tzinfo=timezone.utc),
    )

    assert count == 1


def test_count_run_draft_clusters_keeps_resume_windows_runnable():
    conn = _conn()
    _insert_ready(conn, "draft-item", "2026-05-10T17:30:00+00:00", cluster_id=10)
    conn.execute(
        """INSERT INTO clusters (
             id, last_touched_run_id, published_run_id, ai_title_draft
           ) VALUES (10, 1094, NULL, 'draft title')"""
    )
    conn.execute("INSERT INTO cluster_items (cluster_id, item_id) VALUES (10, 'draft-item')")
    conn.commit()

    count = runner.count_run_draft_clusters(
        conn,
        1094,
        datetime(2026, 5, 10, 17, tzinfo=timezone.utc),
        datetime(2026, 5, 10, 18, tzinfo=timezone.utc),
    )

    assert count == 1


def test_build_window_plan_splits_large_draft_bucket_into_hours():
    conn = _conn()
    for idx in range(4):
        _insert_ready(conn, f"draft-item-{idx}", "2026-05-10T17:30:00+00:00", cluster_id=idx + 1)
        conn.execute(
            """INSERT INTO clusters (
                 id, last_touched_run_id, published_run_id, ai_title_draft
               ) VALUES (?, 1094, NULL, 'draft title')""",
            (idx + 1,),
        )
        conn.execute(
            "INSERT INTO cluster_items (cluster_id, item_id) VALUES (?, ?)",
            (idx + 1, f"draft-item-{idx}"),
        )
    conn.commit()

    plan = runner.build_window_plan(
        conn,
        since=datetime(2026, 5, 10, 12, tzinfo=timezone.utc),
        until=datetime(2026, 5, 11, 0, tzinfo=timezone.utc),
        window_hours=6,
        split_threshold=3,
        split_hours=1,
        run_id=1094,
    )

    split_windows = [window for window in plan if window.split_from_hours == 6]
    assert len(split_windows) == 6
    assert any(window.draft_count == 4 for window in split_windows)


def test_build_backfill_cmd_uses_ready_only_full_window_flags():
    args = types.SimpleNamespace(
        run_id=1094,
        top_k=5,
        judge_workers=1,
        judge_min_interval_sec=6.0,
        summary_workers=3,
        ai_timeout=7200,
        cluster_timeout=7200,
    )
    window = runner.WindowPlan(
        datetime(2026, 5, 10, 17, tzinfo=timezone.utc),
        datetime(2026, 5, 10, 18, tzinfo=timezone.utc),
        ready_count=42,
    )

    cmd = runner.build_backfill_cmd(args, window)

    assert "--run-id" in cmd
    assert cmd[cmd.index("--run-id") + 1] == "1094"
    assert "--ready-cluster-only" in cmd
    assert "--skip-fetch" in cmd
    assert "--window-require-published-at" in cmd
    assert cmd[cmd.index("--process-window-days") + 1] == "0"
    assert cmd[cmd.index("--process-window-hours") + 1] == "1"
    assert cmd[cmd.index("--top-k") + 1] == "5"
    assert cmd[cmd.index("--judge-workers") + 1] == "1"
    assert cmd[cmd.index("--summary-workers") + 1] == "3"
