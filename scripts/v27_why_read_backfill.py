#!/usr/bin/env python3
"""Backfill why_read by regenerating recent visible highlight clusters."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

import enrich_items  # noqa: E402
from env_utils import load_project_env  # noqa: E402
import remote_db  # noqa: E402
from clustering import summary_writer  # noqa: E402


def load_env_values(base_dir: Path) -> dict[str, str]:
    """Load this worktree's .env, falling back to the primary worktree."""
    local_values = load_project_env(base_dir)
    git_marker = base_dir / ".git"
    if not git_marker.is_file():
        return local_values
    try:
        git_dir_text = git_marker.read_text(encoding="utf-8").split("gitdir:", 1)[1].strip()
        git_dir = Path(git_dir_text)
        if not git_dir.is_absolute():
            git_dir = (base_dir / git_dir).resolve()
        primary_root = next(parent.parent for parent in git_dir.parents if parent.name == ".git")
    except (IndexError, OSError, StopIteration):
        return local_values
    values = load_project_env(primary_root)
    values.update(local_values)
    return values


def configure_environment() -> None:
    for key, value in load_env_values(ROOT).items():
        os.environ.setdefault(key, value)


def connect_database():
    return remote_db.connect()


def resolve_summary_runtime() -> tuple[str, str, str, int]:
    config = enrich_items.load_config()
    ai_config = config.get("ai_summary", {})
    api_key, api_base, model = enrich_items.resolve_minimax_runtime_config(ai_config)
    if not api_key:
        raise RuntimeError("MINIMAX_API_KEY missing in env/.env/config")
    clustering = config.get("global", {}).get("clustering", {})
    summary_max_docs = int(clustering.get("summary_max_docs", 20))
    return api_key, api_base, model, summary_max_docs


def iter_days(days: int, *, today: date | None = None) -> list[date]:
    start = today or date.today()
    return [start - timedelta(days=offset) for offset in range(days)]


def fetch_day_candidates(
    conn: Any,
    *,
    day: date,
    threshold: float,
    limit: int,
    schema: str,
    force: bool,
) -> list[dict[str, Any]]:
    why_read_filter = "" if force else "\n               AND c.why_read IS NULL"
    rows = conn.execute(
        f"""SELECT c.id,
                   c.ai_title,
                   COALESCE(
                       c.first_doc_at,
                       c.last_doc_at,
                       c.last_updated_at,
                       c.published_at,
                       c.created_at
                   ) AS event_at,
                   (d.score_inputs->>'max_flag_score10')::float AS max_flag_score10
              FROM {schema}.clusters c
              JOIN {schema}.highlight_cluster_decisions d ON d.cluster_id = c.id
             WHERE c.is_visible_in_feed IS TRUE{why_read_filter}
               AND (
                     (d.score_inputs->>'max_flag_score10')::float >= %(threshold)s
                     OR d.manual_display = 'force_show'
                   )
               AND COALESCE(
                       c.first_doc_at,
                       c.last_doc_at,
                       c.last_updated_at,
                       c.published_at,
                       c.created_at
                   ) >= %(day_start)s::date
               AND COALESCE(
                       c.first_doc_at,
                       c.last_doc_at,
                       c.last_updated_at,
                       c.published_at,
                       c.created_at
                   ) < (%(day_start)s::date + interval '1 day')
             ORDER BY event_at DESC, c.id DESC
             LIMIT %(limit)s""",
        {
            "day_start": day,
            "threshold": threshold,
            "limit": limit,
        },
    ).fetchall()
    return [dict(row) for row in rows]


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def _non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Regenerate recent visible highlight clusters that lack why_read"
    )
    parser.add_argument("--days", type=_positive_int, default=1, help="days from today, newest first")
    parser.add_argument("--threshold", type=float, default=7.0, help="minimum max_flag_score10")
    parser.add_argument("--limit", type=_positive_int, default=200, help="global safety limit")
    parser.add_argument(
        "--sleep-seconds",
        type=_non_negative_float,
        default=1.0,
        help="sleep between summary regenerations",
    )
    parser.add_argument("--dry-run", action="store_true", help="print targets without calling the LLM")
    parser.add_argument(
        "--force",
        action="store_true",
        help="regenerate clusters even when why_read is already populated",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace, *, today: date | None = None) -> int:
    configure_environment()
    schema = remote_db.remote_schema()
    selected_by_day: list[tuple[date, list[dict[str, Any]]]] = []
    selected_total = 0

    with connect_database() as conn:
        for target_day in iter_days(args.days, today=today):
            remaining = args.limit - selected_total
            if remaining <= 0:
                break
            rows = fetch_day_candidates(
                conn,
                day=target_day,
                threshold=args.threshold,
                limit=remaining,
                schema=schema,
                force=args.force,
            )
            selected_by_day.append((target_day, rows))
            selected_total += len(rows)
            print(
                f"[v27-why-read] day={target_day.isoformat()} candidates={len(rows)} "
                f"selected_total={selected_total}/{args.limit}",
                flush=True,
            )

        if args.dry_run:
            for target_day, rows in selected_by_day:
                for row in rows:
                    print(
                        json.dumps(
                            {"day": target_day.isoformat(), **row},
                            ensure_ascii=False,
                            default=str,
                        ),
                        flush=True,
                    )
            print(
                "[v27-why-read] dry-run: no LLM calls or database writes",
                flush=True,
            )
            return 0

        if not selected_total:
            print("[v27-why-read] no eligible clusters", flush=True)
            return 0
        if not remote_db.cluster_to_remote():
            raise RuntimeError("INFO2ACTION_CLUSTER_BACKEND must resolve to supabase")

        api_key, api_base, model, summary_max_docs = resolve_summary_runtime()
        processed = 0
        succeeded = 0
        for target_day, rows in selected_by_day:
            for row in rows:
                ok = summary_writer.regenerate_and_swap(
                    conn,
                    int(row["id"]),
                    api_key=api_key,
                    api_base=api_base,
                    model=model,
                    summary_max_docs=summary_max_docs,
                    publish_immediately=True,
                )
                processed += 1
                succeeded += int(ok)
                print(
                    f"[v27-why-read] day={target_day.isoformat()} "
                    f"processed={processed}/{selected_total} succeeded={succeeded} "
                    f"cluster_id={row['id']}",
                    flush=True,
                )
                if processed < selected_total and args.sleep_seconds:
                    time.sleep(args.sleep_seconds)

    failed = selected_total - succeeded
    print(
        f"[v27-why-read] done processed={selected_total} succeeded={succeeded} failed={failed}",
        flush=True,
    )
    return 1 if failed else 0


def main() -> int:
    return run(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
