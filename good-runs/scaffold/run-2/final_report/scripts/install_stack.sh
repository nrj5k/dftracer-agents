#!/bin/bash
# ONE install script for the WHOLE stack: ScaFFold + dftracer + dftracer-utils.
#
# Derived verbatim from the app's own source/scripts/install-tuolumne.sh.
# dftracer is installed INSIDE this same script, in the SAME module env and the
# SAME venv as the app. For DL workloads a consistent install==run environment
# is mandatory: any divergence shows up later as a swallowed ImportError in
# dftracer.dftracer (GLIBCXX/RPATH), or as a bogus "ROCProfiler breaks torch".
set -e
set -o pipefail   # otherwise `pip install ... | tee` returns tee's 0 and hides build failures
WS="$PROJECT_ROOT/workspaces/scaffold/20260709_081340"
cd "$WS"

# ---- modules: EXACTLY what the app declares -------------------------------
ml load python/3.11.5
ml cce/21.0.0 cray-mpich/9.1.0 rocm/7.1.1 rccl/fast-env-slows-mpi

# ---- WCI index (a user ~/.pip/pip.conf can override the site index) --------
export PIP_INDEX_URL="https://wci-repo.llnl.gov/repository/pypi-group/simple"
export PIP_TRUSTED_HOST="wci-repo.llnl.gov"

# ---- compilers: bind to the SAME MPI THE APP USES --------------------------
# `which mpicc` under PrgEnv-cray gives the crayclang wrapper -> libmpi_cray.so.12.
# But ScaFFold's torch/mpi4py wheels are built against the GNU MPICH ABI and the
# job script preloads libmpi_gnu.so.12. Linking dftracer_core against libmpi_cray
# puts TWO MPI runtimes in one process and aborts at teardown with
# "double free or corruption (!prev)". Build dftracer with the GNU wrappers.
MPI_ROOT="/opt/cray/pe/mpich/9.1.0/ofi/gnu/11.2"
export MPICC="$MPI_ROOT/bin/mpicc"
export MPICXX="$MPI_ROOT/bin/mpicxx"
export CC="$MPICC"
export CXX="$MPICXX"

# ---- LD_LIBRARY_PATH: rocm + cray mpich + cce + /usr/lib64 (libdl/dlopen) ---
export LD_LIBRARY_PATH="/opt/rocm-7.1.1/lib:/opt/cray/pe/mpich/9.1.0/ofi/crayclang/20.0/lib:/opt/cray/pe/cce/21.0.0/cce/x86_64/lib:/opt/cray/pe/cce/21.0.0/cce/x86_64/lib/default64:/usr/lib64:${LD_LIBRARY_PATH}"

# Cray's linker runs with --no-allow-shlib-undefined, so anything touching
# dlopen (brahma/gotcha, dftracer_core) must link libdl EXPLICITLY. Having
# /usr/lib64 on LD_LIBRARY_PATH is necessary but NOT sufficient:
#   ld.lld: error: undefined reference: dlopen
# NOTE: keep this a single token with no trailing space — CMake policy CMP0004
# errors on link items with leading/trailing whitespace.
export LDFLAGS="-ldl"

python3 -m venv "$WS/venv"
source "$WS/venv/bin/activate"
pip install --upgrade pip

# ---- 1. the app (annotated tree), WCI rocm wheels -------------------------
pip install -e "$WS/annotated/source[rocmwci]" 2>&1 | tee "$WS/artifacts/install_app.log"

# ---- 2. dftracer, SAME venv, SAME env, develop branch ---------------------
# ROCProfiler/HIP stay ON: the app is a ROCm workload and the module env is now
# correct, so the HIP interception works.
#
# ORDER MATTERS: dftracer BEFORE dftracer-utils. utils drops headers into
# site-packages/dftracer/include/; a stale zconf.h there makes dftracer's own
# build fail with "fatal error: 'zlib_name_mangling.h' file not found".
rm -rf "$WS/venv/lib/python3.11/site-packages/dftracer" \
       "$WS/venv/lib/python3.11/site-packages/dftracer"*.dist-info
#
# Pass MPI and ROCm in explicitly -> cleanest install. dftracer's setup.py reads
# ENV VARS, not CMAKE_ARGS. ROCm on CMAKE_PREFIX_PATH is what lets it find
# rocprofiler-sdk (/opt/rocm-*/lib/cmake/rocprofiler-sdk); otherwise HIP tracing
# is silently skipped ("rocprofiler-sdk is not found").
# ScaFFold uses no HDF5. For an HDF5 workload add:
#   export DFTRACER_ENABLE_HDF5=ON HDF5_ROOT=<prefix> HDF5_DIR=<prefix>
# Do NOT use --no-build-isolation: the build needs setuptools_scm.
export ROCM_PATH=/opt/rocm-7.1.1
export CMAKE_PREFIX_PATH="${ROCM_PATH}:${ROCM_PATH}/lib/cmake:${ROCM_PATH}/lib/cmake/rocprofiler-sdk:${CMAKE_PREFIX_PATH}"
export rocprofiler_sdk_DIR="${ROCM_PATH}/lib/cmake/rocprofiler-sdk"
export DFTRACER_ENABLE_MPI=ON
export DFTRACER_BUILD_WITH_MPI=ON
export DFTRACER_MPI_CC="$MPICC"
export DFTRACER_MPI_CXX="$MPICXX"
pip install "git+https://github.com/LLNL/dftracer.git@develop" \
    2>&1 | tee "$WS/artifacts/install_dftracer.log"

# ---- 2b. dftracer-utils LAST (analyzer/diagnoser use develop too) ---------
pip install "git+https://github.com/LLNL/dftracer-utils.git@develop" \
    2>&1 | tee "$WS/artifacts/install_dftracer_utils.log"

# ---- 3. patch torch's MPI SONAME (app script does this) --------------------
TORCH_LIB_DIR="$WS/venv/lib/python3.11/site-packages/torch/lib"
OLD="libmpi_gnu_112.so.12"
NEW="libmpi_gnu.so.12"
if [ -d "$TORCH_LIB_DIR" ]; then
  cd "$TORCH_LIB_DIR"
  for f in *.so*; do
    [ -f "$f" ] || continue
    if patchelf --print-needed "$f" 2>/dev/null | grep -Fxq "$OLD"; then
      echo "Patching $f"; patchelf --replace-needed "$OLD" "$NEW" "$f"
    fi
  done
  echo "Verification (should be empty):"
  for f in *.so*; do
    [ -f "$f" ] || continue
    patchelf --print-needed "$f" 2>/dev/null | grep -Fxq "$OLD" && echo "STILL NEEDS $OLD -> $f"
  done
  cd "$WS"
fi
echo "=== install_stack.sh complete ==="
