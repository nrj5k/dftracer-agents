#!/bin/bash
set -e
WS="$PROJECT_ROOT/workspaces/scaffold/20260709_081340"
source "$WS/scripts/env.sh"

export DFTRACER_ENABLE=1
export DFTRACER_INIT=FUNCTION
export DFTRACER_INC_METADATA=1
export DFTRACER_DATA_DIR=all
export DFTRACER_LOG_FILE="$WS/baseline/traces/raw/baseline-"

CFG="$WS/baseline/baseline_config.yml"
cd "$WS/baseline"

# Phase A: benchmark's volumegen reads pre-existing fractal instances. At
# problem_scale=5 point_num=128, so they must exist under .../instances/np128/.
# The libomp+MKL OpenMP clash (see env.sh) aborts at EXIT, after the trace is
# flushed. Tolerate SIGABRT (134) only; any other non-zero exit is a real failure.
run_phase() {
  set +e; "$@"; rc=$?; set -e
  if [ $rc -ne 0 ] && [ $rc -ne 134 ]; then echo "PHASE FAILED (rc=$rc)" >&2; exit $rc; fi
  [ $rc -eq 134 ] && echo "(exit-time SIGABRT tolerated)"
  return 0
}

echo "=== Phase A: generate_fractals (32 ranks) ==="
date
run_phase flux run -N 8 -n 32 "$(which scaffold)" generate_fractals -c "$CFG"

echo "=== Phase B: benchmark, 8 nodes x 4 GPUs = 32 ranks ==="
date
time run_phase torchrun-hpc -N 8 -n 4 --gpus-per-proc 1 "$(which scaffold)" benchmark -c "$CFG"
echo "=== done ==="; date
