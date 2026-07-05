#!/usr/bin/env python3
"""Reset run-scoped singleton clusters created from failed LLM judge calls.

This is a recovery tool for historical backfill runs. A failed judge response
must not make an item look successfully clustered; this script puts those items
back into the pending clustering queue by clearing ``items.cluster_id`` and
removing the one-item clusters that were created only as failure fallbacks.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "src"))

import db  # noqa: E402


def _window_sql_filter(
    window_start: str | None,
    window_end: str | None,
    *,
    require_published_at: bool,
) -> tuple[str, list[str]]:
    if not window_start and not window_end:
        return "", []
    expr = (
        "datetime(NULLIF(i.published_at, ''))"
        if require_published_at
        else "COALESCE(datetime(NULLIF(i.published_at, '')), datetime(NULLIF(i.fetched_at, '')))"
    )
    clauses: list[str] = []
    params: list[str] = []
    if require_published_at:
        clauses.append(" AND datetime(NULLIF(i.published_at, '')) IS NOT NULL")
    if window_start:
        clauses.append(f" AND {expr} >= datetime(?)")
        params.append(window_start)
    if window_end:
        clauses.append(f" AND {expr} < datetime(?)")
        params.append(window_end)
    return "".join(clauses), params


def reset_failed_judge_singletons(
    conn: Any,
    *,
    run_id: int,
    window_start: str | None = None,
    window_end: str | None = None,
    require_published_at: bool = True,
    dry_run: bool = False,
) -> dict[str, int]:
    window_filter, window_params = _window_sql_filter(
        window_start,
        window_end,
        require_published_at=require_published_at,
    )
    rows = conn.execute(
        f"""SELECT DISTINCT i.id AS item_id, i.cluster_id
              FROM items i
              JOIN cluster_judge_log l ON l.item_id = i.id
             WHERE i.fetch_run_id = ?
               AND i.cluster_id IS NOT NULL
               AND l.selection_reason = 'llm-failed-fallback-singleton'
               {window_filter}
             ORDER BY i.id ASC""",
        (run_id, *window_params),
    ).fetchall()
    item_ids = [row["item_id"] for row in rows]
    singleton_cluster_ids: set[int] = set()
    non_singleton_clusters = 0
    for row in rows:
        cluster_id = row["cluster_id"]
        members = conn.execute(
            "SELECT item_id FROM cluster_items WHERE cluster_id = ?",
            (cluster_id,),
        ).fetchall()
        member_ids = {member["item_id"] for member in members}
        if member_ids == {row["item_id"]}:
            singleton_cluster_ids.add(int(cluster_id))
        else:
            non_singleton_clusters += 1

    stats = {
        "items_reset": len(item_ids),
        "singleton_clusters_deleted": len(singleton_cluster_ids),
        "non_singleton_clusters_touched": non_singleton_clusters,
        "judge_logs_deleted": 0,
    }
    if dry_run or not item_ids:
        return stats

    placeholders = ",".join("?" * len(item_ids))
    cluster_placeholders = ",".join("?" * len(singleton_cluster_ids))
    try:
        conn.execute("BEGIN")
        deleted_logs = conn.execute(
            f"""DELETE FROM cluster_judge_log
                  WHERE selection_reason = 'llm-failed-fallback-singleton'
                    AND item_id IN ({placeholders})""",
            item_ids,
        ).rowcount or 0
        conn.execute(
            f"DELETE FROM cluster_items WHERE item_id IN ({placeholders})",
            item_ids,
        )
        if singleton_cluster_ids:
            conn.execute(
                f"DELETE FROM clusters WHERE id IN ({cluster_placeholders})",
                list(singleton_cluster_ids),
            )
        conn.execute(
            f"UPDATE items SET cluster_id = NULL WHERE id IN ({placeholders})",
            item_ids,
        )
        conn.commit()
        stats["judge_logs_deleted"] = deleted_logs
    except Exception:
        conn.rollback()
        raise
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reset failed judge fallback singleton clusters")
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--window-start", default=None)
    parser.add_argument("--window-end", default=None)
    parser.add_argument("--no-window-require-published-at", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    conn = db.get_conn()
    try:
        stats = reset_failed_judge_singletons(
            conn,
            run_id=args.run_id,
            window_start=args.window_start,
            window_end=args.window_end,
            require_published_at=not args.no_window_require_published_at,
            dry_run=args.dry_run,
        )
    finally:
        conn.close()
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
