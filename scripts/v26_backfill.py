#!/usr/bin/env python3
"""Apply a reviewed v26 offline score manifest to production items."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

import remote_db  # noqa: E402


DEFAULT_INPUT = (
    ROOT
    / ".features"
    / "highlights-refactor-v26"
    / "offline-rescore"
    / "scores.jsonl"
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


def apply_record(
    conn: Any,
    record: dict[str, Any],
    *,
    threshold: float,
) -> None:
    """Adapt one JSONL row to Block A's dedicated nested-v26 writer."""
    result = {
        "dims": record.get("dims") or {},
        "marketing": record.get("marketing"),
        "score10": record.get("score10"),
        "content_type": record.get("content_type"),
        "reject": bool(record.get("reject")),
        "veto": record.get("veto"),
        "uncertainty": record.get("uncertainty"),
        "value_path": record.get("value_path"),
        "reason": record.get("reason"),
        "confidence": record.get("confidence"),
        "is_flag_bearer": bool(record.get("flag_bearer")),
        "runs": record.get("runs"),
        "pass2_error": record.get("pass2_error"),
    }
    remote_db.write_highlight_score_v26_remote(
        conn,
        str(record["item_id"]),
        result,
        threshold=threshold,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill reviewed v26 scores into production; requires --yes to write"
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="scores.jsonl path")
    parser.add_argument("--threshold", type=float, default=4.75, help="verdict threshold (default: 4.75, gate-calibrated)")
    parser.add_argument("--dry-run", action="store_true", help="print the first 10 changes without writing")
    parser.add_argument("--yes", action="store_true", help="confirm production writes")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    records = load_records(args.input)
    print(f"[v26-backfill] rows_to_affect={len(records)}", flush=True)

    if args.dry_run:
        for record in records[:10]:
            print(json.dumps(record, ensure_ascii=False), flush=True)
        print("[v26-backfill] dry-run: no database writes", flush=True)
        return 0

    if not args.yes:
        print("[v26-backfill] refusing production writes without --yes", flush=True)
        return 2

    with remote_db.connect() as conn:
        for index, record in enumerate(records, start=1):
            apply_record(conn, record, threshold=args.threshold)
            print(
                f"[v26-backfill] {index}/{len(records)} item={record['item_id']}",
                flush=True,
            )

    print("[v26-backfill] done; separately trigger a decisions re-sync", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
