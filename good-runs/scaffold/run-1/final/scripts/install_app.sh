#!/bin/bash
# ScaFFold original-source install into session venv (follows scripts/install-tuolumne-torchpypi.sh)
set -x
WS=/usr/WS2/haridev/dftracer-agents/workspaces/scaffold/20260705_175606
source /usr/share/lmod/lmod/init/bash 2>/dev/null || source /etc/profile.d/z00_lmod.sh 2>/dev/null
deactivate 2>/dev/null
module load python/3.13.2 cce/21.0.1 cray-mpich/9.1.0 rocm/7.1.1 rccl/fast-env-slows-mpi 2>&1
set -e
# Create session venv at <WS>/install
python3 -m venv "$WS/install"
source "$WS/install/bin/activate"
python3 -m pip install --upgrade pip
# Install ScaFFold (editable) with ROCm extras from the source tree
cd "$WS/source"
pip install -e .[rocm] \
  --find-links https://download.pytorch.org/whl/torch/ \
  --find-links https://download.pytorch.org/whl/torchaudio/ \
  --find-links https://download.pytorch.org/whl/torchvision/ \
  --find-links https://download.pytorch.org/whl/triton-rocm/ 2>&1 | tee "$WS/tmp/install_app.log"
# Patch mpi4py for Cray MPICH (libmpi.so.12 does not exist; use libmpi_gnu.so.12)
MPI_SO=$(ls "$WS"/install/lib/python3.13/site-packages/mpi4py/MPI.mpich.cpython-313-*.so 2>/dev/null | head -1)
if [ -n "$MPI_SO" ]; then
  patchelf --replace-needed libmpi.so.12 libmpi_gnu.so.12 "$MPI_SO" && echo "PATCHED mpi4py: $MPI_SO"
fi
echo "=== VERIFY torch ==="
python3 -c "import torch; print('torch', torch.__version__); print('cuda_avail', torch.cuda.is_available())"
echo "INSTALL_APP_DONE_OK"
