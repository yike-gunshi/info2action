#!/usr/bin/env python3
"""Run ready-only historical clustering in adaptive newest-first windows."""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parents[1]
SRC = BASE / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import backfill_since as bf  # noqa: E402
import db  # noqa: E402
from clustering import visibility_policy  # noqa: E402


@dataclass(frozen=True)
class WindowPlan:
    start: datetime
    end: datetime
    ready_count: int
    draft_count: int = 0
    split_from_hours: int | None = None

    def as_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["start"] = bf._iso_utc(self.start)
        data["end"] = bf._iso_utc(self.end)
        return data


def _parse_utc_sqlite(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def latest_ready_published_at(conn: Any) -> datetime:
    row = conn.execute(
        """SELECT max(datetime(published_at)) AS max_published_at
             FROM items
            WHERE published_at IS NOT NULL
              AND trim(published_at) != ''
              AND datetime(published_at) IS NOT NULL"""
    ).fetchone()
    value = row[0] if row else None
    if not value:
        raise RuntimeError("no parseable published_at found in local DB")
    return _parse_utc_sqlite(str(value))


def count_ready_candidates(conn: Any, start: datetime, end: datetime) -> int:
    category_ids = sorted(visibility_policy.HIGH_VALUE_SINGLE_SOURCE_CATEGORY_ALIASES)
    placeholders = ",".join("?" * len(category_ids))
    row = conn.execute(
        f"""SELECT COUNT(*)
             FROM items
            WHERE cluster_id IS NULL
              AND embedding IS NOT NULL
              AND ai_summary IS NOT NULL AND ai_summary != ''
              AND ai_category IS NOT NULL AND ai_category != ''
              AND lower(COALESCE(ai_category, '')) IN ({placeholders})
              AND ai_categories IS NOT NULL AND ai_categories != ''
              AND ai_quality_score IS NOT NULL
              AND datetime(NULLIF(published_at, '')) IS NOT NULL
              AND datetime(NULLIF(published_at, '')) >= datetime(?)
              AND datetime(NULLIF(published_at, '')) < datetime(?)""",
        (*category_ids, bf._iso_utc(start), bf._iso_utc(end)),
    ).fetchone()
    return int(row[0] or 0)


def count_run_draft_clusters(conn: Any, run_id: int | None, start: datetime, end: datetime) -> int:
    if run_id is None:
        return 0
    category_ids = sorted(visibility_policy.HIGH_VALUE_SINGLE_SOURCE_CATEGORY_ALIASES)
    placeholders = ",".join("?" * len(category_ids))
    row = conn.execute(
        f"""SELECT COUNT(DISTINCT c.id)
             FROM clusters c
             JOIN cluster_items ci ON ci.cluster_id = c.id
             JOIN items i ON i.id = ci.item_id
            WHERE c.last_touched_run_id = ?
              AND c.archived = 0
              AND c.merged_into IS NULL
              AND COALESCE(c.published_run_id, -1) != ?
              AND (
                c.ai_title_draft IS NOT NULL
                OR c.ai_summary_draft IS NOT NULL
                OR c.ai_key_points_draft IS NOT NULL
                OR c.pending_is_visible_in_feed IS NOT NULL
                OR (
                  (
                    COALESCE(c.unique_source_count, 0) >= 2
                    OR EXISTS (
                      SELECT 1
                        FROM cluster_items ci_candidate
                        JOIN items i_candidate ON i_candidate.id = ci_candidate.item_id
                       WHERE ci_candidate.cluster_id = c.id
                         AND lower(COALESCE(i_candidate.ai_category, '')) IN ({placeholders})
                    )
                  )
                  AND (
                    c.ai_title IS NULL
                    OR c.ai_summary IS NULL
                    OR c.ai_key_points IS NULL
                  )
                )
              )
              AND datetime(NULLIF(i.published_at, '')) IS NOT NULL
              AND datetime(NULLIF(i.published_at, '')) >= datetime(?)
              AND datetime(NULLIF(i.published_at, '')) < datetime(?)""",
        (run_id, run_id, *category_ids, bf._iso_utc(start), bf._iso_utc(end)),
    ).fetchone()
    return int(row[0] or 0)


def build_window_plan(
    conn: Any,
    *,
    since: datetime,
    until: datetime,
    window_hours: int,
    split_threshold: int,
    split_hours: int,
    run_id: int | None = None,
) -> list[WindowPlan]:
    plan: list[WindowPlan] = []
    base_windows = bf.iter_processing_windows(since, until, days=1, hours=window_hours)
    for base_start, base_end in base_windows:
        ready = count_ready_candidates(conn, base_start, base_end)
        drafts = count_run_draft_clusters(conn, run_id, base_start, base_end)
        if ready + drafts > split_threshold:
            for start, end in bf.iter_processing_windows(base_start, base_end, days=1, hours=split_hours):
                plan.append(
                    WindowPlan(
                        start=start,
                        end=end,
                        ready_count=count_ready_candidates(conn, start, end),
                        draft_count=count_run_draft_clusters(conn, run_id, start, end),
                        split_from_hours=window_hours,
                    )
                )
        else:
            plan.append(WindowPlan(base_start, base_end, ready, drafts))
    return plan


def build_backfill_cmd(args: argparse.Namespace, window: WindowPlan) -> list[str]:
    process_window_hours = max(
        1,
        int(math.ceil((window.end - window.start).total_seconds() / 3600)),
    )
    cmd = [
        sys.executable,
        "-u",
        str(BASE / "src" / "backfill_since.py"),
        "--since",
        bf._iso_utc(window.start),
        "--until",
        bf._iso_utc(window.end),
        "--skip-fetch",
        "--ready-cluster-only",
        "--window-require-published-at",
        "--process-window-days",
        "0",
        "--process-window-hours",
        str(process_window_hours),
    ]
    if args.top_k is not None:
        cmd.extend(["--top-k", str(args.top_k)])
    cmd.extend([
        "--judge-workers",
        str(args.judge_workers),
        "--judge-min-interval-sec",
        str(args.judge_min_interval_sec),
        "--summary-workers",
        str(args.summary_workers),
        "--ai-timeout",
        str(args.ai_timeout),
        "--cluster-timeout",
        str(args.cluster_timeout),
    ])
    if args.run_id is not None:
        cmd.extend(["--run-id", str(args.run_id)])
    return cmd


def _log_line(log_file: Path | None, line: str) -> None:
    if log_file is None:
        return
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as f:
        f.write(line)
        if not line.endswith("\n"):
            f.write("\n")


def run_command(cmd: list[str], *, log_file: Path | None) -> int:
    _log_line(log_file, f"$ {' '.join(cmd)}\n")
    proc = subprocess.Popen(
        cmd,
        cwd=BASE,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="")
        _log_line(log_file, line)
    return proc.wait()


def execute_plan(args: argparse.Namespace, plan: list[WindowPlan]) -> int:
    log_file = Path(args.log_file) if args.log_file else None
    executed = 0
    for idx, window in enumerate(plan, start=1):
        if window.ready_count <= 0 and window.draft_count <= 0:
            print(
                f"[skip] {idx}/{len(plan)} {bf._window_label(window.start, window.end)} "
                "ready=0 draft=0",
                flush=True,
            )
            continue
        if args.max_windows is not None and executed >= args.max_windows:
            print(f"[stop] max_windows={args.max_windows} reached", flush=True)
            return 0
        label = bf._window_label(window.start, window.end)
        print(
            f"\n[window] {idx}/{len(plan)} {label} "
            f"ready={window.ready_count} draft={window.draft_count}",
            flush=True,
        )
        cmd = build_backfill_cmd(args, window)
        for attempt in range(args.retries + 1):
            if attempt:
                print(f"[retry] attempt {attempt + 1}/{args.retries + 1} after {args.retry_sleep_sec}s", flush=True)
                time.sleep(args.retry_sleep_sec)
            _log_line(log_file, f"\n===== {datetime.now(timezone.utc).isoformat()} {label} attempt={attempt + 1} =====\n")
            code = run_command(cmd, log_file=log_file)
            if code == 0:
                executed += 1
                break
            if attempt >= args.retries:
                print(f"[fail] {label} exit={code}; stop before later windows", flush=True)
                return code or 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Adaptive ready-only local backfill runner")
    parser.add_argument("--since", default=None, help="UTC/local datetime start; default: until - --days")
    parser.add_argument("--until", default=None, help="UTC/local datetime end; default: latest parseable published_at")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--run-id", type=int, default=None, help="reuse an existing fetch_run, e.g. 1094")
    parser.add_argument("--window-hours", type=int, default=6)
    parser.add_argument("--split-threshold", type=int, default=300)
    parser.add_argument("--split-hours", type=int, default=1)
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--judge-workers", type=int, default=1)
    parser.add_argument("--judge-min-interval-sec", type=float, default=6.0)
    parser.add_argument("--summary-workers", type=int, default=1)
    parser.add_argument("--ai-timeout", type=int, default=7200)
    parser.add_argument("--cluster-timeout", type=int, default=7200)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--retry-sleep-sec", type=float, default=90.0)
    parser.add_argument("--max-windows", type=int, default=None)
    parser.add_argument("--log-file", default=None)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    bf._apply_project_env()
    args = build_parser().parse_args(argv)
    conn = db.get_conn()
    try:
        until = bf._parse_window_end(args.until) if args.until else latest_ready_published_at(conn)
        since = bf._parse_window_start(args.since) if args.since else until - timedelta(days=args.days)
        plan = build_window_plan(
            conn,
            since=since,
            until=until,
            window_hours=args.window_hours,
            split_threshold=args.split_threshold,
            split_hours=args.split_hours,
            run_id=args.run_id,
        )
    finally:
        conn.close()
    payload = {
        "since": bf._iso_utc(since),
        "until": bf._iso_utc(until),
        "windows": [window.as_json() for window in plan],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
    if args.dry_run:
        return 0
    return execute_plan(args, plan)


if __name__ == "__main__":
    raise SystemExit(main())
