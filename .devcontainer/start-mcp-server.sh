#!/usr/bin/env bash
# Start the DFTracer MCP HTTP server in the background.
# Called from devcontainer postStartCommand; safe to re-run.
set -euo pipefail

PID_FILE="/tmp/dftracer-mcp.pid"
LOG_FILE="/tmp/dftracer-mcp.log"
PORT="${DFTRACER_MCP_PORT:-5000}"

# If already running, do nothing.
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "DFTracer MCP server already running (pid $(cat "$PID_FILE"))"
  exit 0
fi

nohup dftracer-mcp-server --port "$PORT" > "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
echo "DFTracer MCP server started (pid $!, port $PORT, log $LOG_FILE)"
