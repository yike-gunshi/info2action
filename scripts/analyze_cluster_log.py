"""分析 cluster_events.jsonl + cluster_judge_log + cluster representative_vector
矩阵,产出聚合效果诊断报告。

用途:
  - cosine 分布直方图(Stage 1 召回质量)
  - 误合候选清单(`selected_cluster_id IS NOT NULL` + 主体看起来不一致)
  - 漏合候选清单(USC=1 singleton 与 visible cluster cosine ≥ 阈值的对子)
  - cluster 间相似度矩阵(visible cluster 两两 cosine,> 阈值的报警)

用法:
  cd .worktrees/cluster-log-analysis
  python3 -u scripts/analyze_cluster_log.py [--threshold-merge 0.85] [--limit 50]

输出:
  docs/优化/2026-04-28-cluster-log-analysis/{report.md, cosine_hist.txt,
                                            possible_misclusters.csv,
                                            possible_missed_merges.csv}
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(REPO / 'src/clustering'))
os.chdir(REPO)

import numpy as np
import vector_utils as vu
import db


def load_jsonl_events(path: Path) -> list[dict]:
    out = []
    with path.open() as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def cosine_histogram(events: list[dict]) -> dict:
    """Stage 1 召回质量:从 stage1_candidates_fetched 抽 top_cosines / max / min。"""
    max_buckets = Counter()
    min_buckets = Counter()
    all_top_cosines = []
    candidate_count_dist = Counter()

    def bucket(c):
        if c is None:
            return 'n/a'
        if c < 0.5:
            return '<0.50'
        return f'{c:.2f}'[:4]

    for ev in events:
        if ev.get('event') != 'stage1_candidates_fetched':
            continue
        candidate_count_dist[ev.get('candidate_count', 0)] += 1
        if ev.get('max_cosine') is not None:
            max_buckets[bucket(ev['max_cosine'])] += 1
        if ev.get('min_cosine') is not None:
            min_buckets[bucket(ev['min_cosine'])] += 1
        for c in ev.get('top_cosines', []) or []:
            all_top_cosines.append(c)

    return {
        'max_dist': dict(max_buckets),
        'min_dist': dict(min_buckets),
        'all_cosines': all_top_cosines,
        'candidate_count_dist': dict(candidate_count_dist),
    }


def all_pairs_cosine(conn, threshold_merge: float = 0.85,
                    visible_only: bool = True) -> list[dict]:
    """跑 visible cluster 两两 cosine,找 ≥ threshold 的对子(可能漏合)。"""
    where = "is_visible_in_feed=1" if visible_only else "1=1"
    rows = conn.execute(
        f"""SELECT id, ai_title, doc_count, unique_source_count,
                   representative_vector
            FROM clusters
            WHERE {where} AND archived=0 AND merged_into IS NULL
              AND representative_vector IS NOT NULL"""
    ).fetchall()

    parsed = []
    for r in rows:
        v = vu.unpack_blob(r['representative_vector'])
        if v is None:
            continue
        parsed.append({
            'id': r['id'], 'ai_title': r['ai_title'] or '',
            'doc_count': r['doc_count'],
            'unique_source_count': r['unique_source_count'],
            'vec': v,
        })

    pairs = []
    n = len(parsed)
    for i in range(n):
        for j in range(i + 1, n):
            sim = float(vu.cosine_similarity(parsed[i]['vec'], parsed[j]['vec']))
            if sim >= threshold_merge:
                pairs.append({
                    'cluster_a': parsed[i]['id'],
                    'title_a': parsed[i]['ai_title'][:60],
                    'usc_a': parsed[i]['unique_source_count'],
                    'cluster_b': parsed[j]['id'],
                    'title_b': parsed[j]['ai_title'][:60],
                    'usc_b': parsed[j]['unique_source_count'],
                    'cosine': round(sim, 4),
                })
    pairs.sort(key=lambda x: -x['cosine'])
    return pairs


def potential_misclusters(conn, limit: int = 50) -> list[dict]:
    """误合候选:cluster 内成员作者跨多个,且 cluster 标题与某些成员 title 不一致。

    简单启发式:cluster 含 >=3 distinct author 的 visible cluster,人工抽查。
    """
    rows = conn.execute(
        """SELECT ci.cluster_id AS cid,
                  c.ai_title,
                  c.unique_source_count,
                  COUNT(DISTINCT i.author_name) AS authors,
                  COUNT(*) AS members,
                  group_concat(substr(i.title, 1, 40), ' | ') AS titles
           FROM cluster_items ci
           JOIN items i ON i.id = ci.item_id
           JOIN clusters c ON c.id = ci.cluster_id
           WHERE c.is_visible_in_feed=1 AND c.archived=0 AND c.merged_into IS NULL
           GROUP BY ci.cluster_id
           HAVING members >= 3
           ORDER BY members DESC, authors DESC
           LIMIT ?""", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def judge_log_outcomes(conn) -> dict:
    rows = conn.execute(
        """SELECT selection_reason, COUNT(*) AS n
           FROM cluster_judge_log
           GROUP BY selection_reason ORDER BY n DESC"""
    ).fetchall()
    return {r['selection_reason'] or '(null)': r['n'] for r in rows}


def stuck_singletons_with_close_visible(conn, threshold: float = 0.80) -> list[dict]:
    """USC=1 singleton (invisible) 与某 visible cluster cosine ≥ threshold 的样本.
    LLM judge 漏合候选(应合却分开了)。"""
    visible = conn.execute(
        """SELECT id, ai_title, representative_vector
           FROM clusters
           WHERE is_visible_in_feed=1 AND archived=0 AND merged_into IS NULL
             AND representative_vector IS NOT NULL"""
    ).fetchall()
    visible_parsed = []
    for r in visible:
        v = vu.unpack_blob(r['representative_vector'])
        if v is not None:
            visible_parsed.append((r['id'], r['ai_title'] or '', v))

    singletons = conn.execute(
        """SELECT id, ai_title, representative_vector
           FROM clusters
           WHERE unique_source_count=1 AND archived=0 AND merged_into IS NULL
             AND representative_vector IS NOT NULL"""
    ).fetchall()

    matches = []
    for s in singletons:
        sv = vu.unpack_blob(s['representative_vector'])
        if sv is None:
            continue
        for vid, vtitle, vvec in visible_parsed:
            sim = float(vu.cosine_similarity(sv, vvec))
            if sim >= threshold:
                matches.append({
                    'singleton_id': s['id'],
                    'singleton_title': (s['ai_title'] or '')[:60],
                    'visible_id': vid,
                    'visible_title': vtitle[:60],
                    'cosine': round(sim, 4),
                })
    matches.sort(key=lambda x: -x['cosine'])
    return matches


def render_histogram(cosines: list[float], buckets: int = 20) -> str:
    if not cosines:
        return '(no data)'
    arr = np.array(cosines)
    hist, edges = np.histogram(arr, bins=buckets, range=(0.0, 1.0))
    lines = ['cosine    count  bar']
    max_n = max(hist) if hist.max() else 1
    for i, n in enumerate(hist):
        lo, hi = edges[i], edges[i + 1]
        bar = '█' * int(40 * n / max_n) if n else ''
        lines.append(f'{lo:.2f}-{hi:.2f}  {n:>5}  {bar}')
    lines.append(f'\nstats: n={len(arr)}, min={arr.min():.4f}, '
                 f'p25={np.percentile(arr,25):.4f}, '
                 f'p50={np.percentile(arr,50):.4f}, '
                 f'p75={np.percentile(arr,75):.4f}, '
                 f'p90={np.percentile(arr,90):.4f}, '
                 f'max={arr.max():.4f}')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--threshold-merge', type=float, default=0.85,
                        help='cluster pair cosine ≥ X 视为可能漏合')
    parser.add_argument('--threshold-missed', type=float, default=0.80,
                        help='singleton-visible cosine ≥ X 视为可能漏合')
    parser.add_argument('--limit', type=int, default=50,
                        help='误合候选返回数')
    parser.add_argument('--out-dir', default='docs/优化/2026-04-28-cluster-log-analysis')
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f'Reading logs/cluster_events.jsonl ...', flush=True)
    events = load_jsonl_events(Path('logs/cluster_events.jsonl'))
    print(f'  {len(events)} events loaded', flush=True)

    print(f'Reading data/feed.db ...', flush=True)
    conn = db.get_conn()

    # 1. cosine histogram
    print('1) cosine histogram (Stage 1 召回)...', flush=True)
    h = cosine_histogram(events)
    hist_text = render_histogram(h['all_cosines'])
    (out_dir / 'cosine_hist.txt').write_text(
        f'all top_cosines histogram (Stage 1 召回 candidate cosine):\n\n{hist_text}\n\n'
        f'max_cosine 分布:\n' +
        '\n'.join(f'  {k}: {v}' for k, v in sorted(h['max_dist'].items(), reverse=True)) +
        f'\n\nmin_cosine 分布:\n' +
        '\n'.join(f'  {k}: {v}' for k, v in sorted(h['min_dist'].items(), reverse=True)) +
        f'\n\ncandidate_count 分布:\n' +
        '\n'.join(f'  k={k}: {v} 次' for k, v in sorted(h['candidate_count_dist'].items())) +
        '\n', encoding='utf-8'
    )

    # 2. 漏合候选(visible cluster 两两高 cosine)
    print(f'2) 漏合候选(visible cluster 两两 cosine ≥ {args.threshold_merge})...',
          flush=True)
    pairs = all_pairs_cosine(conn, threshold_merge=args.threshold_merge)
    if pairs:
        with (out_dir / 'possible_missed_merges.csv').open('w') as f:
            w = csv.DictWriter(f, fieldnames=pairs[0].keys())
            w.writeheader()
            w.writerows(pairs)
    print(f'  → {len(pairs)} 对候选写入 possible_missed_merges.csv', flush=True)

    # 3. singleton 与 visible cluster 高相似(应合未合)
    print(f'3) singleton 漏合候选(USC=1 vs visible cosine ≥ {args.threshold_missed})...',
          flush=True)
    stuck = stuck_singletons_with_close_visible(conn, threshold=args.threshold_missed)
    if stuck:
        with (out_dir / 'singletons_should_merge.csv').open('w') as f:
            w = csv.DictWriter(f, fieldnames=stuck[0].keys())
            w.writeheader()
            w.writerows(stuck)
    print(f'  → {len(stuck)} 个 singleton 与 visible cluster 高相似', flush=True)

    # 4. 误合候选(成员作者多样,标题杂)
    print(f'4) 误合候选(visible cluster 含 ≥3 成员 + 多作者)...', flush=True)
    misc = potential_misclusters(conn, limit=args.limit)
    if misc:
        with (out_dir / 'possible_misclusters.csv').open('w') as f:
            w = csv.DictWriter(f, fieldnames=misc[0].keys())
            w.writeheader()
            w.writerows(misc)
    print(f'  → {len(misc)} 个候选写入 possible_misclusters.csv', flush=True)

    # 5. judge_log 整体分布
    print('5) cluster_judge_log selection_reason 分布...', flush=True)
    outcomes = judge_log_outcomes(conn)
    print('   ' + ' / '.join(f'{k}={v}' for k, v in outcomes.items()), flush=True)

    # 6. 综合 report.md
    cosines_arr = np.array(h['all_cosines']) if h['all_cosines'] else np.array([])
    summary_lines = [
        '# Cluster log 分析报告',
        f'> 生成于 2026-04-28,基于 `logs/cluster_events.jsonl` ({len(events)} 事件) + DB',
        '',
        '## 1. Stage 1 召回 cosine 分布',
        '',
        '```',
        hist_text,
        '```',
        '',
        f'数据点 {len(cosines_arr)} 个,中位数 {np.median(cosines_arr):.4f}' if len(cosines_arr) else '(无数据)',
        '',
        '**当前 BF-0428-3 阈值 = 0.75**。',
        f'- p25 以下命中率 = {(cosines_arr < 0.75).sum() / len(cosines_arr) * 100:.1f}% (这部分被新阈值过滤)' if len(cosines_arr) else '',
        f'- p75 以下命中率 = {(cosines_arr < 0.85).sum() / len(cosines_arr) * 100:.1f}% (0.85 是 merge 阈值)' if len(cosines_arr) else '',
        '',
        '## 2. cluster_judge_log selection_reason 分布',
        '',
        '| reason | count |',
        '|---|---|',
        *[f'| {k} | {v} |' for k, v in outcomes.items()],
        '',
        '## 3. 漏合候选(visible cluster 间高 cosine)',
        '',
        f'阈值 cosine ≥ {args.threshold_merge}: **{len(pairs)} 对**',
        '详见 `possible_missed_merges.csv`',
        '',
        '## 4. singleton 漏合候选',
        '',
        f'阈值 cosine ≥ {args.threshold_missed} (singleton vs visible): **{len(stuck)} 个**',
        '详见 `singletons_should_merge.csv`',
        '',
        '## 5. 误合候选(visible cluster 多作者抽查)',
        '',
        f'**{len(misc)} 个 cluster** ≥3 成员,详见 `possible_misclusters.csv`',
        '',
        '## 下一步建议',
        '',
        '1. 按 missed_merges.csv 抽 5 对人工判 → 若多数应合,降低 cosine_min 阈值或开 merge_detector',
        '2. 按 misclusters.csv 抽 5 个 visible cluster 看成员是否同事件 → 若有错合,反例加进 prompt 10',
        '3. singletons_should_merge.csv 看 LLM 漏判模式 → Stage 2 prompt shared_entity 是否过严',
        '',
    ]
    (out_dir / 'report.md').write_text('\n'.join(summary_lines), encoding='utf-8')
    print(f'\n✅ 报告写入 {out_dir}/report.md', flush=True)


if __name__ == '__main__':
    main()
