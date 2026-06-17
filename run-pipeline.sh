#!/usr/bin/env bash
# Run the dftracer annotation pipeline with Claude Code.
#
# Usage:
#   ./run-pipeline.sh <git-url> [ref] [smoke-test-cmd] [extra-cmake-flags]
#
# Examples:
#   ./run-pipeline.sh https://github.com/hpc/ior 4.0.0
#   ./run-pipeline.sh https://github.com/hpc/ior main "mpirun -n 2 ./src/ior -t 1m -b 4m"
#
# Requirements:
#   - claude CLI must be on PATH or discoverable in ~/.vscode-server
#   - dftracer MCP server will be auto-registered if not already present

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="$REPO_ROOT/venv"

# ── Resolve claude binary ──────────────────────────────────────────────────
CLAUDE_BIN="${CLAUDE_BIN:-}"
if [[ -z "$CLAUDE_BIN" ]]; then
    CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
fi
if [[ -z "$CLAUDE_BIN" ]]; then
    CLAUDE_BIN="$(find "$HOME/.vscode-server" -name "claude" -type f 2>/dev/null | head -1 || true)"
fi
if [[ -z "$CLAUDE_BIN" || ! -x "$CLAUDE_BIN" ]]; then
    echo "Error: claude CLI not found. Set CLAUDE_BIN or add it to PATH." >&2
    echo "  Hint: find it with: find ~/.vscode-server -name claude -type f" >&2
    exit 1
fi

# ── Activate venv so dftracer-mcp-server is on PATH ───────────────────────
if [[ -f "$VENV/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "$VENV/bin/activate"
fi

# ── Parse arguments ────────────────────────────────────────────────────────
APP_URL="${1:-}"
REF="${2:-main}"
SMOKE_CMD="${3:-}"
EXTRA_FLAGS="${4:-}"

if [[ -z "$APP_URL" ]]; then
    echo "Usage: $0 <git-url> [ref] [smoke-test-cmd] [extra-cmake-flags]"
    echo ""
    echo "Examples:"
    echo "  $0 https://github.com/hpc/ior 4.0.0"
    echo "  $0 https://github.com/hpc/ior main 'mpirun -n 2 ./src/ior -t 1m -b 4m'"
    exit 1
fi

# ── Ensure dftracer MCP server is registered ──────────────────────────────
if ! "$CLAUDE_BIN" mcp list 2>/dev/null | grep -q "dftracer"; then
    echo "Registering dftracer MCP server..."
    "$CLAUDE_BIN" mcp add dftracer "$VENV/bin/dftracer-mcp-server"
fi

# ── Build the initial message ──────────────────────────────────────────────
INITIAL_MSG="/dftracer-pipeline

APP_URL     = $APP_URL
REF         = $REF"

[[ -n "$SMOKE_CMD"   ]] && INITIAL_MSG+="
SMOKE_CMD   = $SMOKE_CMD"
[[ -n "$EXTRA_FLAGS" ]] && INITIAL_MSG+="
EXTRA_FLAGS = $EXTRA_FLAGS"

INITIAL_MSG+="

All inputs are already provided above — skip STEP 1 (Q1-Q4 questions) and go
directly to STEP 0.5 (fetch docs) then STEP 2 (setup). Run the full pipeline
autonomously. At STEP 6 (annotation report), show the report and wait for
my confirmation before proceeding to the trace run."

echo "╔════════════════════════════════════════════════════════╗"
echo "║  dftracer Annotation Pipeline — Claude Code            ║"
echo "╠════════════════════════════════════════════════════════╣"
printf "║  App:  %-49s ║\n" "$APP_URL"
printf "║  Ref:  %-49s ║\n" "$REF"
[[ -n "$SMOKE_CMD"   ]] && printf "║  Smoke: %-48s ║\n" "$SMOKE_CMD"
[[ -n "$EXTRA_FLAGS" ]] && printf "║  Flags: %-48s ║\n" "$EXTRA_FLAGS"
echo "╚════════════════════════════════════════════════════════╝"
echo ""
echo "Starting Claude Code (interactive — you will be asked to confirm at Step 6)..."
echo ""

# ── Launch Claude Code with the pipeline as the first message ─────────────
exec "$CLAUDE_BIN" "$INITIAL_MSG"
