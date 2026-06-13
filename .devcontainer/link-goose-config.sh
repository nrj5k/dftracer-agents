#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_GOOSE_DIR="${PWD}/.goose"
mkdir -p "${WORKSPACE_GOOSE_DIR}"
mkdir -p "${HOME}/.config"

# Keep Goose config versioned in the repo and exposed via standard user paths.
for target in "${HOME}/.config/goose" "${HOME}/.goose"; do
  if [ -L "${target}" ]; then
    rm -f "${target}"
  elif [ -e "${target}" ]; then
    mv "${target}" "${target}.bak.$(date +%Y%m%d%H%M%S)"
  fi
  ln -s "${WORKSPACE_GOOSE_DIR}" "${target}"
done
