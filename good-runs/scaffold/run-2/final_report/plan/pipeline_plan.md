# DFTracer Pipeline Plan — scaffold/20260709_081340

## Overview

**App:** ScaFFold (LBANN) — Python/PyTorch 3D U-Net benchmark trained on
procedurally-generated fractal volumes. Pure-Python package (`ScaFFold/`),
installed as a console entry point `scaffold` via `pyproject.toml`. NOT a
compiled/C project -> use the **Python annotation path**
(`ml_annotate_*` / `python_annotate_*` tools / `dftracer-annotate-python`
agent), never clang/C annotators.

**Key source facts (verified from clone, do not re-derive):**
- Repo root: `source/` -> package at `source/ScaFFold/`
- Entry points used at runtime:
  - `scaffold generate_fractals -c ScaFFold/configs/benchmark_default.yml`
  - `scaffold benchmark -c ScaFFold/configs/benchmark_default.yml` (launched via `torchrun-hpc`)
- CLI dispatch: `ScaFFold/cli.py` (321 lines)
- Training loop / DDP / checkpoint / I/O: `ScaFFold/worker.py` (316 lines) — primary annotation target (train step, dataloader iteration, checkpoint save/load).
- Fractal dataset generation: `ScaFFold/generate_fractals.py` (48 lines) + `ScaFFold/datagen/`, `ScaFFold/fractals/`
- Model: `ScaFFold/unet/`
- Benchmark driver: `ScaFFold/benchmark.py` (97 lines)
- Config: `ScaFFold/configs/benchmark_default.yml` — relevant keys: `dataset_dir: "datasets"`, `base_run_dir: "benchmark_runs"`, `fract_base_dir: "fractals"`, `checkpoint_dir: "checkpoints"`, `problem_scale: 7` (default; too large for a bounded smoke — override to a small scale, see STEP 6), `checkpoint_interval: -1` (disabled by default; must enable a small interval for I/O tracing), `epochs: -1` (must override to a small finite N for bounded runs), `batch_size: 1`, `dataloader_num_workers: 1`, `dist: 1`.
- Install docs (README) confirm ROCm path for Tuolumne (MI300A): `ml cce/21.0.0 cray-mpich/9.1.0 rocm/7.1.1 rccl/fast-env-slows-mpi`, then `pip install .[rocmwci] --prefix=.venvs/scaffoldvenv` (LLNL WCI wheel) or `.[rocm]` generic with `--extra-index-url https://download.pytorch.org/whl/rocm7.1`. Prefer WCI path on Tuolumne if the wheel is available; otherwise generic ROCm.
- `scripts/install-rccl.sh` may be needed for the ccl plugin unless using the WCI wheel.
- Run launcher: `torchrun-hpc -N <nodes> -n <procs-per-node> --gpus-per-proc 1 $(which scaffold) benchmark -c <config>`. Recall: `-n` is procs-per-node, NOT total procs. Target scale for the bounded/full runs in this plan: 8 nodes x 4 GPUs -> `-N 8 -n 4 --gpus-per-proc 1`. Use a smaller shape (e.g. `-N 1 -n 4`) for the smoke test.

**System (tuolumne, from system_detect):**
- AMD MI300A APU cluster, Cray PE, no sudo, launcher `flux run` (torchrun-hpc wraps this).
- Module load order: craype-x86-trento, libfabric/match_SHS, craype-network-ofi, perftools-base/25.09.0, craype/2.7.35, PrgEnv-cray/8.7.0, flux_wrappers/0.1, xpmem/2.6.5, cce/20.0.0, cray-libsci/25.09.0, cray-mpich/9.0.1, python/3.13.2. (README suggests cce/21.0.0 + cray-mpich/9.1.0 + rocm/7.1.1 for ScaFFold specifically — STEP 1 must reconcile these against what's actually available via `module avail` and record the resolved set.)
- `export LD_LIBRARY_PATH="/opt/cray/pe/cce/20.0.0/cce/x86_64/lib:/opt/cray/pe/cce/20.0.0/cce/x86_64/lib/default64:/usr/lib64:${LD_LIBRARY_PATH}"` — MUST include `/usr/lib64` (libdl) or dftracer_core / brahma linking fails with `undefined reference: dlopen`. Set this BEFORE `session_install_dftracer` / `session_build_annotated` — those run in a separate process that does not inherit Bash-tool exports; write it into the wrapper script and any env file the build tools read.
- Never reload StdEnv; if module commands fail check `module list | grep StdEnv`.
- Always write a bash wrapper script for any `flux proxy` / `flux run` command — never inline module loads into an ad hoc one-off command.

**Canonical paths (from session_get_run_paths, run_name="baseline"; re-derive per run_name for other runs):**
- Workspace root: `$PROJECT_ROOT/workspaces/scaffold/20260709_081340`
- `source_dir`: `<WS>/source` (already cloned)
- `baseline/source`, `baseline/traces/raw`, `baseline/traces/compact`, `baseline/scripts`, `baseline/patches`
- `dftracer_log_prefix` (baseline): `<WS>/baseline/traces/raw/baseline`
- Subdirs already present in `<WS>`: `performance/`, `source/`, `baseline/`, `annotated/`, `artifacts/`, `tmp/`, `dataset/`
- **Data placement rule:** app data (`datasets/`, `fractals/`, `benchmark_runs/` incl. `checkpoints/`) -> Lustre (use `<WS>/dataset/` if it is Lustre-backed, else the site's scratch/Lustre root — STEP 1 confirms). **dftracer TRACES -> `<WS>/<run_name>/traces/` (NOT Lustre)**, because `session_optimization_iteration` reads traces from the session workspace. Set `DFTRACER_LOG_FILE` to the session workspace trace path for every run, e.g. `DFTRACER_LOG_FILE=<WS>/baseline/traces/raw/baseline-`.
- One shared venv for dftracer + ScaFFold — do not create separate installs.
- Bound every training run to ~30 min wall clock (small `epochs`, small `problem_scale`, or a training-step cap if the app config exposes one — check `worker.py` for a max-steps/iteration knob during STEP 1).
- dfanalyzer calls: always `cluster_n_workers=32`, never `cluster_cores`.
- Optimization loop: bounded to **max 4 iterations** (L1/L2/L3 escalation + compare), terminate early if the target metric (I/O time fraction or wall-clock) stops improving by >5% between iterations, or on the first iteration that regresses accuracy convergence signal (dice/loss) — the diagnoser step defines the exact metric before the loop starts.

## STEP 1: dftracer-session-setup
**Status: COMPLETED** (2026-07-09)

Inputs:
- `run_id=scaffold/20260709_081340`. Session already exists and source already cloned (see Overview) — do NOT re-clone; call `session_status` to confirm, then proceed to environment setup only.
- Detect build tool: this is a pure Python package (`pyproject.toml` at repo root, no CMakeLists/Makefile) -> classify as `python-pip`, not cmake/autotools. ✓ **CONFIRMED**
- Resolve module set: try README's ScaFFold-specific combo first (`cce/21.0.0`, `cray-mpich/9.1.0`, `rocm/7.1.1`, `rccl/fast-env-slows-mpi`) via `module avail`; fall back to the system_detect default list if any module is unavailable (see Overview "System" section for defaults). Record the resolved, actually-loadable module list into this section (edit `pipeline_plan.md` STEP 1 in place) before STEP 2 runs.

**Resolved Module Stack** (via `module avail` + `module spider`):
  - cce/21.0.0 ✓ FOUND (system_detect default was 20.0.0)
  - cray-mpich/9.1.0 ✓ AUTO-LOADED when cce/21.0.0 is swapped (system_detect default was 9.0.1)
  - rocm/7.1.1 ✓ FOUND (new, not in system_detect default)
  - rccl/fast-env-slows-mpi ✓ FOUND (new, not in system_detect default)
  - python/3.13.2 ✓ FOUND (from system_detect default)
  - cray-libsci/26.03.0 ✓ AUTO-UPDATED when cce/21.0.0 was swapped

**Load sequence (in env.sh)**:
  ```
  module load PrgEnv-cray
  module swap cce cce/21.0.0          # triggers auto-swap of cray-mpich 9.0.1 -> 9.1.0
  module load rocm/7.1.1
  module load rccl/fast-env-slows-mpi
  module load python/3.13.2
  ```
No inactive modules detected. All versions are compatible and form a stable stack.

- Create ONE venv shared by dftracer and ScaFFold: ✓ **COMPLETED**
  - Path: `<WS>/venv`
  - Venv python: `$PROJECT_ROOT/workspaces/scaffold/20260709_081340/venv/bin/python3`
  - pip upgraded to 26.1.2

- `export LD_LIBRARY_PATH=/usr/lib64:$LD_LIBRARY_PATH` plus the CCE lib dirs from Overview — write this into a wrapper script `<WS>/scripts/env.sh` sourced by every subsequent step (build, install, run). ✓ **COMPLETED**
  - Created: `<WS>/scripts/env.sh` (executable, sources modules + sets LD_LIBRARY_PATH + activates venv)
  - LD_LIBRARY_PATH: `/opt/cray/pe/cce/21.0.0/cce/x86_64/lib:/opt/cray/pe/cce/21.0.0/cce/x86_64/lib/default64:/usr/lib64`

- Confirm whether RCCL plugin build (`scripts/install-rccl.sh`) is required, or whether the LLNL WCI wheel (`.[rocmwci]`) already bundles it — check `source/scripts/install-rccl.sh` and `source/pyproject.toml` extras during this step.
  ✓ **DECISION: RCCL plugin NOT required**
    - `scripts/install-rccl.sh` does not exist in the source
    - `pyproject.toml` includes 'rocmwci' extra with pre-built LLNL WCI wheels:
      - torch==2.10.0+rocm710
      - mpi4py==4.1.1+mpich.9.1.0
    - These wheels bundle RCCL and are tested/validated for Tuolumne
    - Use: `pip install .[rocmwci]` in STEP 2

- Confirm Lustre root for app data: check `<WS>/dataset` — if not itself Lustre, resolve to the site Lustre scratch path and record it here (e.g. `dataset_dir`, `fract_base_dir`, `base_run_dir`/`checkpoint_dir` in the benchmark config all point under this Lustre path). ✓ **CONFIRMED**
  - Lustre mount: /p/lustre5 (42P capacity, 35P available)
  - App data root: `/p/lustre5/$USER/workspaces/scaffold`
  - Subdirs created: fractals, datasets, runs, checkpoints
  - Traces remain in: `<WS>/<run_name>/traces/` (NOT Lustre, per STEP 6 rule)

- HDF5 requirement check: ✓ **NOT NEEDED**
  - ScaFFold uses PyTorch + in-situ fractal generation, no HDF5 I/O
  - Skip HDF5 source build; system HDF5 1.10.5 available if dftracer needs it

Artifacts:
- `<WS>/scripts/env.sh` — sourced by every downstream step
- `<WS>/artifacts/01_session_setup.log` — full resolution log with all decisions
- Resolved module list and data paths documented here (above)

## STEP 2: dftracer-build-app
**Status: COMPLETED** (2026-07-09)

Inputs:
- Use `<WS>/scripts/env.sh` from STEP 1 for modules + LD_LIBRARY_PATH + venv activation.
- Install ScaFFold (original, unannotated) into the shared venv from `source/`: `pip install .[rocmwci]` or `pip install ".[rocm]" --extra-index-url https://download.pytorch.org/whl/rocm7.1` if WCI wheels unavailable.
- Verify: `python -c "import ScaFFold; import torch; print(torch.__version__)"` and `which scaffold` inside venv, cwd = `<WS>/source`.

**Build Outcome:**
- Installation method: `pip install ".[rocm]" --extra-index-url https://download.pytorch.org/whl/rocm7.1`
- Reason: rocmwci wheels not available on standard PyPI; generic rocm extra resolved successfully
- PyTorch version: 2.12.0+rocm7.1 (installed successfully)
- HIP support: 7.1.52802 verified working; GPU device count = 4
- Venv python: `$PROJECT_ROOT/workspaces/scaffold/20260709_081340/venv/bin/python3`
- Scaffold CLI entry: `$PROJECT_ROOT/workspaces/scaffold/20260709_081340/venv/bin/scaffold`
- mpi4py: 4.1.1 installed (requires Cray MPI linkage fix; deferred to STEP 3)
- Core packages verified: torch, numpy, ScaFFold, distconv all import successfully

**Known Issue (non-blocking):**
- mpi4py generic PyPI wheel cannot load Cray libmpi.so out-of-box (expected Tuolumne caveat)
- Symptom: "cannot load MPI library" when importing modules using `from mpi4py import MPI`
- Impact: Deferred to STEP 3/run setup; single-node torch tests do not require MPI
- Workaround: STEP 3 dftracer install or MPI4PY_MPIABI env var / patchelf may resolve

**Artifacts:**
- Working shared venv: `<WS>/venv/`
- Build logs: `<WS>/artifacts/02_build_app.log` and `02_build_summary.txt`


## STEP 3: dftracer-build-dftracer
Inputs:
- Same shared venv as STEP 2 — do NOT create a second venv (dftracer and the app MUST share one venv for this AI/ML workload).
- `LD_LIBRARY_PATH` from `<WS>/scripts/env.sh` MUST be exported before this step's install call (separate process, does not inherit Bash-tool exports).
- Build HDF5 from source into the session workspace if any HDF5 use is detected in ScaFFold's I/O path (check during STEP 1/4 scoping — likely not needed since ScaFFold uses fractal/numpy data, not HDF5; confirm before skipping).
- Install dftracer + dftracer-utils via `session_install_dftracer` MCP tool into the shared venv, Python bindings mode (this is a Python app, not C/C++). Skip ROCProfiler feature unless ScaFFold itself directly instruments ROCm kernels (check `worker.py`/`unet/` for ROCm profiler calls before enabling).
- MPI feature: enable with the resolved `cray-mpich` version + headers from STEP 1 (compatible on Tuolumne — no special patching needed beyond passing the version).
Artifact expected: `dftracer` + `dftracer_utils` importable from the shared venv, features_enabled list, `<WS>/artifacts/03_build_dftracer.log`.

## STEP 4: dftracer-annotate-python (scoping + annotation) then dftracer-validate-python
Inputs:
- Language: Python. Use `python_annotate_*` / `ml_annotate_*` tools, NOT clang.
- Scope candidate files (confirm file list before annotating — this is a human decision point if the file count is large):
  - `source/ScaFFold/worker.py` (train step, DDP, dataloader iteration, checkpoint save/load — highest priority, primary I/O + compute loop)
  - `source/ScaFFold/benchmark.py` (benchmark driver / epoch loop)
  - `source/ScaFFold/cli.py` (entry dispatch — light annotation, mainly for phase boundaries)
  - `source/ScaFFold/generate_fractals.py` (dataset generation — separate phase, annotate as its own category since it's typically a one-time setup cost)
  - `source/ScaFFold/datagen/` (fractal IFS generation internals — check for hot-loop functions to EXCLUDE from per-call annotation; annotate at coarser granularity, e.g. per-batch not per-fractal-point)
  - `source/ScaFFold/unet/` (model forward/backward — annotate at layer-block granularity if at all; do NOT annotate every tensor op)
  - `source/ScaFFold/utils/` (check for checkpoint I/O helpers, config I/O)
  - Exclude: anything under `source/ScaFFold/viz/`, `source/ScaFFold/package_data/`, `source/ScaFFold/readme_figs/` (visualization/packaging, not on the training hot path).
- Smoke-test command to scope files (run once BEFORE full annotation to confirm which modules actually execute in the training hot path): `cd <WS>/source && python -c "from ScaFFold import worker, benchmark, cli"` plus a dry run of `scaffold generate_fractals -c ScaFFold/configs/benchmark_default.yml` with a tiny `n_categories`/scale override.
- Annotate write target: `<WS>/annotated/` (copy of `source/` with `@dft_fn` decorators / `DFTRACER_PY_FUNCTION` markers added). Never edit `source/` in place.
- Exclude hot-loop / per-sample functions from fine-grained annotation (e.g. per-pixel or per-IFS-point functions inside `datagen/`) — annotate at the batch/epoch/checkpoint granularity instead, per the dftracer Python annotation skill's cost-gate guidance.
- After annotation, run `dftracer-validate-python` (or the annotator's own validation pass) to lint decorator placement and confirm no syntax breakage: `python -m py_compile` on every touched file, then re-run the import smoke test against `<WS>/annotated/`.
Artifact expected: annotated tree in `<WS>/annotated/`, list of annotated files + excluded hot-loop functions, validation pass/fail, `<WS>/artifacts/04_annotate.log`. **Decision point for a human:** confirm the final file list (approx. 6-8 files across worker/benchmark/cli/generate_fractals/datagen/unet/utils) before annotation proceeds if the count differs materially from this estimate.

## STEP 4b: dftracer-validate-python
**Status: COMPLETED** (2026-07-09)

Independent validation of STEP 4's annotation output. Did NOT rubber-stamp
STEP 4's self-assessment of the 6 residual `validate_annotations` findings —
traced actual call graphs via `graph_query`/grep before accepting or fixing
each one.

**Findings and disposition:**
- `datagen/instance.py:main()` (save) — **FIXED** (was wrongly deemed
  "one-time setup"). Reachable at runtime: `scaffold generate_fractals` ->
  `generate_fractals.main()` -> `instance.main()`. Added `@_dft.log`.
- `datagen/volumegen.py:load_np_ptcloud()`, `main()` (load/open/save) —
  **FIXED** (was wrongly deemed "one-time setup"). Reachable from the
  **training** entry path, not just fractal-gen: `worker.py:main()` ->
  `get_dataset()` -> `volumegen.main()` -> `load_np_ptcloud()`. Added
  `@_dft.log` to both.
- `viz/standard_viz.py:main()` (open) — **FIXED** (was wrongly deemed
  "one-time setup"/excluded per original STEP 4 scope). It is in fact
  called from `worker.py:main()` (rank 0, end of every training run,
  `standard_viz.main(config)`), i.e. on the real training entry path.
  Added `@_dft.log`.
- `datagen/mask_detection.py:unique_mask_values()`, `main()` (load/open) —
  **ACCEPTED, no fix.** Confirmed via repo-wide grep: this module has zero
  callers anywhere in `ScaFFold/` (not imported by cli.py, worker.py,
  benchmark.py, generate_fractals.py, or any datagen `__init__`). It is a
  genuinely orphaned standalone dev/debug script. This is the one finding
  where STEP 4's "one-time setup, acceptable" judgment was correct.

**Additional gaps found beyond the validator's 6 findings (validator does
not flag every I/O call, e.g. `Path.write_text`/`.rename`/`.mkdir`):**
- `datagen/get_dataset.py:get_dataset()` — unannotated despite doing file
  I/O (`mkdir`, `write_text`, `rename`) and being called **directly** from
  `worker.py:main()` (the primary training entry point) to materialize/reuse
  the dataset before every training run. **FIXED**: added `@_dft.log`.
- `worker.py` app-parameter metadata was broken: `_dft_log.log_metadata_event(...)`
  calls were passing **literal string values** (e.g. `"config.batch_size"`)
  instead of the actual runtime config values, and were placed at **module
  import time**, before `config` (a local `Namespace` built inside `main()`)
  even exists. **FIXED**: moved the 7 metadata calls inside `main()`,
  immediately after `config = Namespace(**kwargs_dict)`, using
  `str(config.<field>)` for real values.
- **Missing finalize on the `generate_fractals` subcommand path.**
  `worker.py`'s module-level `dftracer.initialize_log(...)` runs
  unconditionally (because `cli.py` always does
  `from ScaFFold import benchmark, generate_fractals`, and `benchmark.py`
  unconditionally does `from ScaFFold import worker`, which executes
  worker.py's module body). But `_dft_log.finalize()` previously only
  existed at the end of `worker.py:main()`, which is only called on the
  `benchmark` subcommand. On the `generate_fractals` subcommand path,
  `worker.main()` is never invoked, so the trace file was **never closed**
  — a truncated trace, silently dropping the newly-annotated fractal/
  instance-generation I/O events. **FIXED**: added
  `dftracer.get_instance().finalize()` in `generate_fractals.py`, right
  before `MPI.Finalize()`, on the `generate_fractals` path.
- **Decorator stacking order (PP6) violated in 2 places**: `worker.py:main()`
  and `dice_score.py:compute_sharded_dice()` had `@_dft.log` stacked ABOVE
  `@annotate()` (the perf_measure/Caliper decorator) instead of closest to
  `def`. Per the Python annotation lessons (PP6), the dftracer decorator
  must be the LAST decorator before `def`. **FIXED** both; confirmed via a
  repo-wide AST sweep that no other file has this ordering violation.
- **Cost-gate completeness gap in `utils/dice_score.py`**: STEP 4 only
  touched 1 function in this file (the `SpatialAllReduce.forward` static
  method, correctly done via `with DFTracerFn(...)` context manager — this
  pattern IS correct, verified). `python_estimate_file_costs` flags
  `dice_coeff` (score 42) and `compute_sharded_dice` (score 46) for
  annotation — both compute the Dice score on every training/validation
  step — but neither had a dftracer decorator. **FIXED**: added `@_dft.log`
  to both (kept below `@annotate()` per PP6). `multiclass_dice_coeff`,
  `dice_loss`, and `SpatialAllReduce.backward` are correctly left
  unannotated (Rule 0: trivial bodies, no I/O/comm, confirmed by the cost
  estimator).

**Verified genuinely correct (not just re-stated from STEP 4):**
- Init (`dftracer.initialize_log(...)`, `worker.py:52`, module import time)
  and finalize (`worker.py:_dft_log.finalize()`, single return path, not the
  first statement) ARE reachable on the real `scaffold` -> `cli.py:main()`
  -> `benchmark.main()` -> `worker.main()` chain (traced import graph, not
  assumed).
- App-parameter metadata IS emitted (after the above fix) with real values.
- The static-method + `with DFTracerFn()` context-manager pattern in
  `dice_score.py:SpatialAllReduce.forward` (wraps the `dist.all_reduce`
  call) is correct per the Python annotation skill — no `@_dlp.log_static`
  anywhere in the tree (confirmed via grep).
- No per-element/per-tensor hot-path over-decoration: `dice_coeff`,
  `multiclass_dice_coeff` etc. are called once per batch (not per-voxel/
  per-tensor-element), consistent with batch_size=1 in the default config.
- All annotated files pass `python -m py_compile` (full-tree sweep) and
  `ast.parse` after every fix.

**Tool gap found (escalate, do not silently work around):** the plan's
`session_annotation_report` MCP tool returned `0/83 functions annotated
(0.0% coverage)` for this tree even after all fixes above, because it keys
strictly off `annotation_log_present`/`annotation_status.md` (never written
by this session's STEP 4) rather than falling back to AST/decorator
detection in the files themselves. This contradicts `validate_annotations`
(the tool that actually inspects source), which correctly reports only 1
residual file (`mask_detection.py`, accepted). Treat
`session_annotation_report`'s coverage percentage as unreliable for
sessions where the annotator did not write `annotation_status.md`, and use
`validate_annotations` as the source of truth instead. Recommend fixing
`session_annotation_report` to fall back to direct decorator/AST scanning
when the status log is absent.

Artifacts: `<WS>/artifacts/05_validate_python.log` (this step's findings and
fixes, as summarized above).

## STEP 5: dftracer-build-smoke
Inputs:
- Subfolder: `<WS>/annotated/`.
- Reinstall the annotated package over the shared venv: `pip install --force-reinstall --no-deps <WS>/annotated 2>&1 | tee <WS>/artifacts/05_build_smoke.log` (using `<WS>/scripts/env.sh`).
- `DFTRACER_INIT` mode: **CONFIRMED** — Python-native init only, no PRELOAD/C shim needed. `dftracer.initialize_log(logfile=None, data_dir=None, process_id=-1)` is called at `worker.py` module import time (line ~52) and is reachable unconditionally on both CLI subcommands (`benchmark` and `generate_fractals`) because `cli.py` unconditionally imports `benchmark`, which unconditionally imports `worker`. `_dft_log.finalize()` is called at the end of `worker.py:main()` (benchmark path) AND now also in `generate_fractals.py` via `dftracer.get_instance().finalize()` before `MPI.Finalize()` (generate_fractals path) — see STEP 4b fix for the previously-missing finalize on that path.
- Set `DFTRACER_LOG_FILE=<WS>/baseline/traces/raw/smoke-` and `DFTRACER_DATA_DIR` (if used) pointing at the Lustre dataset root from STEP 1 — traces still go to `<WS>`, only app data is on Lustre.
- Smoke command (single node, tiny config, cwd = `<WS>/source` or a copy under `<WS>/annotated` — never project root): generate a minimal fractal set first, then
  `torchrun-hpc -N 1 -n 1 --gpus-per-proc 1 $(which scaffold) benchmark -c <smoke_config.yml>` where `smoke_config.yml` overrides `problem_scale` to a small value (e.g. 3-4), `epochs` to 1-2, `checkpoint_interval` to 1 (must be >0 to exercise checkpoint I/O), `n_categories`/`n_instances_used_per_fractal` reduced for speed.
- Bound this smoke run to a few minutes, well under the 30-min budget.
Artifact expected: exit code 0, at least one `.pfw`/trace file created under `<WS>/baseline/traces/raw/`, `<WS>/artifacts/05_build_smoke.log`.

## STEP 6: dftracer-tracer
Inputs:
- `run_name="baseline"`; paths from `session_get_run_paths(run_id, "baseline")` (see Overview canonical paths).
- Allocation shape for the best-case/full trace run: 8 nodes x 4 GPUs -> `torchrun-hpc -N 8 -n 4 --gpus-per-proc 1 $(which scaffold) benchmark -c <baseline_config.yml>`.
- `baseline_config.yml`: derived from `ScaFFold/configs/benchmark_default.yml` with `epochs` set to a small finite N and `problem_scale` kept representative (e.g. default 7, or scale down only if the ~30-min budget requires it — prefer keeping `problem_scale` realistic and instead capping `epochs`), `checkpoint_interval` set >0 (e.g. every epoch) so checkpoint I/O is captured, `dataset_dir`/`fract_base_dir`/`base_run_dir` pointed at the Lustre root from STEP 1.
- `env_extra`: `DFTRACER_LOG_FILE=<WS>/baseline/traces/raw/baseline-`, `DFTRACER_DATA_DIR=<lustre_dataset_root>`, plus everything from `<WS>/scripts/env.sh`.
- Bound total wall clock to ~30 minutes; if `problem_scale=7` at 8x4 does not fit, reduce `epochs` first, then `problem_scale` only as a last resort (record the resolved values here).
- After the run, split traces: `session_split_traces` (or dftracer_utils split CLI) writing to `<WS>/baseline/traces/compact/`.
Artifact expected: `run_id` (baseline), raw trace files in `<WS>/baseline/traces/raw/`, compact/split output in `<WS>/baseline/traces/compact/`, wall-clock duration, `<WS>/artifacts/06_tracer.log`.

## STEP 7: dftracer-analyzer then dftracer-diagnoser
Inputs (analyzer):
- Preset: this is an AI/ML training workload (not raw POSIX-only, not classic DLIO) — use the ML/DLIO-style preset if `list_presets()` offers one for PyTorch training; otherwise the generic `posix` preset over the compact traces plus a Python function-time view for the `@dft_fn`-annotated regions.
- `cluster_n_workers=32` for every `session_analyze_traces` call — NEVER `cluster_cores`.
- Views to request: per-phase time breakdown (dataset/fractal generation vs. dataloader vs. compute (forward/backward) vs. checkpoint I/O vs. communication/DDP), I/O time fraction, POSIX read/write bandwidth to the Lustre dataset/checkpoint paths.
- `checkpoint dir` for analyzer output: `<WS>/baseline/analysis/` (create if absent).
Inputs (diagnoser):
- Consume the analyzer output above; identify top bottleneck categories (e.g. dataloader stall, checkpoint write time, small-file fractal reads, DDP allreduce wait).
- Define the exact objective metric for the optimization loop here (e.g. "reduce checkpoint-I/O wall-clock fraction" or "reduce total step time"), since STEP 8 needs a single scalar objective and termination rule.
Artifact expected: bottleneck list ranked by impact, chosen objective metric + termination criteria for STEP 8, `<WS>/artifacts/07_analyze.log` and `<WS>/artifacts/07_diagnose.log`.

## STEP 8: dftracer-optimizer
Inputs:
- Objective metric and termination criteria: as defined by STEP 7's diagnoser output (fill in here once STEP 7 completes; do not re-derive).
- Max iterations: **4** (L1 config-only tweaks e.g. `dataloader_num_workers`, `batch_size`, checkpoint interval/format -> L2 code-level e.g. async checkpoint, prefetch -> L3 structural e.g. sharding/DistConv params `dc_num_shards`/`dc_shard_dims` -> proposals+compare).
- Each iteration: apply one change to a fresh `<WS>/opt<N>/` run dir (via `session_get_run_paths(run_id, "opt<N>")`), rebuild/reinstall into the shared venv if code changed, rerun the STEP 6 trace command at the same 8x4 allocation and ~30-min bound, rerun STEP 7's analyzer with `cluster_n_workers=32`, compare the objective metric against baseline and the previous iteration.
- Traces for every optimization iteration MUST land under `<WS>/opt<N>/traces/` (session workspace), never Lustre — `session_optimization_iteration` reads from there.
- Early-stop if improvement <5% between consecutive iterations, or after 4 iterations, whichever first. Report a final ranked comparison table (baseline vs opt1..opt4) with the objective metric and wall-clock.
Artifact expected: comparison table, best configuration found, all iteration logs under `<WS>/artifacts/08_opt<N>.log`.

**Status: COMPLETED** (2026-07-09). Objective metric: total_train_time (s, lower better) / FOM (higher better); I/O-health = POSIX+STDIO event count + aggregate I/O time.

| Iter | Change (level) | total_train_time | FOM | vs baseline | Kept? | Citation |
| --- | --- | --- | --- | --- | --- | --- |
| baseline | num_workers=0, sync loading | 0.626 s | 1.596 | - | - | - |
| opt1 | dataloader_num_workers 0->4 + auto persistent_workers/prefetch_factor=2 (L1, config-only) | 0.340 s | 2.944 | **-45.7%** | **YES (best)** | Mohan et al VLDB2021, arxiv.org/abs/2007.06775 |
| opt2 | + async_save=1 checkpoint offload (L1, config-only) | 0.462 s | 2.164 | -26.2% (but +36% WORSE than opt1) | NO (reverted) | Mohan et al FAST2021 CheckFreq |
| (L3) | Lustre DoM/stripe on dataset dirs | not run | - | - | N/A already optimal | Lustre PFL docs |

**Best config = opt1** (`dataloader_num_workers: 4`, else baseline). Config: `<WS>/opt1/opt1_config.yml`.

Key findings:
- opt1 comparator (baseline vs opt1 raw): aggregate I/O time 41.4s->28.5s (-31.1%), total data volume 6.880->6.888 GB (+0.1%, SAME work), open64 dur -89.7%, mkdir dur -77.9%, close dur -54.3%, POSIX ops 453751->427809. Speedup is real overlap, not "doing less".
- opt2 (async_save) REGRESSED vs opt1. checkpointing.py already gates torch.save on world_rank==0 (NOT the diagnosed N-to-N pattern); async offload adds ThreadPoolExecutor overhead with nothing to gain. Checkpoint=322 events=0.05% of I/O at scale 5.
- L3 Lustre striping NOT run: /p/lustre5 already provisions a Progressive File Layout w/ Data-on-MDT (comp0=pattern:mdt size 65536; then raid0 1->2->4->8->16). datasets/runs inherit it. Real .npy reads ~473KB p50 (above 64KB DoM threshold); metadata storm is MDS-bound regardless of stripe -> near-certain no-op, not worth an 8-node alloc.

Honest caveats (NOT verifiable at this scale): total_train_time is sub-second at scale 5, so absolute deltas carry run-to-run noise. opt1's -45.7% is large enough to trust; opt2's regression magnitude is within noise, but the conclusion "async_save gives nothing here" is robust because the code is already rank-0-gated. Checkpoint/comm optimizations can't be meaningfully evaluated until problem_scale/epochs make checkpoint write time a non-trivial fraction of epoch time.

## STEP 9: dftracer-privacy-guard
Inputs:
- Run after ALL self-learning writes (skills/agent-definition edits proposed by any step) have been confirmed and applied.
- `privacy_scan()` over the session workspace and any modified skill/agent files; must report `clean` before the pipeline is considered done.
Artifact expected: `clean` privacy scan result.

## DISPATCH ORDER
dftracer-session-setup, dftracer-build-app, dftracer-build-dftracer, dftracer-annotate-python, dftracer-validate-python, dftracer-build-smoke, dftracer-tracer, dftracer-analyzer, dftracer-diagnoser, dftracer-optimizer, dftracer-privacy-guard
