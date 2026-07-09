# DFTracer Pipeline Plan — flash_x/20260708_201403

## Overview

- **run_id**: `flash_x/20260708_201403`
- **Workspace**: `/usr/WS2/haridev/dftracer-agents/workspaces/flash_x/20260708_201403`
  Subdirs present: `source/`, `baseline/`, `annotated/`, `artifacts/`, `tmp/`, `dataset/`, `build/`, `install/`
- **App**: Flash-X (`git@github.com:Flash-X/Flash-X.git`, ref `main`), Sedov 3D test problem.
- **System**: Tuolumne (AMD MI300A, Cray PE, no sudo). Launcher: `flux run` (direct `flux run -x VAR...` on a single node; wrap in a bash script + `flux proxy <alloc>` for multi-node — see Flux Proxy Run Pattern below). ROCm present but HIP tracing not needed for this CPU-bound I/O objective (HIP tracing flag can stay ON in dftracer build per detection defaults, just unused).
- **Objective**: "Annotate and optimize FlashX on Lustre" — I/O optimization, trace output written to Lustre-backed run directories, with trace files themselves in `<WS>/traces/` per policy (see `feedback_optimization_pipeline_traces`), and application checkpoint/plot output on Lustre via a short `ds` symlink (flash.par is 80-column limited — long absolute paths get silently truncated).
- **Language mix**: ~2600 Fortran `.F90` files, 126 C, 6 C++, 70 Python. dftracer's clang-based auto-annotators (`dftracer-annotate-c`, `dftracer-annotate-cpp`) do **not** cover Fortran. **Decision (carried from prior session lessons): rely primarily on PRELOAD-mode library-level interception (POSIX/HDF5/MPI-IO), which requires NO Fortran source annotation** — HDF5/POSIX/MPI calls made from Fortran still go through the C libraries dftracer intercepts. FUNCTION-mode source annotation is a secondary, optional enhancement limited to the C/C++ IO layer (`source/IO/IOMain/**` has thin C/C++ wrappers) plus a hand-written C constructor/destructor shim for INIT/FINI (Fortran has no C `main()`). If FUNCTION-mode constructors don't fire reliably under `crayftn`/CCE, pivot immediately to pure PRELOAD.

### Known-good build recipe (from prior session, `workload-flashx` skill)
1. `git submodule update --init --recursive` in `source/` (PARAMESH submodule required before setup).
2. Build HDF5 1.14.x from source into `<WS>/hdf5_1.14` (NEVER use Cray/system HDF5 module). Patch `H5Apublic.h` line ~932 `chid_t` → `hid_t` if using 1.14.3-era headers.
3. `HDF5_PATH=<WS>/hdf5_1.14 bash setup Sedov -auto -3d` from `source/` → generates `source/object/`.
4. Edit `source/object/Makefile.h`: set `HDF5_PATH=<WS>/hdf5_1.14`, `MPI_PATH=/opt/cray/pe/mpich/9.0.1/ofi/gnu/11.2` — **use the GNU 11.2 MPI wrappers** (`mpif90`/`mpicc`/`mpicxx` under that MPI_PATH), NOT `crayftn`/`craycc` (Cray PE compilers fail on Flash-X Fortran). Add `-fallow-argument-mismatch` to FFLAGS (gfortran strict-checking vs MPI Fortran module interfaces).
5. `make -j4` in `source/object/` → executable `source/object/flashx`.
6. Runtime `LD_LIBRARY_PATH` must include `<WS>/hdf5_1.14/lib`, CCE libs `/opt/cray/pe/cce/20.0.0/cce/x86_64/lib`, and `/usr/lib64`.
7. Module load order (only if NOT inside a `flux proxy` job — reloading modules inside a flux-proxied script deadlocks Lmod): `craype-x86-trento`, `libfabric/match_SHS`, `craype-network-ofi`, `perftools-base/25.09.0`, `craype/2.7.35`, `PrgEnv-cray/8.7.0`, `flux_wrappers/0.1`, `xpmem/2.6.5`, `cce/20.0.0`, `cray-libsci/25.09.0`, `cray-mpich/9.0.1`, `python/3.13.2`. StdEnv is loaded by default — do not reload.

### Global gotchas (apply to every step)
- **Never run app build/run from the project root** — always `cwd` inside `<WS>/baseline/` or `<WS>/annotated/` (or `source/object` beneath them).
- Flash-X ships dangling symlinks in a shallow clone (e.g. `TurbGen.h`) — copies between `baseline/`/`annotated/` must use `symlinks=True, ignore_dangling_symlinks=True` (already patched into `session_tools.py`; if you hit `FileNotFoundError` on copy, that's the symptom).
- `flash.par` has an **80-column limit** — long absolute Lustre paths get truncated silently. Use a short `ds` symlink (e.g. `ln -s /p/lustre5/$USER/flashx <WS>/ds`) and reference `ds/...` in `flash.par` `basenm`.
- Traces (`.pfw`/`.pfw.gz`) go to `<WS>/<run>/traces/raw/` (workspace, local FS) — NEVER to Lustre. Set `DFTRACER_LOG_FILE` explicitly to the workspace path.
- Inside a `flux proxy` session, do NOT `module load` in the submitted script (already-loaded modules; reloading via Lmod deadlocks). Wrap env exports (not module loads) in a bash script and submit that script via `flux proxy <alloc> flux run ...` (see Flux Proxy Run Pattern in `workload-flashx` skill). For single-node/no-proxy `flux run`, use `-x VAR` to forward env vars.
- Ask the user for their active allocation ID before any multi-node/production run; verify with `flux jobs` it's still active.
- Canonical run paths (per-run, via `session_get_run_paths(run_id, run_name)`): `run_dir`, `source_dir`, `patches_dir`, `traces_raw`, `traces_compact`, `scripts_dir`, `dftracer_log_prefix`. Baseline run's paths already resolved: `run_dir=<WS>/baseline`, `source_dir=<WS>/baseline/source`, `traces_raw=<WS>/baseline/traces/raw`, `traces_compact=<WS>/baseline/traces/compact`, `scripts_dir=<WS>/baseline/scripts`, `dftracer_log_prefix=<WS>/baseline/traces/raw/baseline`. Get the equivalent for `annotated`/other run names via the same tool before using them.

---

## STEP 1: dftracer-build-dftracer ✅ COMPLETED

**Goal**: Install dftracer (+ dftracer-utils) into `<WS>/install` with MPI + HDF5 + POSIX support, pointed at the session's source-built HDF5 and the GNU 11.2 MPI wrappers.

**Inputs**:
- `run_id=flash_x/20260708_201403`
- HDF5 1.14.5 pre-built at `<WS>/hdf5_1.14`
- MPI: GNU 11.2 wrappers at `/opt/cray/pe/mpich/9.0.1/ofi/gnu/11.2/bin/{mpicc,mpicxx}` (correctly detected)
- dftracer env vars: `DFTRACER_ENABLE_MPI=ON`, `DFTRACER_ENABLE_HDF5=ON`, `DFTRACER_ENABLE_TESTS=OFF`, `DFTRACER_ENABLE_PYTHON=ON`, HIP disabled (CPU-only per user requirement)

**RESOLVED FACTS (2026-07-08T13:41 UTC)**:
- `session_install_dftracer` completed successfully (exit 0)
- Features enabled: `['mpi', 'hdf5=1.14.5', 'hwloc']`
- HIP disabled as per user requirement (CPU-only, no GPU for Sedov 3D)
- MPI-IO tracing DISABLED: MPICH 9.0.1 not in dftracer native range (3.4.3-3.4.x or 4.2.3-4.2.x)
  - Status: Expected and acceptable; PRELOAD-mode POSIX/HDF5 interception (primary objective) works independently

**Library verification (ldd clean)**:
- `libdftracer_preload.so` (1.6 MB): All dependencies resolved; HDF5 1.14.5 and Cray MPI (GNU wrappers) correctly linked
- `libdftracer_core.so.4.1.0` (44 MB): All dependencies resolved; HDF5 1.14.5 and Cray MPI correctly linked

**Installation paths**:
```
DFTRACER_LIB_DIR=/usr/WS2/haridev/dftracer-agents/workspaces/flash_x/20260708_201403/install/lib/python3.13/site-packages/dftracer/lib64
DFTRACER_PRELOAD_LIB=${DFTRACER_LIB_DIR}/libdftracer_preload.so
DFTRACER_CORE_LIB=${DFTRACER_LIB_DIR}/libdftracer_core.so
DFTRACER_INCLUDE_DIR=/usr/WS2/haridev/dftracer-agents/workspaces/flash_x/20260708_201403/install/lib/python3.13/site-packages/dftracer/include
```

**Artifacts**: `<WS>/artifacts/01_dftracer_install_verification.log`

---

## STEP 2: dftracer-build-app

**Goal**: Build the ORIGINAL (unannotated) Flash-X Sedov 3D baseline in `<WS>/baseline/source/object/flashx`.

**Inputs**: `run_id=flash_x/20260708_201403`, `run_name=baseline`, `source_dir=<WS>/baseline/source` (get exact path via `session_get_run_paths(run_id, "baseline")`).

**Exact recipe** (known-good from prior session — see Overview "Known-good build recipe"):
1. `cd <WS>/baseline/source && git submodule update --init --recursive`
2. If `<WS>/hdf5_1.14` doesn't exist yet, build HDF5 1.14.x from source (curl from HDF5 FTP) into `<WS>/hdf5_1.14`; patch `H5Apublic.h` `chid_t`→`hid_t` if needed for the 1.14.3 header used previously.
3. `HDF5_PATH=<WS>/hdf5_1.14 bash setup Sedov -auto -3d` from `<WS>/baseline/source` → generates `object/`.
4. Edit `<WS>/baseline/source/object/Makefile.h`: `HDF5_PATH=<WS>/hdf5_1.14`, `MPI_PATH=/opt/cray/pe/mpich/9.0.1/ofi/gnu/11.2`, ensure FFLAGS include `-fallow-argument-mismatch`.
5. `make -j4` in `<WS>/baseline/source/object/` (cwd MUST be inside the workspace, never project root).
6. Verify: `<WS>/baseline/source/object/flashx` exists, exit code 0, dynamically linked (`ldd` shows HDF5 1.14 + cray-mpich 9.0.1 gnu variant, no missing libs when `LD_LIBRARY_PATH` includes `<WS>/hdf5_1.14/lib` and `/opt/cray/pe/cce/20.0.0/cce/x86_64/lib`).

**Expected artifacts**: `flashx` binary, build log in `<WS>/artifacts/`, confirmation of NXB/NYB/NZB compile flags (`grep -E 'NXB|NYB|NZB' <build_log>` — needed later for production sizing).

**Decision point for human**: none expected if the recipe above matches the source tree still; flag if PARAMESH submodule or setup script has changed since 2026-07-08.

---

## STEP 3: dftracer-annotator (+ dftracer-annotate-c / dftracer-annotate-cpp)

**Goal**: Annotate the C/C++ I/O layer for FUNCTION-mode region names, add the Fortran-entry-point C shim, and explicitly SCOPE what is/isn't covered — but do not block the pipeline on Fortran coverage, since PRELOAD mode (Step 5/6) is the primary I/O-capture mechanism.

**Inputs**: `run_id`, copy `<WS>/baseline/source` → `<WS>/annotated/source` (use `symlinks=True, ignore_dangling_symlinks=True` semantics — dangling symlinks like `TurbGen.h` exist in this shallow clone).

**Scope for the annotator**:
- Run `dftracer-annotate-c` and `dftracer-annotate-cpp` over the 126 C / 6 C++ files, focused first on `source/IO/IOMain/**` (the I/O layer) — confirm actual file count before running and report it as a decision point if it's large (>50 files) so the human can confirm scope before committing.
- **Fortran (`.F90`, ~2600 files) is OUT OF SCOPE for source-level annotation in this pass** — no dftracer clang/python auto-annotator supports Fortran. Do not attempt manual Fortran annotation unless Step 6/7 shows PRELOAD-mode I/O coverage is insufficient (e.g., if a later diagnoser step finds major I/O time NOT attributed to any POSIX/HDF5 call, e.g. custom binary I/O bypassing libc). If that happens, escalate to a human decision point before hand-annotating Fortran IO routines (`source/IO/IOMain/**`) with dftracer's Fortran API.
- **Fortran entry-point shim (still required, this is not Fortran source annotation, just a tiny C file)**: create `<WS>/annotated/source/object/dftracer_init_fini.c`:
  ```c
  #include <stddef.h>
  #include <dftracer/dftracer.h>
  __attribute__((constructor)) static void dftracer_init(void) {
      DFTRACER_C_INIT(NULL, NULL, NULL);
  }
  __attribute__((destructor)) static void dftracer_fini(void) {
      DFTRACER_C_FINI();
  }
  ```
  Add its `.o` to `ALL_OBJ_FILES` in the generated `object/Makefile` after Step 4 regenerates it. **Known risk**: CCE/`crayftn` linker may not fire constructor/destructor attributes reliably. If FUNCTION mode produces zero traces despite the wrapper linking cleanly, do NOT debug further — pivot to PRELOAD-only mode immediately (this is expected and pre-documented, not a new failure).

**Expected artifacts**: annotated C/C++ files under `<WS>/annotated/source`, `dftracer_init_fini.c`, a short coverage note (files annotated / files skipped / Fortran file count acknowledged as unannotated) written into `<WS>/artifacts/`.

**Record lessons**: any new Fortran/annotation-tooling gaps go into `dftracer-annotation-lessons` (`## Fortran Entry Point Pitfall` section already documents the shim pattern) and `workload-flashx`.

---

## STEP 4: dftracer-build-smoke

**Goal**: Rebuild the annotated tree and run a tiny single-rank smoke test

**RESOLVED (2026-07-08, dftracer-build-smoke agent):**
- Annotated tree rebuilt successfully: `<WS>/annotated/source/object/flashx` (make -j4, exit 0).
- Makefile.h changes: MPI_PATH/HDF5_PATH set as in baseline; `-fallow-argument-mismatch` added to FFLAGS_*;
  `-I${DFTRACER_PATH}/include` added to CFLAGS_OPT and CFLAGS_HDF5; `LIB_OPT/LIB_DEBUG/LIB_TEST` set to
  `-L${DFTRACER_PATH}/lib64 -Wl,-rpath,${DFTRACER_PATH}/lib64 -ldftracer_core`; `MACHOBJ = dftracer_init_fini.o`.
  DFTRACER_PATH = `<WS>/install/lib/python3.13/site-packages/dftracer`.
- `dftracer_init_fini.c` must be recreated in `object/` AFTER `bash setup` runs (setup wipes/regenerates
  `object/` from scratch, deleting any pre-existing shim file placed there beforehand).
- **VERDICT: FUNCTION mode WORKS on this build** — no PRELOAD pivot needed. `ldd` shows
  `libdftracer_core.so.4.1.0` linked with RPATH baked in; no missing libs (session HDF5 1.14.5 +
  cray-mpich gnu 11.2 all resolved).
- Smoke run: single rank via `flux proxy <alloc> flux run -N1 -n1 <script>` (bare `flux run` without
  `flux proxy` from outside the allocation silently queues a NEW pbatch job instead of running inside
  the existing allocation — always use `flux proxy <alloc_id>` when an allocation is already up).
  `mpirun`/`srun` are not directly usable here (`mpirun` absent; `srun` is a flux wrapper) — use `flux run`.
- Runtime `LD_LIBRARY_PATH` for the annotated binary needs the MPI GNU wrapper lib dir too:
  `/opt/cray/pe/mpich/9.0.1/ofi/gnu/11.2/lib:/opt/cray/pe/lib64` (baseline's ldd worked without this
  explicitly only because the loader found it via RPATH/other means at build-verification time; at
  RUNTIME under `flux run` it must be exported explicitly or `libmpifort_gnu_112.so.12` fails to load).
- `flash.par` `basenm="ds/..."` requires the `ds -> /p/lustre5/$USER/flashx` symlink to exist BOTH at
  `<WS>/ds` (documentation/reference) AND at `<WS>/annotated/source/object/ds` (actual run cwd) — Flash-X
  resolves output paths relative to the process cwd (`object/`), not the workspace root.
- Trace result: `annotated-<hash>-app.pfw.gz`, 6566 events — HDF5 5236, POSIX 696, C_APP 552, dftracer 43,
  STDIO 36, MPI 3. Confirms both FUNCTION-mode C_APP annotations and PRELOAD-independent HDF5/POSIX
  interception (via brahma, linked directly into FUNCTION mode) are both active simultaneously.
 to confirm the binary runs and (if FUNCTION mode works) produces trace files.

**Inputs**: `run_id`, `subfolder=<WS>/annotated`, rebuild with the same recipe as Step 2 (submodules already init'd if copied post-Step-1; re-run `HDF5_PATH=<WS>/hdf5_1.14 bash setup Sedov -auto -3d` in `<WS>/annotated/source` then `make -j4` in `object/`, remembering to add `dftracer_init_fini.o` to `ALL_OBJ_FILES`).
- Smoke `flash.par`: 16³ grid, 1 MPI rank, wall time < 5s (see Overview "Smoke Test vs Production Run" table — never compare a smoke test to a production run).
- `DFTRACER_INIT` mode to try FIRST: `FUNCTION` (with the C/C++ region annotations + shim). Env: `DFTRACER_ENABLE=1`, `DFTRACER_DATA_DIR=all`, `DFTRACER_LOG_FILE=<WS>/annotated/traces/raw/smoke` (get exact path via `session_get_run_paths(run_id, "annotated")` or whatever run_name the smoke test uses), `LD_LIBRARY_PATH` including dftracer lib64, HDF5 1.14 lib, CCE lib.
- Smoke command (single node, no `flux proxy` needed): `flux run -N 1 -n 1 -x DFTRACER_ENABLE -x DFTRACER_INIT -x DFTRACER_DATA_DIR -x DFTRACER_LOG_FILE -x LD_LIBRARY_PATH -x LD_PRELOAD ./flashx` from `cwd=<WS>/annotated/source/object`.

**Decision logic (must execute, not just note)**:
1. Run smoke test with `DFTRACER_INIT=FUNCTION` (no `LD_PRELOAD` needed, only the linked `.so`/shim). If trace files appear in `traces/raw/` with function-level events → FUNCTION mode works, proceed to Step 5 using FUNCTION mode as primary with PRELOAD as supplement for full IO coverage if desired.
2. If NO trace files appear (or empty/near-empty), **pivot to PRELOAD mode**: re-run with `DFTRACER_INIT=PRELOAD`, `LD_PRELOAD=<WS>/install/lib/python3.13/site-packages/dftracer/lib64/libdftracer_preload.so`, no source annotation needed. This is the expected/pre-documented outcome for CCE-linked Fortran; do not treat it as a build failure.
3. Whichever mode produces valid `.pfw`/`.pfw.gz` trace output becomes the mode for Step 6 (baseline production trace run).

**Expected artifacts**: rebuilt `flashx` binary in `<WS>/annotated/source/object/`, smoke trace files, a one-line verdict ("FUNCTION mode works" or "PRELOAD mode required") recorded into `<WS>/artifacts/` AND back into this plan file's Step 6 section (update the plan per living-document policy).

---

## STEP 5: dftracer-tracer

**Goal**: Run the BEST-CASE (largest meaningful, or explicitly-approved) baseline trace of Flash-X Sedov 3D with I/O routed to Lustre and dftracer traces routed to the workspace, then split traces.

**Inputs**: `run_id`, `run_name=baseline` (paths already resolved: `traces_raw=<WS>/baseline/traces/raw`, `traces_compact=<WS>/baseline/traces/compact`, `dftracer_log_prefix=<WS>/baseline/traces/raw/baseline`).
- **Mode**: whatever Step 4 determined (FUNCTION or PRELOAD). Given the documented CCE constructor-firing risk, default expectation is **PRELOAD**, `DFTRACER_DATA_DIR=all`.
- **Decision point for human (MANDATORY before this step runs)**: ask for the active Flux allocation ID; confirm node count and whether this should be a smoke-scale or full production-scale run (production config below needs ≥30 min wall time, multi-GB checkpoints, all nodes in the allocation — do not default to production without explicit confirmation given cost).
- **Lustre setup**: `mkdir -p /p/lustre5/$USER/flashx/<run_name>/`; create/verify short symlink `<WS>/ds -> /p/lustre5/$USER/flashx` and set `basenm` in `flash.par` to `ds/<run_name>/sedov_` (stay under flash.par's 80-column limit).
- **Recommended production `flash.par` values** (Paramesh AMR mode — see Overview reference table): `iProcs=jProcs=kProcs=1`, `nblockx=nblocky=nblockz=9`, `lrefine_max=6`, `lrefine_min=4`, `checkpointFileIntervalTime=0.03`, `tmax=0.5`, `nend=1000000`, `wall_clock_time_limit=3600`, `useCollectiveHDF5=.true.`. Verify NXB/NYB/NZB compile-time value (Step 2 build log) and set `sim_rInit` accordingly.
- **Run pattern**: multi-node → wrap env exports (NOT module loads) in a bash script, submit via `flux proxy <alloc_id> flux run -N <nnodes> -n $((nnodes*48)) --exclusive ./production_run.sh` (script sets `DFTRACER_ENABLE=1`, `DFTRACER_INIT=<mode>`, `DFTRACER_DATA_DIR=all`, `DFTRACER_LOG_FILE=<WS>/baseline/traces/raw/baseline`, `LD_PRELOAD=...` if PRELOAD mode, `LD_LIBRARY_PATH`, `HDF5_USE_FILE_LOCKING=FALSE`, `MPICH_GPU_SUPPORT_ENABLED=0`). Single-node smoke-only run can use direct `flux run -x VAR ...` without proxy.
- **Post-run validation** (per Configuration Validation Checklist in `workload-flashx`): checkpoint file size ≥500MB, ≥50 timesteps in first 5 minutes, trace files growing in `traces/raw/`.
- Then call the trace-split MCP tool to produce `traces/compact/`.

**Expected artifacts**: `.pfw`/`.pfw.gz` files in `<WS>/baseline/traces/raw/`, split/compacted traces in `<WS>/baseline/traces/compact/`, checkpoint/plot files on Lustre under `/p/lustre5/$USER/flashx/baseline/`, run log in `<WS>/artifacts/`.

**Record lessons** in `workload-flashx` (dated) for any new Flux/Lustre/checkpoint-sizing surprises.

---

## STEP 6: dftracer-analyzer, then dftracer-diagnoser

**Goal**: Analyze the compacted baseline trace and diagnose I/O bottlenecks.

**Inputs**: `run_id`, `run_name=baseline`, `traces_compact=<WS>/baseline/traces/compact`.
- **Preset**: POSIX/HDF5-focused I/O preset (not DLIO — this is not an AI/ML DLIO workload). Use `list_presets` to confirm exact preset name before running; if only "posix" vs "dlio" exist, choose "posix" and ensure HDF5 view is included since Flash-X I/O is HDF5-based.
- **Views to prioritize**: per-function/per-call time breakdown for I/O routines, HDF5 write/attribute-create breakdown, POSIX read/write/open breakdown, MPI-IO collective vs independent breakdown.
- **Known prior finding to check against** (from `workload-flashx`, 2026-07-08 trace analysis, may or may not reproduce at new scale): `io_h5write_unknowns_` ~32% of I/O time; xfer pipeline (`io_xfer_cont_slab`→`io_h5_xfer_wrapper`→`io_h5_xfer`→`io_h5_type_matched_xfer`) ~26.6%; `io_h5_attribute_create` (156 calls) ~9.6%. Confirm or refute against this run's actual numbers — do not assume they transfer unchanged if grid size / rank count differ from the prior run.
- **Diagnoser** should rank bottlenecks by wall-time contribution and propose candidate optimization levers (see Step 7) tied to specific evidence (call counts, time %, collective vs independent I/O ratio).

**Expected artifacts**: analysis summary (JSON/table) and a ranked bottleneck list handed to Step 7, written to `<WS>/artifacts/`.

---

## STEP 7: dftracer-optimizer

**Goal**: Run the L1/L2/L3 optimization loop targeting Lustre I/O time reduction for Flash-X Sedov 3D, iterating against the baseline trace from Step 5/6.

**Inputs**: `run_id`, baseline metrics from Step 6, `run_name` for each iteration (e.g. `opt1`, `opt2`, ... — get paths via `session_get_run_paths` per iteration, never hand-build).
- **Metric objective**: minimize total I/O wall-time fraction (specifically HDF5 write + attribute-create + xfer-pipeline time) while keeping checkpoint correctness/size expectations from the validation checklist.
- **Candidate levers, in priority order** (seeded from Step 6 diagnosis and prior-run lessons):
  1. Increase `checkpointFileIntervalTime` (e.g. 0.01→0.05, or per Step 6 finding) to reduce checkpoint frequency.
  2. Reduce `plot_var_N` count (fewer plotted variables → less attribute/write overhead).
  3. Enable/confirm `useCollectiveHDF5=.true.` and tune `ROMIO_CB_WRITE=enable`, `CB_BUFFER_SIZE=16777216` for Lustre collective buffering.
  4. Lustre striping: verify/set stripe count to match or exceed OST count (4-8 on Tuolumne) on the output directory before writing.
  5. `HDF5_USE_FILE_LOCKING=FALSE` (already default per run pattern) to avoid Lustre lock contention.
- **Iteration protocol**: each iteration = apply ONE lever (or a validated combination) → rebuild if a compile-time flag, else just edit `flash.par`/env → run at the SAME scale/class as baseline (same node count, same run class: do not compare smoke vs production) → split+analyze trace → compare I/O time % against baseline and previous iteration.
- **Termination criteria**: stop when (a) an iteration yields <2% further improvement over the previous best, or (b) all 5 candidate levers have been tried, or (c) max 4 optimization iterations reached — whichever comes first. Always keep the best iteration's config as the final recommendation.
- **Comparison output**: table of baseline vs each opt iteration — total I/O time %, HDF5 write time, attribute-create time, checkpoint size, wall time — written to `<WS>/artifacts/optimization_comparison.md` (or similar).

**Expected artifacts**: per-iteration run dirs (`<WS>/opt1`, `<WS>/opt2`, ...) each with traces + analysis, final comparison table, and a recommended `flash.par`/env configuration.

---

## DISPATCH ORDER
dftracer-build-dftracer, dftracer-build-app, dftracer-annotator, dftracer-build-smoke, dftracer-tracer, dftracer-analyzer, dftracer-diagnoser, dftracer-optimizer
