#!/bin/bash
# Canonical ScaFFold environment — derived VERBATIM from the app's own
# source/scripts/install-tuolumne.sh and source/scripts/scaffold-tuolumne.job.
#
# RULE: the SAME module set + python + LD_PRELOAD is used for INSTALL and for RUN.
# Divergence here is what previously produced (a) a swallowed ImportError in
# dftracer.dftracer via a GLIBCXX/RPATH mismatch against python-3.13's lib dir,
# and (b) an apparent "ROCProfiler breaks torch" failure that was really a
# missing rocm libomp preload.
WS="$PROJECT_ROOT/workspaces/scaffold/20260709_081340"

ml load python/3.11.5
ml cce/21.0.0 cray-mpich/9.1.0 rocm/7.1.1 rccl/fast-env-slows-mpi

source "$WS/venv/bin/activate"

# Same library paths as install_stack.sh. dftracer_core is built with crayclang,
# so the RUN env needs the CCE runtime libs (libmodules.so.1, libfi.so.1, ...)
# and /usr/lib64 (libdl) exactly as the build did. install env == run env.
# GNU MPICH (not crayclang): dftracer_core is built against libmpi_gnu.so.12 to
# match torch/mpi4py and the LD_PRELOAD below. Mixing the two MPI runtimes aborts.
export LD_LIBRARY_PATH="/opt/rocm-7.1.1/lib:/opt/cray/pe/mpich/9.1.0/ofi/gnu/11.2/lib:/opt/cray/pe/cce/21.0.0/cce/x86_64/lib:/opt/cray/pe/cce/21.0.0/cce/x86_64/lib/default64:/usr/lib64:${LD_LIBRARY_PATH}"

# torch/lib must be on LD_LIBRARY_PATH: dftracer's gotcha layer intercepts dlopen,
# which defeats torch's $ORIGIN-relative RPATH lookup of its own lazily-dlopen'd
# shims. Without this, torch.cuda.init() dies with
#   RuntimeError: Error in dlopen: libcaffe2_nvrtc.so: cannot open shared object file
export LD_LIBRARY_PATH="$WS/venv/lib/python3.11/site-packages/torch/lib:${LD_LIBRARY_PATH}"

# From scaffold-tuolumne.job:
# (1) libmagma error  (2) mpi4py import segfault  (3-5) cblas_gemm_f16f16f32
#
# ROCm's LLVM libomp.so is DELIBERATELY OMITTED from the preload. Preloading it
# alongside MKL's libmkl_gnu_thread puts two OpenMP runtimes in the process; with
# dftracer's gotcha loaded that aborts at exit with
#   double free or corruption (!prev)
# (bisected: libomp+mkl fails, mpi+mkl passes, each alone passes).
# torch's own libomp still resolves via torch/lib on LD_LIBRARY_PATH above, so
# the libmagma error the job script guards against does not reappear.
# KNOWN, BENIGN: this exact preload set (which the app REQUIRES — libomp for
# torch's libmagma __kmpc_dispatch_deinit, MKL for libtorch_cpu's
# cblas_gemm_f16f16f32) puts ROCm's LLVM libomp and MKL's libmkl_gnu_thread in
# one process. With dftracer's gotcha loaded, the process aborts AT EXIT with
#   double free or corruption (!prev)
# AFTER all work is done and after dftracer has flushed a complete trace.
# Bisected: libomp+mkl_gnu_thread aborts; libomp+mpi ok; mpi+mkl ok; each alone ok.
# Not fixable by KMP_DUPLICATE_LIB_OK / OMP_NUM_THREADS / MKL_THREADING_LAYER,
# nor by moving MKL to LD_LIBRARY_PATH (the symbols need interposition), nor by
# libmkl_intel_thread (a stale Anaconda libmkl_gnu_thread then hijacks the dlopen).
# => Runner scripts tolerate an exit-time SIGABRT (134) and validate the trace instead.
export LD_PRELOAD="/opt/rocm-7.1.1/llvm/lib/libomp.so /opt/cray/pe/mpich/9.1.0/ofi/gnu/11.2/lib/libmpi_gnu.so.12 /opt/intel/oneapi/mkl/2024.2/lib/libmkl_core.so.2 /opt/intel/oneapi/mkl/2024.2/lib/libmkl_gnu_thread.so.2 /opt/intel/oneapi/mkl/2024.2/lib/libmkl_intel_lp64.so.2"

export MIOPEN_DEBUG_CONV_DIRECT_NAIVE_CONV_FWD=0
export MIOPEN_DEBUG_CONV_DIRECT_NAIVE_CONV_BWD=0
export MIOPEN_DEBUG_CONV_DIRECT_NAIVE_CONV_WRW=0

echo "ScaFFold env: python/3.11.5 cce/21.0.0 cray-mpich/9.1.0 rocm/7.1.1 rccl"
echo "  venv: $WS/venv"
