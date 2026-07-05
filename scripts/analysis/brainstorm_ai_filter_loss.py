"""一次性脚本: 评估「频道页套 AI 过滤」对各平台数据量的影响

口径:
- baseline     = 当前频道页过滤 (visibility + non-manual)
- strict       = baseline + ai_category IS NOT NULL AND != 'other' (当前推荐 tab 口径)
- upgraded     = baseline + (ai_category 有 OR ai_categories 非空)

run: set -a && source .env && set +a && uv run --with-requirements requirements.txt python scripts/brainstorm_ai_filter_loss.py
"""
import os
import psycopg

dsn = os.environ.get("SUPABASE_DB_DIRECT_URL") or os.environ["SUPABASE_DB_URL"]
schema = os.environ.get("SUPABASE_REMOTE_DB_SCHEMA", "public")

VISIBLE_GUARD = "(visible IS NULL OR visible = 1)"

SQL_BASELINE = f"""
    SELECT platform, COUNT(*) AS cnt
    FROM {schema}.items
    WHERE {VISIBLE_GUARD}
      AND platform IS DISTINCT FROM 'manual'
    GROUP BY platform
    ORDER BY cnt DESC
"""

SQL_STRICT = f"""
    SELECT platform, COUNT(*) AS cnt
    FROM {schema}.items
    WHERE {VISIBLE_GUARD}
      AND platform IS DISTINCT FROM 'manual'
      AND ai_category IS NOT NULL
      AND ai_category <> 'other'
    GROUP BY platform
    ORDER BY cnt DESC
"""

SQL_UPGRADED = f"""
    SELECT platform, COUNT(*) AS cnt
    FROM {schema}.items
    WHERE {VISIBLE_GUARD}
      AND platform IS DISTINCT FROM 'manual'
      AND (
            (ai_category IS NOT NULL AND ai_category <> 'other')
         OR (ai_categories IS NOT NULL
             AND ai_categories::text NOT IN ('[]', 'null', '"null"'))
      )
    GROUP BY platform
    ORDER BY cnt DESC
"""


def fetch_map(cur, sql):
    cur.execute(sql)
    return {row[0]: row[1] for row in cur.fetchall()}


def main():
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            base = fetch_map(cur, SQL_BASELINE)
            strict = fetch_map(cur, SQL_STRICT)
            upgrade = fetch_map(cur, SQL_UPGRADED)

    platforms = sorted(base.keys(), key=lambda p: -base[p])

    print(f"{'platform':<22} {'baseline':>10} {'strict':>10} {'strict%':>9} {'upgrade':>10} {'upgrade%':>10}")
    print("-" * 78)
    tot_b = tot_s = tot_u = 0
    for p in platforms:
        b = base.get(p, 0)
        s = strict.get(p, 0)
        u = upgrade.get(p, 0)
        tot_b += b
        tot_s += s
        tot_u += u
        s_pct = f"{(s / b * 100):.0f}%" if b else "-"
        u_pct = f"{(u / b * 100):.0f}%" if b else "-"
        print(f"{p:<22} {b:>10d} {s:>10d} {s_pct:>9} {u:>10d} {u_pct:>10}")
    print("-" * 78)
    s_pct_t = f"{(tot_s / tot_b * 100):.0f}%" if tot_b else "-"
    u_pct_t = f"{(tot_u / tot_b * 100):.0f}%" if tot_b else "-"
    print(f"{'TOTAL':<22} {tot_b:>10d} {tot_s:>10d} {s_pct_t:>9} {tot_u:>10d} {u_pct_t:>10}")


if __name__ == "__main__":
    main()
