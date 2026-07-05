"""v2 极简版本地验收 runner。

设计稿 docs/讨论/clustering/2026-04-29-event-pipeline-v2-design.md §5.5.4

行为：
  1. cp 主仓库 data/feed.db → /tmp/info2action-cluster-v2-eval/feed.db（不污染生产）
  2. 触发 v2 schema 迁移
  3. 跑 Stage A（确保最近 14 天 enriched item 有 embedding；上限 limit_a 条）
  4. reset_clusters_v2 + run_stage_z（最近 14 天 doc 聚簇）
  5. run_pending_stage_p（限制处理 limit_p 个最大簇，节省 LLM 调用）
  6. 落盘 markdown 报告：量化 + 簇分布 + 抽样 LLM 清洗结果

用法：
  uv run --with sentence-transformers python scripts/cluster_v2_local_run.py
  uv run --with sentence-transformers python scripts/cluster_v2_local_run.py --limit-a 1000 --limit-p 30
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

EVAL_DIR = Path("/tmp/info2action-cluster-v2-eval")
EVAL_DIR.mkdir(parents=True, exist_ok=True)
EVAL_DB = EVAL_DIR / "feed.db"
PROD_DB = REPO_ROOT / "data" / "feed.db"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def _refresh_eval_db(force: bool) -> None:
    if EVAL_DB.exists() and not force:
        print(f"[runner] reuse 既有副本 {EVAL_DB} (size={EVAL_DB.stat().st_size:,} bytes)")
        return
    if not PROD_DB.exists():
        raise SystemExit(f"主仓库 DB 不存在：{PROD_DB}")
    print(f"[runner] cp {PROD_DB} → {EVAL_DB}")
    shutil.copy2(PROD_DB, EVAL_DB)
    for ext in ("-wal", "-shm"):
        side = PROD_DB.with_name(PROD_DB.name + ext)
        if side.exists():
            shutil.copy2(side, EVAL_DB.with_name(EVAL_DB.name + ext))


def _ensure_schema(db_path: Path) -> None:
    import db as info_db  # type: ignore

    info_db.DB_PATH = str(db_path)
    info_db.get_conn().close()


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _load_env() -> dict:
    cfg_path = REPO_ROOT / "config" / "config.json"
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return cfg


def _stage_a_pass(conn: sqlite3.Connection, limit_a: int) -> dict:
    from clustering import stage_a as sa  # type: ignore

    return sa.stage_a_run(conn, limit=limit_a)


def _select_top_clusters_for_p(conn: sqlite3.Connection, limit_p: int) -> list[int]:
    """挑成员数最多的 limit_p 个 dirty 簇优先跑 Stage P，确保看到杂烩"""
    rows = conn.execute(
        """SELECT id FROM clusters_v2
           WHERE stage_p_state='dirty'
           ORDER BY member_count DESC, created_at DESC
           LIMIT ?""",
        (limit_p,),
    ).fetchall()
    return [r["id"] for r in rows]


def _cluster_size_distribution(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT member_count FROM clusters_v2"
    ).fetchall()
    sizes = [r["member_count"] for r in rows]
    if not sizes:
        return {"n": 0}
    sizes_sorted = sorted(sizes)
    return {
        "n": len(sizes),
        "single_doc": sum(1 for s in sizes if s == 1),
        "max": max(sizes),
        "median": sizes_sorted[len(sizes_sorted) // 2],
        "p90": sizes_sorted[int(len(sizes_sorted) * 0.9)],
        "total_members": sum(sizes),
    }


def _sample_largest_clusters(conn: sqlite3.Connection, n: int = 5) -> list[dict]:
    rows = conn.execute(
        """SELECT id, dominant_category, member_count, event_summary, event_certainty,
                  stage_p_state
           FROM clusters_v2
           WHERE stage_p_state IN ('clean','failed','unsupported')
           ORDER BY member_count DESC LIMIT ?""",
        (n,),
    ).fetchall()
    out = []
    for r in rows:
        members = conn.execute(
            """SELECT i.id, i.platform, i.title, ci.removed_at, ci.removed_reason
               FROM cluster_items_v2 ci
               JOIN items i ON i.id = ci.item_id
               WHERE ci.cluster_id = ?
               ORDER BY ci.added_at""",
            (r["id"],),
        ).fetchall()
        out.append({
            "cluster_id": r["id"],
            "dominant_category": r["dominant_category"],
            "stage_p_state": r["stage_p_state"],
            "event_summary": r["event_summary"],
            "event_certainty": r["event_certainty"],
            "kept": [dict(m) for m in members if m["removed_at"] is None],
            "removed": [dict(m) for m in members if m["removed_at"] is not None],
        })
    return out


def _write_report(stage_a_stats: dict, stage_z_stats: dict,
                   stage_p_stats: list[dict], cluster_dist: dict,
                   samples: list[dict], runtime: float, limit_a: int, limit_p: int) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = EVAL_DIR / f"report-{ts}.md"

    p_summary = {"clean": 0, "failed": 0, "skipped": 0, "unsupported": 0}
    for r in stage_p_stats:
        st = r.get("status", "?")
        if st == "skipped":
            p_summary["skipped"] += 1
            if "unsupported" in r.get("reason", ""):
                p_summary["unsupported"] += 1
        elif st in p_summary:
            p_summary[st] += 1

    lines = [
        f"# v2 极简版本地验收报告 — {ts}",
        "",
        f"- limit-a (Stage A): {limit_a}",
        f"- limit-p (Stage P): {limit_p}",
        f"- runtime: {runtime:.2f}s",
        f"- 副本 DB: {EVAL_DB}",
        "",
        "## 1. Stage A（embedding 生成）",
        "",
        f"```\n{json.dumps(stage_a_stats, ensure_ascii=False, indent=2)}\n```",
        "",
        "## 2. Stage Z（cosine 聚簇）",
        "",
        f"```\n{json.dumps(stage_z_stats, ensure_ascii=False, indent=2)}\n```",
        "",
        "### 簇分布",
        "",
        f"```\n{json.dumps(cluster_dist, ensure_ascii=False, indent=2)}\n```",
        "",
        "## 3. Stage P（LLM 清洗）",
        "",
        f"- clean: {p_summary['clean']}",
        f"- failed: {p_summary['failed']}",
        f"- skipped (含 unsupported {p_summary['unsupported']}): {p_summary['skipped']}",
        "",
        "### 详细执行结果",
        "",
        "| cluster_id | status | kept | removed | certainty | took(s) |",
        "|---|---|---|---|---|---|",
    ]
    for r in stage_p_stats:
        lines.append(
            f"| {r.get('cluster_id')} | {r.get('status','?')} | "
            f"{r.get('kept','-')} | {r.get('removed','-')} | "
            f"{r.get('certainty','-')} | {r.get('took_seconds',0)} |"
        )
    lines.extend([
        "",
        f"## 4. 抽样：最大 {len(samples)} 个簇（Stage P 后）",
        "",
    ])
    for s in samples:
        lines.append(f"### cluster #{s['cluster_id']} — dominant={s['dominant_category']} state={s['stage_p_state']}")
        lines.append("")
        if s["event_summary"]:
            lines.append(f"**event_summary**: {s['event_summary']}  (certainty={s['event_certainty']})")
        else:
            lines.append("_event_summary 未生成_")
        lines.append("")
        lines.append(f"**保留 {len(s['kept'])} 条**：")
        for m in s["kept"][:8]:
            title = (m.get("title") or "").replace("\n", " ").replace("|", "\\|")[:80]
            lines.append(f"- `[{m['platform']}]` {title}")
        if len(s["kept"]) > 8:
            lines.append(f"- … 还有 {len(s['kept']) - 8} 条")
        lines.append("")
        lines.append(f"**剔除 {len(s['removed'])} 条**：")
        for m in s["removed"][:8]:
            title = (m.get("title") or "").replace("\n", " ").replace("|", "\\|")[:80]
            reason = (m.get("removed_reason") or "").replace("|", "\\|")[:60]
            lines.append(f"- `[{m['platform']}]` {title}  → {reason}")
        if len(s["removed"]) > 8:
            lines.append(f"- … 还有 {len(s['removed']) - 8} 条")
        lines.append("")

    lines.extend([
        "## 5. 验收对照（设计稿 §5.5.4）",
        "",
        "**Stage Z**:",
        f"- [{'x' if cluster_dist.get('n',0) > 0 else ' '}] 至少形成一个簇",
        f"- [{'x' if cluster_dist.get('max',0) <= 50 else ' '}] 最大簇 ≤ 50（硬上限），实际 max={cluster_dist.get('max',0)}",
        "- [ ] 抽样最大 5 个簇主题一致性（人眼审第 4 节）",
        "",
        "**Stage P**:",
        f"- [{'x' if p_summary['clean'] >= 3 else ' '}] 至少 3 个簇成功清洗（实际 clean={p_summary['clean']}）",
        f"- [{'x' if p_summary['failed'] == 0 else ' '}] LLM 调用 + JSON 解析全部成功（实际 failed={p_summary['failed']}）",
        "- [ ] LLM event_summary 准确性（人眼审第 4 节）",
        "- [ ] LLM removed 是否合理（人眼审第 4 节）",
        "",
        "## 6. cluster_p_log 摘要",
        "",
    ])

    return path, lines


def _finalize_report(path: Path, lines: list[str], conn: sqlite3.Connection) -> Path:
    summary_rows = conn.execute(
        """SELECT action, COUNT(*) AS n FROM cluster_p_log GROUP BY action ORDER BY n DESC"""
    ).fetchall()
    lines.append("| action | count |")
    lines.append("|---|---|")
    for r in summary_rows:
        lines.append(f"| {r['action']} | {r['n']} |")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-a", type=int, default=2000,
                    help="Stage A 处理上限")
    ap.add_argument("--limit-p", type=int, default=20,
                    help="Stage P 处理多少个最大簇")
    ap.add_argument("--days", type=int, default=14,
                    help="Stage Z 时间窗（天）")
    ap.add_argument("--concurrency", type=int, default=4,
                    help="Stage P LLM 并发度（默认 4）")
    ap.add_argument("--refresh-db", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    _setup_logging(args.verbose)

    _refresh_eval_db(force=args.refresh_db)
    _ensure_schema(EVAL_DB)
    cfg = _load_env()
    conn = _connect(EVAL_DB)

    started = time.time()

    print("\n========== Stage A ==========")
    stage_a_stats = _stage_a_pass(conn, args.limit_a)
    print(f"[runner] stage_a: {json.dumps(stage_a_stats, ensure_ascii=False)}")

    print("\n========== Stage Z (reset + run) ==========")
    from clustering import stage_z  # type: ignore
    reset = stage_z.reset_clusters_v2(conn)
    print(f"[runner] reset clusters_v2: {reset}")
    stage_z_stats = stage_z.run_stage_z(conn, days=args.days)
    print(f"[runner] stage_z: {json.dumps(stage_z_stats, ensure_ascii=False)}")

    cluster_dist = _cluster_size_distribution(conn)
    print(f"[runner] cluster size dist: {json.dumps(cluster_dist, ensure_ascii=False)}")

    print("\n========== Stage P ==========")
    from clustering import stage_p  # type: ignore
    target_ids = _select_top_clusters_for_p(conn, args.limit_p)
    print(f"[runner] picked {len(target_ids)} largest dirty clusters: {target_ids[:10]}…")
    print(f"[runner] using concurrency={args.concurrency} (with large-cluster batching)")
    # 关闭主连接释放 wal lock 给 worker 用
    conn.commit()
    conn.close()
    stage_p_stats = stage_p.run_pending_stage_p_concurrent(
        str(EVAL_DB), target_ids,
        concurrency=args.concurrency, config=cfg,
    )
    # 重新连一个用于后续报告读取
    conn = _connect(EVAL_DB)
    for r in sorted(stage_p_stats, key=lambda x: x.get("cluster_id") or 0):
        print(f"  cluster={r.get('cluster_id')}: {r.get('status')} "
              f"kept={r.get('kept')} removed={r.get('removed')} "
              f"cert={r.get('certainty')} took={r.get('took_seconds')}s")

    runtime = time.time() - started
    samples = _sample_largest_clusters(conn, n=5)

    path, lines = _write_report(stage_a_stats, stage_z_stats, stage_p_stats,
                                  cluster_dist, samples, runtime,
                                  args.limit_a, args.limit_p)
    _finalize_report(path, lines, conn)
    print(f"\n[runner] 报告: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
