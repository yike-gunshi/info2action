#!/bin/bash
# v15.1 V2 人肉验收 - 7 平台抓取 30 条/平台
# 平台：bilibili / RSS / HackerNews / Reddit / GitHub / 公众号 / Twitter（跳过小红书）
# 临时改 config.json 减少 fetcher 工作量；跑完 SQL 裁剪每平台保留 30 条；恢复 config.json
set -uo pipefail

BASE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BASE"

echo "================================================"
echo "  QA fetch — 7 平台 × 30 条/平台"
echo "================================================"

# 1. 备份 config + 临时打补丁
cp config/config.json config/config.json.qa-bak
python3 - <<'PYEOF'
import json
cfg = json.load(open('config/config.json'))
# X: sources 注册表中的每个账号最多取 30 条
cfg['twitter']['user_posts_count'] = 30
# 小红书禁用
cfg['xiaohongshu']['enabled'] = False
# Bilibili: 仅 hot 30，关 UP / rank / search
cfg['bilibili']['hot_count'] = 30
cfg['bilibili']['rank_count'] = 0
cfg['bilibili']['up_list'] = []
cfg['bilibili']['videos_per_up'] = 0
cfg['bilibili']['search']['count'] = 0
# HN/Reddit/GitHub
cfg['hackernews']['count'] = 30
cfg['reddit']['count'] = 30
cfg['reddit']['subreddits'] = ['ClaudeAI']
cfg['github_trending']['count'] = 30
cfg['github_trending']['languages'] = ['']
# 公众号
cfg['lingowhale']['max_items'] = 30
json.dump(cfg, open('config/config.json','w'), ensure_ascii=False, indent=2)
print('[config] patched (qa-30 mode)')
PYEOF

# 安全网：脚本意外退出也恢复 config
trap 'echo "[trap] restoring config"; cp config/config.json.qa-bak config/config.json; rm config/config.json.qa-bak 2>/dev/null' EXIT

mkdir -p data/sources/twitter data/sources/bilibili data/sources/lingowhale

echo ""
echo "=== X（sources 注册表全量账号） ==="
python3 src/fetch_x_users.py 2>&1 | tail -10 || echo "  X registry fetch FAILED"

echo ""
echo "=== Bilibili (hot 30) ==="
python3 src/fetch_bili_hot.py 2>&1 | tail -3 || echo "  bili FAILED"

echo ""
echo "=== 公众号 (max 30) ==="
python3 src/fetch_lingowhale.py 2>&1 | tail -3 || echo "  lingowhale FAILED"

echo ""
echo "=== RSS / HN / Reddit / GitHub ==="
python3 src/fetch_feeds.py 2>&1 | tail -10 || echo "  fetch_feeds FAILED"

echo ""
echo "=== Ingest 入库 ==="
python3 src/ingest.py 2>&1 | tail -8

echo ""
echo "=== SQL 裁剪每平台 30 条（保留最新 fetched_at） ==="
python3 - <<'PYEOF'
import os, sys
sys.path.insert(0, 'src')
import db
conn = db.get_conn()
# 入库后按 platform 统计
print('[trim] before:')
for r in conn.execute("SELECT platform, COUNT(*) FROM items GROUP BY platform ORDER BY platform").fetchall():
    print(f'  {r[0]}: {r[1]}')
# 删除每 platform 超过 30 条的（按 fetched_at DESC，保留前 30）
conn.execute("""
DELETE FROM items WHERE id IN (
  SELECT id FROM (
    SELECT id, platform,
           ROW_NUMBER() OVER (PARTITION BY platform ORDER BY COALESCE(fetched_at, created_at) DESC) AS rn
    FROM items
  ) WHERE rn > 30
)
""")
conn.commit()
print()
print('[trim] after:')
for r in conn.execute("SELECT platform, COUNT(*) FROM items GROUP BY platform ORDER BY platform").fetchall():
    print(f'  {r[0]}: {r[1]}')
print()
total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
print(f'[trim] total items: {total}')
PYEOF

echo ""
echo "✅ fetch + ingest + trim 完成"
