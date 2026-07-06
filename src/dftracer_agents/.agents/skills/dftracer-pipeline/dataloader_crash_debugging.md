# DataLoader Worker Crash Debugging (dftracer-pipeline)

Root causes for the two known PyTorch DataLoader worker segfault patterns
hit during dftracer annotation, plus the 4-step gdb/core-dump debugging
procedure. Load only when a DataLoader worker crash actually occurs.

  DataLoader worker segfault on cleanup: annotating `__len__` on Dataset
  subclasses causes dftracer's C-level event buffer to overflow (DataLoader
  calls __len__ O(10000+) times per epoch to build sampler state). The overflow
  corrupts memory and causes SIGSEGV in worker processes during teardown after
  training is otherwise complete. The fix is to NEVER annotate __len__ — simply
  omit @_dlp.log from it. The function still works normally; its callers capture
  the timing context. Do NOT attempt to fix this by disabling dftracer in workers.

  DataLoader worker segfault during dftracer finalize (flockfile/BufferManager):
  Root cause confirmed on Tuolumne with DFTRACER_ENABLE_HIP_TRACING=ON.
  When dftracer initializes with HIP tracing in the parent process and PyTorch
  forks DataLoader workers, the forked children inherit open FILE* handles.
  On worker exit, dftracer's C++ destructor calls DFTracerCore::finalize() →
  BufferManager::finalize() → flockfile_wrapper() on the inherited FILE*.
  The file's internal mutex is in an inconsistent state (locked in parent thread
  that no longer exists in child), causing a C++ exception during _Unwind_Resume
  which itself SIGSEGV's. Fix: dftracer bugfix/reinitializelogfile branch adds
  fork detection via getpid()/getppid() so finalize() safely no-ops in forked
  children that haven't re-initialized. To rebuild dftracer from that branch,
  see the manual cmake procedure below.

  HOW TO DEBUG DataLoader worker crashes:
  1. Enable core dumps on Lustre (NOT NFS — NFS truncates to 16K):
       ulimit -c unlimited
       COREDIR=/p/lustre5/$USER/workspaces/<session>/cores
       mkdir -p ${COREDIR} && cd ${COREDIR}
     Then launch Python from that directory so workers write cores there.
  2. Add libSegFault.so for C-level backtrace (survives Python teardown):
       export LD_PRELOAD=/usr/lib64/libSegFault.so:${LD_PRELOAD}
       export SEGFAULT_SIGNALS=all
     NOTE: Python faulthandler CANNOT catch crashes that happen after
     Py_Finalize() (C++ destructors run post-Python). Use libSegFault instead.
  3. After crash, run gdb on the full core (redirect stderr to suppress warnings):
       CORE=/p/lustre5/$USER/workspaces/<session>/cores/<node>-<proc>-<pid>.core
       PYTHON=<ws>/install/bin/python3.13
       gdb -batch -ex "bt 20" -ex "info threads" $PYTHON $CORE 2>/dev/null
     Key: look for frames from libdftracer_core.so to identify dftracer source.
  4. Check whether crash is triggered by a specific env var (bisect):
     Run once with DFTRACER_ENABLE_HIP_TRACING=ON and once without. If the
     no-HIP run completes cleanly, the crash is in librocprofiler-sdk state
     inherited across fork.

  For any of these, simply skip the decorator; the function will still be
  called normally and its callers (which ARE annotated) will capture the timing.
