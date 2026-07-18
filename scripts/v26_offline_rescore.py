#!/usr/bin/env python3
"""Rescore selected production items with v26 and write local-only artifacts."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
import json
import os
from pathlib import Path
import sys
import threading
from typing import Any, Callable, Sequence


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

import enrich_items  # noqa: E402
from env_utils import load_project_env  # noqa: E402
from highlight_score_v26 import (  # noqa: E402
    compute_score10,
    is_flag_bearer,
    normalize_score_result,
)


PROMPT_FILE = ROOT / "prompts" / "15_item_score_v26.md"
DEFAULT_OUT_DIR = ROOT / ".features" / "highlights-refactor-v26" / "offline-rescore"
SCORES_FILE_NAME = "scores.jsonl"
REPORT_FILE_NAME = "diff-报告.md"
READ_OPTIONS = (
    "-c search_path=remote_poc,public "
    "-c statement_timeout=180000 "
    "-c default_transaction_read_only=on"
)


def load_env_values(base_dir: Path) -> dict[str, str]:
    """Load this worktree's .env, falling back to the primary worktree."""
    local_values = load_project_env(base_dir)
    git_marker = base_dir / ".git"
    if not git_marker.is_file():
        return local_values
    try:
        marker = git_marker.read_text(encoding="utf-8").strip()
        git_dir_text = marker.split("gitdir:", 1)[1].strip()
        git_dir = Path(git_dir_text)
        if not git_dir.is_absolute():
            git_dir = (base_dir / git_dir).resolve()
        primary_root = next(parent.parent for parent in git_dir.parents if parent.name == ".git")
    except (IndexError, OSError, StopIteration):
        return local_values
    values = load_project_env(primary_root)
    values.update(local_values)
    return values


def load_runtime() -> tuple[str, str, str]:
    for key, value in load_env_values(ROOT).items():
        os.environ.setdefault(key, value)
    config = enrich_items.load_config()
    api_key, api_base, model = enrich_items.resolve_minimax_runtime_config(
        config.get("ai_summary", {})
    )
    if not api_key:
        raise RuntimeError("MiniMax API key missing in .env/config")
    return api_key, api_base, model


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


def build_fetch_query(
    *,
    days: int | None,
    start_date: date | None,
    end_date: date | None,
    skip_scored: bool,
    limit: int,
) -> tuple[str, tuple[Any, ...]]:
    if days is not None:
        if start_date is not None or end_date is not None:
            raise ValueError("days and explicit date band are mutually exclusive")
        date_filter = f"fetched_at > now() - interval '{days} days'"
        params: tuple[Any, ...] = (limit,)
    else:
        if start_date is None or end_date is None:
            raise ValueError("explicit date band requires start_date and end_date")
        date_filter = "fetched_at >= %s::date AND fetched_at < %s::date"
        params = (start_date, end_date, limit)
    skip_filter = "\n                   AND NOT (highlight_scores ? 'v26')" if skip_scored else ""
    query = f"""SELECT id, title, author_name, platform, content, ai_summary, detail_json,
                       highlight_include_in_highlights, highlight_verdict
                  FROM items
                 WHERE highlight_verdict IS NOT NULL
                   AND {date_filter}{skip_filter}
                 ORDER BY fetched_at DESC
                 LIMIT %s"""
    return query, params


def fetch_items(
    *,
    days: int | None,
    limit: int,
    start_date: date | None = None,
    end_date: date | None = None,
    skip_scored: bool = False,
) -> list[dict[str, Any]]:
    """Read selected scored items using a read-only database session."""
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError("psycopg is required; install project requirements") from exc

    query, params = build_fetch_query(
        days=days,
        start_date=start_date,
        end_date=end_date,
        skip_scored=skip_scored,
        limit=limit,
    )
    with psycopg.connect(
        database_url(),
        row_factory=dict_row,
        options=READ_OPTIONS,
        connect_timeout=15,
    ) as conn:
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def load_checkpoint_item_ids(scores_file: Path) -> set[str]:
    if not scores_file.exists():
        return set()
    completed: set[str] = set()
    for line in scores_file.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        item_id = record.get("item_id") if isinstance(record, dict) else None
        if item_id:
            completed.add(str(item_id))
    return completed


def filter_pending_items(
    items: list[dict[str, Any]],
    completed_item_ids: set[str],
) -> list[dict[str, Any]]:
    return [item for item in items if str(item.get("id")) not in completed_item_ids]


def load_score_records(scores_file: Path) -> list[dict[str, Any]]:
    if not scores_file.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in scores_file.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(record, dict) and record.get("item_id"):
            records.append(record)
    return records


def _score_with_retries(
    item: dict[str, Any],
    scorer: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    last_error = "unknown scoring error"
    for _attempt in range(3):
        try:
            result = scorer(item)
            if not isinstance(result, dict):
                raise ValueError("scorer_result_not_object")
            if result.get("error"):
                raise ValueError(str(result["error"]))
            if result.get("score10") is None and not result.get("reject"):
                raise ValueError("score10_missing")
            return result
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)[:500]
    raise RuntimeError(last_error)


def _error_record(
    item: dict[str, Any],
    error: str,
    runs: list[float | None],
) -> dict[str, Any]:
    return {
        "item_id": str(item.get("id") or ""),
        "score10": None,
        "runs": runs,
        "dims": None,
        "marketing": None,
        "veto": None,
        "uncertainty": None,
        "value_path": None,
        "content_type": None,
        "reject": None,
        "reason": None,
        "confidence": None,
        "flag_bearer": False,
        "old_include": bool(item.get("highlight_include_in_highlights")),
        "error": error[:500],
    }


def rescore_item(
    item: dict[str, Any],
    *,
    threshold: float,
    scorer: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Score one item, rerunning the inclusive T +/- 1 edge band."""
    runs: list[float | None] = []
    try:
        first = _score_with_retries(item, scorer)
        first_score = first.get("score10")
        runs.append(float(first_score) if first_score is not None else None)
        if first_score is not None and abs(float(first_score) - threshold) <= 1.0:
            second = _score_with_retries(item, scorer)
            if second.get("score10") is None:
                raise ValueError("edge rerun returned no score")
            runs.append(float(second["score10"]))
    except Exception as exc:  # noqa: BLE001
        return _error_record(item, str(exc), runs)

    numeric_runs = [score for score in runs if score is not None]
    score10 = round(sum(numeric_runs) / len(numeric_runs), 2) if numeric_runs else None
    return {
        "item_id": str(item["id"]),
        "score10": score10,
        "runs": runs,
        "dims": first.get("dims"),
        "marketing": first.get("marketing"),
        "veto": first.get("veto"),
        "uncertainty": first.get("uncertainty"),
        "value_path": first.get("value_path"),
        "content_type": first.get("content_type"),
        "reject": first.get("reject"),
        "reason": first.get("reason"),
        "confidence": first.get("confidence"),
        "flag_bearer": is_flag_bearer(first, score10, threshold),
        "old_include": bool(item.get("highlight_include_in_highlights")),
        "error": None,
    }


def classify_diff(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    classified = {
        "newly_included": [],
        "removed": [],
        "unchanged": [],
        "errors": [],
    }
    for record in records:
        if record.get("error"):
            classified["errors"].append(record)
        elif not bool(record.get("old_include")) and bool(record.get("flag_bearer")):
            classified["newly_included"].append(record)
        elif bool(record.get("old_include")) and not bool(record.get("flag_bearer")):
            classified["removed"].append(record)
        else:
            classified["unchanged"].append(record)
    return classified


def _markdown_text(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def _reason_summary(record: dict[str, Any]) -> str:
    if record.get("reason"):
        return _markdown_text(record["reason"])
    return _markdown_text(
        f"type={record.get('content_type')}; value={record.get('value_path')}; "
        f"veto={record.get('veto')}; uncertainty={record.get('uncertainty')}"
    )


def _diff_table(rows: list[dict[str, Any]], item_by_id: dict[str, dict[str, Any]]) -> list[str]:
    lines = ["| item_id | 标题 | 新分数 | 理由摘要 |", "|---|---|---:|---|"]
    for row in rows:
        item = item_by_id.get(str(row.get("item_id")), {})
        lines.append(
            f"| {_markdown_text(row.get('item_id'))} | {_markdown_text(item.get('title'))} "
            f"| {_markdown_text(row.get('score10'))} | {_reason_summary(row)} |"
        )
    if not rows:
        lines.append("| — | — | — | 无 |")
    return lines


def render_diff_report(
    records: list[dict[str, Any]],
    items: list[dict[str, Any]],
) -> str:
    classified = classify_diff(records)
    item_by_id = {str(item["id"]): item for item in items}
    return "\n".join([
        "# v26 离线重打新旧准入 diff",
        "",
        "## 汇总",
        "",
        f"- 已比较：{len(records)}",
        f"- 新准入：{len(classified['newly_included'])}",
        f"- 被踢出：{len(classified['removed'])}",
        f"- 不变：{len(classified['unchanged'])}",
        f"- 失败：{len(classified['errors'])}",
        "",
        "## 新准入",
        "",
        *_diff_table(classified["newly_included"], item_by_id),
        "",
        "## 被踢出",
        "",
        *_diff_table(classified["removed"], item_by_id),
        "",
    ])


def build_scorer() -> Callable[[dict[str, Any]], dict[str, Any]]:
    api_key, api_base, model = load_runtime()
    system_prompt = PROMPT_FILE.read_text(encoding="utf-8")
    rate_gate = enrich_items.MiniMaxRateLimitGate()

    def score(item: dict[str, Any]) -> dict[str, Any]:
        raw = enrich_items.call_minimax(
            api_key,
            api_base,
            model,
            system_prompt,
            enrich_items.build_item_content_v26(item),
            max_tokens=2048,
            rate_gate=rate_gate,
            temperature=0.0,
        )
        result = normalize_score_result(raw)
        if result.get("error"):
            raise ValueError(str(result["error"]))
        return {**result, "score10": compute_score10(result)}

    return score


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
        description="Read recent production items, rescore with v26, and write local JSONL/diff only"
    )
    parser.add_argument(
        "--days",
        type=_positive_int,
        default=None,
        help="item fetched_at lookback days (default: 3; conflicts with explicit date band)",
    )
    parser.add_argument(
        "--start-date",
        type=_date_arg,
        help="item fetched_at lower bound, inclusive (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end-date",
        type=_date_arg,
        help="item fetched_at upper bound, exclusive (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--skip-scored",
        action="store_true",
        help="exclude items whose highlight_scores already contains v26",
    )
    parser.add_argument("--limit", type=_positive_int, default=5000, help="maximum rows (default: 5000)")
    parser.add_argument("--workers", type=_positive_int, default=4, help="concurrent scorers (default: 4)")
    parser.add_argument("--threshold", type=float, default=4.75, help="flag threshold (default: 4.75, gate-calibrated)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR, help="local output directory")
    parser.add_argument("--dry-run", action="store_true", help="score only the first 5 rows")
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
        args.days = 3
    return args


def main() -> int:
    args = parse_args()
    query_limit = min(args.limit, 5) if args.dry_run else args.limit
    items = fetch_items(
        days=args.days,
        start_date=args.start_date,
        end_date=args.end_date,
        skip_scored=args.skip_scored,
        limit=query_limit,
    )
    args.out.mkdir(parents=True, exist_ok=True)
    scores_file = args.out / SCORES_FILE_NAME
    report_file = args.out / REPORT_FILE_NAME
    completed = load_checkpoint_item_ids(scores_file)
    pending = filter_pending_items(items, completed)
    scorer = build_scorer()
    semaphore = threading.Semaphore(args.workers)
    write_lock = threading.Lock()

    def run_one(item: dict[str, Any]) -> dict[str, Any]:
        with semaphore:
            return rescore_item(item, threshold=args.threshold, scorer=scorer)

    print(
        f"[v26-rescore] fetched={len(items)} checkpoint={len(completed)} "
        f"pending={len(pending)} workers={args.workers}",
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(run_one, item): item for item in pending}
        for index, future in enumerate(as_completed(futures), start=1):
            record = future.result()
            with write_lock:
                with scores_file.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(
                f"[v26-rescore] {index}/{len(pending)} item={record['item_id']} "
                f"score10={record['score10']} error={record['error']}",
                flush=True,
            )

    records_by_id = {
        str(record["item_id"]): record for record in load_score_records(scores_file)
    }
    current_records = [
        records_by_id[str(item["id"])]
        for item in items
        if str(item["id"]) in records_by_id
    ]
    report_file.write_text(render_diff_report(current_records, items), encoding="utf-8")
    print(f"[v26-rescore] wrote {scores_file}", flush=True)
    print(f"[v26-rescore] wrote {report_file}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
