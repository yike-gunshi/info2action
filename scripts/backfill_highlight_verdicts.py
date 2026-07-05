#!/usr/bin/env python3
"""Backfill item-level Highlights verdicts in Supabase."""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import enrich_items  # noqa: E402
import highlight_verdict  # noqa: E402
import remote_db  # noqa: E402


def _load_env_file(path: str | None) -> None:
    if not path:
        return
    env_path = Path(path).expanduser()
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def _process_item(item: dict, *, api_key: str, api_base: str, model: str, dry_run: bool, gate) -> tuple[str, str]:
    try:
        raw = enrich_items.call_minimax(
            api_key,
            api_base,
            model,
            highlight_verdict.load_system_prompt(),
            highlight_verdict.build_item_content(item),
            max_tokens=2048,
            rate_gate=gate,
            temperature=0.0,
        )
        result = highlight_verdict.normalize_verdict_result(raw)
        result["highlight_model"] = model
        if not dry_run:
            remote_db.write_highlight_verdict_remote(None, item["id"], result)
        return item["id"], result["cluster_verdict"]
    except Exception as exc:
        if not dry_run:
            remote_db.record_highlight_verdict_failure_remote(
                None,
                item["id"],
                str(exc)[:500],
                retry_after=30 * 60,
            )
        return item["id"], "error"


def _run_bounded(items: list[dict], workers: int, fn) -> list[tuple[str, str]]:
    total = len(items)
    if workers <= 1:
        results = []
        for idx, item in enumerate(items, 1):
            result = fn(item)
            results.append(result)
            if idx == total or idx % 25 == 0:
                print(f"Progress {idx}/{total}: {result[1]}", flush=True)
        return results
    results: list[tuple[str, str]] = []
    iterator = iter(items)
    in_flight = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for _ in range(workers):
            try:
                item = next(iterator)
            except StopIteration:
                break
            in_flight[executor.submit(fn, item)] = item
        while in_flight:
            done, _ = wait(tuple(in_flight.keys()), return_when=FIRST_COMPLETED)
            for future in done:
                in_flight.pop(future, None)
                result = future.result()
                results.append(result)
                if len(results) == total or len(results) % 25 == 0:
                    print(f"Progress {len(results)}/{total}: {result[1]}", flush=True)
                try:
                    item = next(iterator)
                except StopIteration:
                    continue
                in_flight[executor.submit(fn, item)] = item
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill Supabase item highlight verdicts")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--ids", default="")
    parser.add_argument("--window-days", type=int, default=None)
    parser.add_argument("--window-start", default=None)
    parser.add_argument("--window-end", default=None)
    parser.add_argument("--window-require-published-at", action="store_true")
    parser.add_argument("--rescore-version-mismatch", action="store_true")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    _load_env_file(args.env_file)
    config = enrich_items.load_config()
    api_key, api_base, model = enrich_items.resolve_minimax_runtime_config(config.get("ai_summary", {}))
    if not api_key:
        print("ERROR: No MiniMax API key configured", flush=True)
        return 1
    ids = [item_id.strip() for item_id in args.ids.split(",") if item_id.strip()] or None
    window_start = args.window_start
    if args.window_days and not window_start:
        window_start = (datetime.now(timezone.utc) - timedelta(days=max(1, args.window_days))).isoformat()
    items = remote_db.query_pending_highlight_verdict_items_remote(
        limit=args.limit,
        ids=ids,
        window_start=window_start,
        window_end=args.window_end,
        require_published_at=args.window_require_published_at,
        rescore_prompt_version=(
            highlight_verdict.PROMPT_VERSION
            if args.rescore_version_mismatch
            else None
        ),
    )
    print(f"Found {len(items)} items needing highlight verdict", flush=True)
    if not items:
        return 0
    gate = enrich_items.MiniMaxRateLimitGate(min_interval=0.8 if args.workers > 1 else 0.0)
    results = _run_bounded(
        items,
        max(1, int(args.workers or 1)),
        lambda item: _process_item(
            item,
            api_key=api_key,
            api_base=api_base,
            model=model,
            dry_run=args.dry_run,
            gate=gate,
        ),
    )
    counts: dict[str, int] = {}
    for _, verdict in results:
        counts[verdict] = counts.get(verdict, 0) + 1
    print(f"Done: {counts}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
