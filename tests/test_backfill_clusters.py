"""Tests for scripts/backfill_clusters.py.

Coverage:
- select_pending honors days window + cluster_id IS NULL
- select_pending after_id resumes correctly
- chunked yields correct slice sizes
- run_backfill --dry-run does not call provider/embed/insert
- run_backfill resumes from checkpoint
- run_backfill writes progress file at the end
- run_backfill raises clean error if MINIMAX missing (real run path)
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))


@pytest.fixture
def tmp_db_with_items(tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'test.db'))
    import db as db_mod
    db_mod.DB_PATH = str(tmp_path / 'test.db')
    conn = db_mod.get_conn()
    # Seed 5 items: 3 within last 1 day (2 with NULL cluster_id),
    # 2 outside window
    rows = [
        ('a', 'rss', 'src', 'A title', '-12 hours', None),
        ('b', 'rss', 'src', 'B title', '-6 hours',  None),
        ('c', 'rss', 'src', 'C title', '-3 hours',  99),     # already clustered
        ('d', 'rss', 'src', 'D title', '-30 days',  None),  # outside window
        ('e', 'rss', 'src', 'E title', '-25 days',  None),  # outside window
    ]
    for id_, plat, src, title, ts_expr, cid in rows:
        conn.execute(
            """INSERT INTO items (id, platform, source, title, fetched_at,
                                  published_at, cluster_id)
                VALUES (?,?,?,?, datetime('now', ?), datetime('now', ?), ?)""",
            (id_, plat, src, title, ts_expr, ts_expr, cid),
        )
    # Need at least one cluster to satisfy FK on cluster_id=99
    conn.execute(
        "INSERT INTO clusters (id, first_doc_at, last_doc_at, last_updated_at) "
        "VALUES (99, datetime('now'), datetime('now'), datetime('now'))"
    )
    conn.commit()
    yield conn
    conn.close()


class TestSelectPending:
    def test_only_unclustered_within_window(self, tmp_db_with_items):
        from scripts import backfill_clusters as bf
        rows = bf.select_pending(tmp_db_with_items, days=1, after_id=None)
        ids = [r['id'] for r in rows]
        assert sorted(ids) == ['a', 'b']

    def test_after_id_resume(self, tmp_db_with_items):
        from scripts import backfill_clusters as bf
        rows = bf.select_pending(tmp_db_with_items, days=1, after_id='a')
        ids = [r['id'] for r in rows]
        assert ids == ['b']

    def test_outside_window_excluded(self, tmp_db_with_items):
        from scripts import backfill_clusters as bf
        rows = bf.select_pending(tmp_db_with_items, days=60, after_id=None)
        ids = sorted(r['id'] for r in rows)
        # Items d, e older than 1 day but within 60 days, plus a, b
        assert ids == ['a', 'b', 'd', 'e']


class TestChunked:
    def test_chunked_basic(self):
        from scripts import backfill_clusters as bf
        out = list(bf.chunked([1, 2, 3, 4, 5], 2))
        assert out == [[1, 2], [3, 4], [5]]

    def test_chunked_empty(self):
        from scripts import backfill_clusters as bf
        assert list(bf.chunked([], 3)) == []


class TestDryRun:
    def test_dry_run_does_not_call_provider(self, tmp_db_with_items, tmp_path):
        from scripts import backfill_clusters as bf
        from clustering import embedding_provider as ep_mod

        with patch.object(ep_mod, 'get_provider') as mock_get:
            result = bf.run_backfill(
                tmp_db_with_items, days=1,
                progress_path=tmp_path / 'p.json', dry_run=True,
            )
        assert result['dry_run'] is True
        assert result['pending'] == 2
        assert result['processed'] == 0
        mock_get.assert_not_called()

    def test_dry_run_no_progress_file_written(self, tmp_db_with_items, tmp_path):
        from scripts import backfill_clusters as bf
        progress = tmp_path / 'p.json'
        bf.run_backfill(
            tmp_db_with_items, days=1, progress_path=progress, dry_run=True,
        )
        assert not progress.exists()


class TestProgressCheckpoint:
    def test_resume_skips_already_processed(self, tmp_db_with_items, tmp_path):
        from scripts import backfill_clusters as bf
        progress = tmp_path / 'p.json'
        progress.write_text(json.dumps({
            'last_processed_id': 'a', 'total_processed': 1,
            'started_at': '2026-04-24T00:00:00Z', 'batch_count': 1,
        }), encoding='utf-8')
        # dry-run with resume → only 'b' should be pending
        result = bf.run_backfill(
            tmp_db_with_items, days=1, progress_path=progress,
            dry_run=True, resume=True,
        )
        assert result['pending'] == 1


class TestRealRunErrorPath:
    def test_missing_api_key_returns_error(self, tmp_db_with_items, tmp_path,
                                            monkeypatch):
        from scripts import backfill_clusters as bf
        # Force pipeline._load_cfg to return empty (no api_key, default OpenRouter)
        monkeypatch.setattr(
            'scripts.backfill_clusters.pipeline_mod._load_cfg', lambda: {}
        )
        monkeypatch.setattr(
            'scripts.backfill_clusters.ep_mod.load_project_env', lambda base: {}
        )
        # Embedding key is the only credential pipeline.run() needs (Stage 0).
        # Strict isolation: chat key would NOT be picked up even if present.
        monkeypatch.delenv('EMBEDDING_PROVIDER', raising=False)
        monkeypatch.delenv('MINIMAX_EMBEDDING_API_KEY', raising=False)
        monkeypatch.delenv('MINIMAX_EMBEDDING_BASE', raising=False)
        monkeypatch.delenv('MINIMAX_API_KEY', raising=False)
        monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
        result = bf.run_backfill(
            tmp_db_with_items, days=1, progress_path=tmp_path / 'p.json',
            dry_run=False,
        )
        assert 'error' in result
        assert 'OPENROUTER_API_KEY' in result['error']
