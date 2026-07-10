#!/bin/bash
set -x
WS=$PROJECT_ROOT/workspaces/scaffold/20260705_175606
LUSTRE=/p/lustre5/$USER/workspaces/scaffold
source /usr/share/lmod/lmod/init/bash 2>/dev/null
deactivate 2>/dev/null
module load python/3.13.2 cce/21.0.1 cray-mpich/9.1.0 rocm/7.1.1 rccl/fast-env-slows-mpi 2>/dev/null
source "$WS/install/bin/activate"
export MPI4PY_MPIABI=mpich
export LD_LIBRARY_PATH="/opt/cray/pe/mpich/9.1.0/ofi/cray/20.0/lib:/opt/cray/pe/mpich/9.1.0/ofi/cray/20.0/lib-abi-mpich:/opt/cray/pe/lib64:${LD_LIBRARY_PATH}"
export MIOPEN_DEBUG_CONV_DIRECT=0
CONFIG=/p/lustre5/$USER/workspaces/scaffold/smoke_config.yml
cd "$LUSTRE"
scaffold generate_fractals -c "$CONFIG" --fract-base-dir "$LUSTRE/fractals" 2>&1
echo "GENFRACTALS_DONE_EXIT=$?"
