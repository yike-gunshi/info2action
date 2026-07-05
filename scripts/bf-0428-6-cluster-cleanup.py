"""BF-0428-6 cluster_items 历史误合数据修复 (Moxt 从 #125 移除).

Background: BF-0428-3 改的是 Stage 1+2 的判合逻辑(对新进 doc 生效),
不会反向清理已 merge 的成员。Moxt(lw_69ef4426...) 是 cluster #125 的
第一个成员(创建 singleton),后续 5 个 HappyHorse items + 1 twitter
被错合进来。本脚本:

1. 把 Moxt 从 cluster #125 移除
2. 给 Moxt 创建独立 singleton cluster (USC=1, invisible)
3. 重算 cluster #125 的 doc_count / unique_source_count /
   representative_vector / first_doc_at / last_doc_at
4. 重新触发 cluster #125 的 Stage 4 summary regen (因为成员变了)
5. 同根扫描:其他 BF-0428-3 反例 cluster 是否需要相同 cleanup

Idempotent: 跑两次第二次不会重复迁移(检查 Moxt 是否已脱离 #125)。

Usage:
    set -a && source .env && set +a
    python3 -u scripts/bf-0428-6-cluster-cleanup.py [--apply | --dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(REPO_ROOT, 'src'))
sys.path.insert(0, os.path.join(REPO_ROOT, 'src/clustering'))
os.chdir(REPO_ROOT)

import db
import vector_utils as vu
from pipeline import _finalize_cluster_state, _create_singleton, _TAU_HOURS_DEFAULT
from summary_writer import regenerate_and_swap


# Cleanup map — cluster_id → list of item_ids to remove
# Source: BF-0428-3 reviewer doc + cluster_judge_log analysis
CLEANUP_MAP: dict[int, list[str]] = {
    125: ['lw_69ef4426a3ad7425d54406fa'],  # Moxt vs HappyHorse
    # 可扩展:其他确认错合的 cluster
    # 95: ['<emirates twitter id>'],  # SpaceX vs Emirates(若 user 确认要清)
}


def load_api_creds() -> tuple[str, str | None, str]:
    with open('config/config.json') as f:
        cfg = json.load(f)
    ai = cfg.get('ai_summary', {})
    api_key = os.environ.get('MINIMAX_API_KEY') or ai.get('api_key', '')
    api_base = ai.get('api_base') or 'https://api.minimaxi.com'
    model = ai.get('model', 'MiniMax-Text-01')
    return api_key, api_base, model


def remove_item_from_cluster(conn, cluster_id: int, item_id: str, *, dry_run: bool):
    row = conn.execute(
        'SELECT 1 FROM cluster_items WHERE cluster_id = ? AND item_id = ?',
        (cluster_id, item_id),
    ).fetchone()
    if not row:
        print(f'  SKIP: item {item_id} not in cluster {cluster_id} (already removed?)')
        return False
    if dry_run:
        print(f'  [dry-run] would DELETE FROM cluster_items WHERE cluster_id={cluster_id} AND item_id={item_id}')
        return True
    conn.execute(
        'DELETE FROM cluster_items WHERE cluster_id = ? AND item_id = ?',
        (cluster_id, item_id),
    )
    # Clear items.cluster_id back to NULL so re-clustering picks it up
    conn.execute('UPDATE items SET cluster_id = NULL WHERE id = ?', (item_id,))
    print(f'  ✓ removed {item_id} from cluster {cluster_id}')
    return True


def create_singleton_for_orphan(conn, item_id: str, *, dry_run: bool) -> int | None:
    """Re-create a singleton cluster for the orphaned item (using its embedding)."""
    row = conn.execute(
        '''SELECT id, embedding, COALESCE(published_at, fetched_at) AS first_at, url
           FROM items WHERE id = ?''',
        (item_id,),
    ).fetchone()
    if not row:
        print(f'  WARN: item {item_id} missing in items table')
        return None
    if row['embedding'] is None:
        print(f'  WARN: item {item_id} has no embedding — cannot create singleton')
        return None
    vec = vu.unpack_blob(row['embedding'])
    if vec is None:
        print(f'  WARN: item {item_id} embedding blob unpack to None')
        return None
    if dry_run:
        print(f'  [dry-run] would create singleton for {item_id}')
        return -1
    new_cid = _create_singleton(
        conn, row['id'], vec, row['first_at'] or '2026-04-27T00:00:00',
    )
    print(f'  ✓ created singleton cluster {new_cid} for {item_id}')
    return new_cid


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', help='execute (default: dry-run)')
    args = parser.parse_args()
    dry_run = not args.apply

    print(f'BF-0428-6 cluster cleanup ({"APPLY" if args.apply else "DRY-RUN"})')
    print('=' * 60)

    api_key, api_base, model = load_api_creds()
    conn = db.get_conn()

    cleanup_log: list[dict] = []

    for cid, item_ids in CLEANUP_MAP.items():
        print(f'\nCluster {cid} cleanup:')
        before_members = conn.execute(
            'SELECT COUNT(*) FROM cluster_items WHERE cluster_id = ?', (cid,)
        ).fetchone()[0]
        print(f'  members before: {before_members}')

        affected = 0
        for iid in item_ids:
            if remove_item_from_cluster(conn, cid, iid, dry_run=dry_run):
                affected += 1
                # Create new singleton for the orphan
                new_cid = create_singleton_for_orphan(conn, iid, dry_run=dry_run)
                cleanup_log.append({
                    'orig_cluster': cid,
                    'item_id': iid,
                    'new_singleton_cluster': new_cid,
                })

        if affected == 0:
            print(f'  → no changes for cluster {cid}')
            continue

        if dry_run:
            print(f'  [dry-run] would recompute cluster {cid} state + regen summary')
            continue

        # Recompute cluster state (doc_count / USC / representative_vector / first/last_doc_at)
        _finalize_cluster_state(conn, cid, tau_hours=_TAU_HOURS_DEFAULT)
        after = conn.execute(
            '''SELECT doc_count, unique_source_count,
                      (SELECT COUNT(*) FROM cluster_items WHERE cluster_id=?) AS member_n
               FROM clusters WHERE id = ?''', (cid, cid),
        ).fetchone()
        print(f'  ✓ recomputed: doc_count={after["doc_count"]} '
              f'USC={after["unique_source_count"]} members={after["member_n"]}')

        # Re-trigger Stage 4 summary regen
        print(f'  regenerating Stage 4 summary for cluster {cid} ...', flush=True)
        ok = regenerate_and_swap(
            conn, cid, api_key=api_key, api_base=api_base, model=model,
        )
        conn.commit()
        print(f'  → summary regen {"OK" if ok else "FAIL"}')

    if not dry_run:
        conn.commit()

    print('\n' + '=' * 60)
    print('Summary:')
    for entry in cleanup_log:
        print(f'  cluster {entry["orig_cluster"]} → '
              f'item {entry["item_id"]} migrated to '
              f'singleton {entry["new_singleton_cluster"]}')
    if dry_run:
        print('\n  (dry-run; rerun with --apply to commit)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
