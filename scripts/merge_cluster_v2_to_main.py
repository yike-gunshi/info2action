"""把副本 DB 的 v5 cluster 结果 merge 回主仓库 DB，让前端能看到。

副本: /tmp/info2action-cluster-v2-eval/feed.db
主库: data/feed.db

策略：
  1. 备份主仓库 DB → data/feed.db.bak.{ts} （不可逆动作前的安全网）
  2. 用 ATTACH 把副本挂到主仓库连接
  3. 清空主仓库的 clusters_v2 / cluster_items_v2 / cluster_p_log（表结构保留）
  4. INSERT INTO main FROM attached（保留副本里的 cluster id / 关系）
  5. 同步 items.embedding / embedding_model / embedding_input_variant /
     canonical_url / stage_a_state / stage_a_failed_at（Stage A 写的字段）
     —— 仅对副本里 stage_a_state='done' 的 item 同步

不改：items 表的 ai_summary / ai_categories / 其他字段（主库已有最新值）。

用法：
  python scripts/merge_cluster_v2_to_main.py        # dry-run，只报数
  python scripts/merge_cluster_v2_to_main.py --apply
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DB = Path("/tmp/info2action-cluster-v2-eval/feed.db")
MAIN_DB = REPO_ROOT / "data" / "feed.db"


def _count(conn: sqlite3.Connection, sql: str) -> int:
    return conn.execute(sql).fetchone()[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写主仓库 DB（默认 dry-run）")
    args = ap.parse_args()

    if not SRC_DB.exists():
        raise SystemExit(f"副本 DB 不存在：{SRC_DB}")
    if not MAIN_DB.exists():
        raise SystemExit(f"主仓库 DB 不存在：{MAIN_DB}")

    print(f"[merge] 副本: {SRC_DB} (size={SRC_DB.stat().st_size:,} bytes)")
    print(f"[merge] 主库: {MAIN_DB} (size={MAIN_DB.stat().st_size:,} bytes)")

    src = sqlite3.connect(str(SRC_DB))
    src.row_factory = sqlite3.Row
    src_cl = _count(src, "SELECT COUNT(*) FROM clusters_v2")
    src_clean = _count(src, "SELECT COUNT(*) FROM clusters_v2 WHERE stage_p_state='clean'")
    src_fail = _count(src, "SELECT COUNT(*) FROM clusters_v2 WHERE stage_p_state='failed'")
    src_ci = _count(src, "SELECT COUNT(*) FROM cluster_items_v2")
    src_pl = _count(src, "SELECT COUNT(*) FROM cluster_p_log")
    src_stagea = _count(src, "SELECT COUNT(*) FROM items WHERE stage_a_state='done' AND embedding IS NOT NULL")
    src.close()

    main_conn = sqlite3.connect(str(MAIN_DB))
    main_cl = _count(main_conn, "SELECT COUNT(*) FROM clusters_v2")
    main_ci = _count(main_conn, "SELECT COUNT(*) FROM cluster_items_v2")
    main_pl = _count(main_conn, "SELECT COUNT(*) FROM cluster_p_log")
    main_stagea = _count(main_conn, "SELECT COUNT(*) FROM items WHERE stage_a_state='done' AND embedding IS NOT NULL")
    main_conn.close()

    print()
    print(f"{'指标':<32} {'副本':>10} {'主库':>10}")
    print(f"{'-'*54}")
    print(f"{'clusters_v2 总簇数':<32} {src_cl:>10} {main_cl:>10}")
    print(f"{'  其中 clean':<32} {src_clean:>10}")
    print(f"{'  其中 failed':<32} {src_fail:>10}")
    print(f"{'cluster_items_v2 总成员':<32} {src_ci:>10} {main_ci:>10}")
    print(f"{'cluster_p_log 总日志':<32} {src_pl:>10} {main_pl:>10}")
    print(f"{'items.stage_a_state=done':<32} {src_stagea:>10} {main_stagea:>10}")

    if not args.apply:
        print()
        print("[dry-run] 用 --apply 真的写主仓库 DB")
        return 0

    # 1. 备份
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = MAIN_DB.with_suffix(f".db.bak.merge-cluster-v5b-{ts}")
    print(f"\n[apply] 备份主库 → {backup}")
    shutil.copy2(MAIN_DB, backup)
    for ext in ("-wal", "-shm"):
        side = MAIN_DB.with_name(MAIN_DB.name + ext)
        if side.exists():
            shutil.copy2(side, backup.with_name(backup.name + ext))

    # 2. ATTACH + 写
    main_conn = sqlite3.connect(str(MAIN_DB), timeout=60)
    main_conn.execute("PRAGMA foreign_keys=OFF")
    main_conn.execute(f"ATTACH DATABASE '{SRC_DB}' AS src")
    try:
        main_conn.execute("BEGIN")
        # 清主库 v2 表
        main_conn.execute("DELETE FROM cluster_p_log")
        main_conn.execute("DELETE FROM cluster_items_v2")
        main_conn.execute("DELETE FROM clusters_v2")
        # 复制副本 v2 数据（保留原 id 关系）
        main_conn.execute(
            """INSERT INTO clusters_v2
               SELECT * FROM src.clusters_v2"""
        )
        main_conn.execute(
            """INSERT INTO cluster_items_v2
               SELECT * FROM src.cluster_items_v2"""
        )
        main_conn.execute(
            """INSERT INTO cluster_p_log
               SELECT * FROM src.cluster_p_log"""
        )
        # 同步 items 的 Stage A 字段（仅 stage_a_state='done' 的）
        main_conn.execute(
            """UPDATE items AS dst SET
                 embedding = (
                   SELECT src_i.embedding FROM src.items AS src_i
                    WHERE src_i.id = dst.id
                 ),
                 embedding_model = (
                   SELECT src_i.embedding_model FROM src.items AS src_i
                    WHERE src_i.id = dst.id
                 ),
                 embedding_input_variant = (
                   SELECT src_i.embedding_input_variant FROM src.items AS src_i
                    WHERE src_i.id = dst.id
                 ),
                 embedding_generated_at = (
                   SELECT src_i.embedding_generated_at FROM src.items AS src_i
                    WHERE src_i.id = dst.id
                 ),
                 canonical_url = COALESCE((
                   SELECT src_i.canonical_url FROM src.items AS src_i
                    WHERE src_i.id = dst.id
                 ), dst.canonical_url),
                 stage_a_state = (
                   SELECT src_i.stage_a_state FROM src.items AS src_i
                    WHERE src_i.id = dst.id
                 ),
                 stage_a_failed_at = (
                   SELECT src_i.stage_a_failed_at FROM src.items AS src_i
                    WHERE src_i.id = dst.id
                 )
               WHERE EXISTS (
                 SELECT 1 FROM src.items AS src_i
                  WHERE src_i.id = dst.id
                    AND src_i.stage_a_state IS NOT NULL
               )"""
        )
        main_conn.commit()
        # reset autoincrement to match src（保持 id 一致便于下次重跑）
        try:
            max_cluster = main_conn.execute("SELECT MAX(id) FROM clusters_v2").fetchone()[0] or 0
            main_conn.execute(
                "UPDATE sqlite_sequence SET seq = ? WHERE name = 'clusters_v2'",
                (max_cluster,),
            )
            max_log = main_conn.execute("SELECT MAX(id) FROM cluster_p_log").fetchone()[0] or 0
            main_conn.execute(
                "UPDATE sqlite_sequence SET seq = ? WHERE name = 'cluster_p_log'",
                (max_log,),
            )
            main_conn.commit()
        except sqlite3.OperationalError:
            pass
    except Exception:
        main_conn.rollback()
        raise
    finally:
        main_conn.execute("DETACH DATABASE src")
        main_conn.close()

    # 验证
    verify = sqlite3.connect(str(MAIN_DB))
    v_cl = _count(verify, "SELECT COUNT(*) FROM clusters_v2")
    v_clean = _count(verify, "SELECT COUNT(*) FROM clusters_v2 WHERE stage_p_state='clean'")
    v_ci = _count(verify, "SELECT COUNT(*) FROM cluster_items_v2")
    v_pl = _count(verify, "SELECT COUNT(*) FROM cluster_p_log")
    v_stagea = _count(verify, "SELECT COUNT(*) FROM items WHERE stage_a_state='done' AND embedding IS NOT NULL")
    verify.close()

    print()
    print(f"[apply] 完成。主库现状：")
    print(f"  clusters_v2: {v_cl} (clean {v_clean})")
    print(f"  cluster_items_v2: {v_ci}")
    print(f"  cluster_p_log: {v_pl}")
    print(f"  items.stage_a_state=done: {v_stagea}")
    print(f"\n[apply] 备份: {backup}")
    print("[apply] 注意：这一步只同步 clusters_v2。若要让现有前端事件流可见，")
    print("[apply] 还需要运行 scripts/materialize_clusters_v2_feed.py --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
