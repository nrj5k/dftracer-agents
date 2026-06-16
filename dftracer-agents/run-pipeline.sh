#!/usr/bin/env bash
# Start an interactive dftracer annotation pipeline session.
# Usage: ./run-pipeline.sh
#
# Launches goose session and injects the startup prompt automatically,
# then hands control to the user for interactive Q&A.

set -euo pipefail

SKILL_PATH="/workspaces/dftracer-agents/.agents/skills/dftracer-pipeline/SKILL.md"

if [[ ! -f "$SKILL_PATH" ]]; then
  echo "ERROR: Pipeline skill not found at $SKILL_PATH" >&2
  exit 1
fi

STARTUP="Load and follow the dftracer annotation pipeline skill from: $SKILL_PATH

Read the full SKILL.md file now, then immediately start Step 1 by asking me:
  'What is the Git URL of the application you want to annotate?'"

echo "Starting dftracer annotation pipeline..."
echo "The agent will ask you 4 questions, then run the full pipeline."
echo "Press Ctrl+C at any time to stop."
echo ""

# Inject the startup message then let the TTY take over for interactive input.
# goose session reads from stdin; /dev/tty provides the user's keyboard input.
{ printf '%s\n' "$STARTUP"; cat /dev/tty; } | goose session
