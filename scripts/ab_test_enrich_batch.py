#!/usr/bin/env python3
"""Manual A/B runner for unified enrichment batch mode.

This script intentionally writes only a Markdown report. It does not change DB
rows because it runs enrich_items.py with --dry-run.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(BASE_DIR, "src")
sys.path.insert(0, SRC_DIR)

import db
import enrich_items


REPORT_PATH = os.path.join(BASE_DIR, "docs", "bugfix", "minimax-governance-batch-ab.md")


def select_pending_ids(limit: int) -> list[str]:
    conn = db.get_conn()
    try:
        rows = enrich_items.query_pending_items(conn, limit=limit)
        return [row["id"] for row in rows]
    finally:
        conn.close()


def run_enrich(ids: list[str], batch_size: int) -> dict:
    started = time.time()
    cmd = [
        sys.executable,
        os.path.join(SRC_DIR, "enrich_items.py"),
        "--ids",
        ",".join(ids),
        "--batch-size",
        str(batch_size),
        "--dry-run",
    ]
    result = subprocess.run(cmd, cwd=BASE_DIR, capture_output=True, text=True, timeout=1800)
    elapsed = time.time() - started
    output = (result.stdout or "") + (result.stderr or "")
    return {
        "batch_size": batch_size,
        "returncode": result.returncode,
        "elapsed": elapsed,
        "request_estimate": len(ids) if batch_size == 1 else (len(ids) + batch_size - 1) // batch_size,
        "parse_errors": output.count("[ERR]") + output.count("[BATCH FALLBACK]"),
        "output": output[-4000:],
    }


def write_report(ids: list[str], single: dict, batch: dict) -> None:
    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("# MiniMax Unified Enrichment Batch A/B\n\n")
        f.write(f"- generated_at: {datetime.now().isoformat()}\n")
        f.write(f"- item_count: {len(ids)}\n")
        f.write(f"- ids: {', '.join(ids)}\n\n")
        f.write("| mode | returncode | request_estimate | elapsed_s | parse_errors |\n")
        f.write("|---|---:|---:|---:|---:|\n")
        for row in (single, batch):
            f.write(
                f"| batch_size={row['batch_size']} | {row['returncode']} | "
                f"{row['request_estimate']} | {row['elapsed']:.1f} | {row['parse_errors']} |\n"
            )
        f.write("\n## Single Output Tail\n\n```text\n")
        f.write(single["output"])
        f.write("\n```\n\n## Batch Output Tail\n\n```text\n")
        f.write(batch["output"])
        f.write("\n```\n")


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    ids = select_pending_ids(limit)
    if not ids:
        print("No pending items for A/B")
        return 0
    single = run_enrich(ids, batch_size=1)
    batch = run_enrich(ids, batch_size=5)
    write_report(ids, single, batch)
    print(f"Report written: {REPORT_PATH}")
    return 0 if single["returncode"] == 0 and batch["returncode"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
