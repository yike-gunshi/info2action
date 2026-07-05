"""Tests for src/clustering/merge_detector.py.

Coverage:
- Single candidate → ACTION_REJECT (no trigger)
- 2+ candidates ≥ τ + LLM yes → ACTION_MERGE with target = max doc_count
- 2+ candidates ≥ τ + LLM no → ACTION_REJECT (no merge)
- LLM transport raises → ACTION_NEW_SINGLETON (R7.2 宁漏不错合)
- Malformed LLM JSON → not 'yes' → ACTION_REJECT
- Tie-break on doc_count → smallest cluster_id wins
- apply_merge wraps cluster_manage.merge_clusters
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from clustering import merge_detector as md  # noqa: E402


def _cand(cid, sim, dc=1, text=None):
    return md.CandidateCluster(
        cluster_id=cid, cosine_sim=sim, doc_count=dc,
        representative_text=text or f'cluster {cid} sample text',
    )


class TestDetectAndMerge:
    def test_single_candidate_rejects(self):
        decision = md.detect_and_merge(
            [_cand(1, 0.9)], tau_high=0.83,
            judge_fn=lambda **kw: '{"same_event": "yes"}',
        )
        assert decision.action == md.ACTION_REJECT

    def test_two_high_candidates_llm_yes_merges(self):
        cands = [_cand(1, 0.9, dc=5), _cand(2, 0.88, dc=2)]
        decision = md.detect_and_merge(
            cands, tau_high=0.83,
            judge_fn=lambda **kw: json.dumps({
                'same_event': 'yes', 'confidence': 'high', 'rationale': 'same launch'
            }),
        )
        assert decision.action == md.ACTION_MERGE
        assert decision.target_cluster_id == 1  # doc_count larger
        assert decision.source_cluster_ids == [2]

    def test_two_high_candidates_llm_no_rejects(self):
        cands = [_cand(1, 0.9, dc=5), _cand(2, 0.88, dc=2)]
        decision = md.detect_and_merge(
            cands, tau_high=0.83,
            judge_fn=lambda **kw: '{"same_event": "no", "rationale": "different products"}',
        )
        assert decision.action == md.ACTION_REJECT
        # Target/sources still attached for diagnostics
        assert decision.target_cluster_id == 1

    def test_llm_transport_failure_returns_new_singleton(self):
        cands = [_cand(1, 0.9, dc=5), _cand(2, 0.88, dc=2)]

        def boom(**kw):
            raise ConnectionError('boom')

        decision = md.detect_and_merge(
            cands, tau_high=0.83, judge_fn=boom,
        )
        assert decision.action == md.ACTION_NEW_SINGLETON
        assert 'transport' in decision.reason.lower()

    def test_llm_malformed_json_treated_as_no(self):
        cands = [_cand(1, 0.9, dc=5), _cand(2, 0.88, dc=2)]
        decision = md.detect_and_merge(
            cands, tau_high=0.83,
            judge_fn=lambda **kw: 'not-json-at-all',
        )
        assert decision.action == md.ACTION_REJECT

    def test_tiebreak_by_smallest_cluster_id(self):
        # Same doc_count → smaller id wins
        cands = [_cand(7, 0.9, dc=3), _cand(3, 0.88, dc=3)]
        decision = md.detect_and_merge(
            cands, tau_high=0.83,
            judge_fn=lambda **kw: '{"same_event": "yes"}',
        )
        assert decision.action == md.ACTION_MERGE
        assert decision.target_cluster_id == 3
        assert decision.source_cluster_ids == [7]

    def test_below_threshold_rejects(self):
        cands = [_cand(1, 0.5), _cand(2, 0.6)]
        decision = md.detect_and_merge(
            cands, tau_high=0.83,
            judge_fn=lambda **kw: '{"same_event": "yes"}',
        )
        assert decision.action == md.ACTION_REJECT


class TestApplyMerge:
    def test_apply_merge_invokes_cluster_manage(self, monkeypatch):
        captured = {}

        def fake_merge(conn, **kwargs):
            captured.update(kwargs)
            return {'sources_merged': len(kwargs.get('sources', []))}

        monkeypatch.setattr(
            'tools.cluster_manage.merge_clusters', fake_merge
        )
        out = md.apply_merge(
            conn=object(), target=1, sources=[2, 3],
            api_key='k', api_base='b', model='m',
        )
        assert out == {'sources_merged': 2}
        assert captured['target'] == 1
        assert captured['sources'] == [2, 3]
