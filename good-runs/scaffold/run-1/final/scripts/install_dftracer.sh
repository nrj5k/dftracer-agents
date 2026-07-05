#!/bin/bash
# Install dftracer (HIP tracing + MPI, no HDF5) + pydftracer[dynamo] into session venv
set -x
WS=/usr/WS2/haridev/dftracer-agents/workspaces/scaffold/20260705_175606
source /usr/share/lmod/lmod/init/bash 2>/dev/null || source /etc/profile.d/z00_lmod.sh 2>/dev/null
deactivate 2>/dev/null
module load python/3.13.2 cce/21.0.1 cray-mpich/9.1.0 rocm/7.1.1 rccl/fast-env-slows-mpi 2>&1
source "$WS/install/bin/activate"
set -e
export ROCM_PATH=/opt/rocm-7.1.1
export CMAKE_ARGS="-DDFTRACER_ENABLE_HIP_TRACING=ON -DROCM_PATH=$ROCM_PATH -DCMAKE_PREFIX_PATH=$ROCM_PATH -DDFTRACER_ENABLE_MPI=ON -DMPI_C_COMPILER=cc -DMPI_CXX_COMPILER=CC -DDFTRACER_ENABLE_HDF5=OFF"
echo "=== Installing dftracer (develop) ==="
pip install --no-cache-dir "git+https://github.com/llnl/dftracer.git@develop" 2>&1 | tee "$WS/tmp/install_dftracer.log"
echo "=== Installing pydftracer[dynamo] (feature/explict-io) ==="
pip install --no-cache-dir "pydftracer[dynamo] @ git+https://github.com/llnl/pydftracer.git@feature/explict-io" 2>&1 | tee "$WS/tmp/install_pydftracer.log"
echo "=== VERIFY dftracer ==="
python3 -c "import dftracer, os; print('dftracer', os.path.dirname(dftracer.__file__))"
python3 -c "from dftracer.python.torch import trace_handler; from dftracer.python.dynamo import create_backend; from dftracer.python import ai; assert hasattr(ai.data,'io'); print('pydftracer extras OK')" || echo "PYDFTRACER_EXTRAS_FAIL"
echo "=== VERIFY HIP tracing compiled ==="
python3 -c "import dftracer,os; p=os.path.join(os.path.dirname(dftracer.__file__)); import glob; print([f for f in glob.glob(p+'/lib*/*.so')])"
echo "INSTALL_DFTRACER_DONE_OK"
