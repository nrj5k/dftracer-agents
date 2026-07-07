#!/bin/bash
# Reproduces the annotated Montage build used in this session.
# Run from the session workspace root (montage/20260706_062459/).
set -e

WS="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

export LD_LIBRARY_PATH="/opt/cray/pe/cce/20.0.0/cce/x86_64/lib:/opt/cray/pe/cce/20.0.0/cce/x86_64/lib/default64:/usr/lib64:${LD_LIBRARY_PATH}"
export CPATH="$WS/venv/lib/python3.13/site-packages/dftracer/include"

# Shim gcc/cc via PATH instead of `make CC=...` -- some vendored Montage
# Makefiles embed flags directly in CC (e.g. `CC = gcc -g -fPIC -I .`),
# and overriding CC on the command line discards those flags.
mkdir -p "$WS/tmp/binshim"
cp "$(dirname "${BASH_SOURCE[0]}")/gcc_shim.sh" "$WS/tmp/binshim/gcc"
cp "$(dirname "${BASH_SOURCE[0]}")/cc_shim.sh" "$WS/tmp/binshim/cc"
chmod +x "$WS/tmp/binshim/gcc" "$WS/tmp/binshim/cc"
export PATH="$WS/tmp/binshim:$PATH"

cd "$WS/annotated"
make -j1        # -j1: top-level directory recipes are not jobserver-safe
mkdir -p "$WS/install_ann/bin"
make install
