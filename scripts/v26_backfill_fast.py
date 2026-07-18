#!/usr/bin/env python3
"""Batch-apply reviewed v26 scores to selected visible cluster members."""
from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterator, Sequence

from psycopg import sql


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from env_utils import load_project_env  # noqa: E402
import highlight_score_v26  # noqa: E402
import remote_db  # noqa: E402


DEFAULT_INPUT = (
    ROOT
    / ".features"
    / "highlights-refactor-v26"
    / "offline-rescore"
    / "scores.jsonl"
)
WRITE_OPTIONS = "-c statement_timeout=180000"
VALUES_ROW = "(%s, %s, %s, %s, %s, %s, %s, %s, %s)"
DECISION_CLUSTER_TABLE = "pg_temp.v26_backfill_decision_clusters"


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


def database_url() -> str:
    values = load_env_values(ROOT)
    dsn = (
        os.environ.get("SUPABASE_DB_DIRECT_URL")
        or values.get("SUPABASE_DB_DIRECT_URL")
        or os.environ.get("SUPABASE_DB_URL")
        or values.get("SUPABASE_DB_URL")
    )
    if not dsn:
        raise RuntimeError("SUPABASE_DB_DIRECT_URL or SUPABASE_DB_URL missing in .env")
    return dsn


def connect_database():
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("psycopg is required; install project requirements") from exc
    return psycopg.connect(
        database_url(),
        options=WRITE_OPTIONS,
        connect_timeout=15,
    )


def load_records(input_file: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        input_file.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at line {line_number}: {exc}") from exc
        if not isinstance(record, dict) or not record.get("item_id"):
            raise ValueError(f"missing item_id at line {line_number}")
        if record.get("error"):
            continue
        records.append(record)
    return records


def intersect_records(
    records: Sequence[dict[str, Any]],
    target_item_ids: set[str],
) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if str(record["item_id"]) in target_item_ids
    ]


def iter_batches(rows: Sequence[Any], batch_size: int) -> Iterator[list[Any]]:
    for start in range(0, len(rows), batch_size):
        yield list(rows[start : start + batch_size])


def fetch_target_item_ids(
    conn: Any,
    *,
    days: int | None,
    schema: str,
    start_date: date | None = None,
    end_date: date | None = None,
) -> set[str]:
    if days is not None:
        if start_date is not None or end_date is not None:
            raise ValueError("days and explicit date band are mutually exclusive")
        date_filter = "c.last_updated_at > now() - (%s * interval '1 day')"
        params: tuple[Any, ...] = (days,)
    else:
        if start_date is None or end_date is None:
            raise ValueError("explicit date band requires start_date and end_date")
        date_filter = "c.last_updated_at >= %s::date AND c.last_updated_at < %s::date"
        params = (start_date, end_date)
    query = sql.SQL(
        f"""SELECT ci.item_id
             FROM {{}}.{{}} AS c
             JOIN {{}}.{{}} AS ci ON ci.cluster_id = c.id
            WHERE c.is_visible_in_feed = true
              AND c.merged_into IS NULL
              AND (c.archived IS NULL OR c.archived = false)
              AND {date_filter}"""
    ).format(
        sql.Identifier(schema),
        sql.Identifier("clusters"),
        sql.Identifier(schema),
        sql.Identifier("cluster_items"),
    )
    rows = conn.execute(query, params).fetchall()
    return {str(row[0]) for row in rows}


def resync_affected_cluster_decisions(
    conn: Any,
    *,
    item_ids: set[str],
    schema: str,
) -> int:
    if not item_ids:
        return 0
    create_query = sql.SQL(
        """CREATE TEMP TABLE v26_backfill_decision_clusters ON COMMIT DROP AS
           SELECT DISTINCT ci.cluster_id
             FROM {}.{} AS ci
            WHERE ci.item_id::text = ANY(%s::text[])"""
    ).format(
        sql.Identifier(schema),
        sql.Identifier("cluster_items"),
    )
    conn.execute(create_query, (sorted(item_ids),))
    conn.execute(f"ANALYZE {DECISION_CLUSTER_TABLE}")
    row = conn.execute(f"SELECT count(*) FROM {DECISION_CLUSTER_TABLE}").fetchone()
    affected = int((row or [0])[0] or 0)
    if affected:
        remote_db._sync_highlight_cluster_decisions(
            conn,
            schema,
            window_days=365,
            min_github_stars=remote_db.HIGHLIGHTS_READ_MODEL_MIN_GITHUB_STARS,
            delta_cluster_table=DECISION_CLUSTER_TABLE,
        )
    return affected


def _record_values(record: dict[str, Any], threshold: float) -> tuple[Any, ...]:
    dims = record.get("dims") or {}
    v26 = {
        "authority": dims.get("authority"),
        "substance": dims.get("substance"),
        "novelty": dims.get("novelty"),
        "timeliness": dims.get("timeliness"),
        "audience_fit": dims.get("audience_fit"),
        "marketing": record.get("marketing"),
        "score10": record.get("score10"),
        "content_type": record.get("content_type"),
        "reject": bool(record.get("reject")),
        "veto": record.get("veto"),
        "runs": record.get("runs"),
        "pass2_error": record.get("pass2_error"),
    }

    if record.get("reject") or record.get("veto") != "none":
        verdict = "drop"
    elif record.get("score10") is not None and record["score10"] >= threshold:
        verdict = "featured"
    else:
        verdict = "borderline"

    return (
        str(record["item_id"]),
        json.dumps(v26, ensure_ascii=False),
        bool(record.get("flag_bearer")),
        verdict,
        record.get("value_path"),
        record.get("uncertainty"),
        record.get("reason"),
        record.get("confidence"),
        highlight_score_v26.PROMPT_VERSION,
    )


def build_batch_update(
    records: Sequence[dict[str, Any]],
    *,
    threshold: float,
    schema: str,
) -> tuple[sql.Composed, tuple[Any, ...]]:
    placeholders = sql.SQL(", ").join(sql.SQL(VALUES_ROW) for _record in records)
    query = sql.SQL(
        """UPDATE {}.{} AS i
              SET highlight_scores = COALESCE(i.highlight_scores, '{{}}'::jsonb)
                                     || jsonb_build_object('v26', v.v26::jsonb),
                  highlight_include_in_highlights = v.include,
                  highlight_verdict = v.verdict,
                  highlight_value_path = v.value_path,
                  highlight_uncertainty = v.uncertainty,
                  highlight_reason = v.reason,
                  highlight_confidence = v.confidence,
                  highlight_prompt_version = v.pv,
                  highlight_scored_at = now(),
                  highlight_error_count = 0,
                  highlight_last_error = NULL,
                  highlight_retry_after = NULL
             FROM (VALUES {}) AS v(
                  id, v26, include, verdict, value_path,
                  uncertainty, reason, confidence, pv
             )
            WHERE i.id = v.id"""
    ).format(
        sql.Identifier(schema),
        sql.Identifier("items"),
        placeholders,
    )
    params = tuple(
        value
        for record in records
        for value in _record_values(record, threshold)
    )
    return query, params


def write_batch(
    conn: Any,
    records: Sequence[dict[str, Any]],
    *,
    threshold: float,
    schema: str,
) -> int:
    query, params = build_batch_update(records, threshold=threshold, schema=schema)
    cursor = conn.execute(query, params)
    return cursor.rowcount


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def _date_arg(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be YYYY-MM-DD") from exc


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Batch-backfill reviewed v26 scores for visible cluster members; "
            "the time selector applies to clusters.last_updated_at, then intersects scores.jsonl"
        )
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="scores.jsonl path")
    parser.add_argument(
        "--days",
        type=_positive_int,
        default=None,
        help="clusters.last_updated_at lookback days (default: 1; conflicts with explicit date band)",
    )
    parser.add_argument(
        "--start-date",
        type=_date_arg,
        help="clusters.last_updated_at lower bound, inclusive (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end-date",
        type=_date_arg,
        help="clusters.last_updated_at upper bound, exclusive (YYYY-MM-DD)",
    )
    parser.add_argument("--batch", type=_positive_int, default=200, help="rows per UPDATE")
    parser.add_argument("--threshold", type=float, default=4.75, help="featured threshold")
    parser.add_argument("--dry-run", action="store_true", help="print targets without writing")
    parser.add_argument("--yes", action="store_true", help="confirm production writes")
    parser.add_argument(
        "--resync-decisions",
        action="store_true",
        help="after writes, directly re-sync decisions for clusters linked to selected items",
    )
    args = parser.parse_args(argv)
    has_date_bound = args.start_date is not None or args.end_date is not None
    if args.days is not None and has_date_bound:
        parser.error("--days cannot be combined with --start-date/--end-date")
    if has_date_bound:
        if args.start_date is None or args.end_date is None:
            parser.error("--start-date and --end-date must be provided together")
        if args.start_date >= args.end_date:
            parser.error("--start-date must be earlier than --end-date")
    else:
        args.days = 1
    return args


def run(args: argparse.Namespace) -> int:
    if not args.dry_run and not args.yes:
        print("[v26-backfill-fast] refusing production writes without --yes", flush=True)
        return 2

    records = load_records(args.input)
    schema = remote_db.remote_schema()
    with connect_database() as conn:
        target_item_ids = fetch_target_item_ids(
            conn,
            days=args.days,
            start_date=args.start_date,
            end_date=args.end_date,
            schema=schema,
        )
        selected = intersect_records(records, target_item_ids)
        print(
            f"[v26-backfill-fast] cluster_items={len(target_item_ids)} "
            f"scored_rows={len(records)} target_rows={len(selected)}",
            flush=True,
        )

        if args.dry_run:
            for record in selected[:5]:
                print(json.dumps(record, ensure_ascii=False), flush=True)
            print("[v26-backfill-fast] dry-run: no database writes", flush=True)
            return 0

        written = 0
        for batch_number, batch in enumerate(iter_batches(selected, args.batch), start=1):
            affected = write_batch(
                conn,
                batch,
                threshold=args.threshold,
                schema=schema,
            )
            conn.commit()
            written += affected
            print(
                f"[v26-backfill-fast] batch={batch_number} "
                f"written={written}/{len(selected)}",
                flush=True,
            )

        if args.resync_decisions:
            affected_clusters = resync_affected_cluster_decisions(
                conn,
                item_ids={str(record["item_id"]) for record in selected},
                schema=schema,
            )
            conn.commit()
            print(
                f"[v26-backfill-fast] decisions_resync_target_clusters={affected_clusters}",
                flush=True,
            )

    print(f"[v26-backfill-fast] total_written={written}", flush=True)
    if not args.resync_decisions:
        print("[v26-backfill-fast] separately trigger a decisions re-sync", flush=True)
    return 0


def main() -> int:
    return run(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
