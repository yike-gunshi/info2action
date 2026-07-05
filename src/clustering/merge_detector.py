"""Passive cluster merging (PRD R8 / RESEARCH §1.2).

Trigger: when a new doc embedding hits ≥ 2 candidate clusters with cosine
similarity ≥ TAU_HIGH, those clusters might describe the same event with
slightly different vector profiles. We invoke an LLM judge using
prompts/09_cluster_merge_decision.md to decide:

    - merge          : LLM says yes → fold sources into the target cluster
                       (target = highest doc_count, ties → smallest id)
    - reject         : LLM says no → leave clusters alone, attach new doc
                       to the highest-similarity cluster
    - new_singleton  : LLM error → R7.2/R8.2 宁漏不错合, fall back to a
                       brand-new singleton cluster for the new doc

This module exposes:
  - MergeDecision dataclass
  - detect_and_merge(...): returns MergeDecision; CALLER applies the change
  - apply_merge(...):      executes the merge atomically (shares cluster_manage logic)

The pipeline.run_pipeline integration is opt-in via `merge_check=True`
on the run kwargs; default keeps existing behavior to avoid surprising
changes to live data.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Callable, Sequence

import numpy as np

from clustering import pipeline as pipeline_mod
from clustering import summary_writer as sw_mod
from prompt_loader import load_prompt

logger = logging.getLogger('clustering.merge_detector')

ACTION_MERGE = 'merge'
ACTION_REJECT = 'reject'
ACTION_NEW_SINGLETON = 'new_singleton'


@dataclass
class CandidateCluster:
    cluster_id: int
    cosine_sim: float
    doc_count: int = 0
    representative_text: str = ''


@dataclass
class MergeDecision:
    action: str  # 'merge' | 'reject' | 'new_singleton'
    target_cluster_id: int | None = None
    source_cluster_ids: list[int] = field(default_factory=list)
    reason: str = ''
    raw_llm_output: str = ''


def _pick_target_and_sources(candidates: Sequence[CandidateCluster]) -> tuple[int, list[int]]:
    """target = max doc_count (ties → min cluster_id)."""
    sorted_c = sorted(
        candidates,
        key=lambda c: (-c.doc_count, c.cluster_id),
    )
    target = sorted_c[0].cluster_id
    sources = [c.cluster_id for c in sorted_c[1:]]
    return target, sources


def _judge_via_prompt(
    *,
    target_text: str,
    source_text: str,
    api_key: str,
    api_base: str | None,
    model: str,
    judge_fn: Callable[..., str] | None = None,
) -> tuple[bool, str, str]:
    """Call LLM with prompt 09 and parse `{"same_event": "yes"|"no", ...}`.

    Returns (is_same, reason, raw_output). Raises on transport/JSON failure.
    """
    system = load_prompt(
        '09_cluster_merge_decision.md',
        doc_a_content=target_text,
        doc_b_content=source_text,
        scenario='cluster_a_vs_cluster_b',
    ) or ''
    if judge_fn is not None:
        raw = judge_fn(
            api_key=api_key, api_base=api_base, model=model,
            system_prompt=system,
            user_content=f'Cluster A:\n{target_text}\n\nCluster B:\n{source_text}',
            max_tokens=256, timeout=30,
        )
    else:
        raw = sw_mod._call_llm_chat(
            api_key=api_key, api_base=api_base, model=model,
            system_prompt=system,
            user_content=f'Cluster A:\n{target_text}\n\nCluster B:\n{source_text}',
            max_tokens=256, timeout=30,
        )
    raw = (raw or '').strip()
    text = raw
    if text.startswith('```'):
        text = '\n'.join(ln for ln in text.splitlines() if not ln.startswith('```')).strip()
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return (False, f'invalid LLM JSON: {raw[:80]}', raw)
    if not isinstance(obj, dict):
        return (False, f'LLM did not return JSON object: {raw[:80]}', raw)
    same = (obj.get('same_event') or '').strip().lower() == 'yes'
    reason = (obj.get('rationale') or '').strip()[:200]
    return (same, reason, raw)


def detect_and_merge(
    candidates: Sequence[CandidateCluster],
    *,
    tau_high: float,
    api_key: str = '',
    api_base: str | None = None,
    model: str = '',
    judge_fn: Callable[..., str] | None = None,
) -> MergeDecision:
    """Decide whether to merge across candidate clusters.

    Triggers only when ≥ 2 candidates have cosine_sim >= tau_high.
    Otherwise returns action='reject' (no merge needed).

    On LLM failure (transport/JSON) → action='new_singleton' (R7.2/R8.2).
    """
    high = [c for c in candidates if c.cosine_sim >= tau_high]
    if len(high) < 2:
        return MergeDecision(action=ACTION_REJECT,
                             reason=f'only {len(high)} cluster(s) ≥ τ_high={tau_high}')

    target, sources = _pick_target_and_sources(high)
    target_text = next(c.representative_text for c in high if c.cluster_id == target)
    # For >2 clusters we judge target vs each source individually; if any one
    # comes back "yes" we still only merge that pair on this round (incremental).
    same_any = False
    reason_parts: list[str] = []
    last_raw = ''
    confirmed_sources: list[int] = []
    try:
        for c in high:
            if c.cluster_id == target:
                continue
            same, reason, raw = _judge_via_prompt(
                target_text=target_text, source_text=c.representative_text,
                api_key=api_key, api_base=api_base, model=model,
                judge_fn=judge_fn,
            )
            last_raw = raw
            reason_parts.append(f'#{c.cluster_id}:{ "yes" if same else "no" }/{reason}')
            if same:
                same_any = True
                confirmed_sources.append(c.cluster_id)
    except Exception as e:
        logger.warning('LLM judge transport failed: %s', e)
        return MergeDecision(
            action=ACTION_NEW_SINGLETON,
            reason=f'LLM transport error: {e!r}',
        )

    if same_any:
        return MergeDecision(
            action=ACTION_MERGE,
            target_cluster_id=target,
            source_cluster_ids=confirmed_sources,
            reason='; '.join(reason_parts),
            raw_llm_output=last_raw,
        )
    return MergeDecision(
        action=ACTION_REJECT,
        target_cluster_id=target,
        source_cluster_ids=sources,
        reason='LLM rejected all pairs: ' + '; '.join(reason_parts),
        raw_llm_output=last_raw,
    )


def apply_merge(conn, *, target: int, sources: list[int],
                api_key: str = '', api_base: str | None = None,
                model: str = '', skip_summary: bool = False) -> dict:
    """Execute the merge atomically. Wraps tools.cluster_manage.merge_clusters
    so callers don't need to import the tools layer.
    """
    # Local import to avoid coupling clustering/ → tools/
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
    from tools import cluster_manage  # noqa: E402

    return cluster_manage.merge_clusters(
        conn, target=target, sources=sources,
        api_key=api_key, api_base=api_base, model=model,
        skip_summary=skip_summary,
    )
