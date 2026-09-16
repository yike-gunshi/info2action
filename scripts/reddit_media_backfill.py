#!/usr/bin/env python3
"""Inspect missing Reddit media or backfill one explicitly allowed item."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "src"))

from reddit_media import complete_reddit_media  # noqa: E402


def _cutoff_iso(days: int, now_iso: str | None = None) -> str:
    now = datetime.fromisoformat((now_iso or datetime.now(timezone.utc).isoformat()).replace("Z", "+00:00"))
    return (now - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def count_missing_media(
    conn: sqlite3.Connection,
    *,
    days: int = 30,
    now_iso: str | None = None,
) -> int:
    cutoff = _cutoff_iso(days, now_iso)
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM items
        WHERE platform = 'reddit'
          AND fetched_at >= ?
          AND (media_json IS NULL OR trim(media_json) IN ('', '[]'))
        """,
        (cutoff,),
    ).fetchone()
    return int(row[0] if row else 0)


def _safe_schema(schema: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise ValueError("invalid remote schema")
    return schema


def count_missing_media_remote(
    conn: Any,
    *,
    schema: str,
    days: int = 30,
    now_iso: str | None = None,
) -> int:
    schema = _safe_schema(schema)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM {schema}.items
        WHERE platform = 'reddit'
          AND fetched_at >= %s
          AND (
            media_json IS NULL
            OR media_json = '[]'::jsonb
          )
        """,
        (_cutoff_iso(days, now_iso),),
    ).fetchone()
    if not row:
        return 0
    return int(row["count"] if isinstance(row, dict) else row[0])


def backfill_one_item(
    conn: sqlite3.Connection,
    item_id: str,
    *,
    media_loader: Callable[[str], list[dict[str, Any]]] = complete_reddit_media,
) -> dict[str, str]:
    row = conn.execute(
        "SELECT id, platform, url FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if row is None or row["platform"] != "reddit":
        return {"item_id": item_id, "status": "not_found"}
    media = media_loader(str(row["url"] or ""))
    if not media:
        return {"item_id": item_id, "status": "no_media"}
    poster = str(media[0].get("poster_url") or "").strip() or None
    conn.execute(
        """
        UPDATE items
        SET media_json = ?,
            cover_url = COALESCE(?, cover_url)
        WHERE id = ?
        """,
        (json.dumps(media, ensure_ascii=False), poster, item_id),
    )
    conn.commit()
    return {"item_id": item_id, "status": "updated"}


def backfill_one_item_remote(
    conn: Any,
    item_id: str,
    *,
    allowed_item_ids: set[str],
    schema: str,
    media_loader: Callable[[str], list[dict[str, Any]]] = complete_reddit_media,
) -> dict[str, str]:
    if item_id not in allowed_item_ids:
        raise ValueError(f"item {item_id} is not in the explicit allowlist")
    schema = _safe_schema(schema)
    row = conn.execute(
        f"SELECT id, platform, url FROM {schema}.items WHERE id = %s",
        (item_id,),
    ).fetchone()
    if row is None or row["platform"] != "reddit":
        return {"item_id": item_id, "status": "not_found"}
    media = media_loader(str(row["url"] or ""))
    if not media:
        return {"item_id": item_id, "status": "no_media"}
    poster = str(media[0].get("poster_url") or "").strip() or None
    conn.execute(
        f"""
        UPDATE {schema}.items
        SET media_json = %s::jsonb,
            cover_url = COALESCE(%s, cover_url)
        WHERE id = %s
        """,
        (json.dumps(media, ensure_ascii=False), poster, item_id),
    )
    commit = getattr(conn, "commit", None)
    if callable(commit):
        commit()
    return {"item_id": item_id, "status": "updated"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("local", "remote"), required=True)
    parser.add_argument("--db", type=Path, help="Explicit local SQLite feed.db path")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--item-id", help="Resolve and optionally update exactly one Reddit item")
    parser.add_argument(
        "--allow-item-id",
        action="append",
        default=[],
        help="Exact item allowed to change; repeatable and required with --apply",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Required with --item-id; without it the command only prints the candidate",
    )
    parser.add_argument(
        "--run-asr",
        action="store_true",
        help="After an updated item, run the existing ASR and Item/Cluster resummary chain",
    )
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.backend == "local" and args.db is None:
        raise ValueError("--db is required for --backend local")
    if args.backend == "remote" and args.db is not None:
        raise ValueError("--db cannot be used with --backend remote")
    if args.apply:
        if not args.item_id:
            raise ValueError("--apply requires exactly one --item-id")
        if args.item_id not in set(args.allow_item_id or []):
            raise ValueError("--apply requires a matching --allow-item-id")
    if getattr(args, "run_asr", False) and not args.apply:
        raise ValueError("--run-asr requires --apply")


def run_item_asr(item_id: str, *, conn: Any = None) -> dict[str, Any]:
    """Run the existing transcript and dependent summary refresh chain."""
    import asr_worker

    result = asr_worker.run_asr_inline(
        item_id,
        bypass_quota=False,
        conn=conn,
    )
    return {
        "item_id": item_id,
        "asr_status": result.status,
        "has_transcript": bool(result.transcript),
    }


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        validate_args(args)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2
    if args.backend == "local":
        if not args.db.is_file():
            print(f"ERROR: local SQLite DB not found: {args.db}")
            return 2
        conn = sqlite3.connect(args.db)
        conn.row_factory = sqlite3.Row
        try:
            missing = count_missing_media(conn, days=args.days)
            print(json.dumps({"backend": "local", "days": args.days, "missing_reddit_media": missing}))
            if not args.item_id:
                return 0
            if not args.apply:
                print(json.dumps({"item_id": args.item_id, "status": "dry_run"}))
                return 0
            result = backfill_one_item(conn, args.item_id)
            print(json.dumps(result, ensure_ascii=False))
            if args.run_asr and result.get("status") == "updated":
                print(json.dumps(run_item_asr(args.item_id, conn=conn), ensure_ascii=False))
            return 0
        finally:
            conn.close()

    import remote_db

    with remote_db.connect() as conn:
        missing = count_missing_media_remote(
            conn,
            schema=remote_db.remote_schema(),
            days=args.days,
        )
        print(json.dumps({"backend": "remote", "days": args.days, "missing_reddit_media": missing}))
        if not args.item_id:
            return 0
        if not args.apply:
            print(json.dumps({"item_id": args.item_id, "status": "dry_run"}))
            return 0
        result = backfill_one_item_remote(
            conn,
            args.item_id,
            allowed_item_ids=set(args.allow_item_id),
            schema=remote_db.remote_schema(),
        )
        print(json.dumps(result, ensure_ascii=False))
        if args.run_asr and result.get("status") == "updated":
            print(json.dumps(run_item_asr(args.item_id), ensure_ascii=False))
        return 0


if __name__ == "__main__":
    raise SystemExit(run())
