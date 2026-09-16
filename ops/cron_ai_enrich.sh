#!/bin/bash
# ============================================================
# cron_ai_enrich.sh — bounded AI summary/scoring backstop
# ============================================================
set -euo pipefail

BASE="$(cd "$(dirname "$0")/.." && pwd)"
LIMIT="${INFO2ACTION_AI_LIMIT:-200}"
RUN_ID="${INFO2ACTION_AI_RUN_ID:-}"
LOG="${INFO2ACTION_AI_CRON_LOG:-/var/log/info2action-ai-enrich.log}"
LOCK="/tmp/info2action-ai-enrich.lock"

export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

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
  echo "[$(now_iso)] previous AI enrich run still active; skip" >> "$LOG"
  exit 0
fi

{
  echo ""
  echo "===== $(now_iso) AI enrich start limit=${LIMIT} ====="
  cd "$BASE"
  # 2026-04-29: 注入 .env(含 MINIMAX_API_KEY),让 enrich_items 读到 env var
  set -a
  [ -f "$BASE/.env" ] && source "$BASE/.env"
  set +a
  PYTHON_CMD=(python3)
  if command -v uv >/dev/null 2>&1 && [ -f "$BASE/requirements.txt" ]; then
    PYTHON_CMD=(uv run --with-requirements requirements.txt python)
  fi
  "${PYTHON_CMD[@]}" "$BASE/scripts/probe_ai_provider.py" --only-if-cooldown || true
  ENRICH_ARGS=(--limit "$LIMIT")
  if [ -n "$RUN_ID" ]; then
    ENRICH_ARGS+=(--run-id "$RUN_ID")
    echo "-- run_id=${RUN_ID}"
  fi
  "${PYTHON_CMD[@]}" "$BASE/src/enrich_items.py" "${ENRICH_ARGS[@]}"
  echo "===== $(now_iso) AI enrich done ====="
} >> "$LOG" 2>&1
