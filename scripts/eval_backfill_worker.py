#!/usr/bin/env python3
"""Slow historical discovery worker for the eval category.

Default mode is read-only: scan older windows, write candidate artifacts, and
advance a checkpoint. Use ``--apply`` on the production host to write high
confidence eval classifications and refresh only the affected 信息 tab eval
read-model scopes.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import enrich_items  # noqa: E402
import remote_db  # noqa: E402
from eval_category_dry_run import (  # noqa: E402
    CONFIDENCE_RANK,
    LOCAL_ITEM_BASE,
    _build_prompt,
    _item_payload,
    _normalize_result,
    _score_candidate,
)


DEFAULT_STATE_DIR = ROOT / "data" / "eval_backfill_worker"
DEFAULT_CHECKPOINT = DEFAULT_STATE_DIR / "checkpoint.json"
DEFAULT_OUTPUT_DIR = DEFAULT_STATE_DIR / "windows"
DEFAULT_LOCK_FILE = Path("/tmp/info2action-eval-backfill-worker.lock")
DEFAULT_SNAPSHOT_FILE = DEFAULT_STATE_DIR / "historical-snapshot.jsonl"
DEFAULT_CLASSIFICATION_FILE = DEFAULT_STATE_DIR / "historical-classified.jsonl"
DEFAULT_MANIFEST_FILE = DEFAULT_STATE_DIR / "historical-apply-manifest.json"
EVAL_CATEGORY = "eval"


@dataclass(frozen=True)
class EvalBackfillWindow:
    start: datetime
    end: datetime

    @property
    def label(self) -> str:
        return f"{_compact_iso(self.start)}__{_compact_iso(self.end)}"

    def as_json(self) -> dict[str, str]:
        return {"start": _iso_utc(self.start), "end": _iso_utc(self.end)}


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _compact_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_utc(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_checkpoint(path: Path, initial_until: datetime) -> datetime:
    if not path.exists():
        return initial_until
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cursor = data.get("cursor_until")
        if isinstance(cursor, str) and cursor.strip():
            return parse_utc(cursor)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return initial_until
    return initial_until


def save_checkpoint(path: Path, *, next_cursor_until: datetime, window: EvalBackfillWindow, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = {
        "cursor_until": _iso_utc(next_cursor_until),
        "last_window": window.as_json(),
        "last_window_file": payload.get("output_file"),
        "last_candidate_count": payload.get("candidate_count"),
        "last_llm_checked": payload.get("llm_checked"),
        "last_eval_hits": payload.get("eval_hits"),
        "updated_at": _iso_utc(datetime.now(timezone.utc)),
    }
    path.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")


def iter_backward_windows(cursor_until: datetime, *, window_days: int, max_windows: int) -> Iterator[EvalBackfillWindow]:
    end = cursor_until.astimezone(timezone.utc)
    for _ in range(max_windows):
        start = end - timedelta(days=window_days)
        yield EvalBackfillWindow(start=start, end=end)
        end = start


def output_path_for_window(output_dir: Path, window: EvalBackfillWindow) -> Path:
    return output_dir / f"{window.label}.json"


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return _iso_utc(value)
    return str(value)


def _write_jsonl(path: Path, rows: list[dict[str, Any]], *, append: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=_json_default))
            handle.write("\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            rows.append(json.loads(text))
    return rows


@contextmanager
def acquire_lock(path: Path) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        lock_file.write(_iso_utc(datetime.now(timezone.utc)))
        lock_file.flush()
        try:
            yield True
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def query_candidates_between(
    window: EvalBackfillWindow,
    *,
    limit: int,
    scan_limit: int,
    statement_timeout_sec: int,
) -> list[dict[str, Any]]:
    params: list[Any] = [_iso_utc(window.start), _iso_utc(window.end), scan_limit]
    candidate_sql = """
        SELECT
          i.id,
          i.platform,
          i.title,
          i.url,
          i.author_name,
          left(coalesce(i.description, ''), 1000) AS description,
          left(coalesce(i.ai_summary, ''), 1600) AS ai_summary,
          i.ai_category,
          i.ai_categories,
          i.ai_subcategories,
          i.fetched_at,
          i.published_at,
          coalesce(i.published_at, i.fetched_at) AS sort_at
        FROM items i
        WHERE i.visible = 1
          AND i.fetched_at >= %s
          AND i.fetched_at < %s
        ORDER BY i.fetched_at DESC
        LIMIT %s
    """
    with remote_db.connect() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute(f"SET LOCAL statement_timeout = '{int(statement_timeout_sec)}s'")
        rows = conn.execute(candidate_sql, params).fetchall()
        candidates = [dict(row) for row in rows]
        candidates = [item for item in candidates if _score_candidate(item) > 0]
        candidates.sort(key=lambda item: (_score_candidate(item), str(item.get("sort_at") or "")), reverse=True)
        candidates = candidates[:limit]
        ids = [row["id"] for row in candidates]
        content_by_id: dict[str, str] = {}
        if ids:
            detail_rows = conn.execute(
                """
                SELECT i.id, left(coalesce(i.content, ''), 3500) AS content
                  FROM items i
                 WHERE i.id = ANY(%s)
                """,
                [ids],
            ).fetchall()
            content_by_id = {row["id"]: row.get("content") or "" for row in detail_rows}
    for item in candidates:
        item["content"] = content_by_id.get(item["id"], "")
    candidates.sort(key=lambda item: (_score_candidate(item), str(item.get("sort_at") or "")), reverse=True)
    return candidates


def _base_output(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "title": item.get("title") or "",
        "platform": item.get("platform") or "",
        "sort_at": str(item.get("sort_at") or item.get("published_at") or item.get("fetched_at") or ""),
        "existing": {
            "ai_category": item.get("ai_category"),
            "ai_categories": item.get("ai_categories"),
            "ai_subcategories": item.get("ai_subcategories"),
        },
        "score": _score_candidate(item),
        "link": f"{LOCAL_ITEM_BASE}{item['id']}",
    }


def _snapshot_output(item: dict[str, Any], window: EvalBackfillWindow) -> dict[str, Any]:
    base = _base_output(item)
    return {
        **base,
        "url": item.get("url") or "",
        "author_name": item.get("author_name") or "",
        "description": item.get("description") or "",
        "ai_summary": item.get("ai_summary") or "",
        "content": item.get("content") or "",
        "fetched_at": str(item.get("fetched_at") or ""),
        "published_at": str(item.get("published_at") or ""),
        "window": window.as_json(),
        "snapshot_at": _iso_utc(datetime.now(timezone.utc)),
    }


def _confidence_passes(confidence: str, minimum: str) -> bool:
    return CONFIDENCE_RANK.get(confidence, 0) >= CONFIDENCE_RANK.get(minimum, CONFIDENCE_RANK["medium"])


def select_apply_outputs(outputs: list[dict[str, Any]], minimum_confidence: str) -> list[dict[str, Any]]:
    return [
        item for item in outputs
        if EVAL_CATEGORY in item.get("categories", [])
        and _confidence_passes(str(item.get("confidence") or ""), minimum_confidence)
    ]


def _existing_has_eval(item: dict[str, Any]) -> bool:
    existing = item.get("existing")
    if isinstance(existing, dict):
        category = str(existing.get("ai_category") or "").strip().lower()
        if category == EVAL_CATEGORY:
            return True
        categories = existing.get("ai_categories") or []
    else:
        category = str(item.get("ai_category") or "").strip().lower()
        if category == EVAL_CATEGORY:
            return True
        categories = item.get("ai_categories") or []
    if isinstance(categories, str):
        try:
            categories = json.loads(categories)
        except (TypeError, ValueError):
            categories = [categories]
    if not isinstance(categories, list):
        return False
    return EVAL_CATEGORY in {str(value or "").strip().lower() for value in categories}


def build_apply_manifest(
    classified_outputs: list[dict[str, Any]],
    *,
    minimum_confidence: str,
    include_existing_eval: bool = False,
) -> dict[str, Any]:
    candidates = select_apply_outputs(classified_outputs, minimum_confidence)
    if not include_existing_eval:
        candidates = [item for item in candidates if not _existing_has_eval(item)]
    confidence_counts: dict[str, int] = {}
    for item in classified_outputs:
        if EVAL_CATEGORY not in item.get("categories", []):
            continue
        confidence = str(item.get("confidence") or "unknown")
        confidence_counts[confidence] = confidence_counts.get(confidence, 0) + 1
    return {
        "generated_at": _iso_utc(datetime.now(timezone.utc)),
        "min_confidence": minimum_confidence,
        "include_existing_eval": include_existing_eval,
        "classified_count": len(classified_outputs),
        "eval_hits": sum(1 for item in classified_outputs if EVAL_CATEGORY in item.get("categories", [])),
        "eval_confidence_counts": confidence_counts,
        "apply_count": len(candidates),
        "items": candidates,
    }


def _normalized_eval_categories(item: dict[str, Any]) -> list[str]:
    raw = item.get("categories") or [EVAL_CATEGORY]
    categories: list[str] = []
    for value in raw:
        category = str(value or "").strip().lower()
        if category and category not in categories:
            categories.append(category)
    if EVAL_CATEGORY not in categories:
        categories.insert(0, EVAL_CATEGORY)
    if categories[0] != EVAL_CATEGORY:
        categories = [EVAL_CATEGORY] + [category for category in categories if category != EVAL_CATEGORY]
    return categories[:3]


def _normalized_eval_subcategories(item: dict[str, Any]) -> list[str]:
    raw = item.get("subcategories") or ["other"]
    subcategories: list[str] = []
    for value in raw:
        subcategory = str(value or "").strip().lower()
        if subcategory and subcategory not in subcategories:
            subcategories.append(subcategory)
    return subcategories or ["other"]


def apply_eval_updates(outputs: list[dict[str, Any]], minimum_confidence: str) -> list[dict[str, Any]]:
    updates = select_apply_outputs(outputs, minimum_confidence)
    if not updates:
        return []
    schema = remote_db.remote_schema()
    with remote_db.connect() as conn:
        conn.execute("SET LOCAL statement_timeout = '15s'")
        for item in updates:
            categories = _normalized_eval_categories(item)
            subcategories = _normalized_eval_subcategories(item)
            multi_l1_reason = item.get("reason") if len(categories) > 1 else None
            conn.execute(
                f"""
                UPDATE {schema}.items
                   SET ai_category = %s,
                       ai_categories = %s::jsonb,
                       ai_subcategories = %s::jsonb,
                       multi_l1_reason = %s
                 WHERE id = %s
                """,
                (
                    categories[0],
                    json.dumps(categories, ensure_ascii=False),
                    json.dumps(subcategories, ensure_ascii=False),
                    multi_l1_reason,
                    item["id"],
                ),
            )
            item["applied_categories"] = categories
            item["applied_subcategories"] = subcategories
        conn.commit()
    return updates


def refresh_eval_info_scopes(applied_ids: list[str], *, statement_timeout_sec: int) -> dict[str, Any]:
    ids = [str(item_id) for item_id in applied_ids if str(item_id or "").strip()]
    if not ids:
        return {"ok": True, "skipped": "no_applied_ids"}

    schema = remote_db.remote_schema()
    timeout_sec = max(5, min(int(statement_timeout_sec or 20), 300))
    section_category_expr = remote_db._section_category_expr("i")  # noqa: SLF001
    scope_rows_sql = remote_db._info_read_model_scope_rows_select("pg_temp.eval_backfill_scope_source")  # noqa: SLF001
    t0 = time.time()
    with remote_db.connect() as conn:
        conn.execute(f"SET LOCAL statement_timeout = '{timeout_sec}s'")
        active = remote_db._info_read_model_active_version(conn, schema)  # noqa: SLF001
        if not active or not active.get("version_id"):
            return {"ok": False, "skipped": "no_active_info_read_model"}
        version_id = str(active["version_id"])

        eligible_where, eligible_params = remote_db._base_item_where(  # noqa: SLF001
            public_only=True,
            manual_owner_user_id=None,
            min_github_stars=remote_db.INFO_READ_MODEL_MIN_GITHUB_STARS,
        )
        eligible_where.append("i.visible = 1")
        remote_db._add_ai_relevance_filter(eligible_where)  # noqa: SLF001
        eligible_where.append("i.id = ANY(%(ids)s)")
        eligible_where_sql = remote_db._where_sql(eligible_where)  # noqa: SLF001
        card_upsert = conn.execute(
            f"""INSERT INTO {schema}.info_card_items (
                   version_id, item_id, card_json, platform, source,
                   sort_at, fetched_at, published_at, relevance_score
                 )
                 SELECT %(version_id)s::uuid,
                        i.id::text,
                        jsonb_strip_nulls(jsonb_build_object(
                          'id', i.id::text,
                          'user_id', i.user_id,
                          'platform', i.platform,
                          'source', i.source,
                          'title', i.title,
                          'author_name', i.author_name,
                          'author_id', i.author_id,
                          'author_avatar', i.author_avatar,
                          'url', i.url,
                          'cover_url', i.cover_url,
                          'media_json', i.media_json,
                          'metrics_json', i.metrics_json,
                          'lang', i.lang,
                          'description', i.description,
                          'ai_summary', i.ai_summary,
                          'ai_category', i.ai_category,
                          'ai_keywords', i.ai_keywords,
                          'ai_categories', i.ai_categories,
                          'ai_subcategories', i.ai_subcategories,
                          'content_type', i.content_type,
                          'visible', i.visible,
                          'relevance_score', i.relevance_score,
                          'fetched_at', i.fetched_at,
                          'published_at', i.published_at,
                          'created_at', i.created_at,
                          'read_at', NULL,
                          'clicked_at', NULL,
                          'starred_at', NULL,
                          'hidden_at', NULL
                        )),
                        i.platform,
                        i.source,
                        COALESCE(i.published_at, i.fetched_at),
                        i.fetched_at,
                        i.published_at,
                        i.relevance_score
                   FROM {schema}.items i
                   {eligible_where_sql}
                 ON CONFLICT (version_id, item_id) DO UPDATE SET
                   card_json = excluded.card_json,
                   platform = excluded.platform,
                   source = excluded.source,
                   sort_at = excluded.sort_at,
                   fetched_at = excluded.fetched_at,
                   published_at = excluded.published_at,
                   relevance_score = excluded.relevance_score""",
            {"version_id": version_id, "ids": ids, **eligible_params},
        )
        updated_card_items = int(getattr(card_upsert, "rowcount", 0) or 0)

        conn.execute("DROP TABLE IF EXISTS pg_temp.eval_backfill_affected_scopes")
        conn.execute(
            f"""CREATE TEMP TABLE eval_backfill_affected_scopes ON COMMIT DROP AS
                SELECT DISTINCT sc.scope_key, sc.platform, sc.dimension, sc.value
                  FROM {schema}.info_scope_items si
                  JOIN {schema}.info_scopes sc
                    ON sc.version_id = si.version_id
                   AND sc.scope_key = si.scope_key
                 WHERE si.version_id = %(version_id)s::uuid
                   AND si.item_id = ANY(%(ids)s)
                   AND sc.dimension IN ('section_category', 'section_subcategory')
                UNION
                SELECT DISTINCT
                       'platform=_all|dimension=section_category|value=' || ({section_category_expr}) AS scope_key,
                       '_all'::text AS platform,
                       'section_category'::text AS dimension,
                       ({section_category_expr}) AS value
                  FROM {schema}.items i
                 WHERE i.id = ANY(%(ids)s)
                UNION
                SELECT DISTINCT
                       'platform=_all|dimension=section_subcategory|value='
                         || ({section_category_expr})
                         || %(compound_separator)s
                         || subcat.value AS scope_key,
                       '_all'::text AS platform,
                       'section_subcategory'::text AS dimension,
                       ({section_category_expr}) || %(compound_separator)s || subcat.value AS value
                  FROM {schema}.items i
                  CROSS JOIN LATERAL jsonb_array_elements_text(i.ai_subcategories) AS subcat(value)
                 WHERE i.id = ANY(%(ids)s)""",
            {
                "version_id": version_id,
                "ids": ids,
                "compound_separator": remote_db.INFO_SCOPE_COMPOUND_SEPARATOR,
            },
        )
        conn.execute("ANALYZE pg_temp.eval_backfill_affected_scopes")
        affected_row = conn.execute(
            "SELECT count(*) AS n FROM pg_temp.eval_backfill_affected_scopes"
        ).fetchone()
        affected_scope_count = int((affected_row or {}).get("n") or 0)
        if affected_scope_count <= 0:
            conn.commit()
            return {
                "ok": True,
                "skipped": "no_affected_scopes",
                "version_id": version_id,
                "updated_card_items": updated_card_items,
                "elapsed_ms": int((time.time() - t0) * 1000),
            }

        conn.execute("DROP TABLE IF EXISTS pg_temp.eval_backfill_candidate_item_ids")
        conn.execute(
            f"""CREATE TEMP TABLE eval_backfill_candidate_item_ids ON COMMIT DROP AS
                SELECT DISTINCT si.item_id
                  FROM {schema}.info_scope_items si
                  JOIN pg_temp.eval_backfill_affected_scopes affected
                    ON affected.scope_key = si.scope_key
                 WHERE si.version_id = %(version_id)s::uuid
                UNION
                SELECT i.id::text AS item_id
                  FROM {schema}.items i
                 WHERE i.id = ANY(%(ids)s)""",
            {"version_id": version_id, "ids": ids},
        )
        conn.execute("ANALYZE pg_temp.eval_backfill_candidate_item_ids")

        conn.execute("DROP TABLE IF EXISTS pg_temp.eval_backfill_scope_source")
        conn.execute(
            f"""CREATE TEMP TABLE eval_backfill_scope_source ON COMMIT DROP AS
                SELECT i.id::text AS id,
                       i.platform,
                       i.source,
                       i.detail_json,
                       i.ai_categories,
                       i.ai_category,
                       i.ai_subcategories,
                       COALESCE(i.published_at, i.fetched_at) AS sort_at,
                       i.fetched_at,
                       i.relevance_score,
                       ({section_category_expr}) AS section_category
                  FROM pg_temp.eval_backfill_candidate_item_ids candidate
                  JOIN {schema}.info_card_items ci
                    ON ci.version_id = %(version_id)s::uuid
                   AND ci.item_id = candidate.item_id
                  JOIN {schema}.items i
                    ON i.id::text = ci.item_id
                 WHERE (
                         EXISTS (
                           SELECT 1
                             FROM pg_temp.eval_backfill_affected_scopes a
                            WHERE a.dimension = 'section_category'
                              AND a.value = ({section_category_expr})
                         )
                         OR EXISTS (
                           SELECT 1
                             FROM jsonb_array_elements_text(i.ai_subcategories) AS subcat(value)
                             JOIN pg_temp.eval_backfill_affected_scopes a
                               ON a.dimension = 'section_subcategory'
                              AND a.value = ({section_category_expr}) || %(compound_separator)s || subcat.value
                         )
                       )""",
            {
                "version_id": version_id,
                "compound_separator": remote_db.INFO_SCOPE_COMPOUND_SEPARATOR,
            },
        )
        conn.execute("ANALYZE pg_temp.eval_backfill_scope_source")

        conn.execute("DROP TABLE IF EXISTS pg_temp.eval_backfill_scope_rows")
        conn.execute(
            f"""CREATE TEMP TABLE eval_backfill_scope_rows ON COMMIT DROP AS
                SELECT rows.*
                  FROM ({scope_rows_sql}) rows
                  JOIN pg_temp.eval_backfill_affected_scopes affected
                    ON affected.scope_key = rows.scope_key
                 WHERE rows.dimension IN ('section_category', 'section_subcategory')""",
            {
                "uncategorized": remote_db.UNCATEGORIZED_SENTINEL,
                "compound_separator": remote_db.INFO_SCOPE_COMPOUND_SEPARATOR,
            },
        )
        conn.execute("ANALYZE pg_temp.eval_backfill_scope_rows")

        conn.execute(
            f"""DELETE FROM {schema}.info_scope_items si
                  USING pg_temp.eval_backfill_affected_scopes affected
                 WHERE si.version_id = %(version_id)s::uuid
                   AND si.scope_key = affected.scope_key""",
            {"version_id": version_id},
        )
        conn.execute(
            f"""DELETE FROM {schema}.info_scopes sc
                  USING pg_temp.eval_backfill_affected_scopes affected
                 WHERE sc.version_id = %(version_id)s::uuid
                   AND sc.scope_key = affected.scope_key""",
            {"version_id": version_id},
        )
        conn.execute(
            f"""INSERT INTO {schema}.info_scopes (
                   version_id, scope_key, platform, dimension, value,
                   total_count, max_sort_at, generated_at
                 )
                 SELECT %(version_id)s::uuid,
                        scope_key,
                        platform,
                        dimension,
                        value,
                        count(*)::integer,
                        max(rank_at),
                        now()
                   FROM pg_temp.eval_backfill_scope_rows
                  GROUP BY scope_key, platform, dimension, value""",
            {"version_id": version_id},
        )
        conn.execute(
            f"""WITH ranked AS (
                   SELECT scope_key, item_id, sort_at, fetched_at, relevance_score,
                          row_number() OVER (
                            PARTITION BY scope_key
                            ORDER BY rank_at DESC NULLS LAST,
                                     fetched_at DESC NULLS LAST,
                                     relevance_score DESC NULLS LAST,
                                     item_id DESC
                          ) AS rn
                     FROM pg_temp.eval_backfill_scope_rows
                 )
                 INSERT INTO {schema}.info_scope_items (
                   version_id, scope_key, rank, item_id, sort_at, fetched_at, relevance_score
                 )
                 SELECT %(version_id)s::uuid, scope_key, rn::integer, item_id,
                        sort_at, fetched_at, relevance_score
                   FROM ranked""",
            {"version_id": version_id},
        )
        eval_scope_row = conn.execute(
            f"""SELECT total_count
                  FROM {schema}.info_scopes
                 WHERE version_id = %(version_id)s::uuid
                   AND scope_key = %(scope_key)s""",
            {
                "version_id": version_id,
                "scope_key": "platform=_all|dimension=section_category|value=eval",
            },
        ).fetchone()
        conn.execute(
            f"""UPDATE {schema}.info_read_model_versions
                   SET completed_at = COALESCE(completed_at, now()),
                       meta_json = COALESCE(meta_json, '{{}}'::jsonb) || jsonb_build_object(
                         'last_eval_backfill_apply_at', now(),
                         'last_eval_backfill_applied_count', %(applied_count)s::integer
                       )
                 WHERE version_id = %(version_id)s::uuid""",
            {"version_id": version_id, "applied_count": len(ids)},
        )
        conn.execute(
            f"""UPDATE {schema}.info_read_model_state
                   SET updated_at = now()
                 WHERE key = %(state_key)s""",
            {"state_key": remote_db.INFO_READ_MODEL_STATE_KEY},
        )
        conn.commit()

    cleared_cache_entries = remote_db.clear_feed_cache_keys(clear_remote_snapshots=True)
    return {
        "ok": True,
        "version_id": version_id,
        "applied_ids": ids,
        "updated_card_items": updated_card_items,
        "affected_scope_count": affected_scope_count,
        "eval_total": int((eval_scope_row or {}).get("total_count") or 0),
        "cleared_cache_entries": cleared_cache_entries,
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


def classify_candidates(args: argparse.Namespace, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if args.list_only:
        return [_base_output(item) for item in candidates[: args.max_llm]]

    classification = enrich_items.load_classification()
    categories = classification.get("categories") or []
    valid_l1 = {str(cat.get("id") or "") for cat in categories}
    valid_l2 = {
        str(sub.get("id") or "")
        for cat in categories
        for sub in (cat.get("subcategories") or [])
    }
    config = enrich_items.load_config()
    api_key, api_base, model = enrich_items.resolve_minimax_runtime_config(
        config.get("ai_summary", {})
    )
    if not api_key:
        raise RuntimeError("MiniMax API key missing")

    prompt = _build_prompt(categories)
    gate = enrich_items.MiniMaxRateLimitGate(min_interval=args.request_interval_sec)
    outputs: list[dict[str, Any]] = []
    total = min(args.max_llm, len(candidates))
    for idx, item in enumerate(candidates[: args.max_llm], start=1):
        try:
            raw = enrich_items.call_minimax(
                api_key,
                api_base,
                model,
                prompt,
                _item_payload(item),
                max_tokens=args.max_tokens,
                rate_gate=gate,
            )
            result = _normalize_result(raw, valid_l1, valid_l2)
        except Exception as exc:  # noqa: BLE001
            result = {
                "categories": [],
                "subcategories": [],
                "confidence": "low",
                "reason": f"parse_or_llm_error: {str(exc)[:120]}",
            }
        output = {**_base_output(item), **result}
        outputs.append(output)
        mark = "EVAL" if "eval" in output.get("categories", []) else "skip"
        print(
            f"[eval-worker] [{idx:02d}/{total:02d}] {mark} "
            f"conf={output.get('confidence')} score={output.get('score')} "
            f"title={str(output.get('title') or '')[:80]}",
            flush=True,
        )
    return outputs


def _load_eval_classifier() -> tuple[list[dict[str, Any]], set[str], set[str], str, str, str, str]:
    classification = enrich_items.load_classification()
    categories = classification.get("categories") or []
    valid_l1 = {str(cat.get("id") or "") for cat in categories}
    valid_l2 = {
        str(sub.get("id") or "")
        for cat in categories
        for sub in (cat.get("subcategories") or [])
    }
    config = enrich_items.load_config()
    api_key, api_base, model = enrich_items.resolve_minimax_runtime_config(
        config.get("ai_summary", {})
    )
    if not api_key:
        raise RuntimeError("MiniMax API key missing")
    prompt = _build_prompt(categories)
    return categories, valid_l1, valid_l2, api_key, api_base, model, prompt


def run_snapshot_mode(args: argparse.Namespace) -> int:
    snapshot_file = Path(args.snapshot_file)
    if snapshot_file.exists() and not args.append_output:
        snapshot_file.unlink()
    initial_until = parse_utc(args.until) if args.until else datetime.now(timezone.utc)
    cursor_until = load_checkpoint(Path(args.checkpoint), initial_until)
    windows = list(iter_backward_windows(cursor_until, window_days=args.window_days, max_windows=args.max_windows))
    total_candidates = 0
    for idx, window in enumerate(windows, start=1):
        print(f"[eval-snapshot] window {idx}/{len(windows)} {_iso_utc(window.start)} -> {_iso_utc(window.end)}", flush=True)
        candidates = query_candidates_between(
            window,
            limit=args.candidate_limit,
            scan_limit=args.scan_limit,
            statement_timeout_sec=args.db_statement_timeout_sec,
        )
        rows = [_snapshot_output(item, window) for item in candidates]
        _write_jsonl(snapshot_file, rows, append=True)
        total_candidates += len(rows)
        payload = {
            "output_file": str(snapshot_file),
            "candidate_count": len(rows),
            "llm_checked": 0,
            "eval_hits": 0,
        }
        save_checkpoint(Path(args.checkpoint), next_cursor_until=window.start, window=window, payload=payload)
        print(
            f"[eval-snapshot] wrote={len(rows)} total={total_candidates} checkpoint={args.checkpoint}",
            flush=True,
        )
        if idx < len(windows) and args.sleep_between_windows_sec > 0:
            time.sleep(args.sleep_between_windows_sec)
    print(f"[eval-snapshot] done file={snapshot_file} rows={total_candidates}", flush=True)
    return 0


def run_classify_offline_mode(args: argparse.Namespace) -> int:
    snapshot_file = Path(args.snapshot_file)
    classification_file = Path(args.classification_file)
    snapshot_rows = _read_jsonl(snapshot_file)
    if not snapshot_rows:
        print(f"[eval-offline] no snapshot rows: {snapshot_file}", flush=True)
        return 0
    existing_by_id: dict[str, dict[str, Any]] = {}
    if args.resume_classification:
        existing_by_id = {
            str(row.get("id")): row
            for row in _read_jsonl(classification_file)
            if row.get("id")
        }
    if classification_file.exists() and not args.resume_classification and not args.append_output:
        classification_file.unlink()

    _, valid_l1, valid_l2, api_key, api_base, model, prompt = _load_eval_classifier()
    pending = [row for row in snapshot_rows if str(row.get("id")) not in existing_by_id]
    limit = len(pending) if args.offline_limit <= 0 else min(args.offline_limit, len(pending))
    pending = pending[:limit]
    concurrency = min(args.classification_concurrency, max(1, limit))
    gate = enrich_items.MiniMaxRateLimitGate(min_interval=args.request_interval_sec)
    outputs: list[dict[str, Any]] = []
    print(
        f"[eval-offline] pending={len(pending)} existing={len(existing_by_id)} "
        f"concurrency={concurrency} request_interval_sec={args.request_interval_sec}",
        flush=True,
    )

    def classify_one(item: dict[str, Any]) -> dict[str, Any]:
        try:
            raw = enrich_items.call_minimax(
                api_key,
                api_base,
                model,
                prompt,
                _item_payload(item),
                max_tokens=args.max_tokens,
                rate_gate=gate,
            )
            result = _normalize_result(raw, valid_l1, valid_l2)
        except Exception as exc:  # noqa: BLE001
            result = {
                "categories": [],
                "subcategories": [],
                "confidence": "low",
                "reason": f"parse_or_llm_error: {str(exc)[:120]}",
            }
        return {
            **_base_output(item),
            **result,
            "snapshot_file": str(snapshot_file),
            "classified_at": _iso_utc(datetime.now(timezone.utc)),
        }

    if concurrency <= 1:
        completed: Iterator[tuple[int, dict[str, Any]]] = (
            (idx, classify_one(item)) for idx, item in enumerate(pending, start=1)
        )
        for idx, output in completed:
            outputs.append(output)
            _write_jsonl(classification_file, [output], append=True)
            mark = "EVAL" if EVAL_CATEGORY in output.get("categories", []) else "skip"
            print(
                f"[eval-offline] [{idx:04d}/{limit:04d}] {mark} "
                f"conf={output.get('confidence')} score={output.get('score')} "
                f"title={str(output.get('title') or '')[:80]}",
                flush=True,
            )
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(classify_one, item): idx
                for idx, item in enumerate(pending, start=1)
            }
            for done_count, future in enumerate(as_completed(futures), start=1):
                idx = futures[future]
                output = future.result()
                outputs.append(output)
                _write_jsonl(classification_file, [output], append=True)
                mark = "EVAL" if EVAL_CATEGORY in output.get("categories", []) else "skip"
                print(
                    f"[eval-offline] [{done_count:04d}/{limit:04d} src={idx:04d}] {mark} "
                    f"conf={output.get('confidence')} score={output.get('score')} "
                    f"title={str(output.get('title') or '')[:80]}",
                    flush=True,
                )

    all_outputs = list(existing_by_id.values()) + outputs
    manifest = build_apply_manifest(
        all_outputs,
        minimum_confidence=args.min_confidence,
        include_existing_eval=args.include_existing_eval,
    )
    manifest["snapshot_file"] = str(snapshot_file)
    manifest["classification_file"] = str(classification_file)
    manifest_path = Path(args.manifest_file)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"[eval-offline] done classified_now={len(outputs)} classified_total={len(all_outputs)} "
        f"eval_hits={manifest['eval_hits']} apply_count={manifest['apply_count']} manifest={manifest_path}",
        flush=True,
    )
    return 0


def run_apply_manifest_mode(args: argparse.Namespace) -> int:
    classification_file = Path(args.classification_file)
    classified_outputs = _read_jsonl(classification_file)
    manifest = build_apply_manifest(
        classified_outputs,
        minimum_confidence=args.min_confidence,
        include_existing_eval=args.include_existing_eval,
    )
    manifest["classification_file"] = str(classification_file)
    manifest_path = Path(args.manifest_file)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    if not args.apply:
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            f"[eval-manifest] dry_run=1 classified={manifest['classified_count']} "
            f"eval_hits={manifest['eval_hits']} apply_count={manifest['apply_count']} manifest={manifest_path}",
            flush=True,
        )
        return 0

    applied = apply_eval_updates(manifest["items"], args.min_confidence)
    applied_ids = [item["id"] for item in applied]
    info_refresh_result = None
    if applied and not args.skip_info_read_model_refresh:
        info_refresh_result = refresh_eval_info_scopes(
            applied_ids,
            statement_timeout_sec=args.info_refresh_timeout_sec,
        )
    manifest["apply"] = {
        "enabled": True,
        "applied_count": len(applied),
        "applied_ids": applied_ids,
    }
    manifest["info_read_model_refresh"] = info_refresh_result
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"[eval-manifest] applied={len(applied)} eval_hits={manifest['eval_hits']} "
        f"manifest={manifest_path}",
        flush=True,
    )
    return 0


def write_window_output(
    path: Path,
    *,
    window: EvalBackfillWindow,
    args: argparse.Namespace,
    candidate_count: int,
    outputs: list[dict[str, Any]],
    apply_result: dict[str, Any] | None = None,
    info_refresh_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    eval_hits = [item for item in outputs if "eval" in item.get("categories", [])]
    high_or_medium_hits = [
        item for item in eval_hits
        if CONFIDENCE_RANK.get(str(item.get("confidence") or ""), 0) >= CONFIDENCE_RANK["medium"]
    ]
    payload = {
        "window": window.as_json(),
        "mode": "list_only" if args.list_only else "llm",
        "candidate_count": candidate_count,
        "output_item_count": len(outputs),
        "llm_checked": 0 if args.list_only else len(outputs),
        "eval_hits": len(eval_hits),
        "eval_hits_medium_plus": len(high_or_medium_hits),
        "apply": {
            "enabled": bool(getattr(args, "apply", False)),
            "min_confidence": getattr(args, "min_confidence", "high"),
            **(apply_result or {}),
        },
        "info_read_model_refresh": info_refresh_result,
        "generated_at": _iso_utc(datetime.now(timezone.utc)),
        "items": outputs,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    payload["output_file"] = str(path)
    return payload


def run_once(args: argparse.Namespace) -> int:
    initial_until = parse_utc(args.until) if args.until else datetime.now(timezone.utc)
    cursor_until = load_checkpoint(Path(args.checkpoint), initial_until)
    windows = list(iter_backward_windows(cursor_until, window_days=args.window_days, max_windows=args.max_windows))
    if args.plan_only:
        print(json.dumps({"windows": [window.as_json() for window in windows]}, ensure_ascii=False, indent=2))
        return 0

    for idx, window in enumerate(windows, start=1):
        output_path = output_path_for_window(Path(args.output_dir), window)
        print(f"[eval-worker] window {idx}/{len(windows)} {_iso_utc(window.start)} -> {_iso_utc(window.end)}", flush=True)
        candidates = query_candidates_between(
            window,
            limit=args.candidate_limit,
            scan_limit=args.scan_limit,
            statement_timeout_sec=args.db_statement_timeout_sec,
        )
        print(f"[eval-worker] lexical_candidates={len(candidates)} output={output_path}", flush=True)
        outputs = classify_candidates(args, candidates)
        apply_result: dict[str, Any] | None = None
        info_refresh_result: dict[str, Any] | None = None
        if args.apply:
            applied = apply_eval_updates(outputs, args.min_confidence)
            applied_ids = {item["id"] for item in applied}
            for item in outputs:
                item["applied"] = item["id"] in applied_ids
            apply_result = {
                "applied_count": len(applied),
                "applied_ids": [item["id"] for item in applied],
            }
            if applied and not args.skip_info_read_model_refresh:
                info_refresh_result = refresh_eval_info_scopes(
                    [item["id"] for item in applied],
                    statement_timeout_sec=args.info_refresh_timeout_sec,
                )
        payload = write_window_output(
            output_path,
            window=window,
            args=args,
            candidate_count=len(candidates),
            outputs=outputs,
            apply_result=apply_result,
            info_refresh_result=info_refresh_result,
        )
        save_checkpoint(Path(args.checkpoint), next_cursor_until=window.start, window=window, payload=payload)
        print(
            f"[eval-worker] wrote={output_path} llm_checked={payload['llm_checked']} "
            f"eval_hits={payload['eval_hits']} applied={payload['apply'].get('applied_count', 0)} "
            f"checkpoint={args.checkpoint}",
            flush=True,
        )
        if idx < len(windows) and args.sleep_between_windows_sec > 0:
            time.sleep(args.sleep_between_windows_sec)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Slow eval backfill discovery worker")
    parser.add_argument(
        "--mode",
        choices=("run-window", "snapshot", "classify-offline", "apply-manifest"),
        default="run-window",
    )
    parser.add_argument("--checkpoint", default=str(DEFAULT_CHECKPOINT))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--snapshot-file", default=str(DEFAULT_SNAPSHOT_FILE))
    parser.add_argument("--classification-file", default=str(DEFAULT_CLASSIFICATION_FILE))
    parser.add_argument("--manifest-file", default=str(DEFAULT_MANIFEST_FILE))
    parser.add_argument("--lock-file", default=str(DEFAULT_LOCK_FILE))
    parser.add_argument("--until", default="", help="initial UTC cursor end; ignored after checkpoint exists")
    parser.add_argument("--window-days", type=int, default=7)
    parser.add_argument("--max-windows", type=int, default=1)
    parser.add_argument("--scan-limit", type=int, default=1000)
    parser.add_argument("--candidate-limit", type=int, default=40)
    parser.add_argument("--max-llm", type=int, default=10)
    parser.add_argument("--max-tokens", type=int, default=1200)
    parser.add_argument("--request-interval-sec", type=float, default=2.0)
    parser.add_argument("--db-statement-timeout-sec", type=int, default=8)
    parser.add_argument("--sleep-between-windows-sec", type=float, default=30.0)
    parser.add_argument("--list-only", action="store_true", help="write lexical candidates without LLM calls")
    parser.add_argument("--plan-only", action="store_true", help="print planned windows without touching DB")
    parser.add_argument("--append-output", action="store_true", help="append to JSONL outputs instead of replacing them")
    parser.add_argument("--resume-classification", action="store_true", help="skip snapshot ids already in classification JSONL")
    parser.add_argument("--offline-limit", type=int, default=0, help="classify at most N snapshot rows; 0 means all pending")
    parser.add_argument("--classification-concurrency", type=int, default=1, help="parallel MiniMax calls for classify-offline mode")
    parser.add_argument("--include-existing-eval", action="store_true", help="allow manifest/apply to update items already classified as eval")
    parser.add_argument("--apply", action="store_true", help="write eval classification fields and refresh eval info scopes")
    parser.add_argument(
        "--min-confidence",
        choices=("low", "medium", "high"),
        default="high",
        help="minimum confidence required for --apply",
    )
    parser.add_argument(
        "--skip-info-read-model-refresh",
        action="store_true",
        help="with --apply, skip targeted 信息 tab eval scope refresh",
    )
    parser.add_argument("--info-refresh-timeout-sec", type=int, default=20)
    return parser


def normalize_args(args: argparse.Namespace) -> argparse.Namespace:
    args.window_days = max(1, min(args.window_days, 30))
    args.max_windows = max(1, min(args.max_windows, 24))
    args.candidate_limit = max(1, min(args.candidate_limit, 200))
    args.scan_limit = max(args.candidate_limit, min(args.scan_limit, 5000))
    args.max_llm = max(1, min(args.max_llm, args.candidate_limit))
    args.max_tokens = max(200, min(args.max_tokens, 2000))
    args.request_interval_sec = max(0.8, min(args.request_interval_sec, 30.0))
    args.db_statement_timeout_sec = max(5, min(args.db_statement_timeout_sec, 60))
    args.sleep_between_windows_sec = max(0.0, min(args.sleep_between_windows_sec, 3600.0))
    args.info_refresh_timeout_sec = max(5, min(getattr(args, "info_refresh_timeout_sec", 20), 300))
    args.offline_limit = max(0, min(getattr(args, "offline_limit", 0), 20000))
    args.classification_concurrency = max(1, min(getattr(args, "classification_concurrency", 1), 20))
    if getattr(args, "list_only", False):
        args.apply = False
    return args


def main() -> int:
    args = normalize_args(build_parser().parse_args())
    with acquire_lock(Path(args.lock_file)) as locked:
        if not locked:
            print(f"[eval-worker] another worker is active; lock={args.lock_file}", flush=True)
            return 0
        if args.mode == "snapshot":
            return run_snapshot_mode(args)
        if args.mode == "classify-offline":
            return run_classify_offline_mode(args)
        if args.mode == "apply-manifest":
            return run_apply_manifest_mode(args)
        return run_once(args)


if __name__ == "__main__":
    raise SystemExit(main())
