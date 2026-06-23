#!/usr/bin/env bash

set -euo pipefail

# Detect script directory
if command -v readlink >/dev/null 2>&1 && readlink -f "$0" >/dev/null 2>&1; then
  SCRIPT_PATH="$(readlink -f "$0")"
elif command -v python3 >/dev/null 2>&1; then
  SCRIPT_PATH="$(python3 -c "import os; print(os.path.realpath('$0'))")"
else
  echo "ERROR: Cannot resolve script path" >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! "$ROOT_DIR" = /* ]]; then
  ROOT_DIR="$(cd "$ROOT_DIR" && pwd)"
fi
SCRIPT_DIR="$(dirname "$SCRIPT_PATH")"
PROJECT_DIR=$(dirname "$(dirname "$SCRIPT_PATH")")
VENV_DIR="${PROJECT_DIR}/.venv"
GOOSE_CONFIG="$VENV_DIR/.config/goose"
GOOSE_SHARE="${VENV_DIR}/share/goose"
GOOSE_STATE="${VENV_DIR}/state/goose"

mkdir -p "$GOOSE_CONFIG" "$GOOSE_SHARE" "$GOOSE_STATE"

source "$ROOT_DIR/.env"

# Whitelist of environment variables to pass through
ENV_VARS=(
  # API Keys
  OPENAI_BASE_URL
  OPENAI_API_KEY
  OPENAI_MODEL
  LIVAI_BASE_URL
  LIVAI_MODEL
  LIVAI_API_KEY
  ANTHROPIC_API_KEY
  GOOSE_API_KEY
  GEMINI_API_KEY
  COHERE_API_KEY

  # SSH/Git
  SSH_AUTH_SOCK
  SSH_AGENT_LAUNCHER
  GIT_AUTHOR_NAME
  GIT_AUTHOR_EMAIL
  GIT_COMMITTER_NAME
  GIT_COMMITTER_EMAIL

  # User/Session
  USER
  LOGNAME
  HOME
  SHELL
  TERM
  COLORTERM
  LANG
  LC_ALL

  # XDG
  XDG_CONFIG_HOME
  XDG_CACHE_HOME
  XDG_DATA_HOME
  XDG_RUNTIME_DIR

  # Editor
  EDITOR
  VISUAL

  # Development
  GOPATH
  GOROOT
  NODE_PATH
  PYTHONPATH
  JAVA_HOME
  CARGO_HOME
)

# NOTE: CUSTOM READONLY PATH
EXTRA_PATH=${EXTRA_PATH:-""}
# NOTE: CUSTOM WRITABLE PATH
EXTRA_WPATH=${EXTRA_WPATH:-""}

# Build env args
ENV_ARGS=()
for var in "${ENV_VARS[@]}"; do
  if [[ -n "${!var:-}" ]]; then
    ENV_ARGS+=(--setenv "$var" "${!var}")
  fi
done

# Isolation level: base (EPERM), lockdown (KILL), or none
# Backward compatibility: if GOOSE_ISOLATE_NETWORK is set, treat as lockdown
if [[ -n "${GOOSE_ISOLATE_NETWORK:-}" ]]; then
  GOOSE_ISOLATION_LEVEL="lockdown"
else
  GOOSE_ISOLATION_LEVEL=${GOOSE_ISOLATION_LEVEL:-"base"}
fi

NETWORK_FLAGS=()
SECCOMP_BPF=""
SECCOMP_FD=""


# Verify bwrap and goose exist
if ! command -v bwrap >/dev/null 2>&1; then
  echo "ERROR: bubblewrap not installed" >&2
  exit 1
fi

EXTRA_PATH_BWRAP=()
# NOTE: Process EXTRA_PATH as colon-separated list like PATH
if [[ -n $EXTRA_PATH ]]; then
  IFS=':' read -ra LPATHS <<<"$EXTRA_PATH"
  for path in "${LPATHS[@]}"; do
    if [[ -n "$path" && -e "$path" ]]; then
      EXTRA_PATH_BWRAP+=("--ro-bind" "$path" "$path")
    fi
  done
fi

EXTRA_WPATH_BWRAP=()
# NOTE:  Process EXTRA_WPATH as colon-separated list like PATH
if [[ -n $EXTRA_WPATH ]]; then
  IFS=':' read -ra LPATHS <<<"$EXTRA_WPATH"
  for path in "${LPATHS[@]}"; do
    if [[ -n "$path" && -e "$path" ]]; then
      EXTRA_WPATH_BWRAP+=("--bind" "$path" "$path")
    fi
  done
fi

if ! command -v goose >/dev/null 2>&1; then
  echo "ERROR: goose not found" >&2
  exit 1
fi

# Check seccomp filter
if [[ -n "$SECCOMP_BPF" ]]; then
  if [[ ! -f "$SECCOMP_BPF" ]]; then
    echo "ERROR: Seccomp filter not found: $SECCOMP_BPF" >&2
    echo "BPF files should be in: $SECCOMP_BPF_DIR/" >&2
    exit 1
  fi
fi

echo "Starting Goose in sandbox..."
echo "   Project: $PROJECT_DIR"
echo "   Config: $GOOSE_CONFIG"
echo "   Extra PATH: $EXTRA_PATH"
echo "   Isolation: $GOOSE_ISOLATION_LEVEL"

# Open seccomp filter as FD 3
SECCOMP_ARGS=()
if [[ -n "$SECCOMP_BPF" ]]; then
  exec 3<"$SECCOMP_BPF"
  SECCOMP_ARGS=(--seccomp 3)
fi

exec bwrap \
  "${NETWORK_FLAGS[@]}" \
  "${SECCOMP_ARGS[@]}" \
  --new-session \
  --die-with-parent \
  --clearenv \
  --setenv PATH "/usr/local/bin:/usr/bin:/bin" \
  --setenv HOME "$HOME" \
  --setenv TERM "${TERM:-xterm-256color}" \
  --setenv LANG "${LANG:-en_US.UTF-8}" \
  --proc /proc \
  --dev /dev \
  --tmpfs /tmp \
  --bind "$GOOSE_CONFIG" "$HOME/.config/goose" \
  --bind "$GOOSE_SHARE" "$HOME/.local/share/goose" \
  --bind "$GOOSE_STATE" "$HOME/.local/state/goose" \
  --ro-bind "$SCRIPT_DIR" "$SCRIPT_DIR" \
  "${ENV_ARGS[@]}" \
  "${EXTRA_PATH_BWRAP[@]}" \
  "${EXTRA_WPATH_BWRAP[@]}" \
  --ro-bind /usr /usr \
  --ro-bind /bin /bin \
  --ro-bind /lib /lib \
  --ro-bind /lib64 /lib64 \
  --ro-bind /etc /etc \
  --bind "$PROJECT_DIR" "$HOME/dftracer-agents" \
  --chdir "$HOME/dftracer-agents" \
${VENV_DIR}/bin/goose "$@"

