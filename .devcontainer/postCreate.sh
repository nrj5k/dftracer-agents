#!/usr/bin/env bash
set -euo pipefail

bash .devcontainer/link-goose-config.sh

python3 -m pip install --upgrade pip
python3 -m pip install -e .

if command -v goose >/dev/null 2>&1; then
  goose --version || true
fi

echo "Devcontainer setup complete. OLLAMA_HOST=${OLLAMA_HOST:-unset} GOOSE_CONTEXT_LIMIT=${GOOSE_CONTEXT_LIMIT:-unset}"
