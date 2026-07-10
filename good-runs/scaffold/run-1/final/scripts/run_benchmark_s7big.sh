#!/bin/bash
set -x
WS=$PROJECT_ROOT/workspaces/scaffold/20260705_175606
LUSTRE=/p/lustre5/$USER/workspaces/scaffold
source /usr/share/lmod/lmod/init/bash 2>/dev/null
deactivate 2>/dev/null
module load python/3.13.2 cce/21.0.1 cray-mpich/9.1.0 rocm/7.1.1 rccl/fast-env-slows-mpi 2>/dev/null
source "$WS/install/bin/activate"
# --- MPI / Cray runtime (ML-R36) ---
export MPI4PY_MPIABI=mpich
export LD_LIBRARY_PATH="/opt/cray/pe/mpich/9.1.0/ofi/cray/20.0/lib:/opt/cray/pe/mpich/9.1.0/ofi/cray/20.0/lib-abi-mpich:/opt/cray/pe/lib64:${LD_LIBRARY_PATH}"
# --- ROCm / RCCL / MIOpen (from app job script) ---
export NCCL_NET_PLUGIN=/collab/usr/global/tools/rccl/toss_4_x86_64_ib_cray/rocm-7.1.1/install/lib/librccl-net.so
export MIOPEN_DEBUG_CONV_DIRECT=0
# --- dftracer tracing -> workspace traces (needed by optimization pipeline) ---
mkdir -p "$WS/traces_s7big/scaffold"
touch "$WS/traces_s7big/scaffold/.sentinel"   # ML-R38 Lustre/metadata warmup (here NFS, harmless)
export DFTRACER_ENABLE=1
export DFTRACER_INIT=FUNCTION
export DFTRACER_INC_METADATA=1
export DFTRACER_DATA_DIR=all
export DFT_TORCH_PROFILER=1
export DFTRACER_LOG_FILE="$WS/traces_s7big/scaffold/scaffold"
CONFIG="$LUSTRE/s7big_config.yml"
cd "$LUSTRE"
torchrun-hpc -N 8 -n 4 --gpus-per-proc 1 $(which scaffold) benchmark \
    -c "$CONFIG" --fract-base-dir "$LUSTRE/fractals" 2>&1
echo "BENCHMARK_DONE_EXIT=$?"
echo "=== trace files ==="; ls -lh "$WS/traces_s7big/scaffold/" 2>&1 | head
