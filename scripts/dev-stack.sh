#!/usr/bin/env bash
# Start/stop/status for the local info2action dev stack.
#
# Goals:
# - main keeps the familiar backend/frontend ports: 8080/3567
# - every worktree gets stable, deterministic ports
# - existing tmux sessions/listeners are reused instead of spawning duplicates

set -euo pipefail

ACTION="${1:-start}"

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

need git
need tmux
need lsof
need python3

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

MAIN_ROOT="$(git worktree list --porcelain | awk '
  /^worktree / { wt = substr($0, 10) }
  /^branch refs\/heads\/main$/ { print wt; exit }
')"
if [[ -z "${MAIN_ROOT:-}" ]]; then
  MAIN_ROOT="$ROOT"
fi

BRANCH="$(git symbolic-ref --short -q HEAD || git rev-parse --short HEAD)"
HASH="$(printf '%s' "$ROOT" | cksum | awk '{print $1}')"

if [[ "$ROOT" == "$MAIN_ROOT" && "$BRANCH" == "main" ]]; then
  RAW_SLUG="main"
  DEFAULT_BACKEND_PORT=8080
  DEFAULT_FRONTEND_PORT=3567
else
  RAW_SLUG="$(basename "$ROOT")-$BRANCH"
  DEFAULT_BACKEND_PORT=$((8100 + HASH % 700))
  DEFAULT_FRONTEND_PORT=$((3600 + HASH % 300))
fi

SLUG="$(printf '%s' "$RAW_SLUG" | tr '/ +' '---' | tr -cd 'A-Za-z0-9_.-')"
if [[ ${#SLUG} -gt 42 ]]; then
  SLUG="${SLUG:0:34}-${HASH:0:7}"
fi

BACKEND_SESSION="info2action-${SLUG}-backend"
FRONTEND_SESSION="info2action-${SLUG}-frontend"
STATE_FILE="$ROOT/.devserver.json"

BACKEND_PORT="$DEFAULT_BACKEND_PORT"
FRONTEND_PORT="$DEFAULT_FRONTEND_PORT"

if [[ -f "$STATE_FILE" ]]; then
  eval "$(python3 - "$STATE_FILE" "$BACKEND_PORT" "$FRONTEND_PORT" <<'PY'
import json
import sys

path, default_backend, default_frontend = sys.argv[1:4]
try:
    data = json.load(open(path))
except Exception:
    data = {}
backend = int(data.get("backend_port") or default_backend)
frontend = int(data.get("frontend_port") or default_frontend)
print(f"BACKEND_PORT={backend}")
print(f"FRONTEND_PORT={frontend}")
PY
)"
fi

listener_pids() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | sort -u || true
}

pid_cwd() {
  lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1
}

port_has_listener() {
  [[ -n "$(listener_pids "$1")" ]]
}

port_owned_by_root() {
  local port="$1"
  local pid cwd
  while IFS= read -r pid; do
    [[ -z "$pid" ]] && continue
    cwd="$(pid_cwd "$pid" || true)"
    if [[ "$cwd" == "$ROOT"* ]]; then
      return 0
    fi
  done < <(listener_pids "$port")
  return 1
}

choose_port() {
  local port="$1"
  local attempts=0
  while (( attempts < 200 )); do
    if ! port_has_listener "$port" || port_owned_by_root "$port"; then
      echo "$port"
      return 0
    fi
    port=$((port + 1))
    attempts=$((attempts + 1))
  done
  echo "Could not find a free port near $1" >&2
  exit 1
}

BACKEND_PORT="$(choose_port "$BACKEND_PORT")"
FRONTEND_PORT="$(choose_port "$FRONTEND_PORT")"

write_state() {
  python3 - "$STATE_FILE" "$ROOT" "$BRANCH" "$SLUG" "$BACKEND_PORT" "$FRONTEND_PORT" <<'PY'
import json
import sys

path, root, branch, slug, backend, frontend = sys.argv[1:7]
data = {
    "root": root,
    "branch": branch,
    "slug": slug,
    "backend_port": int(backend),
    "frontend_port": int(frontend),
    "backend_url": f"http://127.0.0.1:{backend}",
    "frontend_url": f"http://127.0.0.1:{frontend}",
}
with open(path, "w") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
}

tmux_has() {
  tmux has-session -t "$1" 2>/dev/null
}

kill_root_listener_on_port() {
  local port="$1"
  local pid cwd
  while IFS= read -r pid; do
    [[ -z "$pid" ]] && continue
    cwd="$(pid_cwd "$pid" || true)"
    if [[ "$cwd" == "$ROOT"* ]]; then
      kill "$pid" 2>/dev/null || true
    fi
  done < <(listener_pids "$port")
}

wait_for_port() {
  local port="$1"
  local tries=30
  while (( tries > 0 )); do
    if port_owned_by_root "$port"; then
      return 0
    fi
    sleep 0.5
    tries=$((tries - 1))
  done
  return 1
}

env_source_snippet() {
  if [[ -f "$ROOT/.env" ]]; then
    printf 'set -a && source .env && set +a'
  elif [[ -f "$MAIN_ROOT/.env" ]]; then
    printf 'set -a && source "%s/.env" && set +a' "$MAIN_ROOT"
  else
    printf 'true'
  fi
}

start_backend() {
  if port_owned_by_root "$BACKEND_PORT"; then
    echo "Backend already listening on http://127.0.0.1:${BACKEND_PORT}"
    return 0
  fi
  if tmux_has "$BACKEND_SESSION"; then
    echo "Restarting stale backend tmux session: $BACKEND_SESSION"
    tmux kill-session -t "$BACKEND_SESSION"
  fi

  local env_cmd
  env_cmd="$(env_source_snippet)"
  local python_runner
  if command -v uv >/dev/null 2>&1 && [[ -f "$ROOT/requirements.txt" ]]; then
    python_runner="uv run --with-requirements requirements.txt python"
  else
    python_runner="python3"
  fi
  local cmd
  cmd="cd \"$ROOT\" && mkdir -p logs && $env_cmd && PORT=$BACKEND_PORT $python_runner -m uvicorn src.app:app --host 127.0.0.1 --port $BACKEND_PORT --workers 1 2>&1 | tee logs/dev-backend-$BACKEND_PORT.log"
  tmux new-session -d -s "$BACKEND_SESSION" "$cmd"
  wait_for_port "$BACKEND_PORT" || {
    echo "Backend did not start on port $BACKEND_PORT. Recent log:" >&2
    tail -80 "$ROOT/logs/dev-backend-$BACKEND_PORT.log" 2>/dev/null || true
    exit 1
  }
  echo "Backend started: http://127.0.0.1:${BACKEND_PORT} ($BACKEND_SESSION)"
}

start_frontend() {
  if port_owned_by_root "$FRONTEND_PORT"; then
    echo "Frontend already listening on http://127.0.0.1:${FRONTEND_PORT}"
    return 0
  fi
  if tmux_has "$FRONTEND_SESSION"; then
    echo "Restarting stale frontend tmux session: $FRONTEND_SESSION"
    tmux kill-session -t "$FRONTEND_SESSION"
  fi

  local cmd
  cmd="cd \"$ROOT/frontend-react\" && mkdir -p ../logs && VITE_API_TARGET=http://127.0.0.1:$BACKEND_PORT npm run dev -- --host 127.0.0.1 --port $FRONTEND_PORT --strictPort 2>&1 | tee ../logs/dev-frontend-$FRONTEND_PORT.log"
  tmux new-session -d -s "$FRONTEND_SESSION" "$cmd"
  wait_for_port "$FRONTEND_PORT" || {
    echo "Frontend did not start on port $FRONTEND_PORT. Recent log:" >&2
    tail -80 "$ROOT/logs/dev-frontend-$FRONTEND_PORT.log" 2>/dev/null || true
    exit 1
  }
  echo "Frontend started: http://127.0.0.1:${FRONTEND_PORT} ($FRONTEND_SESSION)"
}

status() {
  write_state
  echo "Root:      $ROOT"
  echo "Branch:    $BRANCH"
  echo "Slug:      $SLUG"
  echo "Backend:   http://127.0.0.1:${BACKEND_PORT}  session=$BACKEND_SESSION"
  echo "Frontend:  http://127.0.0.1:${FRONTEND_PORT}  session=$FRONTEND_SESSION"
  echo ""
  echo "Listeners:"
  for port in "$BACKEND_PORT" "$FRONTEND_PORT"; do
    if port_has_listener "$port"; then
      while IFS= read -r pid; do
        [[ -z "$pid" ]] && continue
        echo "  :$port pid=$pid cwd=$(pid_cwd "$pid")"
      done < <(listener_pids "$port")
    else
      echo "  :$port not listening"
    fi
  done
  echo ""
  echo "Tmux:"
  tmux list-sessions 2>/dev/null | grep -E "^(${BACKEND_SESSION}|${FRONTEND_SESSION}):" || true
}

stop() {
  tmux kill-session -t "$BACKEND_SESSION" 2>/dev/null || true
  tmux kill-session -t "$FRONTEND_SESSION" 2>/dev/null || true
  kill_root_listener_on_port "$BACKEND_PORT"
  kill_root_listener_on_port "$FRONTEND_PORT"
  echo "Stopped dev stack for $ROOT"
}

case "$ACTION" in
  start)
    write_state
    start_backend
    start_frontend
    status
    ;;
  status)
    status
    ;;
  stop)
    stop
    ;;
  restart)
    stop
    sleep 1
    write_state
    start_backend
    start_frontend
    status
    ;;
  *)
    echo "Usage: $0 [start|status|stop|restart]" >&2
    exit 2
    ;;
esac
