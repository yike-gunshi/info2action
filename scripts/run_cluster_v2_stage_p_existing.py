#!/usr/bin/env python3
"""Run Stage P on the existing clusters_v2 rows without rebuilding Stage Z.

This is the safe runner for the v5b handoff:

  - input DB defaults to /tmp/info2action-cluster-v2-eval/feed.db
  - it never calls reset_clusters_v2 / run_stage_z
  - it can reset only the selected Stage P results before rerun
  - it writes a DB backup before mutating anything

Examples:

  python scripts/run_cluster_v2_stage_p_existing.py --plan-only --mode all
  python scripts/run_cluster_v2_stage_p_existing.py --mode failed --reset-selected
  python scripts/run_cluster_v2_stage_p_existing.py --mode all --reset-selected --concurrency 8
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from clustering import stage_p  # noqa: E402

EVAL_DIR = Path("/tmp/info2action-cluster-v2-eval")
EVAL_DB = EVAL_DIR / "feed.db"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _load_config() -> dict[str, Any]:
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = value.strip().strip('"').strip("'")
    cfg_path = REPO_ROOT / "config" / "config.json"
    try:
        return json.loads(cfg_path.read_text())
    except Exception:
        return {}


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def _select_cluster_ids(conn: sqlite3.Connection, *, mode: str,
                        limit: int | None) -> list[int]:
    where = {
        "dirty": "stage_p_state='dirty'",
        "failed": "stage_p_state='failed'",
        "dirty-failed": "stage_p_state IN ('dirty','failed')",
        "all": "1=1",
    }[mode]
    sql = (
        "SELECT id FROM clusters_v2 "
        f"WHERE {where} "
        "ORDER BY member_count DESC, id ASC"
    )
    params: tuple[Any, ...] = ()
    if limit:
        sql += " LIMIT ?"
        params = (int(limit),)
    return [int(r["id"]) for r in conn.execute(sql, params).fetchall()]


def _backup_db(db_path: Path) -> Path:
    backup = db_path.with_name(f"{db_path.name}.bak.stage-p-existing-{_utc_stamp()}")
    shutil.copy2(db_path, backup)
    for ext in ("-wal", "-shm"):
        side = db_path.with_name(db_path.name + ext)
        if side.exists():
            shutil.copy2(side, backup.with_name(backup.name + ext))
    return backup


def _reset_selected(conn: sqlite3.Connection, cluster_ids: list[int]) -> None:
    if not cluster_ids:
        return
    placeholders = ",".join("?" for _ in cluster_ids)
    conn.execute(
        f"DELETE FROM cluster_p_log WHERE cluster_id IN ({placeholders})",
        cluster_ids,
    )
    conn.execute(
        f"""UPDATE cluster_items_v2
               SET removed_at = NULL,
                   removed_reason = NULL
             WHERE cluster_id IN ({placeholders})""",
        cluster_ids,
    )
    conn.execute(
        f"""UPDATE clusters_v2
               SET stage_p_state = 'dirty',
                   stage_p_run_at = NULL,
                   stage_p_failed_reason = NULL,
                   event_summary = NULL,
                   event_certainty = NULL
             WHERE id IN ({placeholders})""",
        cluster_ids,
    )
    conn.commit()


def _state_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT stage_p_state, COUNT(*) AS n FROM clusters_v2 GROUP BY stage_p_state"
    ).fetchall()
    return {str(r["stage_p_state"]): int(r["n"]) for r in rows}


def _write_report(out_dir: Path, *, args: argparse.Namespace,
                  before: dict[str, int], after: dict[str, int],
                  results: list[dict[str, Any]], elapsed: float,
                  backup: Path) -> Path:
    report = out_dir / f"stage-p-existing-{_utc_stamp()}.md"
    stats: dict[str, int] = {}
    for r in results:
        stats[str(r.get("status", "?"))] = stats.get(str(r.get("status", "?")), 0) + 1

    lines = [
        f"# Existing clusters_v2 Stage P run — {_utc_stamp()}",
        "",
        f"- db: `{args.db}`",
        f"- mode: `{args.mode}`",
        f"- reset_selected: `{bool(args.reset_selected)}`",
        f"- concurrency: `{args.concurrency}`",
        f"- selected: `{len(results)}`",
        f"- elapsed: `{elapsed:.1f}s`",
        f"- backup: `{backup}`",
        "",
        "## State Counts",
        "",
        f"- before: `{json.dumps(before, ensure_ascii=False, sort_keys=True)}`",
        f"- after: `{json.dumps(after, ensure_ascii=False, sort_keys=True)}`",
        "",
        "## Result Stats",
        "",
        f"`{json.dumps(stats, ensure_ascii=False, sort_keys=True)}`",
        "",
        "## Results",
        "",
        "| cluster_id | status | kept | removed | certainty | took(s) | reason |",
        "|---|---|---:|---:|---|---:|---|",
    ]
    for r in sorted(results, key=lambda x: int(x.get("cluster_id") or 0)):
        reason = str(r.get("reason") or "").replace("\n", " ")[:140]
        lines.append(
            f"| {r.get('cluster_id')} | {r.get('status')} | "
            f"{r.get('kept', '')} | {r.get('removed', '')} | "
            f"{r.get('certainty', '')} | {r.get('took_seconds', '')} | {reason} |"
        )
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=EVAL_DB)
    ap.add_argument("--mode", choices=["dirty", "failed", "dirty-failed", "all"],
                    default="dirty")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--reset-selected", action="store_true")
    ap.add_argument("--plan-only", action="store_true")
    args = ap.parse_args()

    if not args.db.exists():
        raise SystemExit(f"DB 不存在：{args.db}")

    cfg = _load_config()
    conn = _connect(args.db)
    before = _state_counts(conn)
    cluster_ids = _select_cluster_ids(
        conn, mode=args.mode, limit=args.limit or None
    )
    print(f"[plan] db={args.db}")
    print(f"[plan] before={json.dumps(before, ensure_ascii=False, sort_keys=True)}")
    print(f"[plan] selected={len(cluster_ids)} mode={args.mode} limit={args.limit or 'none'}")
    print(f"[plan] first_ids={cluster_ids[:20]}")
    if args.plan_only:
        conn.close()
        return 0

    backup = _backup_db(args.db)
    print(f"[run] backup={backup}", flush=True)
    if args.reset_selected:
        print(f"[run] reset selected Stage P rows: {len(cluster_ids)}", flush=True)
        _reset_selected(conn, cluster_ids)
    conn.close()

    api_key = os.environ.get("MINIMAX_API_KEY") or cfg.get("ai_summary", {}).get("api_key")
    if not api_key:
        raise SystemExit("MINIMAX_API_KEY missing in env or config.ai_summary.api_key")

    started = time.time()
    results: list[dict[str, Any]] = []

    def worker(cluster_id: int) -> dict[str, Any]:
        worker_conn = _connect(args.db)
        try:
            return stage_p.run_stage_p_for_cluster(
                worker_conn, cluster_id, api_key=api_key, config=cfg
            )
        finally:
            worker_conn.close()

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {ex.submit(worker, cid): cid for cid in cluster_ids}
        for idx, fut in enumerate(as_completed(futures), 1):
            cid = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:  # noqa: BLE001
                result = {"cluster_id": cid, "status": "failed", "reason": str(exc)}
            results.append(result)
            print(
                f"[{idx}/{len(cluster_ids)}] cluster={cid} status={result.get('status')} "
                f"kept={result.get('kept', '-')} removed={result.get('removed', '-')} "
                f"cert={result.get('certainty', '-')} took={result.get('took_seconds', '-')}",
                flush=True,
            )

    elapsed = time.time() - started
    verify = _connect(args.db)
    after = _state_counts(verify)
    verify.close()
    report = _write_report(
        EVAL_DIR, args=args, before=before, after=after,
        results=results, elapsed=elapsed, backup=backup,
    )
    print(f"[done] elapsed={elapsed:.1f}s")
    print(f"[done] after={json.dumps(after, ensure_ascii=False, sort_keys=True)}")
    print(f"[done] report={report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
