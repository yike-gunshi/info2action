#!/usr/bin/env python3
"""Backfill recent summaries that lack non-heading Markdown bolding."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import ai_bolding  # noqa: E402
import db  # noqa: E402
import enrich_items  # noqa: E402
import remote_db  # noqa: E402
from clustering import summary_writer  # noqa: E402


DEFAULT_WINDOW_HOURS = 24


def select_local_item_candidates(conn, *, since_iso: str, limit: int | None = None) -> list[dict[str, Any]]:
    limit_clause = " LIMIT ?" if limit else ""
    params: list[Any] = [since_iso]
    if limit:
        params.append(limit)
    rows = conn.execute(
        f"""SELECT id, ai_summary, ai_key_points
              FROM items
             WHERE platform != 'bilibili'
               AND COALESCE(visible, 1) != 0
               AND ai_summary IS NOT NULL
               AND ai_summary != ''
               AND COALESCE(published_at, fetched_at, created_at) >= ?
             ORDER BY COALESCE(published_at, fetched_at, created_at) DESC{limit_clause}""",
        tuple(params),
    ).fetchall()
    return [
        dict(row)
        for row in rows
        if ai_bolding.item_needs_bolding(row["ai_summary"], _json_list(row["ai_key_points"]))
    ]


def select_local_cluster_candidates(conn, *, since_iso: str, limit: int | None = None) -> list[dict[str, Any]]:
    limit_clause = " LIMIT ?" if limit else ""
    params: list[Any] = [since_iso]
    if limit:
        params.append(limit)
    rows = conn.execute(
        f"""SELECT id, ai_title, ai_summary
              FROM clusters
             WHERE is_visible_in_feed = 1
               AND archived = 0
               AND merged_into IS NULL
               AND ai_summary IS NOT NULL
               AND ai_summary != ''
               AND COALESCE(published_at, last_updated_at, created_at) >= ?
             ORDER BY COALESCE(published_at, last_updated_at, created_at) DESC{limit_clause}""",
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows if ai_bolding.cluster_needs_bolding(row["ai_summary"])]


def select_remote_item_candidates(*, since_iso: str, limit: int | None = None) -> list[dict[str, Any]]:
    schema = remote_db.remote_schema()
    limit_clause = " LIMIT %s" if limit else ""
    params: list[Any] = [since_iso]
    if limit:
        params.append(limit)
    with remote_db.connect() as conn:
        rows = conn.execute(
            f"""SELECT id, ai_summary, ai_key_points
                  FROM {schema}.items
                 WHERE platform <> 'bilibili'
                   AND COALESCE(visible, 1) <> 0
                   AND ai_summary IS NOT NULL
                   AND ai_summary <> ''
                   AND COALESCE(published_at, fetched_at, created_at) >= %s
                 ORDER BY COALESCE(published_at, fetched_at, created_at) DESC{limit_clause}""",
            tuple(params),
        ).fetchall()
    return [
        dict(row)
        for row in rows
        if ai_bolding.item_needs_bolding(row.get("ai_summary"), _json_list(row.get("ai_key_points")))
    ]


def select_remote_cluster_candidates(*, since_iso: str, limit: int | None = None) -> list[dict[str, Any]]:
    schema = remote_db.remote_schema()
    limit_clause = " LIMIT %s" if limit else ""
    params: list[Any] = [since_iso]
    if limit:
        params.append(limit)
    with remote_db.connect() as conn:
        rows = conn.execute(
            f"""SELECT id, ai_title, ai_summary
                  FROM {schema}.clusters
                 WHERE is_visible_in_feed = TRUE
                   AND archived = FALSE
                   AND merged_into IS NULL
                   AND ai_summary IS NOT NULL
                   AND ai_summary <> ''
                   AND COALESCE(published_at, last_updated_at, created_at) >= %s
                 ORDER BY COALESCE(published_at, last_updated_at, created_at) DESC{limit_clause}""",
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows if ai_bolding.cluster_needs_bolding(row.get("ai_summary"))]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write regenerated summaries; default is dry-run")
    parser.add_argument("--window-hours", type=float, default=DEFAULT_WINDOW_HOURS)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--items-only", action="store_true")
    parser.add_argument("--clusters-only", action="store_true")
    args = parser.parse_args()

    if args.items_only and args.clusters_only:
        parser.error("--items-only and --clusters-only cannot be used together")

    since_iso = _since_iso(args.window_hours)
    limit = args.limit or None
    conn = db.get_conn()
    try:
        item_candidates = [] if args.clusters_only else _select_item_candidates(conn, since_iso=since_iso, limit=limit)
        cluster_candidates = [] if args.items_only else _select_cluster_candidates(conn, since_iso=since_iso, limit=limit)
        print(f"[plan] since={since_iso} apply={args.apply}")
        print(f"[plan] item_candidates={len(item_candidates)} first_ids={_first_ids(item_candidates)}")
        print(f"[plan] cluster_candidates={len(cluster_candidates)} first_ids={_first_ids(cluster_candidates)}")
        if not args.apply:
            print("[dry-run] no writes performed; rerun with --apply to regenerate")
            return 0
        _apply_item_backfill([row["id"] for row in item_candidates])
        _apply_cluster_backfill(conn, [int(row["id"]) for row in cluster_candidates])
    finally:
        conn.close()
    return 0


def _select_item_candidates(conn, *, since_iso: str, limit: int | None) -> list[dict[str, Any]]:
    if remote_db.enrich_to_remote():
        return select_remote_item_candidates(since_iso=since_iso, limit=limit)
    return select_local_item_candidates(conn, since_iso=since_iso, limit=limit)


def _select_cluster_candidates(conn, *, since_iso: str, limit: int | None) -> list[dict[str, Any]]:
    if remote_db.cluster_to_remote():
        return select_remote_cluster_candidates(since_iso=since_iso, limit=limit)
    return select_local_cluster_candidates(conn, since_iso=since_iso, limit=limit)


def _apply_item_backfill(item_ids: list[str]) -> None:
    if not item_ids:
        return
    config = enrich_items.load_config()
    classification = enrich_items.load_classification()
    categories = classification.get("categories", [])
    valid_category_ids = [cat["id"] for cat in categories]
    valid_l2_by_l1 = enrich_items.build_subcategory_map(categories)
    ai_config = config.get("ai_summary", {})
    api_key, api_base, model = enrich_items.resolve_minimax_runtime_config(ai_config)
    max_tokens = int(ai_config.get("max_tokens", 100000))
    system_prompt = enrich_items.build_system_prompt(categories)
    if remote_db.enrich_to_remote():
        items = enrich_items.query_pending_enrichment_items_remote_with_retry(ids=item_ids, limit=len(item_ids))
    else:
        conn = db.get_conn()
        try:
            items = enrich_items.query_pending_items(conn, ids=item_ids)
        finally:
            conn.close()
    for item in items:
        enrich_items.enrich_one_item(
            _row_to_dict(item),
            api_key,
            api_base,
            model,
            system_prompt,
            valid_category_ids,
            max_tokens,
            False,
            valid_l2_by_l1=valid_l2_by_l1,
        )


def _apply_cluster_backfill(conn, cluster_ids: list[int]) -> None:
    if not cluster_ids:
        return
    config = enrich_items.load_config()
    ai_config = config.get("ai_summary", {})
    api_key, api_base, model = enrich_items.resolve_minimax_runtime_config(ai_config)
    clustering_cfg = config.get("global", {}).get("clustering", {})
    summary_max_docs = int(clustering_cfg.get("summary_max_docs", 20))
    for cluster_id in cluster_ids:
        summary_writer.regenerate_and_swap(
            conn,
            cluster_id,
            api_key=api_key,
            api_base=api_base,
            model=model,
            summary_max_docs=summary_max_docs,
            publish_immediately=True,
        )


def _json_list(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _row_to_dict(row: Any) -> dict[str, Any]:
    return row if isinstance(row, dict) else dict(row)


def _since_iso(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _first_ids(rows: list[dict[str, Any]], *, max_items: int = 12) -> list[Any]:
    return [row.get("id") for row in rows[:max_items]]


if __name__ == "__main__":
    raise SystemExit(main())
