#!/bin/bash
# ============================================================
# cron_hourly_pipeline_light.sh — 每小时轻量完整链路（不抓小红书）
#
# 串联:
#   1. cron_fetch_light.sh  抓取 + ingest（明确跳过小红书）
#   2. cron_ai_enrich.sh    AI 总结 / 分类 / 打分
#   3. cron_cluster.sh      embedding + 聚合 + publish
#
# 各阶段脚本内部仍保留自己的 flock 和日志；这里再加一层总锁，
# 防止上一小时完整链路未结束时下一轮重入。
# ============================================================
set -uo pipefail

export PATH="/root/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

BASE="${INFO2ACTION_BASE:-/opt/info2act-git}"
LOG="${INFO2ACTION_HOURLY_PIPELINE_LOG:-/var/log/info2action-hourly-pipeline.log}"
LOCK="/tmp/info2action-hourly-pipeline.lock"
AI_LIMIT="${INFO2ACTION_AI_LIMIT:-200}"
RUN_ID_FILE="${INFO2ACTION_FETCH_RUN_ID_FILE:-$BASE/logs/latest-fetch-run-id}"

now_iso() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

acquire_lock() {
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$LOCK"
    flock -n 9
    return $?
  fi
  LOCK_DIR="${LOCK}.d"
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT
    return 0
  fi
  return 1
}

if ! acquire_lock; then
  echo "[$(now_iso)] previous hourly pipeline still active; skip" >> "$LOG"
  exit 0
fi

{
  echo ""
  echo "===== $(now_iso) hourly pipeline start (light/no XHS, ai_limit=${AI_LIMIT}) ====="

  if ! cd "$BASE"; then
    echo "ERROR: cannot cd to $BASE"
    exit 1
  fi

  echo "-- fetch light (no XHS)"
  /bin/bash "$BASE/ops/cron_fetch_light.sh"
  FETCH_STATUS=$?
  echo "   fetch status=${FETCH_STATUS}"
  if [ "$FETCH_STATUS" -ne 0 ]; then
    echo "===== $(now_iso) hourly pipeline abort fetch=${FETCH_STATUS} ====="
    exit "$FETCH_STATUS"
  fi
  FETCH_RUN_ID=""
  if [ -f "$RUN_ID_FILE" ]; then
    FETCH_RUN_ID=$(tr -dc '0-9' < "$RUN_ID_FILE")
  fi
  if [ -n "$FETCH_RUN_ID" ]; then
    echo "   fetch run_id=${FETCH_RUN_ID}"
  else
    echo "   fetch run_id unavailable; downstream stages will run unscoped"
  fi

  echo "-- ai enrich"
  INFO2ACTION_AI_LIMIT="$AI_LIMIT" INFO2ACTION_AI_RUN_ID="$FETCH_RUN_ID" /bin/bash "$BASE/ops/cron_ai_enrich.sh"
  AI_STATUS=$?
  echo "   ai enrich status=${AI_STATUS}"
  if [ "$AI_STATUS" -ne 0 ]; then
    echo "===== $(now_iso) hourly pipeline abort ai=${AI_STATUS} ====="
    exit "$AI_STATUS"
  fi

  echo "-- cluster"
  INFO2ACTION_CLUSTER_RUN_ID="$FETCH_RUN_ID" /bin/bash "$BASE/ops/cron_cluster.sh"
  CLUSTER_STATUS=$?
  echo "   cluster status=${CLUSTER_STATUS}"
  if [ "$CLUSTER_STATUS" -ne 0 ]; then
    echo "===== $(now_iso) hourly pipeline abort cluster=${CLUSTER_STATUS} ====="
    exit "$CLUSTER_STATUS"
  fi

  echo "-- remote sync"
  /bin/bash "$BASE/ops/remote_sync_after_pipeline.sh"
  REMOTE_SYNC_STATUS=$?
  echo "   remote sync status=${REMOTE_SYNC_STATUS}"
  if [ "$REMOTE_SYNC_STATUS" -ne 0 ]; then
    echo "===== $(now_iso) hourly pipeline abort remote_sync=${REMOTE_SYNC_STATUS} ====="
    exit "$REMOTE_SYNC_STATUS"
  fi

  echo "-- daily digest"
  PYTHON_CMD=(python3)
  if command -v uv >/dev/null 2>&1 && [ -f "$BASE/requirements.txt" ]; then
    PYTHON_CMD=(uv run --with-requirements requirements.txt python)
  fi
  set -a
  [ -f "$BASE/.env" ] && source "$BASE/.env"
  set +a
  "${PYTHON_CMD[@]}" -m src.daily_digest --mode auto
  DAILY_DIGEST_STATUS=$?
  echo "   daily digest status=${DAILY_DIGEST_STATUS}"

  echo "===== $(now_iso) hourly pipeline done fetch=${FETCH_STATUS} ai=${AI_STATUS} cluster=${CLUSTER_STATUS} remote_sync=${REMOTE_SYNC_STATUS} daily_digest=${DAILY_DIGEST_STATUS} ====="
} >> "$LOG" 2>&1

exit 0
