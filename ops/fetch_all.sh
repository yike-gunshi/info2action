#!/bin/bash
# ============================================================
# 一键全量抓取脚本 - 读取 config.json
# Usage: ./fetch_all.sh [--skip-xhs-details] [--skip-bili-covers] [--raw-only] [--run-id N]
# ============================================================
set -uo pipefail
# Note: not using set -e to allow graceful error handling
export PYTHONUNBUFFERED=1
PYTHON_BIN="${PYTHON_BIN:-python3}"

BASE="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="$BASE/config/config.json"
DATA_DIR="${INFO2ACTION_DATA_DIR:-$BASE/data}"
SOURCE_DIR="${INFO2ACTION_SOURCE_DIR:-$DATA_DIR/sources}"
DATE=$(date +%Y-%m-%d)

echo "================================================"
echo "  信息雷达 - 全量抓取 ($DATE)"
echo "================================================"
echo ""

# Parse flags
SKIP_XHS_DETAILS=false
SKIP_BILI_COVERS=false
RAW_ONLY=false
RUN_ID=""
NEXT_IS_RUN_ID=false
for arg in "$@"; do
  if [ "$NEXT_IS_RUN_ID" = true ]; then
    RUN_ID="$arg"
    NEXT_IS_RUN_ID=false
    continue
  fi
  case $arg in
    --skip-xhs-details) SKIP_XHS_DETAILS=true ;;
    --skip-bili-covers) SKIP_BILI_COVERS=true ;;
    --raw-only) RAW_ONLY=true ;;
    --run-id) NEXT_IS_RUN_ID=true ;;
  esac
done

# ============================================================
# 1. TWITTER（v16.0: keyword search 全下线，只留 following / for-you / bookmarks）
# ============================================================
echo "📱 [1/5] Twitter..."
mkdir -p "$SOURCE_DIR/twitter"

# Following feed
echo "  Following feed..."
twitter feed -t following -n $("$PYTHON_BIN" -c "import json; print(json.load(open('$CONFIG'))['twitter']['following_count'])") \
  --json -o "$SOURCE_DIR/twitter/1-following-feed.json" >/dev/null 2>&1
echo "  ✅ Following"

# For-You feed
echo "  For-You feed..."
twitter feed -t for-you -n $("$PYTHON_BIN" -c "import json; print(json.load(open('$CONFIG'))['twitter']['for_you_count'])") \
  --json -o "$SOURCE_DIR/twitter/2-for-you-feed.json" >/dev/null 2>&1
echo "  ✅ For-You"

# Bookmarks
echo "  Bookmarks..."
twitter bookmarks -n 20 --json -o "$SOURCE_DIR/twitter/4-bookmarks.json" >/dev/null 2>&1 || true
echo "  ✅ Bookmarks"

echo ""

# ============================================================
# 2. BILIBILI（v16.0: 只留 hot/rank/watch-later，删 search；UP 主订阅推 TODO CH-FUTURE-1）
# ============================================================
echo "📺 [2/5] B站 (hot + rank + 稍后再看)..."
mkdir -p "$SOURCE_DIR/bilibili"

# 热门 + 排行（绕开 bili hot/rank CLI 输出无 pic 字段的问题，直接调 B 站开放 API）
"$PYTHON_BIN" "$BASE/src/fetch_bili_hot.py" 2>&1 | tail -2

# 稍后再看（绕开 bilibili-cli v0.6.2 的 watch-later bug，直接调 B 站 API）
"$PYTHON_BIN" "$BASE/src/fetch_bili_watch_later.py" 2>&1 | tail -2

# ============================================================
# 3. LINGOWHALE (公众号)
# ============================================================
echo ""
echo "🐋 [3/5] 公众号订阅..."
"$PYTHON_BIN" "$BASE/src/fetch_lingowhale.py" 2>&1 || true

# v16.0: 小红书 section 全部下线（抓取 + 前端 section 隐藏 + 收藏一并停）

# ============================================================
# 4. RSS / HACKER NEWS / REDDIT / GITHUB (Trending + Awesome 仓库)
# ============================================================
echo ""
echo "📡 [4/5] RSS / HN / Reddit / GitHub (trending + awesome)..."
"$PYTHON_BIN" "$BASE/src/fetch_feeds.py" 2>&1 || true

# ============================================================
# 5. WAYTOAGI (Feishu Wiki)
# ============================================================
echo ""
echo "🔖 [5/5] WayToAGI..."
"$PYTHON_BIN" "$BASE/src/fetch_waytoagi.py" 2>&1 || true

echo ""
echo "================================================"
echo "  抓取完成! 开始入库..."
echo "================================================"
echo ""

if [ "$RAW_ONLY" = true ]; then
  echo "raw-only 模式：仅完成平台抓取，入库 / AI 总结 / 事件聚合交给后端 run 编排。"
  exit 0
fi

# Ingest into SQLite
cd "$BASE"
if [ -n "$RUN_ID" ]; then
  "$PYTHON_BIN" "$BASE/src/ingest.py" --run-id "$RUN_ID"
else
  "$PYTHON_BIN" "$BASE/src/ingest.py"
fi

echo ""
echo "================================================"
echo "  AI 统一理解..."
echo "================================================"
echo ""
if [ -n "$RUN_ID" ]; then
  "$PYTHON_BIN" -u "$BASE/src/enrich_items.py" --limit 800 --run-id "$RUN_ID" --run-items-scope inserted 2>&1 || true
else
  "$PYTHON_BIN" -u "$BASE/src/enrich_items.py" --limit 800 2>&1 || true
fi

echo ""
echo "================================================"
echo "  事件聚合 (v15.0 两阶段聚类)..."
echo "================================================"
echo ""
# R9.1 冷启动期 event_aggregation_ready=false 时 pipeline 照跑(写入 DB),
# 只是前端 /api/feed/events 返 enabled=false 降级 TickerBar。失败不阻塞批次。
if [ -n "$RUN_ID" ]; then
  "$PYTHON_BIN" "$BASE/src/clustering/pipeline.py" --run-id "$RUN_ID" --run-items-scope inserted 2>&1 || true
else
  "$PYTHON_BIN" "$BASE/src/clustering/pipeline.py" 2>&1 || true
fi

echo ""
echo "================================================"
echo "  生成 AI 简报..."
echo "================================================"
echo ""
"$PYTHON_BIN" "$BASE/src/generate_briefing.py" 2>&1 || true

echo ""
echo "================================================"
echo "  增量兴趣扫描..."
echo "================================================"
echo ""
"$PYTHON_BIN" "$BASE/src/interest_engine.py" 2>&1 || true

echo ""
echo "================================================"
echo "  行动点生成..."
echo "================================================"
echo ""
AUTO_ACTIONS=$("$PYTHON_BIN" -c "import json; print(json.load(open('$CONFIG')).get('actions', {}).get('auto_generate_enabled', False))")
if [ "$AUTO_ACTIONS" = "True" ]; then
  "$PYTHON_BIN" "$BASE/src/generate_actions.py" 2>&1 || true
else
  echo "自动行动点生成已关闭（config.actions.auto_generate_enabled=false）"
fi

echo ""
echo "================================================"
echo "  行动点去重..."
echo "================================================"
echo ""
AUTO_ACTION_DEDUP=$("$PYTHON_BIN" -c "import json; print(json.load(open('$CONFIG')).get('actions', {}).get('auto_dedup_enabled', False))")
if [ "$AUTO_ACTION_DEDUP" = "True" ]; then
  "$PYTHON_BIN" "$BASE/src/dedup_actions.py" --apply --threshold 0.25 2>&1 || true
else
  echo "自动行动点去重已关闭（config.actions.auto_dedup_enabled=false）"
fi

echo ""
echo "================================================"
echo "  远程增量同步..."
echo "================================================"
echo ""
/bin/bash "$BASE/ops/remote_sync_after_pipeline.sh" 2>&1 || true

echo ""
echo "✅ 全部完成!"
