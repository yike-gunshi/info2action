#!/usr/bin/env python3
"""ENG-0710 读模型降维瘦身:一次性清理已停止物化的维度存量行。

背景:代码侧已停止向 section_subcategory / group_source 两维度物化
(tests/test_info_readmodel_slim_dimensions.py),但 delta 刷新只增改
delta 命中的行,不会主动清老维度——存量 ~70,796 行 scope_items + ~696 行
scopes 需要一次性删除,之后这两类视图由 live 查询接管。

禁全量重建:1GB Micro 上 refresh_info_read_model() 必伤线上
(memory supabase-oom-feed-blank-incident 实测),所以用分批 DELETE。

用法:
  python3 scripts/cleanup_readmodel_cut_dimensions.py            # dry-run(只读统计)
  python3 scripts/cleanup_readmodel_cut_dimensions.py --apply    # 真删(分批)

连接:环境变量 SUPABASE_DB_URL(pooler 5432 session 口)。
"""
import argparse
import os
import sys
import time

try:
    import psycopg
except ImportError:
    print("需要 psycopg:pip install 'psycopg[binary]'", file=sys.stderr)
    sys.exit(1)

CUT_DIMENSIONS = ("section_subcategory", "group_source")
BATCH_ROWS = 5000
BATCH_PAUSE_SEC = 1.0
STATEMENT_TIMEOUT_MS = 30000


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="真删;缺省只做 dry-run 统计")
    parser.add_argument("--schema", default="remote_poc")
    args = parser.parse_args()

    url = os.environ.get("SUPABASE_DB_URL")
    if not url:
        print("缺 SUPABASE_DB_URL 环境变量", file=sys.stderr)
        return 1
    schema = args.schema

    conn = psycopg.connect(url, connect_timeout=15, autocommit=True)
    conn.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT_MS}ms'")
    cur = conn.cursor()

    # info_scope_items 无 dimension 列(维度嵌在 scope_key),经 info_scopes 定位
    cur.execute(
        f"""SELECT sc.dimension, count(*)
              FROM {schema}.info_scope_items si
              JOIN {schema}.info_scopes sc
                ON sc.version_id = si.version_id AND sc.scope_key = si.scope_key
             WHERE sc.dimension = ANY(%s) GROUP BY sc.dimension""",
        (list(CUT_DIMENSIONS),),
    )
    item_counts = dict(cur.fetchall())
    cur.execute(
        f"SELECT dimension, count(*) FROM {schema}.info_scopes"
        f" WHERE dimension = ANY(%s) GROUP BY dimension",
        (list(CUT_DIMENSIONS),),
    )
    scope_counts = dict(cur.fetchall())
    total_items = sum(item_counts.values())
    total_scopes = sum(scope_counts.values())
    print(f"待清理 info_scope_items: {item_counts} (合计 {total_items})")
    print(f"待清理 info_scopes:      {scope_counts} (合计 {total_scopes})")

    if not args.apply:
        print("dry-run 结束(加 --apply 执行删除)")
        conn.close()
        return 0

    deleted_total = 0
    t0 = time.time()
    while True:
        cur.execute(
            f"""DELETE FROM {schema}.info_scope_items
                 WHERE ctid IN (
                       SELECT si.ctid
                         FROM {schema}.info_scope_items si
                         JOIN {schema}.info_scopes sc
                           ON sc.version_id = si.version_id AND sc.scope_key = si.scope_key
                        WHERE sc.dimension = ANY(%s)
                        LIMIT {BATCH_ROWS}
                 )""",
            (list(CUT_DIMENSIONS),),
        )
        deleted = cur.rowcount
        deleted_total += deleted
        print(f"  info_scope_items 批删 {deleted} (累计 {deleted_total}/{total_items})")
        if deleted < BATCH_ROWS:
            break
        time.sleep(BATCH_PAUSE_SEC)

    cur.execute(
        f"DELETE FROM {schema}.info_scopes WHERE dimension = ANY(%s)",
        (list(CUT_DIMENSIONS),),
    )
    print(f"  info_scopes 删除 {cur.rowcount}")

    cur.execute(f"ANALYZE {schema}.info_scope_items")
    cur.execute(f"ANALYZE {schema}.info_scopes")
    print(f"完成:删除 scope_items {deleted_total} 行,耗时 {time.time()-t0:.0f}s(死行回收交给 autovacuum)")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
