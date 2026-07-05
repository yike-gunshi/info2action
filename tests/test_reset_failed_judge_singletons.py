from __future__ import annotations

import importlib.util
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import db as db_mod  # noqa: E402
from clustering import vector_utils as vu  # noqa: E402


def _load_script():
    path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "scripts",
        "reset_failed_judge_singletons.py",
    )
    spec = importlib.util.spec_from_file_location("reset_failed_judge_singletons", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_reset_failed_judge_singletons_restores_pending_item(monkeypatch, tmp_path):
    monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "feed.db"))
    conn = db_mod.get_conn()
    conn.execute(
        """INSERT INTO clusters (
               id, first_doc_at, last_updated_at, representative_vector,
               doc_count, live_version, created_run_id, last_touched_run_id
           ) VALUES (10, datetime('now'), datetime('now'), ?, 1, 0, 77, 77)""",
        (vu.pack_blob(np.array([1, 0, 0], dtype=np.float32)),),
    )
    conn.execute(
        """INSERT INTO items (
               id, platform, source, fetched_at, published_at, content,
               ai_summary, embedding, fetch_run_id, cluster_id
           ) VALUES (
               'failed-item', 'twitter', 'unit',
               '2026-05-10T01:00:00+00:00',
               '2026-05-10T01:00:00+00:00',
               'body', 'summary', ?, 77, 10
           )""",
        (vu.pack_blob(np.array([1, 0, 0], dtype=np.float32)),),
    )
    conn.execute(
        "INSERT INTO cluster_items (cluster_id, item_id, is_primary_source) VALUES (10, 'failed-item', 1)"
    )
    conn.execute(
        """INSERT INTO cluster_judge_log (
               item_id, candidate_cluster_ids, selection_reason, decision_model
           ) VALUES ('failed-item', '[1]', 'llm-failed-fallback-singleton', 'm')"""
    )
    conn.commit()

    script = _load_script()
    stats = script.reset_failed_judge_singletons(conn, run_id=77)

    assert stats["items_reset"] == 1
    assert stats["singleton_clusters_deleted"] == 1
    assert stats["judge_logs_deleted"] == 1
    row = conn.execute("SELECT cluster_id FROM items WHERE id='failed-item'").fetchone()
    assert row["cluster_id"] is None
    assert conn.execute("SELECT COUNT(*) FROM clusters WHERE id=10").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM cluster_items WHERE item_id='failed-item'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM cluster_judge_log WHERE item_id='failed-item'").fetchone()[0] == 0
    conn.close()


def test_reset_failed_judge_singletons_dry_run_does_not_mutate(monkeypatch, tmp_path):
    monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "feed.db"))
    conn = db_mod.get_conn()
    conn.execute(
        """INSERT INTO clusters (id, first_doc_at, last_updated_at, doc_count, live_version)
           VALUES (11, datetime('now'), datetime('now'), 1, 0)"""
    )
    conn.execute(
        """INSERT INTO items (
               id, platform, source, fetched_at, published_at, content,
               ai_summary, fetch_run_id, cluster_id
           ) VALUES (
               'failed-item', 'twitter', 'unit',
               '2026-05-10T01:00:00+00:00',
               '2026-05-10T01:00:00+00:00',
               'body', 'summary', 77, 11
           )"""
    )
    conn.execute(
        "INSERT INTO cluster_items (cluster_id, item_id, is_primary_source) VALUES (11, 'failed-item', 1)"
    )
    conn.execute(
        """INSERT INTO cluster_judge_log (
               item_id, candidate_cluster_ids, selection_reason, decision_model
           ) VALUES ('failed-item', '[1]', 'llm-failed-fallback-singleton', 'm')"""
    )
    conn.commit()

    script = _load_script()
    stats = script.reset_failed_judge_singletons(conn, run_id=77, dry_run=True)

    assert stats["items_reset"] == 1
    assert conn.execute("SELECT cluster_id FROM items WHERE id='failed-item'").fetchone()[0] == 11
    assert conn.execute("SELECT COUNT(*) FROM clusters WHERE id=11").fetchone()[0] == 1
    conn.close()
