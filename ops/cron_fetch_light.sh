#!/bin/bash
# ============================================================
# cron_fetch_light.sh — 轻档抓取（不抓小红书；v16.0 起删 keyword search）
#
# 抓什么：
#   1. X sources 注册表全量账号
#   2. B站 hot + watch-later
#   3. 公众号订阅
#   4. RSS / HN / Reddit / GitHub (trending + awesome)
#   5. WayToAGI（飞书 wiki）
#   → ingest.py 入库
#
# 不做：
#   - 小红书（v16.0 完全下线，section 也隐藏）
#   - keyword search（v16.0 删 Twitter / B 站 / GitHub 全部 search:keyword 调用）
#   - enrich_items（交给 cron_ai_enrich.sh）
#   - clustering pipeline（交给 cron_cluster.sh）
#
# 用 flock 防并发。日志 /var/log/info2action-fetch-light.log。
# ============================================================
set -uo pipefail

export PATH="/root/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
PROXY_PORT="${PROXY_PORT:-}"
if [ -z "$PROXY_PORT" ] && [ -n "${http_proxy:-}" ]; then
  PROXY_PORT=$(echo "$http_proxy" | grep -oE ':[0-9]+$' | tr -d ':')
fi
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
CONFIG="$BASE/config/config.json"
LOG="${INFO2ACTION_FETCH_LIGHT_LOG:-/var/log/info2action-fetch-light.log}"
LOCK="/tmp/info2action-fetch-light.lock"
RUN_ID_FILE="${INFO2ACTION_FETCH_RUN_ID_FILE:-$BASE/logs/latest-fetch-run-id}"
INGEST_EXTRA_ARGS="${INFO2ACTION_INGEST_EXTRA_ARGS:---skip-image-download}"

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

run_with_timeout() {
  local seconds="$1"
  shift
  "$@" &
  local pid=$!
  local elapsed=0
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$elapsed" -ge "$seconds" ]; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      return 124
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  wait "$pid"
}

if ! acquire_lock; then
  echo "[$(now_iso)] previous light fetch still active; skip" >> "$LOG"
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
  echo "===== $(now_iso) light fetch start (no XHS) ====="
  echo "-- proxy: ${PROXY_NOTE}"
  rm -f "$RUN_ID_FILE"

  mkdir -p "$BASE/data/sources/twitter" "$BASE/data/sources/bilibili"

  # 1. X sources 注册表全量账号
  echo "-- X registry users"
  "${PYTHON_CMD[@]}" "$BASE/src/fetch_x_users.py" 2>&1 || true

  # 2. B站 hot + watch-later
  echo "-- bilibili hot"
  "${PYTHON_CMD[@]}" "$BASE/src/fetch_bili_hot.py" 2>&1 | tail -2 || true
  echo "-- bilibili watch-later"
  "${PYTHON_CMD[@]}" "$BASE/src/fetch_bili_watch_later.py" 2>&1 | tail -2 || true

  # 3. 公众号
  echo "-- lingowhale"
  LINGOWHALE_TIMEOUT="${INFO2ACTION_LINGOWHALE_TIMEOUT_SEC:-180}"
  if ! run_with_timeout "$LINGOWHALE_TIMEOUT" "${PYTHON_CMD[@]}" "$BASE/src/fetch_lingowhale.py" 2>&1 | tail -3; then
    echo "   lingowhale skipped/failed after ${LINGOWHALE_TIMEOUT}s"
  fi

  # 4. RSS / HN / Reddit / GitHub (trending + awesome)
  echo "-- feeds (rss/hn/reddit/github)"
  "${PYTHON_CMD[@]}" "$BASE/src/fetch_feeds.py" 2>&1 | tail -3 || true

  # 5. WayToAGI(2026-04-29: ECS 缺 lark-cli,失败静默不阻塞;本地 dev 仍打 log)
  echo "-- waytoagi"
  if [ -n "${LARK_CLI:-}" ] || [ -x /root/claudecode_workspace/工具/lark-cli/lark-cli ] || command -v lark-cli >/dev/null 2>&1; then
    WAYTOAGI_TIMEOUT="${INFO2ACTION_WAYTOAGI_TIMEOUT_SEC:-60}"
    if ! run_with_timeout "$WAYTOAGI_TIMEOUT" "${PYTHON_CMD[@]}" "$BASE/src/fetch_waytoagi.py" 2>&1; then
      echo "   waytoagi skipped/failed after ${WAYTOAGI_TIMEOUT}s"
    fi
  else
    echo "   skip (lark-cli not on PATH)"
  fi

  # 6. ingest 入库
  echo "-- ingest"
  INGEST_TMP=$(mktemp)
  # shellcheck disable=SC2206
  INGEST_ARGS=($INGEST_EXTRA_ARGS)
  "${PYTHON_CMD[@]}" "$BASE/src/ingest.py" "${INGEST_ARGS[@]}" 2>&1 | tee "$INGEST_TMP"
  INGEST_STATUS=${PIPESTATUS[0]}
  RUN_ID=$(grep -m 1 -Eo 'run #[0-9]+' "$INGEST_TMP" | grep -Eo '[0-9]+' || true)
  rm -f "$INGEST_TMP"
  if [ -n "$RUN_ID" ]; then
    mkdir -p "$(dirname "$RUN_ID_FILE")"
    printf "%s\n" "$RUN_ID" > "$RUN_ID_FILE"
    echo "   fetch_run_id=${RUN_ID}"
  fi
  if [ "$INGEST_STATUS" -ne 0 ]; then
    echo "   ingest status=${INGEST_STATUS}"
    exit "$INGEST_STATUS"
  fi

  echo "===== $(now_iso) light fetch done ====="
} >> "$LOG" 2>&1
