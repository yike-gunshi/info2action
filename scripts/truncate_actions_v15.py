#!/usr/bin/env python3
"""v15.0 一次性 actions 清理脚本(PRD §13.6 S1 决策)。

为什么独立 + 需要确认?
  v15 改成 cluster/doc 双入口手动触发后,旧 actions 的 source_type/source_id
  cluster_version/is_stale 均为 NULL,与新查询路径不兼容(SHALL 强制带 source_type='doc')。
  PRD 锁定上线第一波清空所有老 actions,避免 admin 列表混排不一致。

操作:DELETE actions / action_logs / action_feedback;UPDATE settings.actions_v15_truncated。
幂等:如果 settings 已记录 actions_v15_truncated,直接跳过(防止误重复执行)。

强制确认:必须输入大写 `YES, TRUNCATE ACTIONS` 才执行,任何其他输入立即退出。

Usage:
    python scripts/truncate_actions_v15.py
    python scripts/truncate_actions_v15.py --dry-run     # 只显示影响行数,不删
    python scripts/truncate_actions_v15.py --force       # 跳过交互(CI 用,危险)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `src/` importable regardless of cwd.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402


CONFIRM_PHRASE = "YES, TRUNCATE ACTIONS"


def main() -> int:
    parser = argparse.ArgumentParser(description="v15.0 actions truncate (one-shot)")
    parser.add_argument("--dry-run", action="store_true", help="只显示要删的行数")
    parser.add_argument("--force", action="store_true", help="跳过交互式确认 (危险)")
    args = parser.parse_args()

    conn = db.get_conn()

    truncated_row = conn.execute(
        "SELECT value FROM settings WHERE key = 'actions_v15_truncated'"
    ).fetchone()
    if truncated_row is not None:
        print(
            f"[truncate_actions_v15] 已经清过一次了 (settings.actions_v15_truncated={truncated_row['value']}),跳过",
            flush=True,
        )
        return 0

    n_actions = conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
    n_logs = conn.execute("SELECT COUNT(*) FROM action_logs").fetchone()[0]
    n_feedback = conn.execute("SELECT COUNT(*) FROM action_feedback").fetchone()[0]

    print(f"[truncate_actions_v15] actions      = {n_actions}", flush=True)
    print(f"[truncate_actions_v15] action_logs  = {n_logs}", flush=True)
    print(f"[truncate_actions_v15] action_feedback = {n_feedback}", flush=True)

    if args.dry_run:
        print("[truncate_actions_v15] --dry-run,不删", flush=True)
        return 0

    if not args.force:
        print(
            f"\n这将删除上述 3 张表的 {n_actions + n_logs + n_feedback} 条数据。"
            f"\n确认请输入: {CONFIRM_PHRASE}",
            flush=True,
        )
        try:
            answer = input("> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n[truncate_actions_v15] 取消", flush=True)
            return 1
        if answer != CONFIRM_PHRASE:
            print(f"[truncate_actions_v15] 输入不匹配,取消 (got={answer!r})", flush=True)
            return 1

    conn.execute("DELETE FROM action_logs")
    conn.execute("DELETE FROM action_feedback")
    conn.execute("DELETE FROM actions")
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES('actions_v15_truncated', ?)",
        (str(n_actions),),
    )
    conn.commit()
    print(
        f"[truncate_actions_v15] DONE: 清空 {n_actions} actions / {n_logs} logs / {n_feedback} feedback",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
