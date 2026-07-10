#!/bin/bash
# Usage: run_trial2.sh <run_name> <config.yml> [affinity]
# Two-phase (generate_fractals -> benchmark). Pass "affinity" to bind each rank to
# ALL cores of its GPU's die. Tuolumne MI300A: 96 cores / 4 GPUs = 24 cores per die.
# pin_memory=True (already set in trainer.py) only pays off with this binding: pinned
# to one core, the copy thread contends with the 4 dataloader workers.
set -e
WS="$PROJECT_ROOT/workspaces/scaffold/20260709_081340"
RUN_NAME="$1"; CFG="$2"; AFFINITY="${3:-}"
source "$WS/scripts/env.sh"

mkdir -p "$WS/$RUN_NAME/traces/raw"
export DFTRACER_ENABLE=1 DFTRACER_INIT=FUNCTION DFTRACER_INC_METADATA=1 DFTRACER_DATA_DIR=all
export DFTRACER_LOG_FILE="$WS/$RUN_NAME/traces/raw/${RUN_NAME}-"

cd "$WS/$RUN_NAME"

run_phase() {  # tolerate ONLY the known exit-time SIGABRT (libomp+MKL clash, trace already flushed)
  set +e; "$@"; rc=$?; set -e
  if [ $rc -ne 0 ] && [ $rc -ne 134 ]; then echo "PHASE FAILED (rc=$rc): $*" >&2; exit $rc; fi
  [ $rc -eq 134 ] && echo "(exit-time SIGABRT tolerated)"
  return 0
}

# Affinity modes, kept SEPARATE so a regression can be attributed:
#   bind     -> only tell torchrun-hpc the true topology (per-die core range)
#   omp      -> only constrain the OpenMP runtime
#   affinity -> both (the original bundled change; +22.5% but vs a stale baseline)
#   <empty>  -> launcher defaults (control)
# `-p` is nargs='+' and swallows the command path, so it MUST be terminated with `--`.
BIND=()
case "$AFFINITY" in
  bind)
    BIND=(-p cores_per_node=96 gpus_per_node=4 --)
    echo "=== affinity=bind: topology only (24 cores/GPU die), OMP untouched ===" ;;
  omp)
    export OMP_NUM_THREADS=6 OMP_PROC_BIND=close OMP_PLACES=cores
    echo "=== affinity=omp: OMP_NUM_THREADS=6 only, launcher binding untouched ===" ;;
  affinity)
    BIND=(-p cores_per_node=96 gpus_per_node=4 --)
    export OMP_NUM_THREADS=6 OMP_PROC_BIND=close OMP_PLACES=cores
    echo "=== affinity=both: topology + OMP ===" ;;
  "") echo "=== affinity: launcher defaults (control) ===" ;;
esac

echo "=== [$RUN_NAME] Phase A: generate_fractals (32 ranks) ==="; date
run_phase flux run -N 8 -n 32 "$(which scaffold)" generate_fractals -c "$CFG"

echo "=== [$RUN_NAME] Phase B: benchmark 8N x 4GPU = 32 ranks ==="; date
time run_phase torchrun-hpc -N 8 -n 4 --gpus-per-proc 1 "${BIND[@]}" "$(which scaffold)" benchmark -c "$CFG"
echo "=== [$RUN_NAME] done ==="; date
