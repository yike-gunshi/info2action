"""Tests for tools/cluster_eval/{prepare_pairs,label_cli,run_shootout}.py.

Coverage:
- prepare_pairs.build_pairs against an in-memory feed.db fixture
- prepare_pairs falls back to placeholders when DB missing
- label_cli round-trips load + label + save
- run_shootout sweep + recommendation matches a hand-computed scenario
- run_shootout boundary (0 labels / 100% same / 100% different)
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

# Allow imports under tools.* + src.* without installing the project.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from tools.cluster_eval import prepare_pairs, label_cli, run_shootout  # noqa: E402
from clustering import embedding_provider as ep_mod  # noqa: E402


def _make_fixture_db(tmp_path: Path) -> Path:
    db = tmp_path / 'fixture.db'
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE items (
            id TEXT PRIMARY KEY,
            platform TEXT,
            title TEXT,
            content TEXT,
            ai_summary TEXT,
            ai_keywords TEXT,
            published_at TEXT,
            fetched_at TEXT
        )
    """)
    rows = [
        # Same platform + 2+ kw overlap → category A candidates
        ('id1', 'rss', 'A1 LLM scaling laws', 'long body 1' * 30, None,
         json.dumps(['LLM', 'scaling laws', 'training']), '2026-04-20T10:00:00Z', '2026-04-20T10:01:00Z'),
        ('id2', 'rss', 'A2 LLM scaling deep dive', 'long body 2' * 30, None,
         json.dumps(['LLM', 'scaling laws', 'inference']), '2026-04-20T11:00:00Z', '2026-04-20T11:01:00Z'),
        ('id3', 'rss', 'A3 Different topic', 'unrelated body' * 30, None,
         json.dumps(['cooking', 'recipes']), '2026-04-20T12:00:00Z', '2026-04-20T12:01:00Z'),
        # Same platform B (twitter) within 6h → category B candidates
        ('id4', 'twitter', 'B1 Apple event tonight', 'live stream' * 30, None,
         json.dumps(['Apple', 'event']), '2026-04-21T18:00:00Z', '2026-04-21T18:01:00Z'),
        ('id5', 'twitter', 'B2 Apple keynote starts', 'commentary' * 30, None,
         json.dumps(['Apple', 'keynote']), '2026-04-21T19:30:00Z', '2026-04-21T19:31:00Z'),
        ('id6', 'twitter', 'B3 Three days later', 'unrelated' * 30, None,
         json.dumps(['unrelated']), '2026-04-24T19:30:00Z', '2026-04-24T19:31:00Z'),
        # Cross-platform with kw overlap → category C
        ('id7', 'rss', 'C1 GPT-5 RAG paper', 'paper body' * 30, None,
         json.dumps(['GPT-5', 'RAG']), '2026-04-22T09:00:00Z', '2026-04-22T09:01:00Z'),
        ('id8', 'youtube', 'C2 GPT-5 explained', 'video summary' * 30, None,
         json.dumps(['GPT-5', 'tutorial']), '2026-04-22T10:00:00Z', '2026-04-22T10:01:00Z'),
        ('id9', 'twitter', 'C3 GPT-5 thread', 'thread body' * 30, None,
         json.dumps(['GPT-5', 'analysis']), '2026-04-22T11:00:00Z', '2026-04-22T11:01:00Z'),
    ]
    conn.executemany(
        "INSERT INTO items (id, platform, title, content, ai_summary, ai_keywords, "
        "published_at, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db


class TestPreparePairs:
    def test_build_pairs_real_db(self, tmp_path):
        db = _make_fixture_db(tmp_path)
        records, counts = prepare_pairs.build_pairs(
            db, target_a=2, target_b=1, target_c=2, seed=1,
        )
        assert all(r['source'] == 'heuristic' for r in records)
        assert counts['A'] >= 1  # at least id1+id2 share LLM + scaling laws
        assert counts['B'] >= 1  # id4 + id5 within 6h on twitter
        assert counts['C'] >= 1  # id7+id8 or id8+id9 cross-platform GPT-5

    def test_build_pairs_missing_db_returns_placeholders(self, tmp_path):
        records, counts = prepare_pairs.build_pairs(
            tmp_path / 'nope.db',
            target_a=2, target_b=2, target_c=2, seed=1,
        )
        assert all(r['source'] == 'placeholder' for r in records)
        assert len(records) == 6

    def test_pair_record_shape(self, tmp_path):
        db = _make_fixture_db(tmp_path)
        records, _ = prepare_pairs.build_pairs(db, target_a=2, target_b=0, target_c=0, seed=1)
        assert records, 'expected at least one A pair'
        rec = records[0]
        for key in ('doc_a_id', 'doc_b_id', 'doc_a_title', 'doc_b_title',
                    'doc_a_content_preview', 'doc_b_content_preview',
                    'label', 'source', 'category'):
            assert key in rec
        assert rec['label'] is None

    def test_write_jsonl_roundtrip(self, tmp_path):
        records = [
            {'doc_a_id': 'a', 'doc_b_id': 'b', 'label': None, 'category': 'A',
             'source': 'heuristic', 'doc_a_title': 'a', 'doc_b_title': 'b',
             'doc_a_content_preview': '', 'doc_b_content_preview': ''},
        ]
        out = tmp_path / 'out' / 'pairs.jsonl'
        prepare_pairs.write_jsonl(records, out)
        assert out.exists()
        loaded = label_cli._load(out)
        assert loaded == records


class TestLabelCli:
    def test_label_session_y_n_s_q(self, tmp_path):
        path = tmp_path / 'pairs.jsonl'
        records = [
            {'doc_a_id': f'a{i}', 'doc_b_id': f'b{i}', 'label': None,
             'category': 'A', 'source': 'heuristic',
             'doc_a_title': f'A{i}', 'doc_b_title': f'B{i}',
             'doc_a_content_preview': '', 'doc_b_content_preview': '',
             'doc_a_platform': 'rss', 'doc_b_platform': 'rss'}
            for i in range(4)
        ]
        prepare_pairs.write_jsonl(records, path)
        loaded = label_cli._load(path)
        answers = iter(['y', 'n', 's', 'q'])
        counts = label_cli.label_session(loaded, path, prompt_fn=lambda _p: next(answers))
        assert counts['y'] == 1
        assert counts['n'] == 1
        assert counts['s'] == 1
        assert counts['q'] == 1
        # Re-load from disk to ensure save happened
        on_disk = label_cli._load(path)
        assert on_disk[0]['label'] == 1
        assert on_disk[1]['label'] == 0
        assert on_disk[2]['label'] is None  # skipped
        assert on_disk[3]['label'] is None  # quit before reaching


class TestRunShootout:
    def _build_labeled_pairs(self):
        # Two pairs: (a,b) same vector, label=1 → should be detected as same;
        #           (c,d) different vectors, label=0 → should be detected diff.
        return [
            {'doc_a_id': 'a', 'doc_b_id': 'b', 'doc_a_title': 'same alpha',
             'doc_b_title': 'same alpha', 'doc_a_content_preview': '',
             'doc_b_content_preview': '', 'label': 1, 'source': 'heuristic',
             'category': 'A'},
            {'doc_a_id': 'c', 'doc_b_id': 'd',
             'doc_a_title': 'completely different x',
             'doc_b_title': 'totally unrelated zebra',
             'doc_a_content_preview': '', 'doc_b_content_preview': '',
             'label': 0, 'source': 'heuristic', 'category': 'C'},
        ]

    def test_compute_similarities_via_fake_provider(self):
        provider = ep_mod.FakeEmbeddingProvider()
        pairs = self._build_labeled_pairs()
        sims = run_shootout.compute_similarities(pairs, provider)
        assert len(sims) == 2
        # Identical text → cosine ≈ 1
        assert sims[0] == pytest.approx(1.0, abs=1e-3)
        # Different text → strictly less than 1, may still be > 0 with fake provider
        assert sims[1] < 1.0

    def test_sweep_and_recommend(self):
        sims = [0.95, 0.20]
        labels = [1, 0]
        rows = run_shootout.sweep_thresholds(sims, labels, [0.10, 0.50, 0.90])
        # threshold=0.50 → tp=1, fp=0, fn=0, tn=1 → P=1 R=1 F1=1
        mid = next(r for r in rows if r['threshold'] == 0.50)
        assert mid['tp'] == 1 and mid['fp'] == 0 and mid['fn'] == 0 and mid['tn'] == 1
        assert mid['f1'] == pytest.approx(1.0)
        rec = run_shootout.recommend(rows)
        assert rec['TAU_HIGH'] == 0.50

    def test_zero_labels_safe(self):
        sims = [0.95, 0.20]
        labels = [None, None]
        rows = run_shootout.sweep_thresholds(sims, labels, [0.50])
        assert rows[0]['tp'] == 0 and rows[0]['fp'] == 0
        assert rows[0]['f1'] == 0.0

    def test_all_same(self):
        sims = [0.95, 0.96]
        labels = [1, 1]
        rows = run_shootout.sweep_thresholds(sims, labels, [0.50, 0.99])
        assert rows[0]['tp'] == 2 and rows[0]['fn'] == 0
        assert rows[0]['recall'] == 1.0
        assert rows[1]['recall'] == 0.0

    def test_all_different(self):
        sims = [0.10, 0.20]
        labels = [0, 0]
        rows = run_shootout.sweep_thresholds(sims, labels, [0.05, 0.50])
        # threshold 0.05: tp=0, fp=2 → P=0, R=0/0=0
        assert rows[0]['tp'] == 0 and rows[0]['fp'] == 2
        assert rows[0]['recall'] == 0.0

    def test_render_markdown_smoke(self):
        rows = run_shootout.sweep_thresholds([0.9, 0.1], [1, 0], [0.5])
        rec = run_shootout.recommend(rows)
        md = run_shootout.render_markdown(
            provider_name='fake-test', total_pairs=2, labeled=2,
            unlabeled=0, rows=rows, rec=rec,
        )
        assert '阈值矩阵' in md
        assert '推荐阈值' in md
        assert 'fake-test' in md
