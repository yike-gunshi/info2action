#!/bin/bash
# ============================================================
# Optional remote sync after a local SQLite pipeline run.
#
# Default is opt-in. Set INFO2ACTION_REMOTE_SYNC_AFTER_PIPELINE=1 to sync a
# recent full-field window into Supabase after fetch/enrich/cluster finishes.
# ============================================================
set -uo pipefail

BASE="${INFO2ACTION_BASE:-$(cd "$(dirname "$0")/.." && pwd)}"
LOG="${INFO2ACTION_REMOTE_SYNC_LOG:-/var/log/info2action-remote-sync.log}"
LOCK="/tmp/info2action-remote-sync.lock"

now_iso() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

ENABLED="${INFO2ACTION_REMOTE_SYNC_AFTER_PIPELINE:-0}"
case "$ENABLED" in
  1|true|TRUE|yes|YES|on|ON) ;;
  *)
    echo "[$(now_iso)] remote sync not enabled; set INFO2ACTION_REMOTE_SYNC_AFTER_PIPELINE=1" >> "$LOG"
    exit 0
    ;;
esac

if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK"
  if ! flock -n 9; then
    echo "[$(now_iso)] previous remote sync still active; skip" >> "$LOG"
    exit 0
  fi
else
  echo "[$(now_iso)] flock not found; running remote sync without process lock" >> "$LOG"
fi

cd "$BASE"
set -a
[ -f "$BASE/.env" ] && source "$BASE/.env"
set +a

DB_PATH="${INFO2ACTION_REMOTE_SYNC_DB:-$BASE/data/feed.db}"
HOURS="${INFO2ACTION_REMOTE_SYNC_HOURS:-6}"
MAX_ITEMS="${INFO2ACTION_REMOTE_SYNC_MAX_ITEMS:-5000}"
JUDGE_LOG_LIMIT="${INFO2ACTION_REMOTE_SYNC_JUDGE_LOG_LIMIT:-5000}"
BATCH_SIZE="${INFO2ACTION_REMOTE_SYNC_BATCH_SIZE:-500}"
MAX_DB_MIB="${INFO2ACTION_REMOTE_SYNC_MAX_DB_MIB:-2048}"
SAMPLE_NAME="${INFO2ACTION_REMOTE_SYNC_SAMPLE_NAME:-incremental-$(date -u +%Y%m%dT%H%M%SZ)}"
if command -v uv >/dev/null 2>&1; then
  PY_RUN=(uv run --with-requirements requirements.txt python)
else
  PY_RUN=(python3)
fi

{
  echo ""
  echo "===== $(now_iso) remote incremental sync start hours=${HOURS} max_items=${MAX_ITEMS} ====="
  "${PY_RUN[@]}" "$BASE/scripts/sync_sqlite_to_supabase_poc.py" \
    --db "$DB_PATH" \
    --incremental \
    --confirm-incremental-sync \
    --bulk-copy \
    --incremental-hours "$HOURS" \
    --incremental-max-recent-items "$MAX_ITEMS" \
    --incremental-judge-log-limit "$JUDGE_LOG_LIMIT" \
    --batch-size "$BATCH_SIZE" \
    --max-db-mib "$MAX_DB_MIB" \
    --sample-name "$SAMPLE_NAME"
  STATUS=$?
  echo "===== $(now_iso) remote incremental sync done status=${STATUS} ====="
  exit "$STATUS"
} >> "$LOG" 2>&1
