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

    **Data I/O rules (ML-R25) — use `ai.data.io.*` for explicit open/read/write/close:**

    Annotate each I/O phase separately instead of lumping everything into `data.item`.
    ALWAYS compute and pass `image_size` (bytes) as metadata to every I/O region.

    Phase mapping:
      open file / open dataset / h5py.File(...)  → @dft_ai.data.io.open
      np.load / f.read() / dataset[idx]          → @dft_ai.data.io.read  + image_size
      f.write() / np.save()                      → @dft_ai.data.io.write + image_size
      f.close() / file handle cleanup            → @dft_ai.data.io.close

    image_size MUST be the byte size of the actual DATA ARRAY, computed from the
    loaded/written object — NEVER from the path string or file metadata:

      ✅ numpy array:   image_size=array.nbytes          (in-memory array bytes)
      ✅ torch tensor:  image_size=tensor.element_size() * tensor.nelement()
      ✅ bytes object:  image_size=len(buf)
      ✅ checkpoint:    image_size=sum(t.nbytes for t in state_dict.values() if hasattr(t, "nbytes"))
      ❌ WRONG:         image_size=os.path.getsize(path) (file-system metadata, not data)
      ❌ WRONG:         image_size=len(path)             (path string length)

    Pass image_size via update() AFTER the read/write so the value is known:

    ```python
    from dftracer.python import ai

    # numpy example
    def load_sample(path: str):
        with ai.data.io.open:
            pass
        with ai.data.io.read:
            data = np.load(path)
            ai.data.io.read.update(image_size=data.nbytes)
        with ai.data.io.close:
            pass
        return data
    ```

    Context-manager style is preferred when the function mixes phases.
    Decorator style is preferred when the method maps 1:1 to a phase.

### 4d. Checkpoint files

    python_annotate_ai_file(run_id=RUN_ID, filepath=<file>,
      category=<module_stem>)

    Expected decorators:
      save / save_checkpoint / write_ckpt     → @dft_ai.checkpoint.capture
      load / load_checkpoint / restore_ckpt  → @dft_ai.checkpoint.restart

    **Checkpoint I/O rules (ML-R26) — use `ai.checkpoint.io.*` for explicit phases:**

    Wrap the four I/O phases inside the outer capture/restart context.
    ALWAYS compute and pass `image_size` (bytes of the checkpoint) as metadata.

    Phase mapping:
      open file for checkpoint               → @ai.checkpoint.io.open
      torch.load / pickle.load / f.read()    → @ai.checkpoint.io.read  + image_size
      torch.save / pickle.dump / f.write()   → @ai.checkpoint.io.write + image_size
      f.close() / os.remove()               → @ai.checkpoint.io.close

    image_size for checkpoints = total bytes of all tensors in the state dict.
    Compute from the in-memory data — NOT from os.path.getsize or len(path):

      read:  sum(t.nbytes for t in checkpoint.get("model_state_dict", {}).values() if hasattr(t, "nbytes"))
      write: sum(t.nbytes for t in state_dict.get("model_state_dict", {}).values() if hasattr(t, "nbytes"))

    ```python
    from dftracer.python import ai
    import torch

    def save_checkpoint(model, path: str):
        state_dict = {"model_state_dict": model.state_dict()}
        ckpt_bytes = sum(t.nbytes for t in state_dict["model_state_dict"].values() if hasattr(t, "nbytes"))
        with ai.checkpoint.capture:
            with ai.checkpoint.io.open:
                f = open(path, "wb")
            with ai.checkpoint.io.write:
                torch.save(state_dict, f)
                ai.checkpoint.io.write.update(image_size=ckpt_bytes)
            with ai.checkpoint.io.close:
                f.close()

    def load_checkpoint(path: str):
        with ai.checkpoint.restart:
            with ai.checkpoint.io.open:
                f = open(path, "rb")
            with ai.checkpoint.io.read:
                state = torch.load(f)
                ckpt_bytes = sum(t.nbytes for t in state.get("model_state_dict", {}).values() if hasattr(t, "nbytes"))
                ai.checkpoint.io.read.update(image_size=ckpt_bytes)
            with ai.checkpoint.io.close:
                f.close()
        return state
    ```

### 4e. Other I/O files  (config reads, stats writes, utility I/O — ML-R27)

Any I/O that is NOT inside the DataLoader path or checkpoint save/restore goes
under `ai.other.io.*`. This covers: config file reads, CSV/stats file writes,
rendezvous/coordination files, datagen scripts, logging helpers.

    Phase mapping:
      open file / open dataset           → dft_ai.other.io.open
      f.read() / yaml.load / np.load()   → dft_ai.other.io.read  + image_size
      f.write() / outfile.write(row)     → dft_ai.other.io.write + image_size
      f.close()                          → dft_ai.other.io.close

    Use `ai.other.log` for logging/print sinks that should be traced but
    carry no I/O bytes.

    image_size rules are identical to ML-R25:
      bytes object:  image_size=len(buf)
      numpy array:   image_size=array.nbytes
      encoded str:   image_size=len(s.encode())
      NEVER os.path.getsize or len(path)

    Category decision tree:
      Is the I/O inside __getitem__ / DataLoader path?  → data.io.*
      Is the I/O torch.save / torch.load of model weights?  → checkpoint.io.*
      Everything else  → other.io.*

    ```python
    from dftracer.python import ai

    # config read example
    with ai.other.io.open:
        f = open(config_path, "rb")
    with ai.other.io.read:
        raw = f.read()
        cfg = yaml.safe_load(raw)
        ai.other.io.read.update(image_size=len(raw))
    with ai.other.io.close:
        f.close()

    # stats CSV write example
    with ai.other.io.write:
        outfile.write(row)
        ai.other.io.write.update(image_size=len(row.encode()))
    ```

### 4f. Distributed communication files  (when DISTRIBUTED=True)

    python_annotate_ai_file(run_id=RUN_ID, filepath=<file>,
      category=<module_stem>)

    Expected — context manager style (ML-R5):
      dist.all_reduce(...)   → with dft_ai.comm.all_reduce(): ...
      dist.barrier()         → with dft_ai.comm.barrier(): ...
      dist.broadcast(...)    → with dft_ai.comm.broadcast(): ...
      dist.all_gather(...)   → with dft_ai.comm.all_gather(): ...

### 4g. Generic expensive functions  (python_extract_functions + cost estimation)

For every remaining file in OTHER_FILES:

    python_extract_functions(run_id=RUN_ID, filepath=<file>)

    For each function with lines > 10 or that calls I/O:
      python_annotate_file(run_id=RUN_ID, filepath=<file>,
        category=<module_stem>)
      → uses @_dlp.log / @_dlp.log_init / @_dlp.log_static

### 4h. PyTorch / Framework-specific rules

  PyTorch DDP (ML-R11): Annotate INNER model.forward(), not the DDP wrapper.
  Lightning (ML-R12):   training_step → @dft_ai.compute
                        validation_step → @dft_ai.pipeline.evaluate
                        test_step → @dft_ai.pipeline.test
  TensorFlow (ML-R13):  Annotate Python wrapper that calls tf.function, not the
                        tf.function itself.
  JAX (ML-R14):         Call jax.block_until_ready(result) inside annotated fns.

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
  ☐ Data I/O phases use ai.data.io.open/read/write/close (ML-R25)
  ☐ Checkpoint I/O phases use ai.checkpoint.io.open/read/write/close (ML-R26)
  ☐ Non-dataloader/non-checkpoint I/O uses ai.other.io.open/read/write/close (ML-R27)
  ☐ image_size metadata passed in every data.io.read / checkpoint.io.read / write region
  ☐ image_size metadata passed in every other.io.read / other.io.write region

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
      pipeline, compute, data, dataloader, data.io, checkpoint.io, other

    Also verify that data.io, checkpoint.io, and other.io events carry image_size in args.
    Missing image_size means ML-R25/ML-R26/ML-R27 was not applied — re-annotate.

    checkpoint.io events will be ABSENT if checkpoint_interval=-1 (checkpointing disabled).
    This is expected — do not treat absent checkpoint.io as an annotation failure.

    If HIP_NEEDED and "hip" category absent (ML-R16):
      PITFALLS.append({phase:"run", error:"HIP events absent from trace",
        root_cause:"DFTRACER_ENABLE_HIP_TRACING=ON not set at dftracer install time",
        fix:"Re-install dftracer with DFTRACER_ENABLE_HIP_TRACING=ON, re-run with DFTRACER_INIT=FUNCTION; if still absent fall back to HYBRID with LD_PRELOAD=libdftracer_preload.so"})

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
  • Omit image_size from data.io.read / data.io.write / checkpoint.io.read / checkpoint.io.write
  • Compute image_size BEFORE the operation (size must be known after read/write completes)
  • Use os.path.getsize(path) or len(path) for image_size — must be in-memory byte count
  • Write DFTRACER_LOG_FILE to Lustre when using session_optimization_iteration — the tool
    looks for traces in the session workspace (<WS>/traces/); write there instead
  • Use -n <total_procs> with torchrun-hpc — the -n flag is procs-PER-NODE;
    for 8 nodes × 4 GPUs/node use: torchrun-hpc -N 8 -n 4 --gpus-per-proc 1


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
|                                                | ai.checkpoint.io.write + image_size      |
| dlio_benchmark/reader/hdf5_reader.py           | ai.data.io.open/read + image_size        |
| dlio_benchmark/utils/utility.py                | @_dlp.log on expensive helpers           |

Epoch loop: `for epoch in dft_ai.pipeline.epoch.iter(range(num_epochs)):`
Batch loop: `for batch in dft_ai.dataloader.fetch.iter(data_loader):`
Step meta:  `ai.update(step=batch_idx, epoch=epoch_num)` inside batch loop
