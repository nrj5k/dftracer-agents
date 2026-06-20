#!/usr/bin/env bash
# Start (or restart) the DFTracer MCP HTTP server.
# PID is stored in /tmp/dftracer-agents.pid so this script can kill and
# relaunch the server to pick up code changes.
#
# Usage:
#   ./start-mcp-server.sh             # start / restart
#   ./start-mcp-server.sh --service both --port 5000
#   ./start-mcp-server.sh stop        # stop only

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="$REPO_ROOT/venv"
PID_FILE="/tmp/dftracer-agents.pid"
LOG_FILE="/tmp/mcp_server.log"
PORT="${MCP_PORT:-5000}"
SERVICE="${MCP_SERVICE:-both}"

# ── parse args ─────────────────────────────────────────────────────────────
STOP_ONLY=false
EXTRA_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    stop) STOP_ONLY=true; shift ;;
    --port) PORT="$2"; shift 2 ;;
    --service) SERVICE="$2"; shift 2 ;;
    *) EXTRA_ARGS+=("$1"); shift ;;
  esac
done

# ── stop existing server ────────────────────────────────────────────────────
if [[ -f "$PID_FILE" ]]; then
  OLD_PID=$(cat "$PID_FILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "[mcp] Stopping existing server (PID $OLD_PID)…"
    kill "$OLD_PID" 2>/dev/null || true
    sleep 1
    # Force-kill if still running
    kill -9 "$OLD_PID" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
fi

# Also kill any stale server that owns the port
STALE=$(ss -tlnp 2>/dev/null | awk -F'pid=' "/0\.0\.0\.0:$PORT/{split(\$2,a,\",\"); print a[1]}" | head -1)
if [[ -n "$STALE" ]]; then
  echo "[mcp] Killing stale process $STALE on port $PORT…"
  kill "$STALE" 2>/dev/null || true
  sleep 1
fi

$STOP_ONLY && { echo "[mcp] Server stopped."; exit 0; }

# ── resolve the server command ──────────────────────────────────────────────
# Prefer the venv-installed entry point; fall back to python -m.
if [[ -x "$VENV/bin/dftracer-mcp-server" ]]; then
  SERVER_CMD=("$VENV/bin/dftracer-mcp-server")
else
  PYTHON="$VENV/bin/python"
  if [[ ! -x "$PYTHON" ]]; then
    PYTHON=$(command -v python3 || command -v python)
  fi
  SERVER_CMD=("$PYTHON" -m dftracer_agents.mcp_server)
fi

echo "[mcp] Starting DFTracer MCP server (service=$SERVICE port=$PORT)…"
"${SERVER_CMD[@]}" \
  --service "$SERVICE" \
  --port "$PORT" \
  "${EXTRA_ARGS[@]}" \
  >> "$LOG_FILE" 2>&1 &

SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"

# Wait briefly and confirm the server started
sleep 3
if ! kill -0 "$SERVER_PID" 2>/dev/null; then
  echo "[mcp] Server exited unexpectedly. Last log lines:"
  tail -20 "$LOG_FILE"
  exit 1
fi

echo "[mcp] Server running (PID $SERVER_PID) → http://localhost:$PORT/mcp"
echo "[mcp] Logs: $LOG_FILE"
echo "[mcp] PID:  $PID_FILE"
