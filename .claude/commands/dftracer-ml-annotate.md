---
name: dftracer-ml-annotate
description: >
  Annotate ML/DL Python workloads end-to-end with dftracer AI/ML region decorators.
  Detects frameworks and ROCm/HIP requirements, installs dftracer with correct flags,
  annotates all Python files, builds/runs smoke test, collects traces, and auto-updates
  the lessons file with any new pitfalls discovered during the session.
---

Lessons file: workspaces/.agents/skills/dftracer-ml-annotation-lessons/SKILL.md

**Read the lessons file before doing anything else.** Apply every standing rule
(ML-R1 through ML-R16) and check every session log entry for context matching
the current application or framework.

DLIO benchmark (`dlio_benchmark`) is the canonical reference pattern. When in
doubt about how to annotate a function, match what DLIO does.

══════════════════════════════════════════════════════════════════════
STEP 0 — SYSTEM DETECTION
══════════════════════════════════════════════════════════════════════

Invoke the `system-detect` skill to load module/compiler environment.

Store environment as ENV (module paths, MPI wrappers, ROCm paths).


══════════════════════════════════════════════════════════════════════
STEP 1 — GATHER INPUTS  (if not supplied via arguments)
══════════════════════════════════════════════════════════════════════

If the user invoked this with named arguments (run_id=…, url=…, etc.),
use those directly. Otherwise ask one question at a time:

  Q1: "What is the Git URL of the ML application?"  → APP_URL
  Q2: "Branch or tag? (default: main)"              → REF
  Q3: "Smoke test command? (leave blank to auto-detect)" → SMOKE_CMD
  Q4: "Extra build flags? (leave blank to skip)"    → EXTRA_FLAGS

If run_id was supplied, skip Q1–Q4 and jump to Step 3.

Print: "Starting ML annotation pipeline for <APP_URL> @ <REF>"

Track pitfalls found this session in a list: PITFALLS = []


══════════════════════════════════════════════════════════════════════
STEP 2 — SESSION SETUP
══════════════════════════════════════════════════════════════════════

2a. Create session and clone:

    session_create(url=APP_URL, ref=REF)
    → store RUN_ID, WS

2b. Detect ML workload:

    session_detect_ml_workload(run_id=RUN_ID)

    Store:
      FRAMEWORKS       = result.frameworks
      HIP_NEEDED       = result.hip_tracing_needed
      ROCM_INFO        = result.rocm_info
      INSTALL_FLAGS    = result.install_flags
      DISTRIBUTED      = result.distributed
      HAS_DATALOADER   = result.has_dataloader
      CAPABILITIES     = result.capabilities

    Print:
      "Frameworks: <FRAMEWORKS>"
      "ROCm: <ROCM_INFO.found> (<ROCM_INFO.path>)"
      "HIP tracing needed: <HIP_NEEDED>"
      "Distributed: <DISTRIBUTED>"

    If HIP_NEEDED but ROCM_PATH is not set in ENV:
      PITFALLS.append({phase: "install", error: "ROCM_PATH not set",
        root_cause: "ROCm detected but ROCM_PATH env var absent",
        fix: "export ROCM_PATH=<ROCM_INFO.path> before install"})
      Set ROCM_PATH manually before Step 3.

2c. Configure and build original source:

    session_configure(run_id=RUN_ID, extra_cmake_flags=EXTRA_FLAGS)
    session_build_install(run_id=RUN_ID)

2d. Install dftracer with correct flags:

    session_install_dftracer(run_id=RUN_ID)

    The install reads INSTALL_FLAGS (which includes DFTRACER_ENABLE_HIP_TRACING=ON
    when HIP_NEEDED) automatically from the session state set by
    session_detect_ml_workload. No manual env override needed.

    On failure → record pitfall in PITFALLS, then stop.

2e. PyTorch extras (ML-R28, ML-R29) — always run when "pytorch" in FRAMEWORKS:

    After session_install_dftracer completes, install pydftracer from the
    feature/explict-io branch (has data.io, checkpoint.io, other.io — ML-R25/R26/R27)
    combined with the [dynamo] extra. Use PEP 440 direct-reference syntax:

      pip install --no-cache-dir "pydftracer[dynamo] @ git+https://github.com/llnl/pydftracer.git@feature/explict-io"

    WARNING: do NOT use "git+url@ref[extra]" syntax — pip parses [extra] as part
    of the ref and the checkout fails. Always use "pkg[extra] @ git+url@ref" form.

    This provides:
      - dftracer.python.torch.trace_handler  (PyTorch Profiler → category PP)
      - dftracer.python.dynamo.create_backend / @dynamo.compile  (Dynamo → category DY)
      - ai.data.io, ai.checkpoint.io, ai.other.io  (explicit IO phases — ML-R25/R26/R27)

    Verify after install:
      python -c "from dftracer.python.torch import trace_handler; from dftracer.python.dynamo import create_backend; from dftracer.python import ai; assert hasattr(ai.data,'io')"

    Store result:
      PYTORCH_EXTRAS_OK = True on success, False on failure (record pitfall but continue)

Print: "Setup complete. RUN_ID=<RUN_ID>  dftracer installed."


══════════════════════════════════════════════════════════════════════
STEP 3 — COPY SOURCE AND DISCOVER FILES
══════════════════════════════════════════════════════════════════════

3a. Copy source to annotated workspace:

    session_copy_annotated(run_id=RUN_ID)

3b. Discover Python source files:

    find_source_files(run_id=RUN_ID, folder="annotated", language="python",
      exclude_patterns=["**/test*", "**/__pycache__/**", "**/setup.py",
                        "**/conftest.py", "**/docs/**"])

    Categorize files into:
      ENTRY_FILES    = files with if __name__ == "__main__" or def main(
      TRAIN_FILES    = files with train / fit / backward / optimizer.step
      DATA_FILES     = files with __getitem__ / Dataset / DataLoader
      CKPT_FILES     = files with save_checkpoint / load_checkpoint / state_dict
      COMM_FILES     = files with all_reduce / dist.barrier / horovod / hvd.
      OTHER_FILES    = all remaining .py files

Print: "Files: entry=<N> train=<N> data=<N> ckpt=<N> comm=<N> other=<N>"


══════════════════════════════════════════════════════════════════════
STEP 4 — AI/ML REGION ANNOTATION  (python_annotate_ai_file)
══════════════════════════════════════════════════════════════════════

MANDATORY: Use `python_annotate_ai_file` for all files. NEVER manually
write decorators with Edit/Write. The tool handles idempotency, loop
wrapping, and import injection automatically.

Before annotating any function you are unsure about, call:

    dftracer_get_ai_annotation(
      function_name=<fn_name>,
      context=<what it does>,
      phase=<compute|data|dataloader|comm|device|checkpoint|pipeline>
    )

This returns the exact decorator and a ready-to-use code example.

### 4a. Entry-point files (is_entry=True)

    python_annotate_ai_file(
      run_id=RUN_ID, filepath=<file>,
      category=<module_stem>,
      is_entry=True, annotate_loops=True,
    )

    Verify the result contains:
      - initialize_log injected near the top
      - finalize() injected before program exit
      - @dft_ai on main() / run() / __call__()

### 4b. Training files

    python_annotate_ai_file(run_id=RUN_ID, filepath=<file>,
      category=<module_stem>, annotate_loops=True)

    Expected decorators:
      train / fit / run_epoch     → @dft_ai.pipeline.train
      evaluate / validate         → @dft_ai.pipeline.evaluate
      test                        → @dft_ai.pipeline.test
      forward                     → @dft_ai.compute.forward
      backward / loss.backward    → @dft_ai.compute.backward
      for epoch in ...:           → dft_ai.pipeline.epoch.iter(...)
      for batch in ...:           → dft_ai.dataloader.fetch.iter(...)

    Optimizer step — MUST use start/stop style (ML-R4):
      dft_ai.compute.step.start()
      optimizer.step()
      dft_ai.compute.step.stop()

    After annotating each file, add inside the batch loop:
      ai.update(step=step, epoch=epoch)   ← ML-R8

### 4c. Data/Dataset files

    python_annotate_ai_file(run_id=RUN_ID, filepath=<file>,
      category=<module_stem>, annotate_loops=True)

    Expected decorators:
      __getitem__ / read_index / load_sample  → @dft_ai.data.item
      preprocess / transform / augment        → @dft_ai.data.preprocess.derive(name="<op>")
      collate                                 → @dft_ai.data.preprocess.derive(name="collate")
      to_device / .cuda() / .to(device)       → @dft_ai.device.transfer

### 4d. Checkpoint files

    python_annotate_ai_file(run_id=RUN_ID, filepath=<file>,
      category=<module_stem>)

    Expected decorators:
      save / save_checkpoint / write_ckpt     → @dft_ai.checkpoint.capture
      load / load_checkpoint / restore_ckpt  → @dft_ai.checkpoint.restart

### 4e. Distributed communication files  (when DISTRIBUTED=True)

    python_annotate_ai_file(run_id=RUN_ID, filepath=<file>,
      category=<module_stem>)

    Expected — context manager style (ML-R5):
      dist.all_reduce(...)   → with dft_ai.comm.all_reduce(): ...
      dist.barrier()         → with dft_ai.comm.barrier(): ...
      dist.broadcast(...)    → with dft_ai.comm.broadcast(): ...
      dist.all_gather(...)   → with dft_ai.comm.all_gather(): ...

### 4f. Generic expensive functions  (python_extract_functions + cost estimation)

For every remaining file in OTHER_FILES:

    python_extract_functions(run_id=RUN_ID, filepath=<file>)

    For each function with lines > 10 or that calls I/O:
      python_annotate_file(run_id=RUN_ID, filepath=<file>,
        category=<module_stem>)
      → uses @_dlp.log / @_dlp.log_init / @_dlp.log_static

### 4g. PyTorch / Framework-specific rules

  PyTorch DDP (ML-R11): Annotate INNER model.forward(), not the DDP wrapper.
  Lightning (ML-R12):   training_step → @dft_ai.compute
                        validation_step → @dft_ai.pipeline.evaluate
                        test_step → @dft_ai.pipeline.test
  TensorFlow (ML-R13):  Annotate Python wrapper that calls tf.function, not the
                        tf.function itself.
  JAX (ML-R14):         Call jax.block_until_ready(result) inside annotated fns.

  Numba/C-ext (ML-R30): NEVER apply @_dlp.log to @numba.njit, @numba.jit, or
  Cython/C-extension functions — they raise "TypeError: unsupported callable" at
  import time. Instead, rename to _<name>_impl and create a Python wrapper:
    ```python
    @numba.njit
    def _fn_impl(x): ...

    def fn(x):
        _ctx = DFTracerFn("fn")
        with _ctx:
            return _fn_impl(x)
    ```

  Static methods (ML-R31): Always place @staticmethod ABOVE @_dlp.log_static.
  Wrong order causes "got multiple values for argument" TypeError at call time:
    ```python
    # CORRECT              # WRONG
    @staticmethod          @_dlp.log_static
    @_dlp.log_static       @staticmethod
    def fn(x): ...         def fn(x): ...
    ```
  After auto-annotation, batch-fix with:
    `re.sub(r'@_dlp\.log_static\n(\s+)@staticmethod', r'@staticmethod\n\1@_dlp.log_static', txt)`

### 4h. PyTorch Profiler integration (ML-R28) — when "pytorch" in FRAMEWORKS

  When PYTORCH_EXTRAS_OK is True, inject the PyTorch Profiler wrapper into the
  TRAIN_FILE that contains the batch loop. Do this AFTER python_annotate_ai_file
  so the batch loop is already wrapped with dft_ai.dataloader.fetch.iter().

  Inject at the top of the training file (after the dftracer import block):

    ```python
    from torch.profiler import profile, schedule, ProfilerActivity
    from dftracer.python.torch import trace_handler as _dft_torch_trace_handler

    _dft_prof_schedule = schedule(wait=1, warmup=1, active=3, repeat=1)
    ```

  Wrap the batch loop body with the profiler context (enclose the `for batch in ...`
  loop, call `prof.step()` at end of each batch iteration):

    ```python
    with profile(
        activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA],
        schedule=_dft_prof_schedule,
        on_trace_ready=_dft_torch_trace_handler,
        record_shapes=True,
        with_stack=False,
    ) as _dft_prof:
        for batch in dft_ai.dataloader.fetch.iter(loader):
            # ... forward / backward / optimizer step ...
            _dft_prof.step()
    ```

  Rules:
    - Use ONLY `_dft_torch_trace_handler`; do NOT also set tensorboard handler.
    - `prof.step()` must fire once per batch, inside the batch loop.
    - schedule(wait=1, warmup=1, active=3) skips cold-start batches.
    - Trace verification: category `PP` must appear in session_analyze_traces output.

### 4i. PyTorch Dynamo integration (ML-R29) — when "pytorch" in FRAMEWORKS

  When PYTORCH_EXTRAS_OK is True and the source calls `torch.compile` (or the
  model would benefit from compile-level tracing), replace the bare
  `model = torch.compile(model)` with the dftracer Dynamo backend.

  At the top of the training file (after the dftracer import block):

    ```python
    from dftracer.python.dynamo import create_backend as _dft_create_backend
    ```

  In the training loop, replace bare `torch.compile(model)` with:

    ```python
    _dft_dynamo_backend = _dft_create_backend(name="<app_name>", epoch=epoch, step=step)
    compiled_model = torch.compile(model, backend=_dft_dynamo_backend)
    ```

  If `torch.compile` is NOT present in the source, add it before the training loop:

    ```python
    # dftracer Dynamo tracing — wraps model forward pass at compile level
    _dft_dynamo_backend = _dft_create_backend(name="<app_name>", epoch=0, step=0)
    model = torch.compile(model, backend=_dft_dynamo_backend)
    ```

  Rules:
    - Update epoch/step each iteration: recreate backend or call update() if available.
    - Do NOT use both `@dynamo.compile` and `create_backend` on the same function.
    - On AMD/ROCm without triton, compile falls back to eager — events still appear.
    - Trace verification: category `DY` must appear in session_analyze_traces output.
    - If model is already wrapped in DDP, apply torch.compile BEFORE DDP wrapping.

Print per-file status after each file: ✓ <file> (<N> AI regions, <M> generic)


══════════════════════════════════════════════════════════════════════
STEP 5 — ANNOTATION VERIFICATION
══════════════════════════════════════════════════════════════════════

    session_annotation_report(run_id=RUN_ID)

Check:
  ☐ Entry file has initialize_log + finalize()
  ☐ Training loop has @dft_ai.pipeline.train
  ☐ epoch for-loop wrapped with dft_ai.pipeline.epoch.iter()
  ☐ batch for-loop wrapped with dft_ai.dataloader.fetch.iter()
  ☐ __getitem__ annotated with @dft_ai.data.item
  ☐ ai.update(step=, epoch=) inside batch loop
  ☐ Comm ops use context manager style (not decorator)
  ☐ optimizer.step uses start/stop style (not decorator)
  ☐ [pytorch] PyTorch Profiler wrapper injected (trace_handler, prof.step()) — ML-R28
  ☐ [pytorch] Dynamo backend wired to torch.compile or model forward — ML-R29

For any missing item:
  - Re-run python_annotate_ai_file for the relevant file
  - Record the gap as a potential PITFALL if it was not caught automatically

Print:
  ┌────────────────────────────────────────────────────────┐
  │  ML ANNOTATION REPORT — <RUN_ID>                       │
  │  Files annotated:   <N>                                │
  │  AI regions:        <N> (pipeline=N compute=N data=N)  │
  │  Generic regions:   <N>                                │
  │  Entry file:        <name>  initialize_log: YES/NO     │
  │  Frameworks:        <FRAMEWORKS>                       │
  │  HIP tracing:       <HIP_NEEDED>                       │
  │  PyTorch Profiler:  YES/NO  (category PP expected)     │
  │  PyTorch Dynamo:    YES/NO  (category DY expected)     │
  └────────────────────────────────────────────────────────┘

Ask: "Proceed with build + trace run? [yes / no / fix <file> <feedback>]"

  "no"  → jump to Step 8 (update lessons only).
  "fix" → re-annotate the named file, re-run verification, ask again.
  "yes" → continue to Step 6.


══════════════════════════════════════════════════════════════════════
STEP 6 — BUILD + SMOKE TEST
══════════════════════════════════════════════════════════════════════

6a. DFTRACER_INIT mode:

    Primary (always try first):
      DFTRACER_INIT = "FUNCTION"

    The Python dftracer API is the integration point:
      from dftracer.python import dftracer, dft_fn as DFTracerFn, ai as dft_ai
      _dft_log = dftracer.initialize_log(logfile=None, data_dir=None, process_id=-1)
      # ... annotated functions with decorators ...
      _dft_log.finalize()

    Fallback (only if FUNCTION produces empty trace or ImportError at runtime):
      dftracer_lib=$(python -c "import dftracer, os; print(os.path.join(os.path.dirname(dftracer.__file__), 'lib', 'libdftracer_preload.so'))")
      DFTRACER_INIT = "HYBRID"
      LD_PRELOAD = "<dftracer_lib>"

    PRELOAD-only mode is never used — Python API annotations must always be present.

6b. Build annotated:

    session_build_annotated(run_id=RUN_ID, extra_cmake_flags=EXTRA_FLAGS)

    On failure:
      - Extract failing function(s) from compiler error
      - Re-annotate only those files with the offending function excluded
      - Re-run build (max 2 retries)
      - Record the error in PITFALLS

6c. Smoke test without dftracer:

    session_run_smoke_test(run_id=RUN_ID, command=SMOKE_CMD,
      subfolder="build_ann")

    On failure → record in PITFALLS, ask user: "Continue to trace? [yes/stop]"


══════════════════════════════════════════════════════════════════════
STEP 7 — TRACE COLLECTION + VERIFICATION
══════════════════════════════════════════════════════════════════════

7a. Create traces directory:

    mkdir -p <WS>/traces/<APP_NAME>

7b. Run with dftracer:

    session_run_with_dftracer(run_id=RUN_ID, command=SMOKE_CMD,
      subfolder="build_ann",
      env_extra={
        "DFTRACER_ENABLE": "1",
        "DFTRACER_INC_METADATA": "1",
        "DFTRACER_INIT": DFTRACER_INIT,
      },
      data_dir="all")

7c. Split and analyze:

    session_split_traces(run_id=RUN_ID, app_name=<APP_NAME>)
    session_analyze_traces(run_id=RUN_ID, query_type="summary")

7d. Verify AI/ML categories in trace (ML-R15):

    Check that the trace summary includes categories:
      pipeline, compute, data, dataloader

    If HIP_NEEDED and "hip" category absent (ML-R16):
      PITFALLS.append({phase:"run", error:"HIP events absent from trace",
        root_cause:"DFTRACER_ENABLE_HIP_TRACING=ON not set at dftracer install time",
        fix:"Re-install dftracer with DFTRACER_ENABLE_HIP_TRACING=ON, re-run with DFTRACER_INIT=FUNCTION; if still absent fall back to HYBRID with LD_PRELOAD=libdftracer_preload.so"})

    If "pytorch" in FRAMEWORKS and PYTORCH_EXTRAS_OK:

      Check for category "PP" (PyTorch Profiler) — ML-R28:
        If absent → PITFALLS.append({phase:"run", error:"PP category absent",
          root_cause:"trace_handler not invoked — profiler context not entered or prof.step() not called",
          fix:"Verify profiler `with profile(...)` block encloses the batch loop and prof.step() is called at end of each batch"})

      Check for category "DY" (PyTorch Dynamo) — ML-R29:
        If absent → PITFALLS.append({phase:"run", error:"DY category absent",
          root_cause:"create_backend not passed to torch.compile, or torch.compile never executed a forward pass in this run",
          fix:"Confirm torch.compile(model, backend=_dft_dynamo_backend) is called before the training loop; add at least one forward pass to the smoke test"})

    Print missing categories as warnings.

7e. Compute / DataLoader overlap analysis  (GLANCED-IO §4.3, HPDC '26):

    This is the single most important pipeline correctness check.
    A correct DL pipeline runs the DataLoader in parallel with GPU compute
    so that the next batch is already in memory when the current forward/backward
    finishes. If that overlap is absent the pipeline stalls on every step.

    From the dftracer trace timeline, check whether `dataloader` category events
    and `compute` category events overlap in wall-clock time:

      OVERLAPPING (correct):
        |-- dataloader.fetch (batch N+1) --|
                      |-- compute.forward (batch N) -- compute.backward --|

      SEQUENTIAL (broken — pipeline stall):
        |-- compute.forward -- compute.backward --|
                                                   |-- dataloader.fetch --|

    How to detect in dftracer traces:

      session_analyze_traces(run_id=RUN_ID, query_type="timeline_overlap",
        categories=["dataloader", "compute"])

    If the tool returns `overlap_fraction < 0.1` (less than 10% of compute
    time has concurrent dataloader activity), treat this as NO OVERLAP.

    No-overlap diagnosis flowchart:
      Step A — Check DataLoader config in source:
               num_workers == 0  → root cause: no parallel workers at all
               prefetch_factor == 0 or 1 → root cause: no prefetch buffer
               persistent_workers not set → workers respawn each epoch (slow)
      Step B — Check tensor transfer:
               tensor.to(device) without non_blocking=True → blocks CPU,
               stalls the dataloader thread
      Step C — Check distributed sync (if multi-GPU):
               MPI.Barrier / dist.barrier() inside the batch loop →
               serializes all ranks, destroying prefetch pipeline
      Step D — Check DataLoader annotation:
               If `for batch in loader:` was NOT wrapped with
               `dft_ai.dataloader.fetch.iter(loader)`, the trace won't
               show dataloader events at all — re-annotate and re-run

    Fix patterns (DLIO benchmark is the canonical reference):

      # Minimum correct config
      loader = DataLoader(
          dataset,
          num_workers  = max(4, os.cpu_count() // 2),   # L1 param #1
          prefetch_factor = 2,                            # L1 param #2
          persistent_workers = True,
          pin_memory   = True,
      )

      # Non-blocking H2D transfer
      batch = batch.to(device, non_blocking=True)

      # Distributed: avoid per-step barrier unless gradient sync requires it
      # Use gradient accumulation to amortize sync cost

    After applying fix, re-run and re-check overlap_fraction.
    Target: overlap_fraction ≥ 0.5 (dataloader runs during ≥50% of compute).

    Record overlap result in session summary:
      OVERLAP_OK = (overlap_fraction >= 0.1)
    If OVERLAP_OK is False, add to PITFALLS:
      PITFALLS.append({
        phase: "dataloader",
        error: f"No compute/IO overlap — overlap_fraction={overlap_fraction:.2f}",
        root_cause: "... root cause identified in Step A/B/C/D above ...",
        fix: "... exact DataLoader config change applied ...",
        annotation_rule: "ML-R24",
      })

7f. Search local papers for optimization guidance:

    For each top bottleneck identified in the trace summary, call:

        session_search_local_papers(
          query      = "<bottleneck description from trace>",
          bottleneck = "<bottleneck category>",   # e.g. "data_loading", "prefetch"
          framework  = FRAMEWORKS[0] if FRAMEWORKS else "",
        )

    Collect all returned papers into PAPER_REFS list.

    Then generate OPTIMIZATION_PROPOSALS using the paper-backed rules below.
    Every proposal MUST cite at least one paper from PAPER_REFS (use
    "Paper: <title> (<venue> '<year>)" format).

    GLANCED-IO Parameter Ordering (always apply in this order — from
    Table 1 of "Automated Cross-Layer Optimization", HPDC '26):
      L1 — Application-level (highest portability, highest impact first):
           1. num_workers        (DataLoader parallelism)
           2. prefetch_factor    (pipeline depth)
           3. dataset_access_pattern  (sequential vs. random)
      L2 — Framework-level:
           4. framework-specific batch settings, pin_memory, persistent_workers
      L3 — Filesystem-level (lowest portability, tune last):
           5. PFS striping count and stripe size
           6. transfer_size / I/O block size

      RULE: Never report "optimal" after tuning only one parameter — GLANCED-IO
      shows siloed single-parameter tuning misses up to 2.4x throughput gain.
      Always propose joint L1+L2 before L3.

    Cladia Diagnosis Rules (apply when bottleneck is ambiguous):
      DIAG-1  Always build cross-layer dependency graph before diagnosing:
              dftracer → I/O layer → framework layer → compute layer.
      DIAG-2  Compute-bound ≠ I/O-free: use SHAP attribution from Cladia
              to confirm I/O contribution even when GPU utilization is high.
      DIAG-3  Never attribute bottleneck to a single layer from single-layer
              metrics alone. Cross-layer correlation is required.
      DIAG-4  Use quantile regression (Cladia) to separate outlier I/O events
              from median behavior — outliers often dominate epoch time.
      DIAG-5  When Cladia reports uncertainty > 0.3 on root cause, collect
              additional traces with finer granularity before proposing fixes.
      DIAG-6  Checkpoint bottlenecks are cross-layer by definition: combine
              checkpoint frequency (L2) with parallel save (L1) with
              filesystem striping (L3).

    LiveFlow Rules (apply for distributed / multi-GPU workloads):
      LIVE-1  Run critical-path analysis before proposing communication
              optimizations — not all all-reduce ops are on the critical path.
      LIVE-2  Bounded decision windows: propose only changes that can be
              applied within the current epoch without restarting training.
      LIVE-3  For gradient compression proposals, measure compression overhead
              vs. communication savings — LiveFlow shows net benefit varies
              by model size and interconnect bandwidth.
      LIVE-4  Hierarchical coordination: node-level decisions (intra-node
              I/O) before cluster-level decisions (inter-node bandwidth).
      LIVE-5  If training throughput drops after scaling from 1 to N GPUs,
              check critical-path I/O overlap — LiveFlow identifies when
              I/O is on the critical path vs. hidden behind compute.

    HORATIO Rules (apply when trace size > 10 GB or > 100M events):
      TRACE-1  Use HORATIO's RocksDB index for selective event queries —
               do not load full trace into memory for analysis.
      TRACE-2  Bloom filter pre-screening: query by function name before
               loading any trace segment (75x speedup vs. linear scan).
      TRACE-3  For clustering similar trace patterns, use HORATIO's native
               C++ analyzer — Dask-based analysis is 80-83x slower.
      TRACE-4  When storing traces for later analysis, apply HORATIO's
               lossless clustering to reduce storage 40-60% with no info loss.

    Print OPTIMIZATION_PROPOSALS as a numbered list with paper citations.


══════════════════════════════════════════════════════════════════════
STEP 8 — UPDATE LESSONS LEARNED  (MANDATORY — always run)
══════════════════════════════════════════════════════════════════════

This step runs whether or not the pipeline completed. Any pitfall discovered
during the session must be recorded so future sessions avoid it.

For each entry in PITFALLS:

    session_ml_append_lesson(
      app        = APP_URL,
      context    = <one-line description of what was attempted>,
      error      = <verbatim error excerpt>,
      root_cause = <why it happened>,
      fix        = <exact steps that resolved it>,
      phase      = <phase where it occurred>,
      framework  = FRAMEWORKS,
      annotation_rule = <new ML-R rule if this generalizes, else "">,
      tags       = FRAMEWORKS + [<phase>, <error_keyword>],
      run_id     = RUN_ID,
    )

Also append a lesson for any annotation pattern that was NOT caught
automatically by python_annotate_ai_file and had to be fixed manually —
these are candidates for new detection patterns in the tool.

If PITFALLS is empty, call session_ml_append_lesson once with:
  context = "Clean run — no pitfalls"
  error   = ""
  fix     = "No action needed"
  tags    = FRAMEWORKS

This keeps a session history even for successful runs.

Print: "Lessons file updated: workspaces/.agents/skills/dftracer-ml-annotation-lessons/SKILL.md"


══════════════════════════════════════════════════════════════════════
STEP 9 — SESSION REPORT
══════════════════════════════════════════════════════════════════════

Write <WS>/session_report.md with:

  # DFTracer ML Annotation Session Report — <RUN_ID>
  **Application:** <APP_URL> @ <REF>
  **Date:** <YYYY-MM-DD>
  **Frameworks:** <FRAMEWORKS>
  **ROCm/HIP:** <HIP_NEEDED>  <ROCM_INFO.path>

  ## Pipeline Steps
  | Step | Status | Duration |
  |------|--------|----------|
  | session_create | ... | ...s |
  | session_detect_ml_workload | ... | ...s |
  | session_install_dftracer | ... | ...s |
  | session_copy_annotated | ... | ...s |
  | python_annotate_ai_file (N files) | ... | ...s |
  | session_build_annotated | ... | ...s |
  | session_run_with_dftracer | ... | ...s |
  | session_analyze_traces | ... | ...s |

  ## Annotation Summary
  | Metric | Value |
  |--------|-------|
  | Files annotated | N |
  | AI/ML regions | N |
  | Generic regions | N |
  | Missing categories | ... |

  ## Step Timings
  (populate from step_timings.json if session_run_pipeline was used)

  ## Pitfalls This Session
  (one sub-section per PITFALLS entry, or "None" if clean)

  ## Lessons Written
  List each lesson appended to SKILL.md this session.


══════════════════════════════════════════════════════════════════════
TOOL REFERENCE
══════════════════════════════════════════════════════════════════════

| Purpose                               | MCP Tool                          |
|---------------------------------------|-----------------------------------|
| Create session + clone                | session_create                    |
| Detect ML frameworks + ROCm           | session_detect_ml_workload        |
| Look up correct AI/ML annotation      | dftracer_get_ai_annotation        |
| Configure original build              | session_configure                 |
| Build + install original              | session_build_install             |
| Install dftracer (pip, with HIP flag) | session_install_dftracer          |
| Copy source → annotated/              | session_copy_annotated            |
| Find Python source files              | find_source_files                 |
| Annotate Python file (AI/ML regions)  | python_annotate_ai_file           |
| Annotate Python file (generic)        | python_annotate_file              |
| Extract function list                 | python_extract_functions          |
| Annotation coverage report            | session_annotation_report         |
| Build annotated version               | session_build_annotated           |
| Smoke test                            | session_run_smoke_test            |
| Run with dftracer tracing             | session_run_with_dftracer         |
| Split trace files                     | session_split_traces              |
| Analyze traces                        | session_analyze_traces            |
| Search local optimization papers      | session_search_local_papers       |
| Append lesson to lessons file         | session_ml_append_lesson          |

NEVER:
  • Use Edit/Write to manually insert decorators into source files
  • Use @dft_ai.compute.step as a function decorator (use start/stop style)
  • Use @dft_ai.comm.* as a function decorator (use context manager style)
  • Double-wrap a for-loop that already uses dft_ai.*.iter()


══════════════════════════════════════════════════════════════════════
DLIO BENCHMARK REFERENCE PATTERNS
══════════════════════════════════════════════════════════════════════

DLIO benchmark is the canonical reference for ML annotation patterns.
Map new applications' structural equivalents to these files:

| DLIO file                                      | Key annotations                           |
|------------------------------------------------|-------------------------------------------|
| dlio_benchmark/main.py                         | @dft_ai on main(); initialize_log at top  |
| dlio_benchmark/framework/pytorch_loader.py     | @dft_ai.pipeline.train, .evaluate         |
| dlio_benchmark/reader/hdf5_reader.py           | @dft_ai.data.item on __getitem__          |
| dlio_benchmark/data_generator/hdf5_generator.py| @dft_ai.data.preprocess, @_dlp.log       |
| dlio_benchmark/checkpointing/torch_checkpoint.py| @dft_ai.checkpoint.capture/.restart      |
| dlio_benchmark/utils/utility.py                | @_dlp.log on expensive helpers           |

Epoch loop: `for epoch in dft_ai.pipeline.epoch.iter(range(num_epochs)):`
Batch loop: `for batch in dft_ai.dataloader.fetch.iter(data_loader):`
Step meta:  `ai.update(step=batch_idx, epoch=epoch_num)` inside batch loop
