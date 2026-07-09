#!/bin/bash
set -e
WS=/usr/WS2/haridev/dftracer-agents/workspaces/flash_x/20260708_201403
OBJ=$WS/annotated/source/object
export LD_LIBRARY_PATH="$WS/hdf5_1.14/lib:$WS/install/lib/python3.13/site-packages/dftracer/lib64:/opt/cray/pe/mpich/9.0.1/ofi/gnu/11.2/lib:/opt/cray/pe/lib64:/opt/cray/pe/cce/20.0.0/cce/x86_64/lib:/opt/cray/pe/cce/20.0.0/cce/x86_64/lib/default64:/usr/lib64:$LD_LIBRARY_PATH"
export DFTRACER_ENABLE=1
export DFTRACER_INIT=FUNCTION
export DFTRACER_DATA_DIR=all
export DFTRACER_INC_METADATA=1
export DFTRACER_LOG_FILE="$WS/opt1/traces/raw/opt1"
export MPICH_GPU_SUPPORT_ENABLED=0
export HDF5_USE_FILE_LOCKING=FALSE
cd "$OBJ"
exec ./flashx
