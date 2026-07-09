#!/bin/bash
# Reproduces the final Lustre + optimized-binary Pegasus/PMC run used in
# this session's before/after comparison. Assumes:
#   - Pegasus installed + PMC rebuilt against Cray MPICH (see software-pegasus skill)
#   - Annotated+optimized Montage built via build_annotated_montage.sh
#   - montage-workflow-v3 DAX already generated (montage-workflow.py) with
#     PATH pointing at the optimized install_ann/bin at generation time
#   - Flux allocation already running (flux alloc / flux alloc --bg)
set -e

ALLOC_JOBID="$1"          # e.g. <flux-jobid>
RUN_DIR="$2"              # e.g. .../work_lustre/$USER/pegasus/montage/run0001
PEGASUS_BIN="$3"          # e.g. .../pegasus_montage/pegasus/bin
DFTRACER_LIB="$4"         # e.g. .../montage/<run>/venv/lib/python3.13/site-packages/dftracer/lib64
TRACES_DIR="$5"           # e.g. /p/lustre5/$USER/dftracer-pegasus-montage/traces

WRAPPER=$(mktemp)
cat > "$WRAPPER" <<EOF
#!/bin/bash
export LD_LIBRARY_PATH="/opt/cray/pe/lib64:/opt/cray/lib64:/opt/cray/pe/papi/7.2.0.2/lib64:/opt/cray/pe/pmi/6.0.15/lib:/opt/cray/pe/mpich/9.0.1/ofi/crayclang/20.0/lib:/opt/cray/pe/cce/20.0.0/cce/x86_64/lib:/opt/cray/pe/cce/20.0.0/cce-clang/x86_64/lib:/usr/lib64:$DFTRACER_LIB"
export DFTRACER_ENABLE=1
export DFTRACER_INIT=FUNCTION
export DFTRACER_INC_METADATA=1
export DFTRACER_DATA_DIR=all
export DFTRACER_LOG_FILE=$TRACES_DIR/montage-optimized
cd $RUN_DIR
rm -f montage-0.dag.rescue
exec $PEGASUS_BIN/pegasus-mpi-cluster -s -v montage-0.dag
EOF
chmod +x "$WRAPPER"

flux proxy "$ALLOC_JOBID" flux run -N1 -n8 bash "$WRAPPER"
