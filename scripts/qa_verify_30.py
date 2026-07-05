#!/usr/bin/env python3
"""v15.1 V2 人肉验收报告生成器

跑完 fetch + ingest + enrich + clustering 后调用，输出：
A. 总量统计（items / clusters / cluster_items / cluster_status / cluster_judge_log）
B. 每平台 ai_summary 完成率 + embedding 完成率
C. clusters 拆分（total / visible / merged / archived）
D. cluster_judge_log 决策抽样（10 条最新，看 LLM 接受/拒绝原因）
E. 相似度阈值落点（Stage 1 cosine 召回但 Stage 2 LLM 拒绝的样本）
F. cluster 内容样本（前 5 个 cluster 的 ai_title + unique_source_count + doc_count）
G. 前端入口（worktree 端口）
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, 'data', 'feed.db')
DEVSERVER_PATH = os.path.join(ROOT, '.devserver.json')


def section(title: str) -> None:
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print('=' * 70)


def main() -> int:
    if not os.path.exists(DB_PATH):
        print(f"❌ DB not found: {DB_PATH}")
        return 1
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # A. 总量统计
    section('A. 总量统计')
    counts = {}
    for tbl in (
        'items', 'clusters', 'cluster_items', 'cluster_status',
        'cluster_judge_log',
    ):
        try:
            counts[tbl] = conn.execute(f'SELECT COUNT(*) FROM {tbl}').fetchone()[0]
        except sqlite3.OperationalError as e:
            counts[tbl] = f'ERR: {e}'
    for k, v in counts.items():
        print(f'  {k}: {v}')

    # B. 每平台 enrichment + embedding 完成率
    section('B. 每平台 ai_summary / embedding 完成率')
    rows = conn.execute("""
        SELECT platform,
               COUNT(*) AS total,
               SUM(CASE WHEN ai_summary IS NOT NULL THEN 1 ELSE 0 END) AS has_sum,
               SUM(CASE WHEN ai_keywords IS NOT NULL THEN 1 ELSE 0 END) AS has_kw,
               SUM(CASE WHEN ai_key_points IS NOT NULL THEN 1 ELSE 0 END) AS has_kp,
               SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END) AS has_emb,
               SUM(CASE WHEN ai_error_count > 0 THEN 1 ELSE 0 END) AS errs
        FROM items
        GROUP BY platform
        ORDER BY platform
    """).fetchall()
    print(f'  {"platform":<14} {"total":>5} {"summary":>7} {"kw":>5} {"kp":>5} {"emb":>5} {"err":>5}')
    for r in rows:
        print(f'  {r["platform"]:<14} {r["total"]:>5} {r["has_sum"]:>7} {r["has_kw"]:>5} '
              f'{r["has_kp"]:>5} {r["has_emb"]:>5} {r["errs"]:>5}')

    # C. clusters 拆分
    section('C. Clusters 拆分')
    total = conn.execute('SELECT COUNT(*) FROM clusters').fetchone()[0]
    visible = conn.execute(
        'SELECT COUNT(*) FROM clusters WHERE unique_source_count >= 2 AND archived = 0 '
        'AND merged_into IS NULL'
    ).fetchone()[0]
    merged = conn.execute('SELECT COUNT(*) FROM clusters WHERE merged_into IS NOT NULL').fetchone()[0]
    archived = conn.execute('SELECT COUNT(*) FROM clusters WHERE archived = 1').fetchone()[0]
    invisible_low_usc = conn.execute(
        'SELECT COUNT(*) FROM clusters WHERE unique_source_count < 2 AND archived = 0 '
        'AND merged_into IS NULL'
    ).fetchone()[0]
    print(f'  total clusters: {total}')
    print(f'  visible (unique_source_count >= 2, not merged, not archived): {visible}')
    print(f'  merged_into: {merged}')
    print(f'  archived: {archived}')
    print(f'  invisible (unique_source_count < 2, single-source): {invisible_low_usc}')
    if total:
        usc_dist = conn.execute(
            'SELECT unique_source_count AS usc, COUNT(*) AS n FROM clusters '
            'WHERE archived = 0 AND merged_into IS NULL GROUP BY unique_source_count '
            'ORDER BY unique_source_count'
        ).fetchall()
        print('  unique_source_count distribution:')
        for r in usc_dist:
            print(f'    USC={r["usc"]}: {r["n"]} clusters')

    # D. cluster_judge_log 决策抽样
    section('D. Stage 2 LLM judge 决策抽样（最新 10 条）')
    sample = conn.execute("""
        SELECT id, item_id, candidate_cluster_ids, selected_cluster_id,
               selection_reason, possible_merge_candidates, decision_model, created_at
        FROM cluster_judge_log
        ORDER BY id DESC LIMIT 10
    """).fetchall()
    if not sample:
        print('  (no cluster_judge_log entries — Stage 2 未触发或 pipeline 没跑)')
    for r in sample:
        cands = r['candidate_cluster_ids'] or '[]'
        try:
            cand_list = json.loads(cands) if isinstance(cands, str) else cands
            cand_n = len(cand_list) if isinstance(cand_list, list) else 0
        except Exception:
            cand_n = '?'
        sel = r['selected_cluster_id'] or 'NEW'
        reason = (r['selection_reason'] or '')[:80]
        print(f'  [#{r["id"]}] item={r["item_id"][:18]}  cand={cand_n}  selected={sel}  reason="{reason}"')

    # E. Stage 1 召回 vs Stage 2 拒绝（看 cosine 阈值实际表现）
    section('E. Stage 1 cosine 召回 / Stage 2 LLM 决策对照')
    rejected = conn.execute("""
        SELECT id, item_id, candidate_cluster_ids, selected_cluster_id, selection_reason
        FROM cluster_judge_log
        WHERE candidate_cluster_ids IS NOT NULL AND candidate_cluster_ids != '[]'
          AND (selected_cluster_id IS NULL)
        ORDER BY id DESC LIMIT 5
    """).fetchall()
    print('  最近 5 条「Stage 1 召回但 Stage 2 全拒（建新 cluster）」案例：')
    if not rejected:
        print('    (无 — 所有召回都被 LLM 接受合入，或 Stage 2 没运行)')
    for r in rejected:
        try:
            cands = json.loads(r['candidate_cluster_ids']) if r['candidate_cluster_ids'] else []
        except Exception:
            cands = []
        reason = (r['selection_reason'] or '')[:120]
        print(f'    item={r["item_id"][:20]}  rejected_cand={cands}  reason="{reason}"')

    # F. cluster 内容样本
    section('F. Cluster 内容样本（前 5 个 visible cluster）')
    cs = conn.execute("""
        SELECT id, ai_title, ai_summary, doc_count, unique_source_count,
               platforms_json, last_doc_at
        FROM clusters
        WHERE unique_source_count >= 2 AND archived = 0 AND merged_into IS NULL
        ORDER BY unique_source_count DESC, doc_count DESC LIMIT 5
    """).fetchall()
    if not cs:
        print('  (没有可见 cluster — unique_source_count >= 2 的没有)')
    for c in cs:
        platforms = c['platforms_json'] or ''
        try:
            platforms = ', '.join(json.loads(platforms))
        except Exception:
            pass
        title = (c['ai_title'] or '')[:60]
        summary = (c['ai_summary'] or '')[:200].replace('\n', ' ')
        print(f'  [#{c["id"]}] USC={c["unique_source_count"]} docs={c["doc_count"]} '
              f'platforms=[{platforms}]')
        print(f'    title: {title}')
        print(f'    summary: {summary}...')
        # 列出 cluster 内的 items（看跨平台聚合是否合理）
        items = conn.execute("""
            SELECT i.id, i.platform, i.title, ci.source_identity
            FROM cluster_items ci
            JOIN items i ON i.id = ci.item_id
            WHERE ci.cluster_id = ?
            ORDER BY ci.added_at LIMIT 5
        """, (c['id'],)).fetchall()
        for it in items:
            t = (it['title'] or '')[:50]
            print(f'      - {it["platform"]:<10} {it["id"][:18]}  {t}')

    # G. 前端入口
    section('G. 前端 / 后端入口')
    if os.path.exists(DEVSERVER_PATH):
        with open(DEVSERVER_PATH) as f:
            d = json.load(f)
        print(f'  backend:  {d.get("backend_url", "未启动")}')
        print(f'  frontend: {d.get("frontend_url", "未启动")}')
        print(f'  status check: cd {ROOT} && npm run dev:status')
    else:
        print('  .devserver.json 不存在（dev server 未启动），用 npm run dev 启动')

    print('\n' + '=' * 70)
    print('  报告完成')
    print('=' * 70)
    return 0


if __name__ == '__main__':
    sys.exit(main())
