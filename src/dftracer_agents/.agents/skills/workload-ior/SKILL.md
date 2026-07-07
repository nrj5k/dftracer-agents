---
name: workload-ior
description: >
  IOR-specific knowledge: build quirks, annotation pitfalls, ROMIO tuning on
  VAST NVMe storage, and optimal dftracer run configuration for IOR 4.0.0.
  Load this skill whenever working with IOR.
---

Cross-references: [[dftracer-annotation-lessons]] [[dftracer-pipeline]] [[software-mpi]] [[software-hdf5]] [[dftracer-preload-run]]

---

## Build — IOR 4.0.0

### autoreconf fails without -I config and stub files

```
configure: error: cannot find install-sh
X_AC_META: command not found
automake: error: required file './NEWS' not found
```

**Root cause:** IOR 4.0.0 ships without a pre-generated configure script; the
`X_AC_META` m4 macro lives in `config/` not the default autoconf include path.
automake also requires `NEWS` and `AUTHORS` even if empty.

**Fix:**
```bash
cd <source> && touch NEWS AUTHORS && autoreconf -fi -I config
```

---

### Linker fails with duplicate symbols on clang/lld

```
ld.lld: error: duplicate symbol: posix_aiori
ld.lld: error: duplicate symbol: mpiio_aiori
```

**Root cause:** `aiori.h` defines global variables without `extern`, causing
duplicate definitions when included in multiple TUs. Clang/lld is strict;
GCC < 10's `-fcommon` merged them silently.

**Fix:** Always build with `-fcommon` and `-fuse-ld=bfd`, and `make clean`
before rebuilding when changing `CFLAGS`:

```bash
CFLAGS="-g -O2 -Wno-incompatible-function-pointer-types -fcommon" \
LDFLAGS="-fuse-ld=bfd" \
./configure --without-hdf5 --without-ncmpi ...
make clean && make -j8
```

---

### session_build_annotated ignores custom CFLAGS/LDFLAGS for autotools

**Root cause:** `session_build_annotated` runs its own autoreconf+configure
without the project-specific flag overrides needed for IOR.

**Fix:** Build the annotated version manually:

```bash
rm -rf <ws>/build_ann/
mkdir -p <ws>/build_ann/ && cd <ws>/build_ann/
CFLAGS="-g -O2 -Wno-incompatible-function-pointer-types -fcommon -I<dftracer_inc>" \
LDFLAGS="-fuse-ld=bfd -L<dftracer_lib> -Wl,-rpath,<dftracer_lib>" \
LIBS="-ldftracer_core" \
<ws>/annotated/configure --prefix=<ws>/install_ann ...
make -j8 install   # build src/ only; skip contrib/ if broken
```

---

### HDF5 backend: stale autotools config ignores --with-hdf5

**Root cause:** Stale `.deps/`, `config.status`, `autom4te.cache` cause
autotools to skip re-detection of HDF5.

**Fix:**
```bash
make distclean
rm -rf .deps src/.deps autom4te.cache config.status config.log Makefile
export CPPFLAGS="-I${HDF5_PREFIX}/include"
export LDFLAGS="-L${HDF5_PREFIX}/lib -Wl,-rpath,${HDF5_PREFIX}/lib"
export LIBS="-lhdf5 -lz"
./configure --with-hdf5 --prefix=<install> ...
# Verify:
grep USE_HDF5_AIORI config.h  # must show 1
```

---

## Annotation Pitfalls — IOR

### FINI must appear AFTER ior_main()

Placing `DFTRACER_C_FINI()` before the benchmark call leaves all backend spans
untraced:

```c
int main(...) {
  MPI_Init(...);
  DFTRACER_C_INIT(NULL, NULL, NULL);
  DFTRACER_C_FUNCTION_START();
  ...
  ior_main(opts);           // ← real I/O happens here
  DFTRACER_C_FUNCTION_END();
  DFTRACER_C_FINI();        // ← AFTER ior_main(), not before it
  MPI_Finalize();
  return 0;
}
```

---

### Braceless single-line `if` (dryRun pattern)

Inserting `END` into a braceless `if` body makes the early return unconditional:

```c
// Before annotation:
if (dryRun) return NULL;

// WRONG — END steals the if body:
if (dryRun)
  DFTRACER_C_FUNCTION_END();
return NULL;   // unconditional now

// CORRECT — add braces first:
if (dryRun) {
  DFTRACER_C_FUNCTION_END();
  return NULL;
}
```

**Pre-check:** `grep -n "if.*return\|if.*continue\|if.*break" <file.c> | grep -v "{" | grep -v "//"`

---

### HDF5 forward declaration vs definition

`HDF5_Create` appears twice — declaration ends with `;`, definition has a body.
Only annotate the definition:

```bash
grep -n "HDF5_Create" file.c | grep -v ";$"   # shows definition only
```

---

### dftracer built without MPI/HDF5 support

Trace files exist but only contain POSIX events; `MPIIO_*` and `HDF5_*` spans
are absent.

**Fix:**
```bash
cmake -DCMAKE_INSTALL_PREFIX=<prefix> \
      -DDFTRACER_ENABLE_MPI=ON \
      -DDFTRACER_ENABLE_HDF5=ON \
      -DDFTRACER_ENABLE_FTRACING=ON <src>
make -j4 install
# Verify:
grep DFTRACER_MPI_ENABLE <prefix>/include/dftracer/core/dftracer_config.hpp
# Expected: #define DFTRACER_MPI_ENABLE 1
```

Note: IOR's C frontend tolerates `chid_t` in Cray HDF5 headers; only
dftracer/brahma (C++ frontend) cannot — see [[workload-h5bench]] for the fix.

---

## dftracer MPI+HDF5 install on Tuolumne (Cray) — full working recipe (2026-07-06)

`session_install_dftracer` FAILS on Tuolumne two ways: it auto-enables HIP
tracing (no `rocprofiler-sdk/buffer.h` → fatal) and resolves HDF5 to the old
`/usr` 1.10.5. Install manually with **environment variables** (dftracer's
`setup.py` ignores `CMAKE_ARGS` and pip `--config-settings=cmake.args` entirely
— those silently build all-OFF defaults):

```bash
# 1. Patch the Cray HDF5 chid_t typo into a private include tree (Cray libs unchanged,
#    so IOR's ABI is identical). Copy from the crayclang variant:
HDF5_SRC=/opt/cray/pe/hdf5-parallel/1.14.3.7/crayclang/20.0
P=<ws>/tmp/hdf5_patched
cp -r "$HDF5_SRC/include" "$P/include"; ln -s "$HDF5_SRC/lib" "$P/lib"
chmod -R u+w "$P/include"
sed -i 's/H5Aread_async(chid_t attr_id/H5Aread_async(hid_t attr_id/' "$P/include/H5Apublic.h"

# 2. Build dftracer @develop. CRITICAL: module UNLOAD cray-hdf5-parallel first, else the
#    Cray cc-wrapper re-injects the unpatched -I.../cray/20.0/include ahead of $P.
module unload cray-hdf5-parallel
export DFTRACER_ENABLE_MPI=ON DFTRACER_ENABLE_HDF5=ON DFTRACER_ENABLE_FTRACING=ON
export DFTRACER_ENABLE_HIP_TRACING=OFF DFTRACER_DISABLE_HWLOC=ON
export MPI_C_COMPILER=$(which mpicc) MPI_CXX_COMPILER=$(which mpicxx)
export HDF5_ROOT=$P HDF5_DIR=$P
pip install --no-cache-dir --force-reinstall --no-deps -v \
  "git+https://github.com/llnl/dftracer.git@develop"

# 3. Verify ALL three are ==1:
grep -E "DFTRACER_(MPI|HDF5|FTRACING)_ENABLE" \
  <ws>/venv/.../dftracer/include/dftracer/core/dftracer_config.hpp
```

---

## Annotated-build link chain (C app → C++ dftracer_core) on Tuolumne

Linking IOR (C) against `libdftracer_core.so` (C++, needs GLIBCXX_3.4.29 +
`libyaml-cpp`) hits a cascade. Working flags for the annotated `configure`:

```bash
DFT=<ws>/venv/lib/python3.13/site-packages/dftracer
CFLAGS="-g -O2 -Wno-incompatible-function-pointer-types -fcommon -I$DFT/include"
LDFLAGS="-fuse-ld=bfd -L$DFT/lib64 -Wl,-rpath,$DFT/lib64 \
         -Wl,--allow-shlib-undefined -Wl,--no-as-needed"
LIBS="-ldftracer_core -lstdc++"
# Runtime libs so configure can RUN test binaries AND smoke/prod runs work.
# The python module's libstdc++ has GLIBCXX_3.4.29 + libyaml-cpp; it MUST precede
# /usr/lib64 (whose libstdc++ 6.0.25 only has up to 3.4.25):
export LD_LIBRARY_PATH="$DFT/lib64:/usr/tce/packages/python/python-3.13.2/lib:$LD_LIBRARY_PATH"
```

Symptom → cause map:
- `undefined reference ...@GLIBCXX_3.4.26` at link → `-lstdc++` dropped by
  `--as-needed` (C main uses no C++ directly) → add `--no-as-needed` +
  `--allow-shlib-undefined` (NEEDED shlib syms resolve at runtime).
- `C compiler cannot create executables` (link OK) → runtime loader picked old
  `/usr/lib64/libstdc++.so.6` → prepend the python module's lib dir.
- `incompatible integer to pointer conversion ... DFTRACER_C_INIT(NULL,NULL,-1)`
  → `process_id` is `int *`; INIT args MUST be `NULL, NULL, NULL` (pass
  `init_args="NULL, NULL, NULL"` to clang_annotate_project, NOT `-1`).

Build `src/` only (`make -C src -j8 install`); `contrib/cbif.c` fails on
`open64`/`lseek64` implicit decls under cce — unrelated, skip it.

---

## Storage on Tuolumne is Lustre (/p/lustre5), NOT always VAST

The VAST ROMIO rules below assume VAST NVMe. When the run dir is on
`/p/lustre5` (`stat -f -c %T` → `lustre`), those rules DO NOT apply — on Lustre,
collective buffering (cb_read/cb_write) usually HELPS (high per-request
latency), striping (`lfs setstripe`) is the key L3 lever, and data-sieving
tuning behaves oppositely to VAST. Always `stat -f` the output dir first.

Baseline (2026-07-06, Lustre, 8 nodes × 64 ranks, `-a HDF5 -b 64m -t 16m -s 8 -c`,
32 GiB shared file): Write 2197 MiB/s / 14.9s, Read 1332 MiB/s / 24.6s
(read is the weaker path). Annotated trace confirmed annotated spans +
MPI interception (MPI_Reduce/Barrier/Bcast) + POSIX interception (lseek/write/read).

## Trace analysis speed + diagnose tool bug

- `mcp__dftracer__analyze` with MANY per-rank .pfw.gz files: `cluster_n_workers>1`
  RACES on the shared `.dftindex` build and silently ingests only a PARTIAL subset
  (non-deterministic: saw 4 procs/122k events, then 3 procs/63k, on the same 64-file
  dir). Use `cluster_type=local cluster_n_workers=1` for a reliable FULL read (64
  procs / 8 nodes / 1.75M events / 71,275 POSIX ops in ~9s). Wipe stale `.dftindex`
  + `checkpoint/` before re-analyzing. Do NOT pass `cluster_cores` (invalid key).
- `mcp__dftracer__diagnose` currently errors `'Diagnoser' object has no
  attribute 'diagnose_checkpoint'` — API drift vs installed dfdiagnoser. Read
  the `checkpoint/_flat_view_*.parquet` + `_raw_stats_*.json` directly instead.

---

## Smoke Test

Run as a single process:

```bash
./src/ior -a POSIX -b 1m -t 1m -s 1 -F -C -o /tmp/ior_smoke_test
```

**OpenMPI root in container:** Add `--allow-run-as-root` and env vars:

```bash
OMPI_ALLOW_RUN_AS_ROOT=1 OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1 \
  mpirun -np 1 --allow-run-as-root ./src/ior -a POSIX -b 1m -t 1m -s 1 -F -C -o /tmp/ior_smoke_test
```

---

## Trace Collection

### pfw files land in a subdirectory — must copy up before splitting

When `run_id` contains a slash (e.g. `ior/20260617_185032`), dftracer writes:

```
<ws>/traces/ior/20260617_185032-<hash>-app.pfw.gz
```

`session_split_traces` looks only in `traces/` directly:

```bash
mkdir -p <ws>/traces/ior
# ... run benchmark ...
cp <ws>/traces/ior/*.pfw.gz <ws>/traces/
# then call session_split_traces
```

---

### dftracer_service daemon (best practice)

```bash
SERVICE_BIN=<ws>/install_ann/bin/dftracer_service
mkdir -p <ws>/traces/service
DFTRACER_ENABLE=1 DFTRACER_LOG_FILE=<ws>/traces/<RUN_ID> \
  DFTRACER_DATA_DIR=all DFTRACER_TRACE_INTERVAL_MS=1000 \
  "$SERVICE_BIN" start <ws>/traces/service
# ... run benchmark ...
"$SERVICE_BIN" stop <ws>/traces/service
```

---

## ROMIO Optimization on VAST NVMe (Tuolumne)

### Optimal configuration (IOR 4.0.0, HDF5, 192 ranks, 2 nodes, VAST storage)

```bash
MPICH_MPIIO_HINTS="*:romio_cb_write=enable"
# IOR flags:
-a HDF5 -b 64m -t 16m -s 4 -c -Y
```

| Metric | Baseline | Optimized | Change |
|--------|----------|-----------|--------|
| Total time | 168.8s | 112.9s | -33% |
| Write BW | 352 MiB/s | 557 MiB/s | +58% |
| Read BW | 1705 MiB/s | 1991 MiB/s | +17% |
| POSIX calls | 667,363 | 73,991 | -89% |
| seek_slope | 362 | 9.96 | -97% |
| data_slope | 74.3 | 2.19 | -97% |

### VAST-specific ROMIO rules

| Hint | Effect on VAST | Verdict |
|------|---------------|---------|
| `romio_cb_write=enable` | Aggregates scattered writes → large pwrite calls | **USE** |
| `romio_cb_read=enable` | Funnels parallel reads through aggregators; VAST handles natively | **AVOID** (−70% read BW) |
| `romio_ds_write=disable` | Forces individual small non-contiguous writes | **FATAL** (352→95 MiB/s) |

**Key:** VAST is NVMe-based, not Lustre. Tuning that helps on spinning-disk Lustre
(disabling data sieving to avoid read-modify-write) is catastrophic on VAST.
Always test with hints unset first, then add `cb_write` only.

For full MPI/ROMIO software strategies, see [[software-mpi]].
For HDF5-specific tuning, see [[software-hdf5]].

## Failed Configurations

Entries below were applied during optimization loops and caused regressions or had no effect.
Check this section before proposing any configuration for this workload/software/filesystem.

Format per entry:
  date, app, workload, filesystem, system, bottleneck,
  config_attempted, result, metrics_before, metrics_after, delta,
  root_cause, do_not_use_when

<!-- New failed-config entries are appended below by the optimization loop (Step 8d-iii-FAIL) -->

---
date: 2026-06-24
app: https://github.com/llnl/ior (tag 4.0.0)
workload: IOR HDF5 collective write
filesystem: vast
system: Tuolumne (AMD MI300A, cray-mpich/9.0.1)
bottleneck: posix_seek_ops_slope, posix_data_ops_slope
config_attempted: |
  MPICH_MPIIO_HINTS="*:romio_ds_write=disable"
result: REGRESSED
metrics_before: write BW 352 MiB/s, total time 140s
metrics_after:  write BW 95 MiB/s, total time 515s
delta: -73% write BW, +268% total time
root_cause: |
  VAST uses NVMe-backed parallel storage. ROMIO data sieving handles
  non-contiguous HDF5 collective I/O by read-modify-writing large aligned chunks.
  Disabling it forces ROMIO to issue thousands of individual small writes to
  non-contiguous regions, causing extreme I/O amplification. Unlike Lustre
  (spinning disk), VAST NVMe is not hurt by the read-before-write cost.
do_not_use_when: filesystem is VAST, NVMe-backed NAS, or any all-flash parallel storage

---
date: 2026-06-24
app: https://github.com/llnl/ior (tag 4.0.0)
workload: IOR HDF5 collective read
filesystem: vast
system: Tuolumne (AMD MI300A, cray-mpich/9.0.1)
bottleneck: posix_seek_ops_slope (read path)
config_attempted: |
  MPICH_MPIIO_HINTS="*:romio_cb_read=enable"
result: REGRESSED
metrics_before: read BW 2163 MiB/s
metrics_after:  read BW 659 MiB/s
delta: -70% read BW
root_cause: |
  VAST handles 192 concurrent reads natively and efficiently. Collective read
  buffering funnels all reads through a small set of aggregator processes,
  creating a coordination bottleneck. This helps on Lustre (high per-request
  latency) but hurts on VAST's NVMe fabric where parallel reads are optimal.
do_not_use_when: filesystem is VAST or any NVMe parallel storage where concurrent reads are cheap

