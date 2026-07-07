"""Two-stage incremental event-clustering pipeline (v15.0).

Stages (PRD §4.8 技术方案):
  Stage 0: embed unembedded items via configured provider (batch)
  Stage 1: for each newly embedded item, cosine similarity vs every
           recently-active cluster's representative_vector (candidate window:
           last_updated_at > NOW() - 30 days)
  Stage 2: LLM judge on boundary matches (0.70-0.85); >=0.85 is auto-join
  Stage 3: update representative_vector (weighted mean + τ=24h decay)
  Stage 4: if cluster passes the BF-0501-1 latest-events candidate policy,
           regenerate summary/title via summary_writer.regenerate_and_swap()

Pipeline run is idempotent per item (items with embedding & cluster_id skip Stage 0/1).

Invoked from ops/fetch_all.sh after enrich_items step:
    cd $BASE && python3 -m src.clustering.pipeline || true
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import argparse
import urllib.error
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

# Allow running as `python3 src/clustering/pipeline.py` directly: expose src/
# on sys.path so top-level modules `db` / `prompt_loader` resolve.
_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import numpy as np

from clustering import embedding_provider as ep_mod
from clustering import event_text as event_text_mod
from clustering import summary_writer
from clustering import visibility_policy
from clustering import vector_utils as vu
from env_utils import load_project_env
from prompt_loader import load_prompt
from time_utils import parse_datetime, to_utc_iso

import db
import ai_provider_guard
import remote_db

logger = logging.getLogger('clustering.pipeline')

# Batching: OpenRouter text-embedding-3-small accepts batched inputs; keep conservative.
_EMBED_BATCH = 16
_CANDIDATE_WINDOW_DAYS_DEFAULT = 30
_TEMPORAL_ADJACENCY_DAYS_DEFAULT = 3.0
_MAX_MERGED_SPAN_DAYS_DEFAULT = 7.0
_GRAY_RECALL_MAX_TEMPORAL_HOURS_DEFAULT = 2.0
_RUN_ITEMS_SCOPE_TAGGED = 'tagged'
_RUN_ITEMS_SCOPE_INSERTED = 'inserted'
_RUN_ITEMS_SCOPE_CHOICES = (_RUN_ITEMS_SCOPE_TAGGED, _RUN_ITEMS_SCOPE_INSERTED)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_EMBEDDING_CLUSTERING_CONFIG_PATH = _PROJECT_ROOT / 'config' / 'embedding_clustering.json'
_CLUSTER_ITEM_RETRY_LIMIT_DEFAULT = 200
_CLUSTER_SUMMARY_RETRY_LIMIT_DEFAULT = 100
_CLUSTER_RETRY_LOOKBACK_HOURS_DEFAULT = 72.0


def _run_item_scope_sql(run_id: int | None, run_items_scope: str = _RUN_ITEMS_SCOPE_TAGGED) -> tuple[str, list]:
    if run_id is None:
        return "", []
    if run_items_scope == _RUN_ITEMS_SCOPE_INSERTED:
        return (
            """ AND EXISTS (
                    SELECT 1
                      FROM fetch_run_items fri
                     WHERE fri.run_id = ?
                       AND fri.item_id = items.id
                       AND fri.was_inserted = 1
                  )""",
            [run_id],
        )
    if run_items_scope != _RUN_ITEMS_SCOPE_TAGGED:
        raise ValueError(f"Unsupported run_items_scope={run_items_scope!r}")
    return " AND fetch_run_id = ?", [run_id]


def _remote_run_item_scope_sql(
    run_id: int | None,
    run_items_scope: str = _RUN_ITEMS_SCOPE_TAGGED,
    *,
    item_alias: str = "items",
) -> tuple[str, list[Any]]:
    if run_id is None:
        return "", []
    if run_items_scope == _RUN_ITEMS_SCOPE_INSERTED:
        return (
            f""" AND EXISTS (
                    SELECT 1
                      FROM {remote_db.remote_schema()}.fetch_run_items fri
                     WHERE fri.run_id = %s
                       AND fri.item_id = {item_alias}.id
                       AND fri.was_inserted = 1
                  )""",
            [run_id],
        )
    if run_items_scope != _RUN_ITEMS_SCOPE_TAGGED:
        raise ValueError(f"Unsupported run_items_scope={run_items_scope!r}")
    return f" AND {item_alias}.fetch_run_id = %s", [run_id]


def _run_item_exclusion_sql(
    run_id: int | None,
    run_items_scope: str = _RUN_ITEMS_SCOPE_TAGGED,
    *,
    item_alias: str = "items",
) -> tuple[str, list]:
    if run_id is None:
        return "", []
    if run_items_scope == _RUN_ITEMS_SCOPE_INSERTED:
        return (
            f""" AND NOT EXISTS (
                    SELECT 1
                      FROM fetch_run_items fri_retry
                     WHERE fri_retry.run_id = ?
                       AND fri_retry.item_id = {item_alias}.id
                  )""",
            [run_id],
        )
    if run_items_scope != _RUN_ITEMS_SCOPE_TAGGED:
        raise ValueError(f"Unsupported run_items_scope={run_items_scope!r}")
    return f" AND COALESCE({item_alias}.fetch_run_id, -1) != ?", [run_id]


def _remote_run_item_exclusion_sql(
    run_id: int | None,
    run_items_scope: str = _RUN_ITEMS_SCOPE_TAGGED,
    *,
    item_alias: str = "items",
) -> tuple[str, list[Any]]:
    if run_id is None:
        return "", []
    if run_items_scope == _RUN_ITEMS_SCOPE_INSERTED:
        return (
            f""" AND NOT EXISTS (
                    SELECT 1
                      FROM {remote_db.remote_schema()}.fetch_run_items fri_retry
                     WHERE fri_retry.run_id = %s
                       AND fri_retry.item_id = {item_alias}.id
                  )""",
            [run_id],
        )
    if run_items_scope != _RUN_ITEMS_SCOPE_TAGGED:
        raise ValueError(f"Unsupported run_items_scope={run_items_scope!r}")
    return f" AND COALESCE({item_alias}.fetch_run_id, -1) != %s", [run_id]


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default


def _positive_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return default


def _retry_window_start_iso() -> str | None:
    hours = _positive_float_env(
        "INFO2ACTION_CLUSTER_RETRY_LOOKBACK_HOURS",
        _CLUSTER_RETRY_LOOKBACK_HOURS_DEFAULT,
    )
    if hours <= 0:
        return None
    return to_utc_iso(datetime.now(timezone.utc) - timedelta(hours=hours))


def _dedupe_rows_by_id(rows: list[Any]) -> list[Any]:
    seen: set[str] = set()
    out: list[Any] = []
    for row in rows:
        item_id = row["id"]
        if item_id in seen:
            continue
        seen.add(item_id)
        out.append(row)
    return out
_TAU_HOURS_DEFAULT = 24.0


class ProviderRateLimited(RuntimeError):
    """Raised when run-scoped clustering must pause for provider recovery."""


def _window_sql_filter(
    window_start: str | None = None,
    window_end: str | None = None,
    *,
    require_published_at: bool = False,
) -> tuple[str, list[str]]:
    if not window_start and not window_end:
        return "", []
    expr = (
        "datetime(NULLIF(published_at, ''))"
        if require_published_at
        else "COALESCE(datetime(NULLIF(published_at, '')), datetime(NULLIF(fetched_at, '')))"
    )
    clauses: list[str] = []
    params: list[str] = []
    if require_published_at:
        clauses.append(" AND datetime(NULLIF(published_at, '')) IS NOT NULL")
    if window_start:
        clauses.append(f" AND {expr} >= datetime(?)")
        params.append(window_start)
    if window_end:
        clauses.append(f" AND {expr} < datetime(?)")
        params.append(window_end)
    return "".join(clauses), params


def _window_time_expr(*, require_published_at: bool = False) -> str:
    return (
        "datetime(NULLIF(published_at, ''))"
        if require_published_at
        else "COALESCE(datetime(NULLIF(published_at, '')), datetime(NULLIF(fetched_at, '')))"
    )


def _cluster_event_bounds(row) -> tuple[datetime | None, datetime | None]:
    first = parse_datetime(row['first_doc_at'])
    last = parse_datetime(row['last_doc_at'])
    if first is None:
        first = last
    if last is None:
        last = first
    if first is not None and last is not None and last < first:
        first, last = last, first
    return first, last


def _temporal_distance_days(
    item_dt: datetime,
    cluster_first: datetime,
    cluster_last: datetime,
) -> float:
    if cluster_first <= item_dt <= cluster_last:
        return 0.0
    if item_dt < cluster_first:
        return round((cluster_first - item_dt).total_seconds() / 86400.0, 4)
    return round((item_dt - cluster_last).total_seconds() / 86400.0, 4)


def _is_temporal_candidate(
    *,
    item_dt: datetime | None,
    cluster_first: datetime | None,
    cluster_last: datetime | None,
    temporal_adjacency_days: float | None,
    max_merged_span_days: float | None,
) -> tuple[bool, float | None, float | None]:
    """Return whether a cluster is time-adjacent enough for Stage 1 recall.

    The live event semantics are centered on the item, not on "now": every
    item may be clustered, but it only compares against clusters whose
    [first_doc_at, last_doc_at] overlaps the item_time ± N day window.
    """
    if item_dt is None or cluster_first is None or cluster_last is None:
        return True, None, None
    window_days = (
        _TEMPORAL_ADJACENCY_DAYS_DEFAULT
        if temporal_adjacency_days is None
        else float(temporal_adjacency_days)
    )
    adjacency = timedelta(days=max(0.0, window_days))
    is_adjacent = cluster_first <= item_dt + adjacency and cluster_last >= item_dt - adjacency
    merged_first = min(cluster_first, item_dt)
    merged_last = max(cluster_last, item_dt)
    merged_span_days = round((merged_last - merged_first).total_seconds() / 86400.0, 4)
    if not is_adjacent:
        return False, _temporal_distance_days(item_dt, cluster_first, cluster_last), merged_span_days
    if max_merged_span_days is not None:
        max_span = max(0.0, float(max_merged_span_days))
        if merged_span_days > max_span:
            return False, _temporal_distance_days(item_dt, cluster_first, cluster_last), merged_span_days
    return True, _temporal_distance_days(item_dt, cluster_first, cluster_last), merged_span_days


class _ChatGate:
    def __init__(self, min_interval: float = 0.0):
        self._min_interval = max(0.0, float(min_interval or 0.0))
        self._next_allowed_at = 0.0
        self._lock = threading.Lock()

    def wait(self):
        if self._min_interval <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                delay = self._next_allowed_at - now
                if delay <= 0:
                    self._next_allowed_at = now + self._min_interval
                    return
            time.sleep(delay)


def _load_cfg():
    """Load config/config.json (merged with defaults). Tolerant to missing."""
    try:
        with open(_PROJECT_ROOT / 'config' / 'config.json', 'r') as f:
            return json.load(f)
    except Exception:
        return {}


def _load_embedding_clustering_profiles(path: Path | None = None) -> dict[str, Any]:
    """Load embedding + cluster-threshold profiles from config/.

    The threshold is not model trivia: it is the gate between embedding vectors
    and cluster aggregation. Keeping it in a named profile makes future quality
    tuning start from the right place.
    """
    profile_path = path or _EMBEDDING_CLUSTERING_CONFIG_PATH
    try:
        with open(profile_path, 'r') as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning('failed to load embedding clustering config %s: %s', profile_path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _embedding_clustering_profile_matches(
    profile: dict[str, Any],
    *,
    provider_name: str,
    provider: Any,
) -> bool:
    expected_provider = str(profile.get('embedding_provider') or profile.get('provider') or '').strip().lower()
    actual_alias = str(provider_name or '').strip().lower()
    actual_name = str(getattr(provider, 'name', '') or '').strip().lower()
    if expected_provider and expected_provider not in {actual_alias, actual_name}:
        return False
    expected_model = str(profile.get('embedding_model') or profile.get('model') or '').strip()
    actual_model = str(getattr(provider, 'model', '') or '').strip()
    if expected_model and actual_model and expected_model != actual_model:
        return False
    return True


def _apply_embedding_clustering_profile(
    clustering_cfg: dict[str, Any],
    *,
    provider_name: str,
    provider: Any,
    profile_doc: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return clustering config with the matching embedding-threshold profile.

    Precedence:
      1. config.global.clustering.embedding_clustering_profile
      2. config/embedding_clustering.json active_profile
      3. first profile matching the active embedding provider/model

    Only cluster aggregation knobs are copied into the runtime config. Metadata
    fields are retained under private keys for logging/debugging.
    """
    out = dict(clustering_cfg or {})
    doc = profile_doc if profile_doc is not None else _load_embedding_clustering_profiles()
    profiles = doc.get('profiles') if isinstance(doc, dict) else {}
    if not isinstance(profiles, dict):
        return out

    requested = out.get('embedding_clustering_profile') or doc.get('active_profile')
    selected_name: str | None = None
    selected: dict[str, Any] | None = None
    if requested:
        candidate = profiles.get(str(requested))
        if isinstance(candidate, dict) and _embedding_clustering_profile_matches(
            candidate,
            provider_name=provider_name,
            provider=provider,
        ):
            selected_name = str(requested)
            selected = candidate
        elif isinstance(candidate, dict):
            logger.warning(
                'embedding clustering profile %s does not match provider=%s model=%s; ignoring',
                requested,
                provider_name,
                getattr(provider, 'model', None),
            )

    if selected is None:
        for name, candidate in profiles.items():
            if not isinstance(candidate, dict):
                continue
            if _embedding_clustering_profile_matches(candidate, provider_name=provider_name, provider=provider):
                selected_name = str(name)
                selected = candidate
                break

    if selected is None:
        return out

    for key in (
        'stage1_cosine_min',
        'stage1_gray_cosine_min',
        'stage1_shadow_cosine_min',
        'stage1_gray_max_temporal_hours',
        'stage1_top_k',
    ):
        if key in selected:
            out[key] = selected[key]
    out['embedding_clustering_profile'] = selected_name
    out['_embedding_clustering_profile_rationale'] = selected.get('rationale')
    out['_embedding_clustering_profile_report'] = (
        selected.get('offline_eval', {}).get('report')
        if isinstance(selected.get('offline_eval'), dict)
        else None
    )
    return out


def _coerce_optional_float(value: Any, default: float | None) -> float | None:
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("", "none", "null", "false", "off"):
            return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed


def resolve_minimax_chat_runtime_config(
    ai_config: dict[str, Any],
    project_env: dict[str, str] | None = None,
) -> tuple[str, str | None, str]:
    """Resolve chat credentials separately from embedding credentials."""
    if project_env is None:
        project_env = load_project_env(Path(__file__).resolve().parents[2])
    api_key = (
        os.environ.get('MINIMAX_API_KEY')
        or project_env.get('MINIMAX_API_KEY')
        or ai_config.get('api_key')
        or ''
    ).strip()
    api_base = (
        os.environ.get('MINIMAX_API_BASE')
        or project_env.get('MINIMAX_API_BASE')
        or ai_config.get('api_base')
    )
    model = (
        os.environ.get('MINIMAX_MODEL')
        or project_env.get('MINIMAX_MODEL')
        or ai_config.get('model')
        or 'MiniMax-M3'
    )
    return api_key, api_base, model


def _log_event(event: str, **fields):
    """Structured JSONL log (fire-and-forget; never raises)."""
    try:
        base = Path(__file__).resolve().parents[2]
        logs = base / 'logs'
        logs.mkdir(exist_ok=True)
        line = json.dumps({
            'ts': datetime.now(timezone.utc).isoformat(),
            'event': event,
            **fields,
        }, ensure_ascii=False)
        with open(logs / 'cluster_events.jsonl', 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass


def _row_id(row) -> Any:
    try:
        return row['id']
    except (KeyError, IndexError, TypeError):
        return None


def _embed_provider_batch(provider, texts: list[str], *, run_id: int | None, item_ids: list[Any]) -> np.ndarray:
    with ep_mod.embedding_usage_context(
        source='clustering.pipeline',
        stage='stage0_item_embedding',
        run_id=run_id,
        item_ids=item_ids,
    ):
        return provider.embed(texts, mode='db')


def _embed_batch_with_item_fallback(provider, batch, texts: list[str], *, run_id: int | None) -> list[tuple[Any, Any]]:
    item_ids = [_row_id(r) for r in batch]
    try:
        vecs = _embed_provider_batch(provider, texts, run_id=run_id, item_ids=item_ids)
        if getattr(vecs, 'shape', None) and vecs.shape[0] == len(batch):
            return list(zip(batch, vecs))
        got = getattr(vecs, 'shape', ['?'])[0]
        raise RuntimeError(f"embedding count mismatch: got {got} want {len(batch)}")
    except (ai_provider_guard.ProviderActionRequired, ai_provider_guard.ProviderCooldown) as e:
        err = str(e)
        logger.warning('embed batch paused by provider state: %s', err)
        _log_event('embed_fail', batch_size=len(texts), err=err)
        raise ProviderRateLimited(err)
    except Exception as e:
        err = str(e)
        logger.warning('embed batch failed: %s', e)
        _log_event('embed_fail', batch_size=len(texts), err=err)

    if len(batch) <= 1:
        return []

    pairs: list[tuple[Any, Any]] = []
    for row, text, item_id in zip(batch, texts, item_ids):
        try:
            vecs = _embed_provider_batch(provider, [text], run_id=run_id, item_ids=[item_id])
            if not (getattr(vecs, 'shape', None) and vecs.shape[0] == 1):
                got = getattr(vecs, 'shape', ['?'])[0]
                raise RuntimeError(f"embedding count mismatch: got {got} want 1")
        except (ai_provider_guard.ProviderActionRequired, ai_provider_guard.ProviderCooldown) as e:
            err = str(e)
            logger.warning('embed item paused by provider state: %s', err)
            _log_event('embed_fail', batch_size=1, item_id=item_id, err=err)
            raise ProviderRateLimited(err)
        except Exception as e:
            err = str(e)
            logger.warning('embed item failed: item_id=%s err=%s', item_id, e)
            _log_event('embed_item_fail', item_id=item_id, err=err)
            continue
        pairs.append((row, vecs[0]))
    return pairs


def _embed_pending_items(conn, provider, batch_size: int = _EMBED_BATCH,
                         run_id: int | None = None,
                         run_items_scope: str = _RUN_ITEMS_SCOPE_TAGGED,
                         window_start: str | None = None,
                         window_end: str | None = None,
                         require_published_at: bool = False) -> int:
    """Stage 0: embed items with NULL embedding.

    v15.1: structured-first input via clustering.event_text.build_event_embedding_text.
    Pulls full enrich fields (ai_summary / ai_key_points / ai_keywords /
    ai_category / content_type) plus content/transcript fallbacks. Caps total
    length at 10000 chars (was 3800). Logs `event_embedding_text_built` per
    item; logs `cluster_low_confidence_doc_allowed` when used_fallback_content.

    Returns number of items newly embedded.
    """
    if remote_db.embedding_to_remote():
        return _embed_pending_items_remote(
            provider,
            batch_size=batch_size,
            run_id=run_id,
            run_items_scope=run_items_scope,
            window_start=window_start,
            window_end=window_end,
            require_published_at=require_published_at,
        )

    ai_ready_filter = (
        " AND ai_summary IS NOT NULL AND ai_summary != ''"
        if run_id is not None else ""
    )
    run_filter, run_params = _run_item_scope_sql(run_id, run_items_scope)
    window_filter, window_params = _window_sql_filter(
        window_start,
        window_end,
        require_published_at=require_published_at,
    )
    params = list(run_params)
    params.extend(window_params)
    limit_clause = "" if run_id is not None else "LIMIT 500"
    window_active = bool(window_start or window_end)
    order_expr = _window_time_expr(require_published_at=require_published_at)
    order_clause = f"{order_expr} DESC" if window_active else "fetched_at DESC"
    rows = conn.execute(
        """SELECT id, title, content, ai_summary, ai_key_points,
                  ai_keywords, ai_category, content_type,
                  asr_text_cn, asr_text
             FROM items
            WHERE embedding IS NULL
              {ai_ready_filter}
              {run_filter}
              {window_filter}
            ORDER BY {order_clause}
            {limit_clause}""".format(
                ai_ready_filter=ai_ready_filter,
                run_filter=run_filter,
                window_filter=window_filter,
                order_clause=order_clause,
                limit_clause=limit_clause,
            ),
        tuple(params),
    ).fetchall()
    if run_id is not None:
        retry_limit = _positive_int_env(
            "INFO2ACTION_CLUSTER_ITEM_RETRY_LIMIT",
            _CLUSTER_ITEM_RETRY_LIMIT_DEFAULT,
        )
        retry_window_start = window_start or _retry_window_start_iso()
        if retry_limit > 0 and retry_window_start:
            retry_window_filter, retry_window_params = _window_sql_filter(
                retry_window_start,
                window_end,
                require_published_at=require_published_at,
            )
            exclude_filter, exclude_params = _run_item_exclusion_sql(
                run_id,
                run_items_scope,
            )
            retry_params: list[Any] = []
            retry_params.extend(retry_window_params)
            retry_params.extend(exclude_params)
            retry_params.append(retry_limit)
            retry_rows = conn.execute(
                """SELECT id, title, content, ai_summary, ai_key_points,
                          ai_keywords, ai_category, content_type,
                          asr_text_cn, asr_text
                     FROM items
                    WHERE embedding IS NULL
                      AND ai_summary IS NOT NULL AND ai_summary != ''
                      {retry_window_filter}
                      {exclude_filter}
                    ORDER BY {order_expr} ASC
                    LIMIT ?""".format(
                        retry_window_filter=retry_window_filter,
                        exclude_filter=exclude_filter,
                        order_expr=order_expr,
                    ),
                tuple(retry_params),
            ).fetchall()
            if retry_rows:
                _log_event(
                    "cluster_item_embedding_retry_backlog_loaded",
                    run_id=run_id,
                    count=len(retry_rows),
                    limit=retry_limit,
                    window_start=retry_window_start,
                )
                rows = _dedupe_rows_by_id(list(rows) + list(retry_rows))
    if not rows:
        return 0
    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        texts: list[str] = []
        metas: list[dict] = []
        for r in batch:
            text, meta = event_text_mod.build_event_embedding_text(r)
            texts.append(text)
            metas.append(meta)
            _log_event(
                'event_embedding_text_built',
                item_id=r['id'],
                has_ai_summary=meta['has_ai_summary'],
                has_ai_key_points=meta['has_ai_key_points'],
                has_ai_keywords=meta['has_ai_keywords'],
                used_fallback_content=meta['used_fallback_content'],
                embedding_text_chars=meta['embedding_text_chars'],
            )
            if meta['used_fallback_content']:
                _log_event(
                    'cluster_low_confidence_doc_allowed',
                    item_id=r['id'],
                    reason='missing_ai_summary_or_key_points',
                    ai_understanding_status='fallback',
                )
        pairs = _embed_batch_with_item_fallback(provider, batch, texts, run_id=run_id)
        if not pairs:
            continue
        for r, v in pairs:
            conn.execute(
                "UPDATE items SET embedding = ?, embedding_provider = ? WHERE id = ?",
                (vu.pack_blob(v), provider.name, r['id']),
            )
            total += 1
        conn.commit()
    return total


def _remote_time_filter(
    window_start: str | None = None,
    window_end: str | None = None,
    *,
    require_published_at: bool = False,
) -> tuple[str, list[str]]:
    if not window_start and not window_end:
        return "", []
    expr = "published_at" if require_published_at else "COALESCE(published_at, fetched_at)"
    clauses: list[str] = []
    params: list[str] = []
    if require_published_at:
        clauses.append(" AND published_at IS NOT NULL")
    if window_start:
        clauses.append(f" AND {expr} >= %s")
        params.append(window_start)
    if window_end:
        clauses.append(f" AND {expr} < %s")
        params.append(window_end)
    return "".join(clauses), params


def _embed_pending_items_remote(
    provider,
    batch_size: int = _EMBED_BATCH,
    run_id: int | None = None,
    run_items_scope: str = _RUN_ITEMS_SCOPE_TAGGED,
    window_start: str | None = None,
    window_end: str | None = None,
    require_published_at: bool = False,
) -> int:
    """Stage 0 remote variant: write embeddings directly to Supabase pgvector."""
    ai_ready_filter = (
        " AND ai_summary IS NOT NULL AND ai_summary != ''"
        if run_id is not None else ""
    )
    run_filter, run_params = _remote_run_item_scope_sql(run_id, run_items_scope)
    window_filter, window_params = _remote_time_filter(
        window_start,
        window_end,
        require_published_at=require_published_at,
    )
    params: list[Any] = list(run_params)
    params.extend(window_params)
    limit_clause = "" if run_id is not None else "LIMIT 500"
    window_active = bool(window_start or window_end)
    order_expr = "published_at" if require_published_at else "COALESCE(published_at, fetched_at)"
    order_clause = f"{order_expr} DESC" if window_active else "fetched_at DESC"

    with remote_db.connect() as pg_conn:
        remote_db.set_pending_scan_statement_timeout(pg_conn)
        rows = pg_conn.execute(
            f"""SELECT id, title, content, ai_summary, ai_key_points,
                      ai_keywords, ai_category, content_type,
                      asr_text_cn, asr_text
                 FROM {remote_db.remote_schema()}.items
                WHERE embedding IS NULL
                  {ai_ready_filter}
                  {run_filter}
                  {window_filter}
                ORDER BY {order_clause}
                {limit_clause}""",
            tuple(params),
        ).fetchall()
        if run_id is not None:
            retry_limit = _positive_int_env(
                "INFO2ACTION_CLUSTER_ITEM_RETRY_LIMIT",
                _CLUSTER_ITEM_RETRY_LIMIT_DEFAULT,
            )
            retry_window_start = window_start or _retry_window_start_iso()
            if retry_limit > 0 and retry_window_start:
                retry_window_filter, retry_window_params = _remote_time_filter(
                    retry_window_start,
                    window_end,
                    require_published_at=require_published_at,
                )
                exclude_filter, exclude_params = _remote_run_item_exclusion_sql(
                    run_id,
                    run_items_scope,
                )
                retry_params: list[Any] = []
                retry_params.extend(retry_window_params)
                retry_params.extend(exclude_params)
                retry_params.append(retry_limit)
                retry_rows = pg_conn.execute(
                    f"""SELECT id, title, content, ai_summary, ai_key_points,
                              ai_keywords, ai_category, content_type,
                              asr_text_cn, asr_text
                         FROM {remote_db.remote_schema()}.items
                        WHERE embedding IS NULL
                          AND ai_summary IS NOT NULL AND ai_summary != ''
                          {retry_window_filter}
                          {exclude_filter}
                        ORDER BY {order_expr} ASC
                        LIMIT %s""",
                    tuple(retry_params),
                ).fetchall()
                if retry_rows:
                    _log_event(
                        "cluster_item_embedding_retry_backlog_loaded",
                        run_id=run_id,
                        count=len(retry_rows),
                        limit=retry_limit,
                        window_start=retry_window_start,
                    )
                    rows = _dedupe_rows_by_id(list(rows) + list(retry_rows))
        if not rows:
            return 0

        total = 0
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            texts: list[str] = []
            metas: list[dict] = []
            for r in batch:
                text, meta = event_text_mod.build_event_embedding_text(r)
                texts.append(text)
                metas.append(meta)
                _log_event(
                    'event_embedding_text_built',
                    item_id=r['id'],
                    has_ai_summary=meta['has_ai_summary'],
                    has_ai_key_points=meta['has_ai_key_points'],
                    has_ai_keywords=meta['has_ai_keywords'],
                    used_fallback_content=meta['used_fallback_content'],
                    embedding_text_chars=meta['embedding_text_chars'],
                )
            pairs = _embed_batch_with_item_fallback(provider, batch, texts, run_id=run_id)
            if not pairs:
                continue
            for r, v in pairs:
                remote_db.update_item_embedding_remote(pg_conn, r['id'], v, provider.name)
                total += 1
        return total


def _fetch_candidate_clusters(conn, window_days: int):
    """Fetch clusters active within the window (V1 Stage 1 candidate set).

    DEPRECATED in v15.1: kept for backward compatibility with V1 callers and
    tests. V2 callers SHALL use ``_recall_top_k_clusters`` which orders by
    cosine similarity and caps at top-K.
    """
    rows = conn.execute(
        f"""SELECT id, representative_vector, doc_count, live_version,
                  last_updated_at, first_doc_at
            FROM clusters
            WHERE archived = 0
              AND merged_into IS NULL
              AND last_updated_at > datetime('now', '-{int(window_days)} days')
            ORDER BY last_updated_at DESC"""
    ).fetchall()
    out = []
    for r in rows:
        vec = vu.unpack_blob(r['representative_vector'])
        if vec is None:
            continue
        out.append({'id': r['id'], 'vector': vec, 'doc_count': r['doc_count'],
                    'live_version': r['live_version']})
    return out


def _recall_top_k_clusters(
    conn,
    new_vec: np.ndarray,
    *,
    k: int = 10,
    window_days: int = _CANDIDATE_WINDOW_DAYS_DEFAULT,
    cosine_min: float = 0.0,
    item_time: str | datetime | None = None,
    temporal_adjacency_days: float | None = _TEMPORAL_ADJACENCY_DAYS_DEFAULT,
    max_merged_span_days: float | None = _MAX_MERGED_SPAN_DAYS_DEFAULT,
) -> list[dict]:
    """Stage 1 V2 recall: cosine top-K candidates within the temporal window.

    Implements feature-spec R2.1 / R2.2 / R2.3:
      - WHERE archived=0 AND merged_into IS NULL AND representative_vector IS NOT NULL
      - For live item clustering, filter by event-time adjacency:
        item_time ± temporal_adjacency_days must overlap the cluster
        [first_doc_at, last_doc_at] range. This prevents a newly processed
        old cluster from becoming eligible just because last_updated_at changed.
      - Optional max_merged_span_days caps chain growth after a join.
      - Compute cosine(new_vec, cluster.representative_vector) for each
        candidate.
      - Sort cosine DESC, take top K. K candidates? K. <K candidates? all.
        0 candidates? [].
      - BF-0428-3: optional ``cosine_min`` hard-floor filter pre-Stage 2.
        Default 0.0 preserves V15.1 baseline behavior. When configured >0
        (e.g. 0.75 via ``config.global.clustering.cosine_min_threshold``),
        candidates with cosine < threshold are dropped *before* LLM judge,
        cutting LLM误合 (Moxt vs HappyHorse, SpaceX vs Emirates) at recall
        layer where embedding distance already says "not really similar".

    The returned list carries enough cluster metadata for Stage 2 prompt
    construction without a second SELECT round-trip:

      [{cluster_id, representative_vector, cosine, doc_count, live_version,
        first_doc_at, last_doc_at, last_updated_at, ai_title, ai_summary,
        ai_key_points}]

    ``cosine`` is the float similarity vs ``new_vec`` (range -1..1). Ordering
    in the returned list matches cosine DESC.
    """
    if remote_db.cluster_to_remote():
        return remote_db.recall_top_k_clusters_remote(
            None,
            new_vec,
            k=k,
            window_days=window_days,
            cosine_min=cosine_min,
            item_time=item_time,
            temporal_adjacency_days=temporal_adjacency_days,
            max_merged_span_days=max_merged_span_days,
        )
    if new_vec is None:
        return []
    item_dt = parse_datetime(item_time)
    if item_dt is None:
        rows = conn.execute(
            f"""SELECT id, representative_vector, doc_count, live_version,
                      first_doc_at, last_doc_at, last_updated_at,
                      ai_title, ai_summary, ai_key_points
                FROM clusters
                WHERE archived = 0
                  AND merged_into IS NULL
                  AND representative_vector IS NOT NULL
                  AND last_updated_at > datetime('now', '-{int(window_days)} days')"""
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, representative_vector, doc_count, live_version,
                      first_doc_at, last_doc_at, last_updated_at,
                      ai_title, ai_summary, ai_key_points
                FROM clusters
                WHERE archived = 0
                  AND merged_into IS NULL
                  AND representative_vector IS NOT NULL"""
        ).fetchall()
    scored: list[dict] = []
    for r in rows:
        cluster_first, cluster_last = _cluster_event_bounds(r)
        ok_temporal, temporal_distance_days, merged_span_days = _is_temporal_candidate(
            item_dt=item_dt,
            cluster_first=cluster_first,
            cluster_last=cluster_last,
            temporal_adjacency_days=temporal_adjacency_days,
            max_merged_span_days=max_merged_span_days,
        )
        if not ok_temporal:
            continue
        vec = vu.unpack_blob(r['representative_vector'])
        if vec is None:
            # representative_vector column non-null but blob unpacks to None
            # (corrupt / wrong dtype). Skip — same as missing vector.
            continue
        sim = float(vu.cosine_similarity(new_vec, vec))
        # BF-0428-3: hard floor pre-LLM-judge
        if sim < cosine_min:
            continue
        scored.append({
            'cluster_id': r['id'],
            'representative_vector': vec,
            'cosine': sim,
            'doc_count': r['doc_count'],
            'live_version': r['live_version'],
            'first_doc_at': r['first_doc_at'],
            'last_doc_at': r['last_doc_at'],
            'last_updated_at': r['last_updated_at'],
            'ai_title': r['ai_title'],
            'ai_summary': r['ai_summary'],
            'ai_key_points': r['ai_key_points'],
            'temporal_distance_days': temporal_distance_days,
            'merged_span_days': merged_span_days,
        })
    scored.sort(key=lambda c: c['cosine'], reverse=True)
    return scored[: max(0, int(k))]


_ENTITY_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.+-]{2,}")
_ENTITY_STOPWORDS = {
    'about', 'above', 'after', 'agent', 'agents', 'and', 'api', 'app',
    'apps', 'article', 'assistant', 'blog', 'code', 'coding', 'com',
    'content', 'data', 'design', 'dev', 'developer', 'developers', 'for',
    'from', 'github', 'http', 'https', 'index', 'item', 'llm', 'model',
    'models', 'new', 'news', 'official', 'open', 'page', 'plan', 'post',
    'product', 'products', 'project', 'release', 'skill', 'skills',
    'status', 'tech', 'thread', 'tool', 'tools', 'twitter', 'use', 'user',
    'users', 'using', 'with', 'www',
}


def _text_value(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, (list, tuple, set)):
        return ' '.join(_text_value(v) for v in value)
    try:
        return str(value)
    except Exception:
        return ''


def _item_category(item: Any) -> str | None:
    try:
        raw = item.get('ai_category')
    except AttributeError:
        raw = item['ai_category'] if 'ai_category' in item.keys() else None
    return visibility_policy.normalize_category(raw)


def _author_key(value: Any) -> str | None:
    author = _text_value(value).strip().lower()
    return author or None


def _entity_tokens_from_text(*values: Any) -> set[str]:
    text = ' '.join(_text_value(v) for v in values)
    tokens: set[str] = set()
    for match in _ENTITY_TOKEN_RE.finditer(text):
        token = match.group(0).strip("._+-").lower()
        if len(token) < 3 or token in _ENTITY_STOPWORDS:
            continue
        tokens.add(token)
        for part in re.split(r"[._+-]+", token):
            if len(part) >= 4 and part not in _ENTITY_STOPWORDS:
                tokens.add(part)
    return tokens


def _item_entity_tokens(item: Any) -> set[str]:
    fields = []
    for key in ('title', 'content', 'ai_summary', 'ai_key_points', 'ai_keywords', 'url'):
        try:
            fields.append(item.get(key))
        except AttributeError:
            if key in item.keys():
                fields.append(item[key])
    return _entity_tokens_from_text(*fields)


def _candidate_entity_tokens(candidate: dict) -> set[str]:
    return _entity_tokens_from_text(
        candidate.get('ai_title'),
        candidate.get('ai_summary'),
        candidate.get('ai_key_points'),
        candidate.get('primary_title'),
        candidate.get('primary_summary'),
        candidate.get('primary_key_points'),
        candidate.get('primary_keywords'),
        candidate.get('primary_url'),
    )


def _candidate_temporal_distance_hours(
    item: Any,
    candidate: dict,
) -> float | None:
    distance_days = candidate.get('temporal_distance_days')
    if distance_days is not None:
        try:
            return abs(float(distance_days)) * 24.0
        except (TypeError, ValueError):
            pass
    try:
        item_time = item.get('published_at')
    except AttributeError:
        item_time = item['published_at'] if 'published_at' in item.keys() else None
    item_dt = parse_datetime(item_time)
    if item_dt is None:
        return None
    cluster_first, cluster_last = _cluster_event_bounds(candidate)
    ok, distance_days, _ = _is_temporal_candidate(
        item_dt=item_dt,
        cluster_first=cluster_first,
        cluster_last=cluster_last,
        temporal_adjacency_days=None,
        max_merged_span_days=None,
    )
    if not ok or distance_days is None:
        return None
    return abs(float(distance_days)) * 24.0


def _gray_recall_candidate_allowed(
    item: Any,
    candidate: dict,
    *,
    max_temporal_hours: float | None = _GRAY_RECALL_MAX_TEMPORAL_HOURS_DEFAULT,
) -> tuple[bool, list[str], list[str]]:
    """Return whether a sub-threshold candidate is safe to send to Stage 2.

    Gray recall deliberately uses non-embedding anchors before involving the
    LLM: close event time, same author/category where available, and shared
    named entities. This keeps the 0.70-0.75 band from becoming a broad recall
    expansion.
    """
    item_author = None
    try:
        item_author = _author_key(item.get('author_name'))
    except AttributeError:
        if 'author_name' in item.keys():
            item_author = _author_key(item['author_name'])
    candidate_author = _author_key(candidate.get('primary_author_name'))
    same_author = bool(item_author and candidate_author and item_author == candidate_author)

    item_category = _item_category(item)
    candidate_category = visibility_policy.normalize_category(
        candidate.get('primary_category') or candidate.get('dominant_category')
    )
    same_category = bool(item_category and candidate_category and item_category == candidate_category)

    temporal_hours = _candidate_temporal_distance_hours(item, candidate)
    close_time = (
        True
        if max_temporal_hours is None
        else temporal_hours is not None and temporal_hours <= float(max_temporal_hours)
    )

    shared_entities = sorted(_item_entity_tokens(item) & _candidate_entity_tokens(candidate))
    reasons: list[str] = []
    if same_author:
        reasons.append('same_author')
    if same_category:
        reasons.append('same_category')
    if close_time:
        reasons.append('close_time')
    if shared_entities:
        reasons.append('shared_entity')

    allowed = False
    if close_time and same_author and same_category and shared_entities:
        allowed = True
    elif close_time and same_author and len(shared_entities) >= 2:
        allowed = True
    elif close_time and same_category and len(shared_entities) >= 3:
        allowed = True
    return allowed, reasons, shared_entities


def _enrich_recall_candidates(conn, candidates: list[dict]) -> list[dict]:
    cluster_ids = [int(c['cluster_id']) for c in candidates if c.get('cluster_id') is not None]
    if not cluster_ids:
        return candidates
    if remote_db.cluster_to_remote():
        schema = remote_db.remote_schema()
        placeholders = ', '.join(['%s'] * len(cluster_ids))
        with remote_db.connect() as pg_conn:
            rows = pg_conn.execute(
                f"""SELECT ci.cluster_id, i.author_name, i.ai_category,
                          i.title, i.ai_summary, i.ai_key_points,
                          i.ai_keywords, i.url
                     FROM {schema}.cluster_items ci
                     JOIN {schema}.items i ON i.id = ci.item_id
                    WHERE ci.cluster_id IN ({placeholders})
                    ORDER BY ci.cluster_id,
                             ci.is_primary_source DESC,
                             COALESCE(ci.rank_in_cluster, 9999),
                             ci.added_at ASC""",
                tuple(cluster_ids),
            ).fetchall()
    else:
        placeholders = ', '.join(['?'] * len(cluster_ids))
        rows = conn.execute(
            f"""SELECT ci.cluster_id, i.author_name, i.ai_category,
                      i.title, i.ai_summary, i.ai_key_points,
                      i.ai_keywords, i.url
                 FROM cluster_items ci
                 JOIN items i ON i.id = ci.item_id
                WHERE ci.cluster_id IN ({placeholders})
                ORDER BY ci.cluster_id,
                         ci.is_primary_source DESC,
                         COALESCE(ci.rank_in_cluster, 9999),
                         ci.added_at ASC""",
            tuple(cluster_ids),
        ).fetchall()

    metadata_by_cluster: dict[int, Any] = {}
    for row in rows:
        cid = int(row['cluster_id'])
        if cid not in metadata_by_cluster:
            metadata_by_cluster[cid] = row
    for candidate in candidates:
        row = metadata_by_cluster.get(int(candidate['cluster_id']))
        if row is None:
            continue
        candidate.update({
            'primary_author_name': row['author_name'],
            'primary_category': row['ai_category'],
            'primary_title': row['title'],
            'primary_summary': row['ai_summary'],
            'primary_key_points': row['ai_key_points'],
            'primary_keywords': row['ai_keywords'],
            'primary_url': row['url'],
        })
    return candidates


def _recall_floor(
    *,
    cosine_min: float,
    gray_cosine_min: float | None,
    shadow_cosine_min: float | None,
) -> float:
    floor = float(cosine_min or 0.0)
    for value in (gray_cosine_min, shadow_cosine_min):
        if value is None:
            continue
        try:
            floor = min(floor, float(value))
        except (TypeError, ValueError):
            continue
    return floor


def _partition_recall_candidates(
    item: Any,
    candidates: list[dict],
    *,
    cosine_min: float,
    gray_cosine_min: float | None,
    shadow_cosine_min: float | None,
    gray_max_temporal_hours: float | None,
) -> tuple[list[dict], list[dict], list[dict]]:
    strong_floor = float(cosine_min or 0.0)
    gray_floor = float(gray_cosine_min) if gray_cosine_min is not None else None
    shadow_floor = float(shadow_cosine_min) if shadow_cosine_min is not None else None
    judge_candidates: list[dict] = []
    shadow_candidates: list[dict] = []
    rejected_gray: list[dict] = []

    for candidate in candidates:
        sim = float(candidate.get('cosine') or 0.0)
        if sim >= strong_floor:
            candidate['recall_band'] = 'strong'
            judge_candidates.append(candidate)
            continue
        if gray_floor is not None and gray_floor <= sim < strong_floor:
            allowed, reasons, shared_entities = _gray_recall_candidate_allowed(
                item,
                candidate,
                max_temporal_hours=gray_max_temporal_hours,
            )
            candidate['recall_band'] = 'gray'
            candidate['gray_recall_reasons'] = reasons
            candidate['gray_shared_entities'] = shared_entities
            if allowed:
                judge_candidates.append(candidate)
            else:
                rejected_gray.append(candidate)
            continue
        shadow_upper = gray_floor if gray_floor is not None else strong_floor
        if shadow_floor is not None and shadow_floor <= sim < shadow_upper:
            candidate['recall_band'] = 'shadow'
            shadow_candidates.append(candidate)
    judge_candidates.sort(key=lambda c: float(c.get('cosine') or 0.0), reverse=True)
    shadow_candidates.sort(key=lambda c: float(c.get('cosine') or 0.0), reverse=True)
    rejected_gray.sort(key=lambda c: float(c.get('cosine') or 0.0), reverse=True)
    return judge_candidates, shadow_candidates, rejected_gray


def _recompute_doc_count(conn, cluster_id: int) -> int:
    """doc_count = distinct (platform, author_name) across cluster members (R7.3).

    Authors with NULL author_name fallback to id prefix (each doc counts 1) to
    avoid collapsing all nameless items. platform always non-null.
    """
    n = conn.execute(
        """SELECT COUNT(*) FROM (
             SELECT DISTINCT i.platform, COALESCE(i.author_name, i.id)
             FROM cluster_items ci JOIN items i ON i.id = ci.item_id
             WHERE ci.cluster_id = ?
           )""",
        (cluster_id,),
    ).fetchone()[0]
    return n


def _collect_platforms(conn, cluster_id: int) -> list[str]:
    rows = conn.execute(
        """SELECT DISTINCT i.platform FROM cluster_items ci
           JOIN items i ON i.id = ci.item_id WHERE ci.cluster_id = ?""",
        (cluster_id,),
    ).fetchall()
    return sorted({r['platform'] for r in rows if r['platform']})


def _recompute_representative(conn, cluster_id: int, tau_hours: float) -> np.ndarray | None:
    """Weighted mean of member embeddings by published_at age."""
    rows = conn.execute(
        """SELECT i.embedding, COALESCE(i.published_at, i.fetched_at) AS ts
           FROM cluster_items ci JOIN items i ON i.id = ci.item_id
           WHERE ci.cluster_id = ? AND i.embedding IS NOT NULL""",
        (cluster_id,),
    ).fetchall()
    vecs, tss = [], []
    for r in rows:
        v = vu.unpack_blob(r['embedding'])
        if v is None:
            continue
        vecs.append(v)
        tss.append(parse_datetime(r['ts']) or datetime.now(timezone.utc))
    if not vecs:
        return None
    return vu.weighted_mean_with_decay(vecs, tss, now=datetime.now(timezone.utc), tau_hours=tau_hours)


def _cluster_time_bounds(conn, cluster_id: int) -> tuple[str, str]:
    rows = conn.execute(
        """SELECT COALESCE(NULLIF(i.published_at, ''), i.fetched_at) AS ts
           FROM cluster_items ci JOIN items i ON i.id = ci.item_id
           WHERE ci.cluster_id = ?""",
        (cluster_id,),
    ).fetchall()
    parsed = [parse_datetime(r['ts']) for r in rows]
    parsed = [dt for dt in parsed if dt is not None]
    if not parsed:
        now = to_utc_iso(datetime.now(timezone.utc))
        return now, now
    return to_utc_iso(min(parsed)), to_utc_iso(max(parsed))


def _add_item_to_cluster(conn, cluster_id: int, item_id: str,
                         rank_in_cluster: int = 9999,
                         is_primary_source: int = 0,
                         *,
                         source_identity: str | None = None,
                         join_decision_id: int | None = None):
    """Insert into cluster_items + flip items.cluster_id.

    V2 (v15.1) extends the signature with:
      * ``source_identity``: V2 unique-source dedup key (canonical_url →
        normalized_url → original_url → content_fingerprint → item_id). When
        ``None``, column stays NULL — Commit 4 (Stage 3) fills it in.
      * ``join_decision_id``: cluster_judge_log row id linking this membership
        to the Stage 2 decision that produced it. NULL for singleton creation
        and legacy paths.
    """
    if remote_db.cluster_to_remote():
        remote_db.add_item_to_cluster_remote(
            None,
            cluster_id,
            item_id,
            rank_in_cluster=rank_in_cluster,
            is_primary_source=is_primary_source,
            source_identity=source_identity,
            join_decision_id=join_decision_id,
        )
        return
    conn.execute(
        """INSERT OR IGNORE INTO cluster_items
             (cluster_id, item_id, rank_in_cluster, is_primary_source,
              source_identity, join_decision_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (cluster_id, item_id, rank_in_cluster, is_primary_source,
         source_identity,
         str(join_decision_id) if join_decision_id is not None else None),
    )
    conn.execute(
        "UPDATE items SET cluster_id = ? WHERE id = ?",
        (cluster_id, item_id),
    )


def _compute_source_identity(item_row) -> str | None:
    """Compute source_identity for a cluster_items row (feature-spec R8.1/R8.2).

    V2 priority (V2.3 §6 / §13.3 / §18.3):
        canonical_url → normalized_url → original_url → content_fingerprint
        → item_id

    Current items table only has ``url`` and ``id`` (BF-0427: items schema does
    NOT yet ship canonical_url / normalized_url / original_url /
    content_fingerprint columns; see TODO below). We approximate the chain:

      1. ``utils.url_normalize.normalize_url(item.url).canonical_url`` when
         the URL is a known-platform link (Twitter / YouTube). For these
         platforms ``canonical_url`` is the deterministic stable form (e.g.
         ``https://x.com/<user>/status/<id>``), so two ingests of the same
         tweet with different query strings collapse to one identity.
      2. Otherwise the raw stripped ``item.url`` (covers RSS / HN / Reddit
         where URL is already canonical from upstream feeds).
      3. Last resort the ``id`` (item_id) so a brand-new item with no URL at
         all still has a unique identity (R8.2: 同作者多条无 URL 推文仍计多源)。

    TODO (post-V2): once the items schema ships canonical_url /
    normalized_url / original_url / content_fingerprint columns, replace
    this with a clean priority chain and drop the inline normalize_url call.
    """
    keys = item_row.keys() if hasattr(item_row, 'keys') else []
    raw_url = ''
    if 'url' in keys:
        raw_url = (item_row['url'] or '').strip()
    item_id = item_row['id'] if 'id' in keys else None

    if raw_url:
        try:
            from utils.url_normalize import normalize_url
            n = normalize_url(raw_url)
            # Twitter / YouTube: canonical form is deterministic and dedup-safe.
            if n.platform in ('twitter', 'youtube') and n.canonical_url:
                return n.canonical_url
        except Exception:
            # Defensive: any normalize failure falls through to raw URL.
            pass
        return raw_url

    return item_id


def _finalize_cluster_state(conn, cluster_id: int, *, tau_hours: float):
    """Recompute doc_count / platforms_json / last_updated_at / representative_vector
    / last_doc_at / unique_source_count.

    V15.1 (Eng-A): also recompute ``last_doc_at`` = MAX(member published_at,
    falling back to fetched_at). V1 left this field untouched after the
    initial singleton insert, so cluster freshness drifted.

    V15.1 Eng-C (this commit): also recompute ``unique_source_count`` —
    the new V2 visibility threshold uses unique sources (canonical_url-style
    dedup) instead of v15.0's `(platform, author)` `doc_count`. Computed as
    ``COUNT(DISTINCT cluster_items.source_identity)`` over rows where
    source_identity IS NOT NULL.
    """
    if remote_db.cluster_to_remote():
        result = remote_db.finalize_cluster_state_remote(None, cluster_id, tau_hours=tau_hours)
        _log_event(
            'cluster_state_finalized',
            cluster_id=cluster_id,
            last_doc_at=result.get('last_doc_at'),
            doc_count=result.get('doc_count'),
            unique_source_count=result.get('unique_source_count'),
            platforms=result.get('platforms'),
        )
        return
    n = _recompute_doc_count(conn, cluster_id)
    platforms = _collect_platforms(conn, cluster_id)
    rep = _recompute_representative(conn, cluster_id, tau_hours)
    first_doc_at, last_doc_at = _cluster_time_bounds(conn, cluster_id)
    now_iso = to_utc_iso(datetime.now(timezone.utc))
    conn.execute(
        """UPDATE clusters SET
             doc_count = ?,
             platforms_json = ?,
             first_doc_at = ?,
             last_doc_at = ?,
             last_updated_at = ?,
             representative_vector = ?
           WHERE id = ?""",
        (n, json.dumps(platforms, ensure_ascii=False),
         first_doc_at, last_doc_at, now_iso,
         vu.pack_blob(rep) if rep is not None else None,
         cluster_id),
    )
    # V15.1 Eng-C — unique_source_count recompute (feature-spec R4.1 / R8.1).
    conn.execute(
        """UPDATE clusters
             SET unique_source_count = (
               SELECT COUNT(DISTINCT source_identity)
                 FROM cluster_items
                WHERE cluster_id = ?
                  AND source_identity IS NOT NULL
             )
           WHERE id = ?""",
        (cluster_id, cluster_id),
    )
    last_row = conn.execute(
        "SELECT last_doc_at, unique_source_count FROM clusters WHERE id = ?",
        (cluster_id,),
    ).fetchone()
    last_doc_at = last_row['last_doc_at'] if last_row else None
    unique_source_count = last_row['unique_source_count'] if last_row else None
    _log_event(
        'cluster_state_finalized',
        cluster_id=cluster_id,
        last_doc_at=last_doc_at,
        doc_count=n,
        unique_source_count=unique_source_count,
        platforms=platforms,
    )


def _mark_cluster_touched_by_run(conn, cluster_id: int, run_id: int | None):
    if run_id is None:
        return
    if remote_db.cluster_to_remote():
        remote_db.mark_cluster_touched_by_run_remote(None, cluster_id, run_id)
        return
    conn.execute(
        "UPDATE clusters SET last_touched_run_id = ? WHERE id = ?",
        (run_id, cluster_id),
    )


def _summary_candidate_filter_sql(cluster_alias: str = "c") -> tuple[str, list[str]]:
    category_ids = sorted(visibility_policy.HIGH_VALUE_SINGLE_SOURCE_CATEGORY_ALIASES)
    placeholders = ",".join("?" * len(category_ids))
    return (
        f"""AND (
              {cluster_alias}.unique_source_count >= 2
              OR EXISTS (
                SELECT 1
                  FROM cluster_items ci_summary_candidate
                  JOIN items i_summary_candidate
                    ON i_summary_candidate.id = ci_summary_candidate.item_id
                 WHERE ci_summary_candidate.cluster_id = {cluster_alias}.id
                   AND lower(COALESCE(i_summary_candidate.ai_category, '')) IN ({placeholders})
              )
            )""",
        category_ids,
    )


def _feed_candidate_item_filter_sql(item_alias: str | None = None) -> tuple[str, list[str]]:
    category_ids = sorted(visibility_policy.HIGH_VALUE_SINGLE_SOURCE_CATEGORY_ALIASES)
    placeholders = ",".join("?" * len(category_ids))
    prefix = f"{item_alias}." if item_alias else ""
    return f" AND lower(COALESCE({prefix}ai_category, '')) IN ({placeholders})", category_ids


def _summary_window_filter_sql(
    cluster_alias: str = "c",
    *,
    window_start: str | None = None,
    window_end: str | None = None,
    require_published_at: bool = False,
) -> tuple[str, list[str]]:
    if not window_start and not window_end and not require_published_at:
        return "", []
    expr = (
        "datetime(NULLIF(i_summary_window.published_at, ''))"
        if require_published_at
        else (
            "COALESCE(datetime(NULLIF(i_summary_window.published_at, '')), "
            "datetime(NULLIF(i_summary_window.fetched_at, '')))"
        )
    )
    clauses = [
        f"""EXISTS (
              SELECT 1
                FROM cluster_items ci_summary_window
                JOIN items i_summary_window
                  ON i_summary_window.id = ci_summary_window.item_id
               WHERE ci_summary_window.cluster_id = {cluster_alias}.id"""
    ]
    params: list[str] = []
    if require_published_at:
        clauses.append("AND datetime(NULLIF(i_summary_window.published_at, '')) IS NOT NULL")
    if window_start:
        clauses.append(f"AND {expr} >= datetime(?)")
        params.append(window_start)
    if window_end:
        clauses.append(f"AND {expr} < datetime(?)")
        params.append(window_end)
    clauses.append(")")
    return "AND " + "\n".join(clauses), params


def _clusters_requiring_summary(
    conn,
    bumped_clusters: set[int],
    run_id: int | None,
    *,
    window_start: str | None = None,
    window_end: str | None = None,
    require_published_at: bool = False,
) -> list[int]:
    if run_id is None:
        return sorted(bumped_clusters)
    candidate_sql, candidate_params = _summary_candidate_filter_sql("c")
    window_sql, window_params = _summary_window_filter_sql(
        "c",
        window_start=window_start,
        window_end=window_end,
        require_published_at=require_published_at,
    )
    ids: list[int] = []
    if bumped_clusters:
        placeholders = ",".join("?" * len(bumped_clusters))
        rows = conn.execute(
            f"""SELECT c.id
                  FROM clusters c
                 WHERE c.id IN ({placeholders})
                   AND c.archived = 0
                   AND c.merged_into IS NULL
                   {candidate_sql}
                   {window_sql}
                 ORDER BY c.id ASC""",
            tuple(sorted(bumped_clusters)) + tuple(candidate_params) + tuple(window_params),
        ).fetchall()
        ids = [row['id'] for row in rows]
    else:
        rows = conn.execute(
            f"""SELECT c.id
                  FROM clusters c
                 WHERE c.last_touched_run_id = ?
                   AND c.archived = 0
                   AND c.merged_into IS NULL
                   AND COALESCE(c.published_run_id, -1) != ?
                   {candidate_sql}
                   {window_sql}
                   AND (
                     c.pending_is_visible_in_feed IS NULL
                     OR (
                       c.pending_is_visible_in_feed != 0
                       AND (
                         c.ai_title_draft IS NULL
                         OR c.ai_summary_draft IS NULL
                         OR c.ai_key_points_draft IS NULL
                       )
                     )
                   )
                 ORDER BY c.id ASC""",
            (run_id, run_id, *candidate_params, *window_params),
        ).fetchall()
        ids = [row['id'] for row in rows]

    retry_limit = _positive_int_env(
        "INFO2ACTION_CLUSTER_SUMMARY_RETRY_LIMIT",
        _CLUSTER_SUMMARY_RETRY_LIMIT_DEFAULT,
    )
    retry_window_start = window_start or _retry_window_start_iso()
    if retry_limit > 0 and retry_window_start:
        retry_window_sql, retry_window_params = _summary_window_filter_sql(
            "c",
            window_start=retry_window_start,
            window_end=window_end,
            require_published_at=require_published_at,
        )
        retry_rows = conn.execute(
            f"""SELECT c.id
                  FROM clusters c
                 WHERE c.last_touched_run_id IS NOT NULL
                   AND c.last_touched_run_id != ?
                   AND c.archived = 0
                   AND c.merged_into IS NULL
                   AND COALESCE(c.published_run_id, -1) != c.last_touched_run_id
                   {candidate_sql}
                   {retry_window_sql}
                   AND (
                     c.pending_is_visible_in_feed IS NULL
                     OR (
                       c.pending_is_visible_in_feed != 0
                       AND (
                         c.ai_title_draft IS NULL
                         OR c.ai_summary_draft IS NULL
                         OR c.ai_key_points_draft IS NULL
                       )
                     )
                   )
                 ORDER BY c.last_touched_run_id ASC, c.id ASC
                 LIMIT ?""",
            (run_id, *candidate_params, *retry_window_params, retry_limit),
        ).fetchall()
        if retry_rows:
            _log_event(
                "cluster_summary_retry_backlog_loaded",
                run_id=run_id,
                count=len(retry_rows),
                limit=retry_limit,
                window_start=retry_window_start,
            )
            ids.extend(row['id'] for row in retry_rows)
    return list(dict.fromkeys(ids))


def _remote_summary_window_filter_sql(
    cluster_alias: str = "c",
    *,
    window_start: str | None = None,
    window_end: str | None = None,
    require_published_at: bool = False,
) -> tuple[str, list[Any]]:
    if not window_start and not window_end and not require_published_at:
        return "", []
    expr = (
        "i_summary_window.published_at"
        if require_published_at
        else "COALESCE(i_summary_window.published_at, i_summary_window.fetched_at)"
    )
    clauses = [
        f"""EXISTS (
              SELECT 1
                FROM {remote_db.remote_schema()}.cluster_items ci_summary_window
                JOIN {remote_db.remote_schema()}.items i_summary_window
                  ON i_summary_window.id = ci_summary_window.item_id
               WHERE ci_summary_window.cluster_id = {cluster_alias}.id"""
    ]
    params: list[Any] = []
    if require_published_at:
        clauses.append("AND i_summary_window.published_at IS NOT NULL")
    if window_start:
        clauses.append(f"AND {expr} >= %s")
        params.append(remote_db._timestamp_value(window_start))
    if window_end:
        clauses.append(f"AND {expr} < %s")
        params.append(remote_db._timestamp_value(window_end))
    clauses.append(")")
    return "AND " + "\n".join(clauses), params


def _clusters_requiring_summary_remote(
    bumped_clusters: set[int],
    run_id: int | None,
    *,
    window_start: str | None = None,
    window_end: str | None = None,
    require_published_at: bool = False,
) -> list[int]:
    if run_id is None:
        return sorted(bumped_clusters)
    schema = remote_db.remote_schema()
    category_ids = sorted(visibility_policy.HIGH_VALUE_SINGLE_SOURCE_CATEGORY_ALIASES)
    category_placeholders = ", ".join(["%s"] * len(category_ids))
    candidate_sql = f"""AND (
              c.unique_source_count >= 2
              OR EXISTS (
                SELECT 1
                  FROM {schema}.cluster_items ci_summary_candidate
                  JOIN {schema}.items i_summary_candidate
                    ON i_summary_candidate.id = ci_summary_candidate.item_id
                 WHERE ci_summary_candidate.cluster_id = c.id
                   AND lower(COALESCE(i_summary_candidate.ai_category, '')) IN ({category_placeholders})
              )
            )"""
    window_sql, window_params = _remote_summary_window_filter_sql(
        "c",
        window_start=window_start,
        window_end=window_end,
        require_published_at=require_published_at,
    )
    with remote_db.connect() as pg_conn:
        ids: list[int] = []
        if bumped_clusters:
            cluster_placeholders = ", ".join(["%s"] * len(bumped_clusters))
            rows = pg_conn.execute(
                f"""SELECT c.id
                      FROM {schema}.clusters c
                     WHERE c.id IN ({cluster_placeholders})
                       AND c.archived IS NOT TRUE
                       AND c.merged_into IS NULL
                       {candidate_sql}
                       {window_sql}
                     ORDER BY c.id ASC""",
                tuple(sorted(bumped_clusters)) + tuple(category_ids) + tuple(window_params),
            ).fetchall()
            ids = [int(row['id']) for row in rows]
        else:
            rows = pg_conn.execute(
                f"""SELECT c.id
                      FROM {schema}.clusters c
                     WHERE c.last_touched_run_id = %s
                       AND c.archived IS NOT TRUE
                       AND c.merged_into IS NULL
                       AND COALESCE(c.published_run_id, -1) != %s
                       {candidate_sql}
                       {window_sql}
                       AND (
                         c.pending_is_visible_in_feed IS NULL
                         OR (
                           c.pending_is_visible_in_feed != 0
                           AND (
                             c.ai_title_draft IS NULL
                             OR c.ai_summary_draft IS NULL
                             OR c.ai_key_points_draft IS NULL
                           )
                         )
                       )
                     ORDER BY c.id ASC""",
                (run_id, run_id, *category_ids, *window_params),
            ).fetchall()
            ids = [int(row['id']) for row in rows]

        retry_limit = _positive_int_env(
            "INFO2ACTION_CLUSTER_SUMMARY_RETRY_LIMIT",
            _CLUSTER_SUMMARY_RETRY_LIMIT_DEFAULT,
        )
        retry_window_start = window_start or _retry_window_start_iso()
        if retry_limit > 0 and retry_window_start:
            retry_window_sql, retry_window_params = _remote_summary_window_filter_sql(
                "c",
                window_start=retry_window_start,
                window_end=window_end,
                require_published_at=require_published_at,
            )
            retry_rows = pg_conn.execute(
                f"""SELECT c.id
                      FROM {schema}.clusters c
                     WHERE c.last_touched_run_id IS NOT NULL
                       AND c.last_touched_run_id != %s
                       AND c.archived IS NOT TRUE
                       AND c.merged_into IS NULL
                       AND COALESCE(c.published_run_id, -1) != c.last_touched_run_id
                       {candidate_sql}
                       {retry_window_sql}
                       AND (
                         c.pending_is_visible_in_feed IS NULL
                         OR (
                           c.pending_is_visible_in_feed != 0
                           AND (
                             c.ai_title_draft IS NULL
                             OR c.ai_summary_draft IS NULL
                             OR c.ai_key_points_draft IS NULL
                           )
                         )
                       )
                     ORDER BY c.last_touched_run_id ASC, c.id ASC
                     LIMIT %s""",
                (run_id, *category_ids, *retry_window_params, retry_limit),
            ).fetchall()
            if retry_rows:
                _log_event(
                    "cluster_summary_retry_backlog_loaded",
                    run_id=run_id,
                    count=len(retry_rows),
                    limit=retry_limit,
                    window_start=retry_window_start,
                )
                ids.extend(int(row['id']) for row in retry_rows)
    return list(dict.fromkeys(ids))


def _create_singleton(conn, item_id: str, vector: np.ndarray, first_doc_at: str,
                      *, source_identity: str | None = None,
                      run_id: int | None = None) -> int:
    """Create a singleton cluster + insert seed item.

    V2: also writes source_identity on the cluster_items row so even
    singletons (the most common path when V2 LLM judges 'no match') start with
    a populated source_identity. If caller doesn't pass one, we look up the
    item row and call _compute_source_identity ourselves.
    """
    if remote_db.cluster_to_remote():
        cid = remote_db.create_singleton_cluster_remote(
            None,
            item_id,
            vector,
            first_doc_at,
            source_identity=source_identity,
            run_id=run_id,
            tau_hours=_TAU_HOURS_DEFAULT,
        )
        _log_event('cluster_new', cluster_id=cid, item_id=item_id)
        return cid
    event_time = to_utc_iso(first_doc_at) or to_utc_iso(datetime.now(timezone.utc))
    now_iso = to_utc_iso(datetime.now(timezone.utc))
    cur = conn.execute(
        """INSERT INTO clusters
             (first_doc_at, last_doc_at, last_updated_at,
              representative_vector, doc_count, is_visible_in_feed,
              created_run_id, last_touched_run_id)
           VALUES (?, ?, ?, ?, 1, 0, ?, ?)""",
        (event_time, event_time, now_iso, vu.pack_blob(vector), run_id, run_id),
    )
    cid = cur.lastrowid
    if source_identity is None:
        seed = conn.execute(
            "SELECT id, url FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if seed is not None:
            source_identity = _compute_source_identity(seed)
    _add_item_to_cluster(conn, cid, item_id,
                         rank_in_cluster=0, is_primary_source=1,
                         source_identity=source_identity)
    _finalize_cluster_state(conn, cid, tau_hours=_TAU_HOURS_DEFAULT)
    _mark_cluster_touched_by_run(conn, cid, run_id)
    _log_event('cluster_new', cluster_id=cid, item_id=item_id)
    return cid


def _parse_merge_decision(raw: str | None) -> bool | None:
    """Parse LLM merge-decision JSON output.

    Expected shape: ``{"same_event": "yes" | "no" | true | false, ...}``.
    Tolerates markdown code fences (```json ... ```) and surrounding whitespace.

    Returns:
        ``True`` if same_event is "yes" / true, ``False`` if "no" / false,
        ``None`` if parsing failed or the field is missing/unsupported.
    """
    if not raw:
        return None
    text = raw.strip()
    # Strip markdown fences if present
    if text.startswith('```'):
        lines = [ln for ln in text.splitlines() if not ln.startswith('```')]
        text = '\n'.join(lines).strip()
    text = text.strip('`').strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    if 'same_event' not in obj:
        return None
    val = obj.get('same_event')
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        v = val.strip().lower()
        if v == 'yes':
            return True
        if v == 'no':
            return False
        return None
    return None


def _default_llm_judge(doc_a: str, doc_b: str, *, scenario: str,
                        api_key: str, api_base: str | None, model: str) -> bool:
    """V1 single-pair merge-decision judge.

    DEPRECATED in v15.1: Stage 2 改为 top-10 一次大调用（``_judge_top_k``）。
    本函数保留以兼容旧调用方（merge_detector.py 观察期 + 旧测试），新代码不要再用。
    Conservative: any parsing failure or missing field → ``False`` (R7.2 宁漏不错合).
    """
    system = load_prompt(
        '09_cluster_merge_decision.md',
        doc_a_content=doc_a, doc_b_content=doc_b, scenario=scenario,
    ) or ''
    try:
        raw = summary_writer._call_llm_chat(
            api_key=api_key, api_base=api_base, model=model,
            system_prompt=system, user_content=f'A:\n{doc_a}\n\nB:\n{doc_b}',
            max_tokens=256, timeout=30, source='legacy_pair_judge',
        )
    except Exception as e:
        logger.warning('llm_judge error: %s', e)
        _log_event('llm_judge_fail', err=str(e))
        raise
    decision = _parse_merge_decision(raw)
    if decision is None:
        preview = (raw or '').strip().replace('\n', ' ')[:200]
        _log_event('llm_judge_parse_fail',
                   raw_chars=len(raw or ''),
                   raw_preview=preview)
        return False
    return decision


# ─────────────────────────────────────────────────────────────────────────────
# V2 Stage 2: top-10 LLM judge (one call decides all candidates)
# ─────────────────────────────────────────────────────────────────────────────

# Validation vocabularies for LLM-returned matches[].
_CONFIDENCE_LEVELS = ('high', 'medium', 'low')
_RELATIONSHIP_LEVELS = (
    'same_event', 'direct_commentary', 'follow_up_update',
    'same_topic_only', 'unrelated',
)
# Direct-ness rank for ordering same_event=true matches. Lower = more direct.
_RELATIONSHIP_DIRECTNESS = {
    'same_event': 0,
    'follow_up_update': 1,
    'direct_commentary': 2,
    # Should never appear with same_event=true, but rank for stability.
    'same_topic_only': 3,
    'unrelated': 4,
}
_CONFIDENCE_RANK = {'high': 0, 'medium': 1, 'low': 2}


def _build_new_doc_block(item_row, *, max_chars: int = 4000) -> str:
    """Render the new doc as a structured block for the Stage 2 prompt.

    Excludes ``comments_json`` per V2.3 §0.7 (Q13) — comments must NEVER enter
    embedding / Stage 2 / Stage 4 inputs.
    """
    title = (item_row['title'] or '').strip() if 'title' in item_row.keys() else ''
    ai_summary = ''
    ai_key_points_raw = ''
    ai_keywords = ''
    ai_category = ''
    content_type = ''
    content = ''
    platform = ''
    author = ''
    published_at = ''
    keys = item_row.keys() if hasattr(item_row, 'keys') else []
    if 'ai_summary' in keys:
        ai_summary = (item_row['ai_summary'] or '').strip()
    if 'ai_key_points' in keys:
        ai_key_points_raw = (item_row['ai_key_points'] or '').strip()
    if 'ai_keywords' in keys:
        ai_keywords = (item_row['ai_keywords'] or '').strip()
    if 'ai_category' in keys:
        ai_category = (item_row['ai_category'] or '').strip()
    if 'content_type' in keys:
        content_type = (item_row['content_type'] or '').strip()
    if 'content' in keys:
        content = (item_row['content'] or '').strip()
    if 'platform' in keys:
        platform = (item_row['platform'] or '').strip()
    if 'author_name' in keys:
        author = (item_row['author_name'] or '').strip()
    if 'published_at' in keys:
        published_at = (item_row['published_at'] or '').strip()

    # Render key_points list-form if it parses as JSON list, else fallback raw.
    key_points_block = ''
    if ai_key_points_raw:
        try:
            kps = json.loads(ai_key_points_raw)
            if isinstance(kps, list):
                key_points_block = '\n'.join(f'- {x}' for x in kps if x)
            else:
                key_points_block = ai_key_points_raw
        except (json.JSONDecodeError, ValueError, TypeError):
            key_points_block = ai_key_points_raw

    parts = [
        f'doc_id: {item_row["id"] if "id" in keys else ""}',
        f'platform: {platform}',
        f'author: {author}',
        f'published_at: {published_at}',
        f'category: {ai_category}',
        f'content_type: {content_type}',
        f'title: {title}',
        '',
        'summary:',
        ai_summary or '(none)',
        '',
        'key_points:',
        key_points_block or '(none)',
    ]
    if ai_keywords:
        parts.append('')
        parts.append(f'keywords: {ai_keywords}')
    if content:
        parts.append('')
        parts.append('content:')
        parts.append(content)

    text = '\n'.join(parts)
    if len(text) > max_chars:
        text = text[:max_chars]
    return text


def _build_candidate_block(candidate: dict, *, max_summary_chars: int = 600,
                            max_key_point_lines: int = 3) -> str:
    """Render one candidate cluster as a 5-8 line block for Stage 2 prompt."""
    cid = candidate.get('cluster_id')
    ai_title = (candidate.get('ai_title') or '').strip()
    ai_summary = (candidate.get('ai_summary') or '').strip()
    if len(ai_summary) > max_summary_chars:
        ai_summary = ai_summary[:max_summary_chars]
    ai_key_points_raw = candidate.get('ai_key_points') or ''
    key_points_block = ''
    try:
        kps = json.loads(ai_key_points_raw) if ai_key_points_raw else []
        if isinstance(kps, list):
            kps = kps[:max_key_point_lines]
            key_points_block = '\n'.join(f'  - {x}' for x in kps if x)
    except (json.JSONDecodeError, ValueError, TypeError):
        key_points_block = '  ' + ai_key_points_raw[:200]

    doc_count = candidate.get('doc_count', 0)
    first_doc_at = candidate.get('first_doc_at') or ''
    last_doc_at = candidate.get('last_doc_at') or ''
    cosine = candidate.get('cosine')
    cosine_str = f'{cosine:.4f}' if isinstance(cosine, (int, float)) else 'n/a'

    parts = [
        f'cluster_id: {cid}',
        f'ai_title: {ai_title}',
        f'doc_count: {doc_count}',
        f'time_range: {first_doc_at} → {last_doc_at}',
        f'cosine_recall: {cosine_str}',
        f'ai_summary: {ai_summary}',
    ]
    if key_points_block:
        parts.append('ai_key_points:')
        parts.append(key_points_block)
    return '\n'.join(parts)


def _build_judge_input(item_row, candidates: list[dict]) -> tuple[str, str]:
    """Construct ``new_doc`` and ``candidate_clusters`` prompt blocks.

    Returns ``(new_doc_block, candidate_clusters_block)``. Both intended to be
    substituted into ``10_cluster_top10_judge.md`` placeholders. Total prompt
    input target ≤ 8K tokens (V2.3 §5.4) — we cap candidates to safe rendered
    sizes; full LLM input is still bounded by candidate count (≤ 10).
    """
    new_doc = _build_new_doc_block(item_row)
    blocks = [_build_candidate_block(c) for c in candidates]
    candidate_clusters = '\n\n---\n\n'.join(blocks)
    return new_doc, candidate_clusters


def _parse_top_k_response(raw: str | None) -> dict | None:
    """Strict parser for the V2 Stage 2 LLM JSON output.

    Returns ``{'fingerprint': dict, 'matches': list[dict]}`` on success, else
    ``None``. Tolerates markdown fences. Discards matches missing required
    keys (cluster_id / same_event / confidence / relationship); coerces invalid
    enum values to the conservative side (same_event=False).
    """
    if not raw:
        return None
    text = raw.strip()
    if text.startswith('```'):
        lines = [ln for ln in text.splitlines() if not ln.startswith('```')]
        text = '\n'.join(lines).strip()
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    matches_raw = obj.get('matches')
    if not isinstance(matches_raw, list):
        return None

    cleaned: list[dict] = []
    for m in matches_raw:
        if not isinstance(m, dict):
            continue
        cid = m.get('cluster_id')
        try:
            cid_int = int(cid)
        except (TypeError, ValueError):
            continue
        same = m.get('same_event')
        if isinstance(same, str):
            same = same.strip().lower() in ('true', 'yes', '1')
        same_event = bool(same)
        conf = (m.get('confidence') or '').strip().lower()
        if conf not in _CONFIDENCE_LEVELS:
            conf = 'low'
        rel = (m.get('relationship') or '').strip().lower()
        if rel not in _RELATIONSHIP_LEVELS:
            rel = 'unrelated'
        # Relationship/same_event consistency (defensive — prompt also enforces
        # this). If LLM contradicts itself, downgrade.
        if same_event and rel in ('same_topic_only', 'unrelated'):
            same_event = False
        if (not same_event) and rel in (
            'same_event', 'direct_commentary', 'follow_up_update'
        ):
            rel = 'unrelated'
        cleaned.append({
            'cluster_id': cid_int,
            'same_event': same_event,
            'confidence': conf,
            'relationship': rel,
            'subject_check': str(m.get('subject_check') or '')[:500],
            'action_check': str(m.get('action_check') or '')[:500],
            'time_check': str(m.get('time_check') or '')[:500],
            'rationale': str(m.get('rationale') or '')[:500],
        })

    fingerprint = obj.get('new_doc_fingerprint')
    if not isinstance(fingerprint, dict):
        fingerprint = {}
    return {'fingerprint': fingerprint, 'matches': cleaned}


def _judge_top_k(
    item_row,
    candidates: list[dict],
    *,
    api_key: str,
    api_base: str | None,
    model: str,
    timeout: int = 300,
    max_tokens: int = 16384,
    max_5xx_retries: int = 3,
    max_parse_retries: int = 3,
) -> dict:
    """Single LLM call to judge new doc vs all top-K candidates.

    Returns one of three shapes (always a dict, never raises for transient
    errors — caller decides fallback path):

      Success:    {'fingerprint': dict, 'matches': list[dict],
                   'estimated_input_tokens': int}
      Failure:    {'error': str, 'detail': str,
                   'estimated_input_tokens': int}

    Logs ``cluster_judge_llm_call`` (success or failure) with input token
    estimate and error metadata. Never silently returns False — feature-spec
    R3.3 / 关键铁律 (no silent exception swallow).
    """
    new_doc_block, candidate_clusters_block = _build_judge_input(
        item_row, candidates,
    )
    system_prompt = load_prompt(
        '10_cluster_top10_judge.md',
        new_doc=new_doc_block,
        candidate_clusters=candidate_clusters_block,
    ) or ''
    # 4 chars/token rough estimate (Chinese text averages closer to 1.5 chars/token
    # but we keep the generic estimate for cross-language consistency in logs).
    estimated_input_tokens = max(1, len(system_prompt) // 4)
    item_id = item_row['id'] if 'id' in (item_row.keys() if hasattr(item_row, 'keys') else []) else None

    parse_attempts = max(1, int(max_parse_retries or 1))
    for parse_attempt in range(parse_attempts):
        raw = None
        user_content = (
            'Return the matches[] JSON now.'
            if parse_attempt == 0 else
            'Your previous response was invalid or truncated JSON. '
            'Regenerate the full strict JSON object from scratch now. '
            'Return only JSON, with every candidate represented in matches[].'
        )
        try:
            for attempt in range(max(1, int(max_5xx_retries or 1))):
                try:
                    raw = summary_writer._call_llm_chat(
                        api_key=api_key, api_base=api_base, model=model,
                        system_prompt=system_prompt,
                        user_content=user_content,
                        max_tokens=max_tokens,
                        timeout=timeout,
                        source='cluster_judge',
                    )
                    break
                except urllib.error.HTTPError as e:
                    if e.code not in {500, 502, 503, 504}:
                        raise
                    if attempt >= max(1, int(max_5xx_retries or 1)) - 1:
                        raise
                    delay = min(30.0, 2.0 * (2 ** attempt))
                    _log_event(
                        'cluster_judge_llm_retry',
                        item_id=item_id,
                        candidate_count=len(candidates),
                        status=e.code,
                        attempt=attempt + 1,
                        parse_attempt=parse_attempt + 1,
                        delay_sec=delay,
                    )
                    time.sleep(delay)
            if raw is None:
                raise RuntimeError('empty LLM judge response')
        except (ai_provider_guard.ProviderCooldown, ai_provider_guard.ProviderActionRequired) as e:
            _log_event(
                'cluster_judge_llm_fail',
                item_id=item_id,
                candidate_count=len(candidates),
                err=str(e),
                retryable=True,
            )
            return {
                'error': 'provider_rate_limited',
                'detail': str(e),
                'retryable': True,
                'estimated_input_tokens': estimated_input_tokens,
            }
        except urllib.error.HTTPError as e:
            if e.code == 429:
                _log_event(
                    'cluster_judge_llm_fail',
                    item_id=item_id,
                    candidate_count=len(candidates),
                    err=str(e),
                    retryable=True,
                )
                return {
                    'error': 'provider_rate_limited',
                    'detail': str(e),
                    'retryable': True,
                    'estimated_input_tokens': estimated_input_tokens,
                }
            if e.code in {500, 502, 503, 504}:
                _log_event(
                    'cluster_judge_llm_call',
                    item_id=item_id,
                    candidate_count=len(candidates),
                    estimated_input_tokens=estimated_input_tokens,
                    actual_error=str(e),
                    input_too_long=False,
                )
                _log_event(
                    'cluster_judge_llm_fail',
                    item_id=item_id,
                    candidate_count=len(candidates),
                    err=str(e),
                    retryable=True,
                )
                return {
                    'error': 'provider_transient_5xx',
                    'detail': str(e),
                    'retryable': True,
                    'estimated_input_tokens': estimated_input_tokens,
                }
            _log_event(
                'cluster_judge_llm_call',
                item_id=item_id,
                candidate_count=len(candidates),
                estimated_input_tokens=estimated_input_tokens,
                actual_error=str(e),
                input_too_long=False,
            )
            _log_event(
                'cluster_judge_llm_fail',
                item_id=item_id,
                candidate_count=len(candidates),
                err=str(e),
            )
            return {
                'error': 'llm_failed',
                'detail': str(e),
                'estimated_input_tokens': estimated_input_tokens,
            }
        except Exception as e:
            _log_event(
                'cluster_judge_llm_call',
                item_id=item_id,
                candidate_count=len(candidates),
                estimated_input_tokens=estimated_input_tokens,
                actual_error=str(e),
                input_too_long=False,
            )
            _log_event(
                'cluster_judge_llm_fail',
                item_id=item_id,
                candidate_count=len(candidates),
                err=str(e),
            )
            return {
                'error': 'llm_failed',
                'detail': str(e),
                'estimated_input_tokens': estimated_input_tokens,
            }

        parsed = _parse_top_k_response(raw)
        if parsed is not None:
            _log_event(
                'cluster_judge_llm_call',
                item_id=item_id,
                candidate_count=len(candidates),
                estimated_input_tokens=estimated_input_tokens,
                actual_error=None,
                input_too_long=False,
                parse_attempt=parse_attempt + 1,
                match_count=len(parsed['matches']),
            )
            parsed['estimated_input_tokens'] = estimated_input_tokens
            return parsed

        preview = (raw or '').strip().replace('\n', ' ')[:200]
        _log_event(
            'cluster_judge_llm_call',
            item_id=item_id,
            candidate_count=len(candidates),
            estimated_input_tokens=estimated_input_tokens,
            actual_error='parse_fail',
            input_too_long=False,
            parse_attempt=parse_attempt + 1,
            raw_preview=preview,
        )
        _log_event(
            'cluster_judge_llm_fail',
            item_id=item_id,
            candidate_count=len(candidates),
            reason='parse_fail',
            raw_chars=len(raw or ''),
            parse_attempt=parse_attempt + 1,
            raw_preview=preview,
        )
        if parse_attempt < parse_attempts - 1:
            _log_event(
                'cluster_judge_llm_parse_retry',
                item_id=item_id,
                candidate_count=len(candidates),
                parse_attempt=parse_attempt + 1,
                raw_chars=len(raw or ''),
            )
            time.sleep(min(3.0, 0.5 * (parse_attempt + 1)))
            continue
        return {
            'error': 'parse_fail',
            'detail': preview,
            'retryable': True,
            'estimated_input_tokens': estimated_input_tokens,
        }

    return {
        'error': 'parse_fail',
        'detail': 'exhausted parse retries',
        'retryable': True,
        'estimated_input_tokens': estimated_input_tokens,
    }


def _select_cluster_from_matches(
    matches: list[dict], candidates: list[dict]
) -> tuple[int | None, str, list[int]]:
    """Code-layer selection over LLM matches[] (feature-spec R3.2).

    Filters ``same_event=True AND confidence in (high, medium)``, sorts by
    ``(confidence DESC, relationship 直接度, cosine DESC)``, returns:

      ``(selected_cluster_id, selection_reason, possible_merge_candidates)``

    selection_reason values:
      * ``'top-confidence-match'`` — at least one same_event match selected
      * ``'no-same-event-match'`` — no candidate marked same_event=True
      * ``'all-low-confidence'`` — same_event=True candidates exist but all are
        low confidence
    """
    cosine_by_cid = {c['cluster_id']: c.get('cosine', 0.0) for c in candidates}
    same_event_matches = [m for m in matches if m.get('same_event') is True]
    if not same_event_matches:
        return None, 'no-same-event-match', []

    qualified = [
        m for m in same_event_matches
        if m.get('confidence') in ('high', 'medium')
    ]
    if not qualified:
        return None, 'all-low-confidence', []

    qualified.sort(key=lambda m: (
        _CONFIDENCE_RANK.get(m.get('confidence'), 99),
        _RELATIONSHIP_DIRECTNESS.get(m.get('relationship'), 99),
        -float(cosine_by_cid.get(m.get('cluster_id'), 0.0) or 0.0),
    ))
    selected = qualified[0]['cluster_id']
    possible_merge = [m['cluster_id'] for m in qualified[1:]]
    return selected, 'top-confidence-match', possible_merge


def _write_judge_log(
    conn,
    *,
    item_id: str,
    candidate_cluster_ids: list[int],
    estimated_input_tokens: int | None,
    matches: list[dict] | None,
    selected_cluster_id: int | None,
    selection_reason: str,
    possible_merge_candidates: list[int],
    decision_model: str,
) -> int | None:
    """Insert a row into ``cluster_judge_log`` and return its rowid.

    Schema (db.py): id / item_id / candidate_cluster_ids (TEXT JSON) /
    llm_input_tokens / llm_output_tokens / matches_json / selected_cluster_id /
    selection_reason / possible_merge_candidates (TEXT JSON) / decision_model /
    created_at.

    LLM output token count is best-effort (we don't have a tokenizer locally),
    so we currently leave it NULL and rely on input token estimate for cost
    audit.
    """
    if remote_db.cluster_to_remote():
        try:
            return remote_db.write_judge_log_remote(
                None,
                item_id=item_id,
                candidate_cluster_ids=candidate_cluster_ids,
                estimated_input_tokens=estimated_input_tokens,
                matches=matches,
                selected_cluster_id=selected_cluster_id,
                selection_reason=selection_reason,
                possible_merge_candidates=possible_merge_candidates,
                decision_model=decision_model,
            )
        except Exception as e:  # pragma: no cover - defensive parity with SQLite path
            _log_event(
                'cluster_judge_log_write_fail',
                item_id=item_id,
                err=str(e),
            )
            return None
    try:
        cur = conn.execute(
            """INSERT INTO cluster_judge_log
                 (item_id, candidate_cluster_ids, llm_input_tokens,
                  llm_output_tokens, matches_json, selected_cluster_id,
                  selection_reason, possible_merge_candidates, decision_model)
               VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)""",
            (
                item_id,
                json.dumps(candidate_cluster_ids, ensure_ascii=False),
                estimated_input_tokens,
                json.dumps(matches, ensure_ascii=False) if matches is not None else None,
                selected_cluster_id,
                selection_reason,
                json.dumps(possible_merge_candidates, ensure_ascii=False),
                decision_model,
            ),
        )
        return cur.lastrowid
    except Exception as e:  # pragma: no cover — defensive: log table missing in legacy DB
        _log_event(
            'cluster_judge_log_write_fail',
            item_id=item_id,
            err=str(e),
        )
        return None


def _load_pending_cluster_items(
    conn,
    *,
    run_id: int | None,
    run_items_scope: str,
    window_start: str | None,
    window_end: str | None,
    require_published_at: bool,
    feed_candidates_only: bool,
) -> list[dict]:
    """Load embedded, unclustered items from the active clustering backend."""
    ai_ready_filter = (
        " AND ai_summary IS NOT NULL AND ai_summary != ''"
        if run_id is not None else ""
    )
    window_active = bool(window_start or window_end)
    order_direction = "DESC" if window_active else "ASC"

    if remote_db.cluster_to_remote():
        run_filter, run_params = _remote_run_item_scope_sql(run_id, run_items_scope)
        window_filter, window_params = _remote_time_filter(
            window_start,
            window_end,
            require_published_at=require_published_at,
        )
        feed_candidate_filter = ""
        feed_candidate_params: list[str] = []
        if feed_candidates_only:
            category_ids = sorted(visibility_policy.HIGH_VALUE_SINGLE_SOURCE_CATEGORY_ALIASES)
            placeholders = ",".join(["%s"] * len(category_ids))
            feed_candidate_filter = f" AND lower(COALESCE(ai_category, '')) IN ({placeholders})"
            feed_candidate_params = category_ids
        params: list[Any] = list(run_params)
        params.extend(window_params)
        params.extend(feed_candidate_params)
        order_expr = (
            "published_at"
            if require_published_at
            else "COALESCE(published_at, fetched_at)"
        )
        with remote_db.connect() as pg_conn:
            rows = pg_conn.execute(
                f"""SELECT id, embedding::text AS embedding, content, ai_summary,
                          ai_key_points, ai_keywords, ai_category, content_type,
                          title, platform, author_name, url,
                          COALESCE(published_at, fetched_at) AS published_at
                     FROM {remote_db.remote_schema()}.items
                    WHERE embedding IS NOT NULL
                      AND cluster_id IS NULL
                      {ai_ready_filter}
                      {run_filter}
                      {window_filter}
                      {feed_candidate_filter}
                    ORDER BY {order_expr} {order_direction}""",
                tuple(params),
            ).fetchall()
            if run_id is not None:
                retry_limit = _positive_int_env(
                    "INFO2ACTION_CLUSTER_ITEM_RETRY_LIMIT",
                    _CLUSTER_ITEM_RETRY_LIMIT_DEFAULT,
                )
                retry_window_start = window_start or _retry_window_start_iso()
                if retry_limit > 0 and retry_window_start:
                    retry_window_filter, retry_window_params = _remote_time_filter(
                        retry_window_start,
                        window_end,
                        require_published_at=require_published_at,
                    )
                    exclude_filter, exclude_params = _remote_run_item_exclusion_sql(
                        run_id,
                        run_items_scope,
                    )
                    retry_params: list[Any] = []
                    retry_params.extend(retry_window_params)
                    retry_params.extend(feed_candidate_params)
                    retry_params.extend(exclude_params)
                    retry_params.append(retry_limit)
                    retry_rows = pg_conn.execute(
                        f"""SELECT id, embedding::text AS embedding, content, ai_summary,
                                  ai_key_points, ai_keywords, ai_category, content_type,
                                  title, platform, author_name, url,
                                  COALESCE(published_at, fetched_at) AS published_at
                             FROM {remote_db.remote_schema()}.items
                            WHERE embedding IS NOT NULL
                              AND cluster_id IS NULL
                              AND ai_summary IS NOT NULL AND ai_summary != ''
                              {retry_window_filter}
                              {feed_candidate_filter}
                              {exclude_filter}
                            ORDER BY {order_expr} ASC
                            LIMIT %s""",
                        tuple(retry_params),
                    ).fetchall()
                    if retry_rows:
                        _log_event(
                            "cluster_item_retry_backlog_loaded",
                            run_id=run_id,
                            count=len(retry_rows),
                            limit=retry_limit,
                            window_start=retry_window_start,
                        )
                        rows = _dedupe_rows_by_id(list(rows) + list(retry_rows))
        pending: list[dict] = []
        for row in rows:
            item = dict(row)
            parsed_vector = remote_db.pg_vector_to_list(item.get("embedding"))
            if parsed_vector is None:
                continue
            item["embedding"] = vu.pack_blob(np.asarray(parsed_vector, dtype=np.float32))
            item["published_at"] = to_utc_iso(item.get("published_at"))
            pending.append(item)
        return pending

    run_filter, run_params = _run_item_scope_sql(run_id, run_items_scope)
    window_filter, window_params = _window_sql_filter(
        window_start,
        window_end,
        require_published_at=require_published_at,
    )
    feed_candidate_filter = ""
    feed_candidate_params: list[str] = []
    if feed_candidates_only:
        feed_candidate_filter, feed_candidate_params = _feed_candidate_item_filter_sql()
    params = list(run_params)
    params.extend(window_params)
    params.extend(feed_candidate_params)
    order_expr = _window_time_expr(require_published_at=require_published_at)
    rows = conn.execute(
        """SELECT id, embedding, content, ai_summary, ai_key_points,
                  ai_keywords, ai_category, content_type,
                  title, platform, author_name, url,
                  COALESCE(published_at, fetched_at) AS published_at
           FROM items
           WHERE embedding IS NOT NULL AND cluster_id IS NULL
             {ai_ready_filter}
             {run_filter}
             {window_filter}
             {feed_candidate_filter}
           ORDER BY {order_expr} {order_direction}"""
        .format(
            ai_ready_filter=ai_ready_filter,
            run_filter=run_filter,
            window_filter=window_filter,
            feed_candidate_filter=feed_candidate_filter,
            order_expr=order_expr,
            order_direction=order_direction,
        ),
        tuple(params),
    ).fetchall()
    if run_id is not None:
        retry_limit = _positive_int_env(
            "INFO2ACTION_CLUSTER_ITEM_RETRY_LIMIT",
            _CLUSTER_ITEM_RETRY_LIMIT_DEFAULT,
        )
        retry_window_start = window_start or _retry_window_start_iso()
        if retry_limit > 0 and retry_window_start:
            retry_window_filter, retry_window_params = _window_sql_filter(
                retry_window_start,
                window_end,
                require_published_at=require_published_at,
            )
            exclude_filter, exclude_params = _run_item_exclusion_sql(
                run_id,
                run_items_scope,
            )
            retry_params: list[Any] = []
            retry_params.extend(retry_window_params)
            retry_params.extend(feed_candidate_params)
            retry_params.extend(exclude_params)
            retry_params.append(retry_limit)
            retry_rows = conn.execute(
                """SELECT id, embedding, content, ai_summary, ai_key_points,
                          ai_keywords, ai_category, content_type,
                          title, platform, author_name, url,
                          COALESCE(published_at, fetched_at) AS published_at
                   FROM items
                   WHERE embedding IS NOT NULL AND cluster_id IS NULL
                     AND ai_summary IS NOT NULL AND ai_summary != ''
                     {retry_window_filter}
                     {feed_candidate_filter}
                     {exclude_filter}
                   ORDER BY {order_expr} ASC
                   LIMIT ?"""
                .format(
                    retry_window_filter=retry_window_filter,
                    feed_candidate_filter=feed_candidate_filter,
                    exclude_filter=exclude_filter,
                    order_expr=order_expr,
                ),
                tuple(retry_params),
            ).fetchall()
            if retry_rows:
                _log_event(
                    "cluster_item_retry_backlog_loaded",
                    run_id=run_id,
                    count=len(retry_rows),
                    limit=retry_limit,
                    window_start=retry_window_start,
                )
                rows = _dedupe_rows_by_id(list(rows) + list(retry_rows))
    return rows


def run_pipeline(
    conn,
    *,
    provider,
    llm_judge: Callable[[str, str, str], bool] | None = None,
    top_k_judge: Callable[[Any, list[dict]], dict] | None = None,
    api_key: str = '',
    api_base: str | None = None,
    model: str = 'MiniMax-M3',
    tau_hours: float = _TAU_HOURS_DEFAULT,
    candidate_window_days: int = _CANDIDATE_WINDOW_DAYS_DEFAULT,
    summary_max_docs: int = 20,
    skip_summary: bool = False,
    summary_workers: int = 1,
    feed_candidates_only: bool = False,
    top_k: int = 10,
    cosine_min: float = 0.0,
    gray_cosine_min: float | None = None,
    shadow_cosine_min: float | None = None,
    gray_max_temporal_hours: float | None = _GRAY_RECALL_MAX_TEMPORAL_HOURS_DEFAULT,
    temporal_adjacency_days: float | None = _TEMPORAL_ADJACENCY_DAYS_DEFAULT,
    max_merged_span_days: float | None = _MAX_MERGED_SPAN_DAYS_DEFAULT,
    judge_max_tokens: int = 16384,
    judge_workers: int = 20,
    judge_min_interval_sec: float = 0.8,
    run_id: int | None = None,
    run_items_scope: str = _RUN_ITEMS_SCOPE_TAGGED,
    window_start: str | None = None,
    window_end: str | None = None,
    require_published_at: bool = False,
    publish: bool = True,
    # Deprecated parameters kept for backward-compat with V1 callers/tests:
    high_confidence: float | None = None,  # noqa: ARG001 — ignored in V2
    boundary_low: float | None = None,     # noqa: ARG001 — ignored in V2
    merge_check: bool = False,             # noqa: ARG001 — V2 doesn't auto-merge
) -> dict:
    """Run the V2 event-clustering pipeline end-to-end.

    V2 flow (feature-spec R2/R3/R4):
      Stage 0: embed pending items
      Stage 1: top-K cosine recall against event-time-adjacent clusters
      Stage 2: ONE LLM call judges new doc vs all top-K candidates
               -> matches[] dict. Code layer picks best (confidence,
               relationship, cosine). In run-scoped publishing, LLM failures
               leave the item unclustered for retry instead of creating a false
               singleton.
      Stage 3: _finalize_cluster_state (Commit 4 will add unique_source_count).
      Stage 4: regenerate summary (kept as-is; Eng-D will rewrite prompt).

    ``top_k_judge`` lets tests inject a mock; if None, we wire the default
    LLM-backed judge. ``llm_judge`` is V1's single-pair callable — accepted but
    ignored in V2 (kept for backward-compat with existing test callers that
    pass it positionally/by keyword).
    """
    stats = {'embedded': 0, 'assigned_existing': 0, 'new_singletons': 0,
             'judged_with_match': 0, 'judged_no_match': 0,
             'judge_llm_failed': 0,
             'summary_regenerated': 0, 'summary_failed': 0,
             'published_clusters': 0}
    timings_sec: dict[str, float] = {}
    recall_stats = {'calls': 0, 'candidates_total': 0, 'candidates_max': 0}

    def _record_timing(name: str, started_at: float) -> None:
        elapsed = time.monotonic() - started_at
        timings_sec[name] = round(timings_sec.get(name, 0.0) + elapsed, 3)

    started = time.monotonic()
    stats['embedded'] = _embed_pending_items(
        conn,
        provider,
        run_id=run_id,
        run_items_scope=run_items_scope,
        window_start=window_start,
        window_end=window_end,
        require_published_at=require_published_at,
    )
    _record_timing('embed_pending_items', started)

    # Default V2 judge: wires _judge_top_k with the configured LLM credentials.
    chat_gate = _ChatGate(judge_min_interval_sec if judge_workers > 1 else 0.0)

    if top_k_judge is None:
        def _default_top_k_judge(item_row, candidates: list[dict]) -> dict:
            chat_gate.wait()
            return _judge_top_k(
                item_row, candidates,
                api_key=api_key, api_base=api_base, model=model,
                max_tokens=judge_max_tokens,
            )
        top_k_judge = _default_top_k_judge

    # Pull the full set of fields we need so we can build a structured Stage 2
    # input block without a second SELECT per item.
    started = time.monotonic()
    pending = _load_pending_cluster_items(
        conn,
        run_id=run_id,
        run_items_scope=run_items_scope,
        window_start=window_start,
        window_end=window_end,
        require_published_at=require_published_at,
        feed_candidates_only=feed_candidates_only,
    )
    _record_timing('load_pending_items', started)
    stats['pending_items'] = len(pending)

    bumped_clusters: set[int] = set()
    recall_cosine_min = _recall_floor(
        cosine_min=cosine_min,
        gray_cosine_min=gray_cosine_min,
        shadow_cosine_min=shadow_cosine_min,
    )

    def _process_judge_result(item, new_vec, candidates, judge_result):
        """Apply Stage 2 LLM judge result: write judge_log + DB state. Runs on
        main thread (sequential) so cluster state stays consistent."""
        nonlocal stats
        candidate_cluster_ids = [c['cluster_id'] for c in candidates]
        if 'error' in judge_result:
            if run_id is not None:
                stats['judge_llm_failed'] += 1
                _log_event(
                    'cluster_item_deferred_for_retry',
                    run_id=run_id,
                    item_id=item['id'],
                    err=judge_result.get('detail'),
                    error=judge_result.get('error'),
                    retryable=judge_result.get('retryable', True),
                )
                return
            stats['judge_llm_failed'] += 1
            _write_judge_log(
                conn,
                item_id=item['id'],
                candidate_cluster_ids=candidate_cluster_ids,
                estimated_input_tokens=judge_result.get('estimated_input_tokens'),
                matches=None,
                selected_cluster_id=None,
                selection_reason='llm-failed-fallback-singleton',
                possible_merge_candidates=[],
                decision_model=model,
            )
            new_cid = _create_singleton(
                conn, item['id'], new_vec,
                item['published_at'] or datetime.now(timezone.utc).isoformat(),
                run_id=run_id,
            )
            bumped_clusters.add(new_cid)
            stats['new_singletons'] += 1
            return

        matches = judge_result.get('matches') or []
        selected_cluster_id, selection_reason, possible_merge = (
            _select_cluster_from_matches(matches, candidates)
        )
        join_decision_id = _write_judge_log(
            conn,
            item_id=item['id'],
            candidate_cluster_ids=candidate_cluster_ids,
            estimated_input_tokens=judge_result.get('estimated_input_tokens'),
            matches=matches,
            selected_cluster_id=selected_cluster_id,
            selection_reason=selection_reason,
            possible_merge_candidates=possible_merge,
            decision_model=model,
        )
        if selected_cluster_id is not None and selected_cluster_id not in candidate_cluster_ids:
            _log_event(
                'cluster_judge_invalid_selection',
                item_id=item['id'],
                selected_cluster_id=selected_cluster_id,
                candidate_cluster_ids=candidate_cluster_ids,
            )
            selected_cluster_id = None
        if selected_cluster_id is not None:
            stats['judged_with_match'] += 1
            _log_event(
                'cluster_join_judged',
                item_id=item['id'], cluster_id=selected_cluster_id,
                selection_reason=selection_reason,
                possible_merge_candidates=possible_merge,
            )
            if possible_merge:
                _log_event(
                    'possible_cluster_merge',
                    item_id=item['id'],
                    selected_cluster_id=selected_cluster_id,
                    possible_merge_candidates=possible_merge,
                    applied=False,
                )
            _add_item_to_cluster(
                conn, selected_cluster_id, item['id'],
                source_identity=_compute_source_identity(item),
                join_decision_id=join_decision_id,
            )
            _finalize_cluster_state(conn, selected_cluster_id, tau_hours=tau_hours)
            _mark_cluster_touched_by_run(conn, selected_cluster_id, run_id)
            bumped_clusters.add(selected_cluster_id)
        else:
            stats['judged_no_match'] += 1
            new_cid = _create_singleton(
                conn, item['id'], new_vec,
                item['published_at'] or datetime.now(timezone.utc).isoformat(),
                run_id=run_id,
            )
            bumped_clusters.add(new_cid)
            stats['new_singletons'] += 1
            _log_event(
                'cluster_singleton_no_match',
                item_id=item['id'], cluster_id=new_cid,
                selection_reason=selection_reason,
            )

    # Stage 2 prefetch: bounded in-flight LLM calls; main thread keeps Stage 1 +
    # DB write sequential so cluster state stays consistent. New clusters
    # created while LLM calls are in-flight are invisible to those in-flight
    # candidates — accepted trade-off (merge_detector compensates).
    from concurrent.futures import ThreadPoolExecutor
    judge_workers = max(1, int(judge_workers or 1))
    pool = ThreadPoolExecutor(max_workers=judge_workers)
    in_flight: list = []  # (item, new_vec, candidates, future)

    def _checkpoint_item():
        if run_id is not None and not remote_db.cluster_to_remote():
            conn.commit()

    def _drain_oldest():
        if not in_flight:
            return
        item, new_vec, candidates, fu = in_flight.pop(0)
        started = time.monotonic()
        try:
            jr = fu.result()
        except Exception as e:
            jr = {'error': 'judge_uncaught', 'detail': str(e)}
            _log_event(
                'cluster_judge_llm_fail',
                item_id=item['id'], err=str(e),
                origin='top_k_judge_uncaught',
            )
        _process_judge_result(item, new_vec, candidates, jr)
        _record_timing('judge_wait_and_apply', started)
        _checkpoint_item()

    try:
        for item in pending:
            new_vec = vu.unpack_blob(item['embedding'])
            if new_vec is None:
                continue
            started = time.monotonic()
            recalled_candidates = _recall_top_k_clusters(
                conn, new_vec,
                k=top_k, window_days=candidate_window_days,
                cosine_min=recall_cosine_min,
                item_time=item['published_at'],
                temporal_adjacency_days=temporal_adjacency_days,
                max_merged_span_days=max_merged_span_days,
            )
            _record_timing('recall_candidates', started)
            needs_gray_metadata = (
                gray_cosine_min is not None
                and any(
                    float(c.get('cosine') or 0.0) < float(cosine_min or 0.0)
                    and float(c.get('cosine') or 0.0) >= float(gray_cosine_min)
                    for c in recalled_candidates
                )
            )
            if needs_gray_metadata:
                _enrich_recall_candidates(conn, recalled_candidates)
            candidates, shadow_candidates, rejected_gray = _partition_recall_candidates(
                item,
                recalled_candidates,
                cosine_min=cosine_min,
                gray_cosine_min=gray_cosine_min,
                shadow_cosine_min=shadow_cosine_min,
                gray_max_temporal_hours=gray_max_temporal_hours,
            )
            candidates = candidates[: max(0, int(top_k))]
            if shadow_candidates:
                _log_event(
                    'stage1_shadow_candidates_observed',
                    item_id=item['id'],
                    candidate_count=len(shadow_candidates),
                    candidate_cluster_ids=[c['cluster_id'] for c in shadow_candidates],
                    top_cosines=[round(float(c['cosine']), 4) for c in shadow_candidates],
                    shadow_cosine_min=shadow_cosine_min,
                    shadow_cosine_max=gray_cosine_min if gray_cosine_min is not None else cosine_min,
                )
            if rejected_gray:
                _log_event(
                    'stage1_gray_candidates_rejected',
                    item_id=item['id'],
                    candidate_count=len(rejected_gray),
                    candidate_cluster_ids=[c['cluster_id'] for c in rejected_gray],
                    top_cosines=[round(float(c['cosine']), 4) for c in rejected_gray],
                    required_reasons='close_time plus same_author/same_category/shared_entity anchors',
                )
            recall_stats['calls'] += 1
            recall_stats['candidates_total'] += len(candidates)
            recall_stats['candidates_max'] = max(recall_stats['candidates_max'], len(candidates))
            if candidates:
                top_cosines = [round(float(c['cosine']), 4) for c in candidates]
                top_distances = [
                    c.get('temporal_distance_days')
                    for c in candidates
                    if c.get('temporal_distance_days') is not None
                ]
                _log_event(
                    'stage1_candidates_fetched',
                    item_id=item['id'],
                    candidate_count=len(candidates),
                    max_cosine=top_cosines[0],
                    min_cosine=top_cosines[-1],
                    top_cosines=top_cosines,
                    recall_cosine_min=recall_cosine_min,
                    strong_cosine_min=cosine_min,
                    gray_cosine_min=gray_cosine_min,
                    shadow_cosine_min=shadow_cosine_min,
                    temporal_adjacency_days=temporal_adjacency_days,
                    max_merged_span_days=max_merged_span_days,
                    top_temporal_distances_days=top_distances,
                )
            else:
                _log_event(
                    'stage1_candidates_fetched',
                    item_id=item['id'],
                    candidate_count=0,
                    max_cosine=None,
                    min_cosine=None,
                    top_cosines=[],
                    recall_cosine_min=recall_cosine_min,
                    strong_cosine_min=cosine_min,
                    gray_cosine_min=gray_cosine_min,
                    shadow_cosine_min=shadow_cosine_min,
                    temporal_adjacency_days=temporal_adjacency_days,
                    max_merged_span_days=max_merged_span_days,
                )
            if not candidates:
                new_cid = _create_singleton(
                    conn, item['id'], new_vec,
                    item['published_at'] or datetime.now(timezone.utc).isoformat(),
                    run_id=run_id,
                )
                bumped_clusters.add(new_cid)
                stats['new_singletons'] += 1
                _log_event(
                    'cluster_singleton_no_candidates',
                    item_id=item['id'], cluster_id=new_cid,
                )
                _checkpoint_item()
                continue
            # Submit LLM call; drain oldest if pool full.
            fu = pool.submit(top_k_judge, item, candidates)
            in_flight.append((item, new_vec, candidates, fu))
            if len(in_flight) >= judge_workers:
                _drain_oldest()
        # Drain remaining
        while in_flight:
            _drain_oldest()
    finally:
        pool.shutdown(wait=True)

    if not remote_db.cluster_to_remote():
        conn.commit()

    # Stage 4: regenerate summary for clusters that pass the BF-0501-1 latest
    # events candidate policy. Source count is no longer a hard gate.
    if not skip_summary:
        started = time.monotonic()
        if remote_db.cluster_to_remote():
            summary_cluster_ids = _clusters_requiring_summary_remote(
                bumped_clusters,
                run_id,
                window_start=window_start,
                window_end=window_end,
                require_published_at=require_published_at,
            )
        else:
            summary_cluster_ids = _clusters_requiring_summary(
                conn,
                bumped_clusters,
                run_id,
                window_start=window_start,
                window_end=window_end,
                require_published_at=require_published_at,
            )
        effective_summary_workers = max(1, int(summary_workers or 1))
        remote_cluster_backend = remote_db.cluster_to_remote()
        if run_id is None:
            # Immediate publish mutates live version/action staleness and has a
            # narrower call surface. Keep it single-connection for compatibility.
            effective_summary_workers = 1
        if remote_cluster_backend and effective_summary_workers > 1:
            _log_event(
                'cluster_summary_remote_parallel_enabled',
                workers=effective_summary_workers,
            )

        def _regenerate_summary(cid: int, worker_conn=None) -> bool:
            target_conn = None if remote_cluster_backend else (worker_conn or conn)
            return summary_writer.regenerate_and_swap(
                target_conn, cid, api_key=api_key, api_base=api_base,
                model=model, summary_max_docs=summary_max_docs,
                publish_immediately=(run_id is None),
                run_id=run_id,
            )

        if effective_summary_workers <= 1:
            for cid in summary_cluster_ids:
                try:
                    ok = _regenerate_summary(cid)
                except Exception as exc:  # noqa: BLE001
                    ok = False
                    _log_event(
                        'cluster_summary_fail',
                        cluster_id=cid,
                        reason='uncaught',
                        err=str(exc),
                    )
                if ok:
                    stats['summary_regenerated'] += 1
                    _log_event('cluster_summary_ok', cluster_id=cid)
                else:
                    stats['summary_failed'] += 1
                    _log_event('cluster_summary_deferred_for_retry', cluster_id=cid, run_id=run_id)
        else:
            from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

            def _regenerate_summary_in_worker(cid: int) -> tuple[int, bool]:
                if remote_cluster_backend:
                    return cid, _regenerate_summary(cid)
                worker_conn = db.get_conn()
                try:
                    return cid, _regenerate_summary(cid, worker_conn)
                finally:
                    worker_conn.close()

            id_iter = iter(summary_cluster_ids)
            in_flight = {}

            def _submit_next(pool: ThreadPoolExecutor) -> bool:
                try:
                    next_cid = next(id_iter)
                except StopIteration:
                    return False
                fut = pool.submit(_regenerate_summary_in_worker, next_cid)
                in_flight[fut] = next_cid
                return True

            with ThreadPoolExecutor(max_workers=effective_summary_workers) as pool:
                for _ in range(effective_summary_workers):
                    if not _submit_next(pool):
                        break
                while in_flight:
                    done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                    for fut in done:
                        cid = in_flight.pop(fut)
                        try:
                            _cid, ok = fut.result()
                            cid = _cid
                        except Exception as exc:  # noqa: BLE001
                            ok = False
                            _log_event(
                                'cluster_summary_fail',
                                cluster_id=cid,
                                reason='worker_uncaught',
                                err=str(exc),
                            )
                        if ok:
                            stats['summary_regenerated'] += 1
                            _log_event('cluster_summary_ok', cluster_id=cid)
                        else:
                            stats['summary_failed'] += 1
                            _log_event('cluster_summary_deferred_for_retry', cluster_id=cid, run_id=run_id)
                    while len(in_flight) < effective_summary_workers:
                        if not _submit_next(pool):
                            break
        _record_timing('summary_regeneration', started)

    if run_id is not None and publish:
        started = time.monotonic()
        stats['published_clusters'] = summary_writer.publish_run(conn, run_id)
        _record_timing('publish_run', started)
        if stats['summary_failed']:
            _log_event(
                'cluster_run_published_partial',
                run_id=run_id,
                reason='summary_failed',
                summary_failed=stats['summary_failed'],
                summary_regenerated=stats['summary_regenerated'],
                published_clusters=stats['published_clusters'],
            )
        else:
            _log_event('cluster_run_published', run_id=run_id,
                       published_clusters=stats['published_clusters'])

    stats['touched_clusters'] = len(bumped_clusters)
    stats['recall'] = {
        **recall_stats,
        'candidates_avg': (
            round(recall_stats['candidates_total'] / recall_stats['calls'], 2)
            if recall_stats['calls'] else 0.0
        ),
    }
    stats['timings_sec'] = timings_sec
    return stats


def _write_stats_path(path: str | None, stats: dict[str, Any]) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + '.tmp')
    tmp.write_text(json.dumps(stats, ensure_ascii=False, sort_keys=True), encoding='utf-8')
    tmp.replace(target)


def _one_member_content(conn, cluster_id: int) -> str | None:
    r = conn.execute(
        """SELECT i.content, i.ai_summary, i.title
           FROM items i JOIN cluster_items ci ON ci.item_id = i.id
           WHERE ci.cluster_id = ?
           ORDER BY ci.is_primary_source DESC, ci.rank_in_cluster ASC, i.published_at DESC
           LIMIT 1""",
        (cluster_id,),
    ).fetchone()
    if not r:
        return None
    return ((r['content'] or r['ai_summary'] or r['title']) or '')[:2000] or None


def _main():
    """CLI entry: run once with config-derived provider + thresholds."""
    parser = argparse.ArgumentParser(description='Run event clustering pipeline')
    parser.add_argument('--run-id', type=int, default=None,
                        help='only cluster items tagged with this fetch run')
    parser.add_argument('--run-items-scope', choices=_RUN_ITEMS_SCOPE_CHOICES,
                        default=_RUN_ITEMS_SCOPE_TAGGED,
                        help='run item set: tagged=items.fetch_run_id, inserted=fetch_run_items.was_inserted=1')
    parser.add_argument('--window-start', default=None,
                        help='only cluster items at/after this UTC/local datetime')
    parser.add_argument('--window-end', default=None,
                        help='only cluster items before this UTC/local datetime')
    parser.add_argument('--window-require-published-at', action='store_true',
                        help='window by real published_at only; defer undated fetched snapshots')
    parser.add_argument('--judge-workers', type=int, default=None,
                        help='override clustering.stage2_judge_workers')
    parser.add_argument('--top-k', type=int, default=None,
                        help='override clustering.stage1_top_k')
    parser.add_argument('--judge-max-tokens', type=int, default=None,
                        help='override clustering.stage2_judge_max_tokens')
    parser.add_argument('--judge-min-interval-sec', type=float, default=None,
                        help='override clustering.stage2_judge_min_interval_sec')
    parser.add_argument('--summary-workers', type=int, default=None,
                        help='override clustering.cluster_summary_workers')
    parser.add_argument('--temporal-adjacency-days', type=float, default=None,
                        help='candidate clusters must overlap item_time ± this many days')
    parser.add_argument('--max-merged-span-days', type=float, default=None,
                        help='reject candidate if merged cluster span would exceed this many days; <=0 disables')
    parser.add_argument('--feed-candidates-only', action='store_true',
                        help='only cluster categories eligible for latest-events feed display')
    parser.add_argument('--no-publish', action='store_true',
                        help='leave run-scoped cluster summaries in draft state')
    parser.add_argument('--stats-path', default=None,
                        help='write final pipeline stats JSON to this path')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    cfg = _load_cfg()
    glob = cfg.get('global', {})
    clustering_cfg = glob.get('clustering', {})
    ai = cfg.get('ai_summary', {})
    project_env = load_project_env(Path(__file__).resolve().parents[2])

    name, api_key, embedding_base = ep_mod.resolve_runtime_provider(cfg)
    if not api_key:
        logger.error('no API key for provider %s; abort', name)
        sys.exit(2)
    provider = ep_mod.get_provider(name, api_key=api_key, api_base=embedding_base)
    clustering_cfg = _apply_embedding_clustering_profile(
        clustering_cfg,
        provider_name=name,
        provider=provider,
    )
    if clustering_cfg.get('embedding_clustering_profile'):
        logger.info(
            'embedding clustering profile=%s provider=%s model=%s stage1_cosine_min=%s gray=%s shadow=%s top_k=%s report=%s',
            clustering_cfg.get('embedding_clustering_profile'),
            name,
            getattr(provider, 'model', None),
            clustering_cfg.get('stage1_cosine_min'),
            clustering_cfg.get('stage1_gray_cosine_min'),
            clustering_cfg.get('stage1_shadow_cosine_min'),
            clustering_cfg.get('stage1_top_k'),
            clustering_cfg.get('_embedding_clustering_profile_report'),
        )

    chat_api_key, api_base, model = resolve_minimax_chat_runtime_config(ai, project_env)
    if not chat_api_key:
        logger.error('no MiniMax chat API key; abort')
        sys.exit(2)
    temporal_adjacency_days = _coerce_optional_float(
        args.temporal_adjacency_days
        if args.temporal_adjacency_days is not None
        else clustering_cfg.get(
            'temporal_adjacency_days',
            clustering_cfg.get('temporal_candidate_window_days', _TEMPORAL_ADJACENCY_DAYS_DEFAULT),
        ),
        _TEMPORAL_ADJACENCY_DAYS_DEFAULT,
    )
    max_merged_span_days = _coerce_optional_float(
        args.max_merged_span_days
        if args.max_merged_span_days is not None
        else clustering_cfg.get(
            'max_merged_span_days',
            clustering_cfg.get('max_cluster_span_days', _MAX_MERGED_SPAN_DAYS_DEFAULT),
        ),
        _MAX_MERGED_SPAN_DAYS_DEFAULT,
    )
    if max_merged_span_days is not None and max_merged_span_days <= 0:
        max_merged_span_days = None

    conn = None if remote_db.cluster_to_remote() else db.get_conn()
    started = time.time()
    try:
        stats = run_pipeline(
            conn, provider=provider,
            api_key=chat_api_key, api_base=api_base, model=model,
            tau_hours=float(clustering_cfg.get('representative_decay_tau_hours', 24)),
            candidate_window_days=int(clustering_cfg.get('candidate_window_days', 30)),
            summary_max_docs=int(clustering_cfg.get('summary_max_docs', 20)),
            top_k=int(
                args.top_k
                if args.top_k is not None
                else clustering_cfg.get('stage1_top_k', 10)
            ),
            judge_max_tokens=int(
                args.judge_max_tokens
                if args.judge_max_tokens is not None
                else clustering_cfg.get('stage2_judge_max_tokens', 16384)
            ),
            judge_workers=int(
                args.judge_workers
                if args.judge_workers is not None
                else clustering_cfg.get('stage2_judge_workers', 20)
            ),
            judge_min_interval_sec=float(
                args.judge_min_interval_sec
                if args.judge_min_interval_sec is not None
                else clustering_cfg.get('stage2_judge_min_interval_sec', 0.8)
            ),
            summary_workers=int(
                args.summary_workers
                if args.summary_workers is not None
                else clustering_cfg.get('cluster_summary_workers', 1)
            ),
            # BF-0428-3: cosine hard floor before LLM judge.
            # Default 0.0 = no filter (back-compat). Set ≥0.7-0.8 in production.
            cosine_min=float(clustering_cfg.get('stage1_cosine_min', 0.0)),
            gray_cosine_min=_coerce_optional_float(
                clustering_cfg.get('stage1_gray_cosine_min'),
                None,
            ),
            shadow_cosine_min=_coerce_optional_float(
                clustering_cfg.get('stage1_shadow_cosine_min'),
                None,
            ),
            gray_max_temporal_hours=_coerce_optional_float(
                clustering_cfg.get(
                    'stage1_gray_max_temporal_hours',
                    _GRAY_RECALL_MAX_TEMPORAL_HOURS_DEFAULT,
                ),
                _GRAY_RECALL_MAX_TEMPORAL_HOURS_DEFAULT,
            ),
            temporal_adjacency_days=temporal_adjacency_days,
            max_merged_span_days=max_merged_span_days,
            run_id=args.run_id,
            run_items_scope=args.run_items_scope,
            window_start=args.window_start,
            window_end=args.window_end,
            require_published_at=args.window_require_published_at,
            feed_candidates_only=args.feed_candidates_only,
            publish=not args.no_publish,
        )
    except ProviderRateLimited as exc:
        message = str(exc) or ai_provider_guard.provider_message(ai_provider_guard.MINIMAX_CHAT_PROVIDER)
        logger.error('pipeline paused by provider: %s', message)
        _log_event('pipeline_paused_by_provider', run_id=args.run_id, err=message)
        print(json.dumps({
            'error': 'provider_unavailable',
            'message': message,
        }, ensure_ascii=False))
        sys.exit(3)
    elapsed = time.time() - started
    stats['elapsed_sec'] = round(elapsed, 2)
    logger.info('pipeline done: %s', stats)
    _log_event('pipeline_complete', **stats)
    _write_stats_path(args.stats_path, stats)
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == '__main__':
    _main()
