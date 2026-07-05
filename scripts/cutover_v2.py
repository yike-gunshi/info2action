#!/usr/bin/env python3
"""
v15.1 Event Aggregation V2 — Cutover Script

清空 cluster 全量重建：
  1. 备份 data/feed.db → data/backups/feed-pre-v15.1-{timestamp}.db (sqlite3 .backup)
  2. 用户最终确认 (输入 YES 大写)
  3. 提示 ECS 暂停抓取 cron
  4. 单事务执行：
        DELETE FROM cluster_items;
        DELETE FROM cluster_status;
        DELETE FROM clusters;
        UPDATE items SET cluster_id = NULL,
                         embedding = NULL,
                         embedding_provider = NULL,
                         cluster_locked = 0;
        DELETE FROM actions WHERE source_type = 'cluster';
  5. 写日志 logs/cluster_events.jsonl event=cluster_v15_1_executed
  6. 打印 post-cutover 校验 + 下一步指引

权威源：
  - PRD §5.17 「Cutover 数据动作」
  - PRD-CHANGELOG v15.1 设计方案 C6 + roadmap 步骤 12
  - feature-spec.md R9.1/R9.2/R9.3 + 关键约束 #2

铁律：
  - 备份必须先于 DELETE（hard gate；备份失败不允许继续）
  - 不允许静默 swallow exception
  - 不提供 --no-backup bypass
  - --dry-run 是默认；--execute 必须显式给出
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "feed.db"
BACKUP_DIR = PROJECT_ROOT / "data" / "backups"
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "cluster_events.jsonl"


# ---------- output helpers ----------

def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _err(msg: str) -> None:
    _stderr(f"\033[31m[ERROR]\033[0m {msg}")


def _warn(msg: str) -> None:
    print(f"\033[33m[WARN]\033[0m {msg}", flush=True)


def _ok(msg: str) -> None:
    print(f"\033[32m[OK]\033[0m {msg}", flush=True)


def _info(msg: str) -> None:
    print(f"[INFO] {msg}", flush=True)


def _sql(line: str) -> None:
    print(f"  SQL> {line}", flush=True)


# ---------- log writer ----------

def _log_event(event: str, **fields) -> None:
    """Mirror src/clustering/pipeline.py::_log_event channel."""
    try:
        LOG_DIR.mkdir(exist_ok=True)
        line = json.dumps(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "event": event,
                **fields,
            },
            ensure_ascii=False,
        )
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception as e:  # do not raise from logger
        _warn(f"log write failed (non-fatal): {e}")


# ---------- preflight ----------

REQUIRED_TABLES = (
    "clusters",
    "cluster_items",
    "cluster_status",
    "items",
    "actions",
    "cluster_judge_log",
)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def collect_pre_cutover_stats(db_path: Path) -> dict:
    """Read row counts before cutover. Returns dict with all keys present.

    Missing tables map to None so caller can produce a precise error message.
    """
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        stats = {
            "clusters_count": _scalar_or_none(conn, "SELECT COUNT(*) FROM clusters"),
            "cluster_items_count": _scalar_or_none(
                conn, "SELECT COUNT(*) FROM cluster_items"
            ),
            "cluster_status_count": _scalar_or_none(
                conn, "SELECT COUNT(*) FROM cluster_status"
            ),
            "items_with_cluster_id": _scalar_or_none(
                conn, "SELECT COUNT(*) FROM items WHERE cluster_id IS NOT NULL"
            ),
            "items_with_embedding": _scalar_or_none(
                conn, "SELECT COUNT(*) FROM items WHERE embedding IS NOT NULL"
            ),
            "actions_cluster_source": _scalar_or_none(
                conn, "SELECT COUNT(*) FROM actions WHERE source_type = 'cluster'"
            ),
            "cluster_judge_log_rows": _scalar_or_none(
                conn, "SELECT COUNT(*) FROM cluster_judge_log"
            ),
        }
    return stats


def _scalar(conn: sqlite3.Connection, sql: str) -> int:
    cur = conn.execute(sql)
    row = cur.fetchone()
    return int(row[0]) if row else 0


def _scalar_or_none(conn: sqlite3.Connection, sql: str):
    """Return None if table missing (preflight aid), else int."""
    try:
        return _scalar(conn, sql)
    except sqlite3.OperationalError:
        return None


def preflight(db_path: Path) -> dict:
    """
    Verify DB is in a valid state for cutover.
    Raises RuntimeError if blocked (caller decides exit code).
    Returns pre-cutover stats dict (counts; never None for table-existence keys).
    """
    if not db_path.exists():
        raise RuntimeError(f"DB file does not exist: {db_path}")
    if not os.access(db_path, os.R_OK):
        raise RuntimeError(f"DB file not readable: {db_path}")

    # Verify all required tables exist before counting (avoids confusing
    # "no such table" tracebacks; gives one consolidated message).
    with sqlite3.connect(str(db_path)) as conn:
        missing = [t for t in REQUIRED_TABLES if not _table_exists(conn, t)]
    if missing:
        raise RuntimeError(
            "DB is missing required tables: "
            + ", ".join(missing)
            + ". This usually means src.db.init_db() has not been called or "
            "the Eng-B schema migration (cluster_judge_log) has not been applied. "
            "Run the app once (which triggers init_db) and retry."
        )

    stats = collect_pre_cutover_stats(db_path)

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    return stats


def print_pre_cutover_stats(stats: dict) -> None:
    _info("Pre-cutover statistics:")
    print(f"  clusters                    : {stats['clusters_count']}")
    print(f"  cluster_items               : {stats['cluster_items_count']}")
    print(f"  cluster_status              : {stats['cluster_status_count']}")
    print(f"  items.cluster_id NOT NULL   : {stats['items_with_cluster_id']}")
    print(f"  items.embedding NOT NULL    : {stats['items_with_embedding']}")
    print(f"  actions WHERE source=cluster: {stats['actions_cluster_source']}")
    print(f"  cluster_judge_log rows      : {stats['cluster_judge_log_rows']}")


# ---------- backup ----------

def make_backup_path(db_path: Path, now: datetime | None = None) -> Path:
    ts = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return BACKUP_DIR / f"feed-pre-v15.1-{ts}.db"


def perform_backup(db_path: Path, backup_path: Path) -> None:
    """
    Use `sqlite3 <db> ".backup <backup>"` (hot-safe even if DB in use).
    Raises RuntimeError on failure (hard gate).
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    if not shutil.which("sqlite3"):
        raise RuntimeError(
            "sqlite3 CLI not found on PATH; cannot run safe `.backup`. "
            "Install sqlite3 or run cutover from a host with it."
        )

    cmd = ["sqlite3", str(db_path), f".backup '{backup_path}'"]
    try:
        result = subprocess.run(
            cmd, check=False, capture_output=True, text=True, timeout=600
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"sqlite3 .backup timed out after 600s: {e}") from e

    if result.returncode != 0:
        raise RuntimeError(
            f"sqlite3 .backup failed (rc={result.returncode}): "
            f"stderr={result.stderr.strip()!r}"
        )

    if not backup_path.exists():
        raise RuntimeError(
            f"sqlite3 .backup returned 0 but {backup_path} does not exist"
        )

    src_size = db_path.stat().st_size
    bak_size = backup_path.stat().st_size
    if bak_size <= 0:
        raise RuntimeError(
            f"backup is empty: {backup_path} (size=0)"
        )
    # sanity: backup size should be within ±20% of source (sqlite .backup compacts a bit)
    if bak_size < src_size * 0.5:
        raise RuntimeError(
            f"backup size suspicious: src={src_size}, bak={bak_size} "
            f"(less than 50% of source). Aborting."
        )

    _ok(f"Backed up to {backup_path} ({bak_size:,} bytes)")


# ---------- transaction ----------

CUTOVER_SQL = [
    ("DELETE FROM cluster_items", "delete cluster_items"),
    ("DELETE FROM cluster_status", "delete cluster_status"),
    ("DELETE FROM clusters", "delete clusters"),
    (
        "UPDATE items SET cluster_id = NULL, "
        "embedding = NULL, "
        "embedding_provider = NULL, "
        "cluster_locked = 0",
        "reset items.cluster_id / embedding / embedding_provider / cluster_locked",
    ),
    ("DELETE FROM actions WHERE source_type = 'cluster'", "delete cluster-sourced actions"),
]


def execute_cutover_transaction(db_path: Path) -> None:
    """
    Run all 5 cutover statements in a single IMMEDIATE transaction.
    Any failure → rollback + raise.
    """
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        # disable autocommit
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            for sql, label in CUTOVER_SQL:
                _info(f"  executing: {label}")
                conn.execute(sql)
            conn.execute("COMMIT")
            _ok("transaction committed")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


# ---------- dry-run printer ----------

def print_dry_run_plan(db_path: Path, stats: dict) -> None:
    print()
    _info("=== DRY RUN ===  (no DB writes will happen)")
    print()

    backup_path = make_backup_path(db_path)
    _info("Step 1 — Backup (would run):")
    print(f"  sqlite3 {db_path} \".backup '{backup_path}'\"")
    print()

    _info("Step 2 — Cutover SQL (would run inside single BEGIN IMMEDIATE / COMMIT):")
    for sql, _label in CUTOVER_SQL:
        # split UPDATE for readability
        if sql.startswith("UPDATE"):
            _sql("UPDATE items SET cluster_id = NULL,")
            _sql("                 embedding = NULL,")
            _sql("                 embedding_provider = NULL,")
            _sql("                 cluster_locked = 0;")
        else:
            _sql(sql + ";")
    print()

    _info("Expected post-cutover state:")
    print(f"  clusters                    : 0  (was {stats['clusters_count']})")
    print(f"  cluster_items               : 0  (was {stats['cluster_items_count']})")
    print(f"  cluster_status              : 0  (was {stats['cluster_status_count']})")
    print(f"  items.cluster_id NOT NULL   : 0  (was {stats['items_with_cluster_id']})")
    print(f"  items.embedding NOT NULL    : 0  (was {stats['items_with_embedding']})")
    print(f"  actions WHERE source=cluster: 0  (was {stats['actions_cluster_source']})")
    print(
        f"  cluster_judge_log rows      : {stats['cluster_judge_log_rows']} "
        "(unchanged — kept for audit)"
    )
    print()
    _info("To execute for real:")
    print("  python3 scripts/cutover_v2.py --execute --yes")
    print()


# ---------- confirmation ----------

def confirm_yes(prompt: str, *, auto_yes: bool, stdin=sys.stdin) -> bool:
    if auto_yes:
        return True
    try:
        ans = input(prompt).strip()
    except EOFError:
        return False
    return ans == "YES"


def confirm_press_enter(prompt: str, *, auto_yes: bool, stdin=sys.stdin) -> bool:
    if auto_yes:
        return True
    try:
        input(prompt)
    except EOFError:
        return False
    return True


# ---------- main flows ----------

def run_dry_run(db_path: Path) -> int:
    try:
        stats = preflight(db_path)
    except RuntimeError as e:
        _err(str(e))
        return 1

    print_pre_cutover_stats(stats)

    if stats["clusters_count"] == 0 and stats["cluster_items_count"] == 0:
        _warn(
            "clusters and cluster_items are already empty — looks like cutover already ran. "
            "DRY RUN will still print the plan, but execute would be a no-op for those tables."
        )

    print_dry_run_plan(db_path, stats)
    return 0


def run_execute(db_path: Path, *, auto_yes: bool) -> int:
    # 1. preflight
    try:
        stats = preflight(db_path)
    except RuntimeError as e:
        _err(str(e))
        return 1

    print_pre_cutover_stats(stats)

    if stats["clusters_count"] == 0 and stats["cluster_items_count"] == 0:
        _warn(
            "clusters and cluster_items are already empty — cutover may already have run. "
            "Continuing anyway will only reset items.embedding + delete cluster actions."
        )
        if not confirm_yes(
            "  Type YES to continue anyway, anything else to abort: ",
            auto_yes=auto_yes,
        ):
            _err("aborted by user (already-cutover guard)")
            return 1

    # 2. backup BEFORE any prompt to ensure we always have rollback artifact
    backup_path = make_backup_path(db_path)
    _info(f"Step 1 — Backing up DB → {backup_path}")
    try:
        perform_backup(db_path, backup_path)
    except RuntimeError as e:
        _err(f"backup failed; refusing to continue: {e}")
        return 1

    # 3. final user confirmation
    print()
    _warn("THIS IS DESTRUCTIVE AND NOT RECOVERABLE WITHOUT THE BACKUP.")
    _warn(f"Backup: {backup_path}")
    _warn("Will execute 5 SQL statements in a single transaction (see --dry-run for details).")
    if not confirm_yes(
        "Type YES (uppercase) to proceed, anything else to abort: ",
        auto_yes=auto_yes,
    ):
        _err("aborted by user at final confirmation. DB is untouched (only backup was created).")
        return 1

    # 4. ECS cron pause reminder
    print()
    _warn(
        "Reminder: if the production ECS cron is currently running ingest, "
        "concurrent writes during cutover may corrupt state."
    )
    _warn(
        "Pause it now, e.g. on the ECS host:\n"
        "    crontab -l | sed 's|^\\([^#]\\)|# \\1|' | crontab -\n"
        "or stop the relevant systemd timers."
    )
    if not confirm_press_enter(
        "Press ENTER once you've paused ECS ingest (or are running locally only): ",
        auto_yes=auto_yes,
    ):
        _err("aborted at ECS pause checkpoint. DB is untouched.")
        return 1

    # 5. transactional execute
    print()
    _info("Step 2 — Executing cutover transaction")
    try:
        execute_cutover_transaction(db_path)
    except Exception as e:
        _err(f"cutover transaction failed: {e}")
        _err(f"DB rolled back; restore from backup if needed: {backup_path}")
        return 2

    # 6. post-cutover verify
    after_stats = collect_pre_cutover_stats(db_path)
    _info("Post-cutover statistics:")
    print(f"  clusters                    : {after_stats['clusters_count']}  (was {stats['clusters_count']})")
    print(f"  cluster_items               : {after_stats['cluster_items_count']}  (was {stats['cluster_items_count']})")
    print(f"  cluster_status              : {after_stats['cluster_status_count']}  (was {stats['cluster_status_count']})")
    print(f"  items.cluster_id NOT NULL   : {after_stats['items_with_cluster_id']}  (was {stats['items_with_cluster_id']})")
    print(f"  items.embedding NOT NULL    : {after_stats['items_with_embedding']}  (was {stats['items_with_embedding']})")
    print(f"  actions WHERE source=cluster: {after_stats['actions_cluster_source']}  (was {stats['actions_cluster_source']})")

    # validate the post state is what we expect (R9.3 SQL invariants)
    invariants_ok = (
        after_stats["clusters_count"] == 0
        and after_stats["cluster_items_count"] == 0
        and after_stats["cluster_status_count"] == 0
        and after_stats["items_with_cluster_id"] == 0
        and after_stats["items_with_embedding"] == 0
        and after_stats["actions_cluster_source"] == 0
    )
    if not invariants_ok:
        _err(
            "post-cutover invariant violation: expected all counts = 0 above. "
            f"Backup remains at {backup_path}."
        )
        # still log so audit captures the anomaly
        _log_event(
            "cluster_v15_1_executed",
            executed_by="cli",
            db_path=str(db_path),
            backup_path=str(backup_path),
            invariants_ok=False,
            before=stats,
            after=after_stats,
        )
        return 3

    # 7. audit log
    _log_event(
        "cluster_v15_1_executed",
        executed_by="cli",
        db_path=str(db_path),
        backup_path=str(backup_path),
        invariants_ok=True,
        before_clusters_count=stats["clusters_count"],
        before_cluster_items_count=stats["cluster_items_count"],
        before_cluster_status_count=stats["cluster_status_count"],
        before_items_with_cluster_id=stats["items_with_cluster_id"],
        before_items_with_embedding=stats["items_with_embedding"],
        before_actions_cluster_source=stats["actions_cluster_source"],
        after_clusters_count=after_stats["clusters_count"],
        after_cluster_items_count=after_stats["cluster_items_count"],
        after_cluster_status_count=after_stats["cluster_status_count"],
        after_items_with_cluster_id=after_stats["items_with_cluster_id"],
        after_items_with_embedding=after_stats["items_with_embedding"],
        after_actions_cluster_source=after_stats["actions_cluster_source"],
    )
    _ok(f"audit log written: {LOG_FILE} event=cluster_v15_1_executed")

    # 8. next steps
    print()
    _ok("Cutover complete.")
    _info("Next steps:")
    print("  1. Re-run the V2 enrich + cluster pipeline to rebuild data:")
    print("       python3 src/clustering/pipeline.py")
    print("     (or wait for the next scheduled cron tick)")
    print("  2. Resume ECS ingest cron once the rebuild looks healthy.")
    print("  3. Run QA-3 (Wave 8) regression sample checks against newly built clusters.")
    print(f"  4. Keep backup safe: {backup_path}")
    print()
    return 0


# ---------- argparse ----------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cutover_v2.py",
        description="v15.1 Event Aggregation V2 cutover (default: dry-run)",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print plan without touching DB (default mode if neither flag given).",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Actually run cutover (backup + DELETE + UPDATE). Requires --yes or interactive YES.",
    )
    p.add_argument(
        "--db-path",
        type=str,
        default=str(DEFAULT_DB_PATH),
        help=f"Path to DB file (default: {DEFAULT_DB_PATH})",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        default=False,
        help="Skip interactive confirmations (CI / scripted use).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    db_path = Path(args.db_path).expanduser().resolve()

    # default to dry-run when neither --dry-run nor --execute given
    if not args.dry_run and not args.execute:
        args.dry_run = True

    _info(f"DB path: {db_path}")

    if args.execute:
        return run_execute(db_path, auto_yes=args.yes)
    return run_dry_run(db_path)


if __name__ == "__main__":
    sys.exit(main())
