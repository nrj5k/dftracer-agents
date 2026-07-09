#!/bin/bash
set -e
WS="$PROJECT_ROOT/workspaces/scaffold/20260709_081340"
source $WS/scripts/env.sh
mkdir -p $WS/baseline/traces/raw $WS/smoke/traces
export DFTRACER_ENABLE=1
export DFTRACER_INC_METADATA=1
export DFTRACER_LOG_FILE=$WS/smoke/traces/smoke-
export DFTRACER_DATA_DIR=all
export DFTRACER_INIT=FUNCTION
export MPI4PY_MPIABI=mpich
# Single-process DDP rendezvous (env://) so worker.py's unconditional
# dist.get_backend() debug log line doesn't crash even with a lone process.
export MASTER_ADDR=127.0.0.1
export MASTER_PORT=29500
export RANK=0
export WORLD_SIZE=1
export LOCAL_RANK=0
# get_world_rank/size/local_rank look for MPI/SLURM/Flux launcher vars,
# not torchrun's RANK/WORLD_SIZE/LOCAL_RANK -- fake a single Flux task.
export FLUX_TASK_RANK=0
export FLUX_TASK_LOCAL_ID=0
export FLUX_JOB_SIZE=1
cd $WS/smoke

# The libomp+MKL OpenMP clash (see env.sh) aborts at EXIT, after the work is done
# and the trace is flushed. Tolerate exit code 134 (SIGABRT) only; any other
# non-zero exit is a real failure.
run_phase() {
  set +e
  "$@"
  rc=$?
  set -e
  if [ $rc -ne 0 ] && [ $rc -ne 134 ]; then
    echo "PHASE FAILED (rc=$rc): $*" >&2
    exit $rc
  fi
  [ $rc -eq 134 ] && echo "(exit-time SIGABRT tolerated: $1 $2)"
  return 0
}

run_phase scaffold generate_fractals -c $WS/smoke/smoke_config.yml
run_phase scaffold benchmark -c $WS/smoke/smoke_config.yml
