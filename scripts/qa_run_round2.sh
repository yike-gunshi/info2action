#!/bin/bash
# v15.1 V2 第二轮 - 全链路串联：fetch_100 → enrich (5 workers, batch=3) → V2 pipeline
# 顺序跑，输出统一 log /tmp/qa_round2.log
# 注：ASR 启用（DOUBAO_ASR_API_KEY 不 unset），此轮模拟真实抓取
set -uo pipefail

BASE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BASE"

LOG=/tmp/qa_round2.log
exec > >(tee "$LOG") 2>&1

echo "================================================"
echo "  Round 2 START at $(date +%H:%M:%S)"
echo "================================================"

# 1. Fetch + Ingest
echo ""
echo "##### Phase 1: fetch + ingest + trim (ASR enabled) #####"
bash scripts/qa_fetch_100.sh

echo ""
echo "##### Phase 1 DONE at $(date +%H:%M:%S) #####"

# 2. Enrich (5 workers parallel)
echo ""
echo "##### Phase 2: enrich (5 workers, batch=3) #####"
PYTHONUNBUFFERED=1 python3 src/enrich_items.py --limit 1000 --batch-size 3 --workers 5

echo ""
echo "##### Phase 2 DONE at $(date +%H:%M:%S) #####"

# 3. V2 clustering pipeline
echo ""
echo "##### Phase 3: V2 clustering (Stage 0-4) #####"
PYTHONUNBUFFERED=1 python3 -m src.clustering.pipeline

echo ""
echo "##### Phase 3 DONE at $(date +%H:%M:%S) #####"

echo ""
echo "================================================"
echo "  Round 2 DONE at $(date +%H:%M:%S)"
echo "================================================"
