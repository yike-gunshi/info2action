#!/bin/bash
# v15.1 V2 第二轮人肉验收 — 7 平台 ×100 条/平台（小红书跳过）
# 数据混合策略：保留现有 items + clusters，新数据走 enrich + V2 pipeline 进入现有池
# 临时改 config.json 抓取限额；ingest 后 SQL trim 每平台保留 ≤100；恢复 config
set -uo pipefail

BASE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BASE"

echo "================================================"
echo "  QA fetch — 7 平台 ×100 条/平台（小红书跳过，数据追加）"
echo "================================================"

cp config/config.json config/config.json.qa-bak
python3 - <<'PYEOF'
import json
cfg = json.load(open('config/config.json'))
cfg['twitter']['user_posts_count'] = 100
cfg['xiaohongshu']['enabled'] = False
cfg['bilibili']['hot_count'] = 100
cfg['bilibili']['rank_count'] = 0
cfg['bilibili']['up_list'] = []
cfg['bilibili']['videos_per_up'] = 0
cfg['bilibili']['search']['count'] = 0
cfg['hackernews']['count'] = 100
cfg['reddit']['count'] = 100
cfg['reddit']['subreddits'] = ['ClaudeAI']
cfg['github_trending']['count'] = 100
cfg['github_trending']['languages'] = ['']
cfg['lingowhale']['max_items'] = 100
json.dump(cfg, open('config/config.json','w'), ensure_ascii=False, indent=2)
print('[config] patched (qa-100 mode)')
PYEOF

trap 'echo "[trap] restoring config"; cp config/config.json.qa-bak config/config.json; rm config/config.json.qa-bak 2>/dev/null' EXIT

mkdir -p data/sources/twitter data/sources/bilibili data/sources/lingowhale

echo ""
echo "=== X（sources 注册表全量账号，每账号最多 100 条） ==="
python3 src/fetch_x_users.py 2>&1 | tail -10 || echo "  X registry fetch FAILED"

echo ""
echo "=== Bilibili (hot ≤100) ==="
python3 src/fetch_bili_hot.py 2>&1 | tail -3 || echo "  bili FAILED"

echo ""
echo "=== 公众号 (max 100) ==="
python3 src/fetch_lingowhale.py 2>&1 | tail -3 || echo "  lingowhale FAILED"

echo ""
echo "=== RSS / HN / Reddit / GitHub ==="
python3 src/fetch_feeds.py 2>&1 | tail -10 || echo "  fetch_feeds FAILED"

echo ""
echo "=== Ingest (ASR enabled, 数据追加到现有 items) ==="
python3 src/ingest.py 2>&1 | tail -10

echo ""
echo "=== SQL trim 每平台保留最新 100 条（按 fetched_at DESC） ==="
python3 - <<'PYEOF'
import os, sys
sys.path.insert(0, 'src')
import db
conn = db.get_conn()
print('[trim] before:')
for r in conn.execute("SELECT platform, COUNT(*) FROM items GROUP BY platform ORDER BY platform").fetchall():
    print(f'  {r[0]}: {r[1]}')
# 删除每 platform 超过 100 条的（按 fetched_at DESC 保留前 100）
conn.execute("""
DELETE FROM items WHERE id IN (
  SELECT id FROM (
    SELECT id, ROW_NUMBER() OVER (PARTITION BY platform ORDER BY COALESCE(fetched_at, created_at) DESC) AS rn
    FROM items
  ) WHERE rn > 100
)
""")
conn.commit()
print('[trim] after:')
for r in conn.execute("SELECT platform, COUNT(*) FROM items GROUP BY platform ORDER BY platform").fetchall():
    print(f'  {r[0]}: {r[1]}')
total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
print(f'[trim] total items: {total}')
PYEOF

echo ""
echo "✅ fetch + ingest + trim 完成（ASR 在 ingest 中已同步触发；后续 enrich + V2 pipeline）"
