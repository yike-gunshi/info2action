#!/bin/bash
# ============================================================
# cron_cluster.sh — 事件聚合 pipeline（v15.0 两阶段聚类）
#
# 跑 src/clustering/pipeline.py:
#   Stage 0: embed unembedded items（OpenRouter text-embedding-3-small, batch 16）
#   Stage 1: cosine vs 30 天活跃 cluster representative_vector
#   Stage 2: 0.70-0.85 边界 LLM 裁判
#   Stage 3: 加权均值 + τ=24h 衰减更新代表向量
#   Stage 4: doc_count ≥2 触发 summary 重写
#
# 前置：cron_ai_enrich.sh 已跑（item 有 summary，clustering 才能拿到 event_text）
# 用 flock 防并发。日志 /var/log/info2action-cluster.log。
# ============================================================
set -uo pipefail

export PATH="/root/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
PROXY_PORT="${PROXY_PORT:-7890}"
PROXY_NOTE="local proxy disabled"
if [ "${INFO2ACTION_DISABLE_LOCAL_PROXY:-0}" = "1" ]; then
  unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
  PROXY_NOTE="local proxy disabled by INFO2ACTION_DISABLE_LOCAL_PROXY=1"
elif command -v nc >/dev/null 2>&1 && nc -z 127.0.0.1 "$PROXY_PORT" >/dev/null 2>&1; then
  export http_proxy="http://127.0.0.1:${PROXY_PORT}"
  export https_proxy="http://127.0.0.1:${PROXY_PORT}"
  export HTTP_PROXY="http://127.0.0.1:${PROXY_PORT}"
  export HTTPS_PROXY="http://127.0.0.1:${PROXY_PORT}"
  PROXY_NOTE="local proxy enabled on 127.0.0.1:${PROXY_PORT}"
else
  unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
  PROXY_NOTE="local proxy 127.0.0.1:${PROXY_PORT} unavailable; running without forced proxy"
fi

BASE="${INFO2ACTION_BASE:-/opt/info2act-git}"
LOG="${INFO2ACTION_CLUSTER_LOG:-/var/log/info2action-cluster.log}"
LOCK="/tmp/info2action-cluster.lock"
RUN_ID="${INFO2ACTION_CLUSTER_RUN_ID:-}"

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
  echo "[$(now_iso)] previous cluster run still active; skip" >> "$LOG"
  exit 0
fi

cd "$BASE"
set -a
[ -f "$BASE/.env" ] && source "$BASE/.env"
set +a
PYTHON_CMD=(python3)
if command -v uv >/dev/null 2>&1 && [ -f "$BASE/requirements.txt" ]; then
  PYTHON_CMD=(uv run --with-requirements requirements.txt python)
fi

{
  echo ""
  echo "===== $(now_iso) cluster start ====="
  echo "-- proxy: ${PROXY_NOTE}"
  CLUSTER_ARGS=()
  if [ -n "$RUN_ID" ]; then
    CLUSTER_ARGS+=(--run-id "$RUN_ID")
    echo "-- run_id=${RUN_ID}"
  fi
  "${PYTHON_CMD[@]}" "$BASE/src/clustering/pipeline.py" "${CLUSTER_ARGS[@]}" 2>&1
  STATUS=$?
  if [ "$STATUS" -ne 0 ]; then
    echo "===== $(now_iso) cluster failed status=${STATUS} ====="
    exit "$STATUS"
  fi
  echo "===== $(now_iso) cluster done ====="
} >> "$LOG" 2>&1
