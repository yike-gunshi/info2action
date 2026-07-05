"""Stage A 本地验收 runner。

设计稿 docs/讨论/clustering/2026-04-29-event-pipeline-v2-design.md §5.4 验收方式。

行为：
  1. 把主仓库 data/feed.db cp 到 /tmp/info2action-stage-a-eval/feed.db（不污染生产）
  2. 在副本上跑 Stage A 处理最多 N 条 enriched item（默认 200）
  3. 落盘 markdown 报告：量化指标 + canonical_url 抽样 + embedding 召回 sanity check

用法：
  uv run --with sentence-transformers python scripts/stage_a_local_run.py
  uv run --with sentence-transformers python scripts/stage_a_local_run.py --limit 200
  uv run --with sentence-transformers python scripts/stage_a_local_run.py --reset

不动主仓库 data/feed.db。报告写到 /tmp/info2action-stage-a-eval/report-{ts}.md。
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

EVAL_DIR = Path("/tmp/info2action-stage-a-eval")
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
    # 复制 -wal / -shm（如有），避免 SQLite open 时跑回放
    for ext in ("-wal", "-shm"):
        side = PROD_DB.with_name(PROD_DB.name + ext)
        if side.exists():
            shutil.copy2(side, EVAL_DB.with_name(EVAL_DB.name + ext))


def _ensure_schema(db_path: Path) -> None:
    """触发 Stage A schema migration 直接在副本 DB 跑。"""
    import db as info_db  # type: ignore

    info_db.DB_PATH = str(db_path)
    info_db.get_conn().close()


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _reset_stage_a(conn: sqlite3.Connection) -> int:
    """清空已写入的 Stage A 状态，方便重跑。"""
    cur = conn.execute(
        "UPDATE items SET embedding=NULL, embedding_model=NULL, "
        "embedding_input_variant=NULL, embedding_generated_at=NULL, "
        "canonical_url=NULL, stage_a_state=NULL, stage_a_failed_at=NULL"
    )
    conn.commit()
    return cur.rowcount


def _summary(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT "
        "  SUM(CASE WHEN stage_a_state='done' THEN 1 ELSE 0 END) AS done_n, "
        "  SUM(CASE WHEN stage_a_state='failed' THEN 1 ELSE 0 END) AS failed_n, "
        "  SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END) AS embedded_n, "
        "  SUM(CASE WHEN embedding_input_variant='aikw' THEN 1 ELSE 0 END) AS aikw_n, "
        "  SUM(CASE WHEN canonical_url IS NOT NULL THEN 1 ELSE 0 END) AS canonical_n, "
        "  COUNT(*) AS total "
        "FROM items"
    ).fetchone()
    return dict(rows)


def _sample_canonical(conn: sqlite3.Connection, n: int = 20) -> list[dict]:
    """优先选含 utm_/fbclid/ref/source/from/# 等需归一的 URL。"""
    pattern_clauses = " OR ".join([
        "url LIKE '%utm_%'",
        "url LIKE '%fbclid=%'",
        "url LIKE '%gclid=%'",
        "url LIKE '%?ref=%'",
        "url LIKE '%&ref=%'",
        "url LIKE '%?source=%'",
        "url LIKE '%&source=%'",
        "url LIKE '%?from=%'",
        "url LIKE '%&from=%'",
        "url LIKE '%#%'",
    ])
    rows = conn.execute(
        f"SELECT id, url, canonical_url FROM items "
        f"WHERE stage_a_state='done' AND url IS NOT NULL "
        f"  AND ({pattern_clauses}) "
        f"ORDER BY fetched_at DESC LIMIT ?",
        (n,),
    ).fetchall()
    if len(rows) >= n:
        return [dict(r) for r in rows]
    fill = conn.execute(
        "SELECT id, url, canonical_url FROM items "
        "WHERE stage_a_state='done' AND url IS NOT NULL "
        "  AND id NOT IN ({}) "
        "ORDER BY fetched_at DESC LIMIT ?".format(
            ",".join(["?"] * len(rows)) or "''"
        ),
        ([r["id"] for r in rows] + [n - len(rows)]) if rows else [n],
    ).fetchall()
    out = [dict(r) for r in rows] + [dict(r) for r in fill]
    return out[:n]


def _embedding_recall_check(conn: sqlite3.Connection, n_queries: int = 5) -> list[dict]:
    """随机选 n_queries 条 query，跑 cosine top-5 召回，看 vector 健康度。"""
    import numpy as np

    from clustering.vector_utils import unpack_blob  # type: ignore

    rows = conn.execute(
        "SELECT id, title, embedding FROM items "
        "WHERE stage_a_state='done' AND embedding IS NOT NULL "
        "ORDER BY RANDOM() LIMIT ?",
        (n_queries,),
    ).fetchall()
    queries = [(r["id"], r["title"] or "", unpack_blob(r["embedding"])) for r in rows]

    pool = conn.execute(
        "SELECT id, title, platform, embedding FROM items "
        "WHERE stage_a_state='done' AND embedding IS NOT NULL"
    ).fetchall()
    pool_ids = [r["id"] for r in pool]
    pool_titles = [r["title"] or "" for r in pool]
    pool_platforms = [r["platform"] or "" for r in pool]
    pool_mat = np.stack([unpack_blob(r["embedding"]) for r in pool])

    results = []
    for qid, qtitle, qvec in queries:
        sims = pool_mat @ qvec  # 已 L2 归一化 → 内积 = cosine
        order = np.argsort(-sims)
        top = []
        for idx in order:
            iid = pool_ids[idx]
            if iid == qid:
                continue
            top.append({
                "id": iid,
                "platform": pool_platforms[idx],
                "title": pool_titles[idx][:80],
                "cos": round(float(sims[idx]), 4),
            })
            if len(top) >= 5:
                break
        results.append({
            "query_id": qid,
            "query_title": qtitle[:80],
            "top5": top,
        })
    return results


def _write_report(stats: dict, summary: dict, samples: list[dict],
                  recall: list[dict], runtime_seconds: float, limit: int) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = EVAL_DIR / f"report-{ts}.md"

    total = stats["processed"] or 1
    succ_rate = stats["succeeded"] / total

    lines = [
        f"# Stage A 本地验收报告 — {ts}",
        "",
        f"- limit (待跑上限): {limit}",
        f"- runtime: {runtime_seconds:.2f}s",
        f"- 副本 DB: {EVAL_DB}",
        "",
        "## 1. 量化指标",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 本次 processed | {stats['processed']} |",
        f"| succeeded | {stats['succeeded']} |",
        f"| failed | {stats['failed']} |",
        f"| skipped_no_summary | {stats['skipped_no_summary']} |",
        f"| 成功率 | {succ_rate:.4f} (门槛 ≥0.99) |",
        f"| 全库 stage_a_state='done' | {summary['done_n']} |",
        f"| 全库 stage_a_state='failed' | {summary['failed_n']} |",
        f"| 全库 embedding 非 NULL | {summary['embedded_n']} |",
        f"| 全库 embedding_input_variant='aikw' | {summary['aikw_n']} |",
        f"| 全库 canonical_url 非 NULL | {summary['canonical_n']} |",
        f"| 全库 items 总数 | {summary['total']} |",
        "",
        "## 2. canonical_url 抽样（优先含 utm_/fbclid/ref/source/from/#）",
        "",
        "| 序号 | 原始 url | canonical_url |",
        "|---|---|---|",
    ]
    for i, s in enumerate(samples, 1):
        original = (s["url"] or "").replace("|", "\\|")
        canon = (s["canonical_url"] or "(NULL)").replace("|", "\\|")
        lines.append(f"| {i} | `{original}` | `{canon}` |")

    lines.extend([
        "",
        "## 3. embedding 召回 sanity check（5 条随机 query × top-5）",
        "",
    ])
    for q in recall:
        lines.append(f"### query: `{q['query_id']}` — {q['query_title']}")
        lines.append("")
        lines.append("| rank | cos | platform | title |")
        lines.append("|---|---|---|---|")
        for i, t in enumerate(q["top5"], 1):
            lines.append(f"| {i} | {t['cos']} | {t['platform']} | {t['title'].replace('|','\\|')} |")
        lines.append("")

    lines.extend([
        "## 4. 验收对照（设计稿 §5.4）",
        "",
        f"- [{'x' if succ_rate >= 0.99 else ' '}] embedding 生成成功率 ≥ 99% （{succ_rate:.4f}）",
        f"- [{'x' if summary['aikw_n'] == summary['done_n'] and summary['done_n']>0 else ' '}] embedding_input_variant='aikw' 在 100% 成功 item 上写入",
        "- [ ] 抽样 20 条带 utm 参数 URL 验证去除正确（人眼审第 2 节）",
        "- [ ] 5 条 query top-5 召回主题一致性合理（人眼审第 3 节）",
        "",
        "## 5. 后续",
        "",
        "- 若验收通过 → merge worktree 进 main，进入 Stage B 设计",
        "- 若 canonical_url 出现异常 case → 加 unit test 覆盖并迭代",
        "- 若 top-5 召回明显失常 → 复检 BGE-M3 模型加载是否一致 / aikw 输入是否符合 §4.4 契约",
    ])

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--reset", action="store_true",
                    help="清空副本 DB 的 Stage A 字段后重跑")
    ap.add_argument("--refresh-db", action="store_true",
                    help="强制重新从主仓库 cp DB（覆盖既有副本）")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    _setup_logging(args.verbose)

    _refresh_eval_db(force=args.refresh_db)
    _ensure_schema(EVAL_DB)

    from clustering import stage_a as sa  # type: ignore

    conn = _connect(EVAL_DB)
    if args.reset:
        n = _reset_stage_a(conn)
        print(f"[runner] reset Stage A 字段：{n} rows")

    started = time.time()
    stats = sa.stage_a_run(conn, limit=args.limit)
    runtime = time.time() - started
    print(f"[runner] stage_a_run done: {json.dumps(stats, ensure_ascii=False)}")

    summary = _summary(conn)
    samples = _sample_canonical(conn, n=20)
    recall = _embedding_recall_check(conn, n_queries=5)

    report_path = _write_report(stats, summary, samples, recall, runtime, args.limit)
    print(f"[runner] 报告：{report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
