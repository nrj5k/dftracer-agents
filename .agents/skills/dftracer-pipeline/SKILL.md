---
name: dftracer-pipeline
description: >
  Interactive dftracer annotation pipeline.
  Clones the app, builds it, annotates all source files using the
  clang-based MCP annotation tools (clang_annotate_project /
  clang_annotate_file + clang_syntax_check + clang_lint_annotations),
  builds the annotated version, runs a smoke test, collects traces,
  and produces optimization proposals.
---

Lessons file: /workspaces/dftracer-agents/.agents/skills/dftracer-annotation-lessons/SKILL.md

Read the lessons file before doing anything else. Apply every lesson that
matches the current app or language.

══════════════════════════════════════════════════════════════════════
!! ANNOTATION MODE — MANDATORY RULE (READ BEFORE ANY OTHER STEP) !!
══════════════════════════════════════════════════════════════════════

ALWAYS annotate using MCP clang tools. NEVER do manual annotation.

  CORRECT flow:
    1. clang_annotate_project()         ← primary: annotates all files at once
    2. clang_syntax_check() per file    ← validation
    3. clang_lint_annotations() per file← validation
    4. If check/lint FAILS for a specific function → call clang_annotate_file()
       with that function excluded or with comp_overrides to correct it.
       This is "manual correction" — targeted, single-function only.

  FORBIDDEN at all times (do NOT do these, ever):
    ✗ Read a source file → manually compose macros → Write/Edit the file
    ✗ Use Bash gcc/g++ -fsyntax-only to check annotations
    ✗ Call session_annotate_c_file / session_annotate_cpp_file (deprecated)
    ✗ Rewrite or re-annotate an entire file with Edit/Write tools
    ✗ Fall back to "manual mode" when an MCP call fails — instead, re-call
      clang_annotate_file() for only the failing function with overrides.

  If clang_annotate_project itself fails to run (tool error, not annotation
  error): diagnose the tool error, do NOT switch to manual annotation.
  Report the tool failure to the user and stop.

══════════════════════════════════════════════════════════════════════

══════════════════════════════════════════════════════════════════════
STEP 1 — GATHER INPUTS  (if not supplied via arguments)
══════════════════════════════════════════════════════════════════════

If the user invoked this with named arguments (run_id=…, url=…, etc.),
use those directly. Otherwise ask one question at a time and wait:

  Q1: "What is the Git URL of the application you want to annotate?"
      → Store as APP_URL.

  Q2: "Which branch or tag? (default: main)"
      → Store as REF (use "main" if blank).

  Q3: "Smoke test command? (leave blank to auto-detect)"
      → Store as SMOKE_CMD.

  Q4: "Extra CMake/configure build flags? (leave blank to skip)"
      → Store as EXTRA_FLAGS.

If a run_id was supplied skip Q1–Q4 and jump to Step 3.

Print: "Starting pipeline for <APP_URL> @ <REF>"


══════════════════════════════════════════════════════════════════════
STEP 2 — SESSION SETUP  (MCP tools)
══════════════════════════════════════════════════════════════════════

2a. Create session and build the original source:

    session_create(url=APP_URL, ref=REF)
    → store RUN_ID, WS (workspace path)

2b. Validate HDF5 version (only if project uses HDF5):

    h5cc --version 2>/dev/null || h5dump --version 2>/dev/null || \
      pkg-config --modversion hdf5 2>/dev/null

    dftracer-compatible HDF5 versions (exact series only):
      1.8.23  |  1.10.5  |  1.12.3  |  1.14.5 (preferred)

    If the system HDF5 is NOT in one of these series:
      - Build HDF5 1.14.5 from source into <WS>/hdf5_1.14/ (see dftracer-install skill)
      - Add "-DHDF5_DIR=<WS>/hdf5_1.14" to EXTRA_FLAGS for all cmake steps
      - Set HDF5_DIR and LD_LIBRARY_PATH in every subsequent shell command

    The MCP session_detect tool reports hdf5_system.compatible=true/false and
    hdf5_system.recommended with the preferred patch release for the detected series.

2c. Configure + build the original source:

    session_configure(run_id=RUN_ID, extra_cmake_flags=EXTRA_FLAGS)
    session_build_install(run_id=RUN_ID)

2c. Install dftracer into the session (cmake mode, with MPI + HDF5
    auto-detected from the project source):

    session_install_dftracer(run_id=RUN_ID)

    On failure → print the cmake/pip error and stop.

2d. Copy source to annotated/ workspace:

    session_copy_annotated(run_id=RUN_ID)

2e. Baseline annotated build (no macros yet — verifies the build
    system patch works before any annotation):

    session_build_annotated(run_id=RUN_ID,
      extra_cmake_flags=<same flags as 2b>)

    On failure → show cmake/make errors and stop.

Print: "Setup complete. RUN_ID=<RUN_ID>  Baseline build PASSED."


══════════════════════════════════════════════════════════════════════
STEP 3 — WHOLE-PROJECT ANNOTATION  (clang MCP tools)
══════════════════════════════════════════════════════════════════════

The preferred path is a single project-level call that discovers all
C/C++ files, determines entry points, filters trivial functions by AST
cost, and inserts macros in bottom-to-top line order:

    clang_annotate_project(
      run_id      = RUN_ID,
      language    = "c",          # or "cpp" for C++ projects
      init_args   = "NULL, NULL, NULL",
      exclude_patterns = ["test/", "tests/", "vendor/", "third_party/"]
    )

This call:
  • Discovers every .c / .cpp / .cxx / .cc under annotated/
  • Skips files matching exclude_patterns (plus the always-excluded
    /CMakeFiles/, /.git/ paths)
  • Annotates library/inner files first (is_entry=False)
  • Annotates entry-point files last (is_entry=True) so INIT/FINI land
    around main()
  • For each file, internally calls clang_extract_functions to get an
    authoritative function map, then clang_estimate_function_cost per
    function to decide annotate vs. skip (score ≥ 20 or lifecycle rule)
  • Inserts macros in a single in-memory pass (no intermediate writes)
  • Writes each file exactly once via clang_write_annotated_file

Print the result: number of files annotated, functions annotated vs skipped.

### When to use per-file annotation instead

If clang_annotate_project reports errors for specific files, OR if you
need comp= overrides for particular functions, switch to per-file mode
for those files only:

    # 1. Extract the function map
    clang_extract_functions(run_id=RUN_ID, filepath=<file>)

    # 2. For any function where you want to override the auto comp:
    clang_estimate_function_cost(run_id=RUN_ID, filepath=<file>,
      function_name=<name>)
    # → review the cost_info and decide comp= category manually

    # 3. Annotate the file with optional overrides
    clang_annotate_file(
      run_id         = RUN_ID,
      filepath       = <file>,
      is_entry       = <True if file contains main()>,
      language       = "c",        # or "cpp"
      init_args      = "NULL, NULL, NULL",
      comp_overrides = '{"fn_name": "comm", "other_fn": "io"}'
    )
    # clang_annotate_file writes the file in one in-memory pass;
    # call clang_write_annotated_file only if the tool says it is
    # needed to flush (check the response).

Do NOT manually read files, insert macros with Edit/Write, or run
shell `gcc -fsyntax-only` commands. All of that is handled by the
MCP tools.


══════════════════════════════════════════════════════════════════════
STEP 4 — PER-FILE VALIDATION  (clang MCP tools)
══════════════════════════════════════════════════════════════════════

After annotation (project-level or per-file), validate every annotated
C/C++ file using the two MCP validation tools. Run both for each file:

    clang_syntax_check(run_id=RUN_ID, filepath=<file>)
    clang_lint_annotations(run_id=RUN_ID, filepath=<file>)

clang_syntax_check rules:
  • Uses the real gcc/g++ front-end with a dftracer stub header and
    the session's MPI + dftracer include paths — no manual -I flags needed.
  • PASS → move to next file.
  • FAIL → fix ONLY the exact lines named in the compiler error output:
      - Extract function name and line number from "error:" lines.
      - Call clang_annotate_file again for ONLY that function using
        comp_overrides (e.g. to skip it or override the comp= value).
      - NEVER rewrite the whole file manually. NEVER touch functions
        that already pass syntax check.
      - Retry clang_syntax_check. Max 2 targeted fixes per file.
      - On 2nd failure: strip the single failing function's macros by
        re-calling clang_annotate_file with that function excluded, then
        mark it as PENDING in a comment. Move on.

clang_lint_annotations rules check:
  L1 — DFTRACER_C_INIT before DFTRACER_C_FUNCTION_START in main()
  L2 — comp= UPDATE_STR within 3 lines after every START
  L3 — DFTRACER_C_FINI before MPI_Finalize in main()
  L4 — no END immediately before MPI_CHECK / NCMPI_CHECK
  L5 — no END at global scope

  LINT violations → use clang_insert_line to fix ONLY the reported line;
  never re-annotate the whole file. Then re-lint to verify.

Print per-file status: ✓ <file>  (<n> functions annotated, lint PASSED)


══════════════════════════════════════════════════════════════════════
STEP 5 — BUILD ANNOTATED VERSION  (MCP tools)
══════════════════════════════════════════════════════════════════════

5a. Detect explicit INIT usage to determine DFTRACER_INIT mode:

    INIT_COUNT=$(grep -r "DFTRACER_C_INIT\|DFTRACER_CPP_INIT\|DFTracer.initialize_log" \
      <WS>/annotated/ 2>/dev/null | wc -l)
    FINI_COUNT=$(grep -r "DFTRACER_C_FINI\|DFTRACER_CPP_FINI\|dftracer.finalize_log" \
      <WS>/annotated/ 2>/dev/null | wc -l)

    INIT_COUNT > 0 AND FINI_COUNT > 0 → DFTRACER_INIT_ENV = {"DFTRACER_INIT": "FUNCTION"}
      (source has both INIT and FINI: use FUNCTION mode — function-level profiling,
       no LD_PRELOAD needed)

    INIT_COUNT > 0 AND FINI_COUNT == 0 → DFTRACER_INIT_ENV = {"DFTRACER_INIT": "PRELOAD"}
      (missing FINI — FUNCTION/HYBRID would leave traces open; fall back to preload-only)

    INIT_COUNT == 0 → DFTRACER_INIT_ENV = {"DFTRACER_INIT": "PRELOAD"}
      (no annotations — preload-only transparent I/O interception)

    HYBRID mode is NOT the default for annotated code. Use HYBRID only when the
    user explicitly requests BOTH function-level profiling AND I/O interception
    via LD_PRELOAD on top of annotated source that has both INIT and FINI.

    Important: NEVER set DFTRACER_INIT=0 — it disables POSIX-level
    tracing. Valid values: FUNCTION (annotated source with both INIT+FINI,
    default for annotated apps), PRELOAD (transparent I/O only, no annotations
    or missing FINI), HYBRID (annotated source with both INIT+FINI AND
    LD_PRELOAD for I/O interception, only on explicit user request).
    All values are CASE-SENSITIVE uppercase strings.

5b. Build and install annotated version:

    session_build_annotated(run_id=RUN_ID,
      extra_cmake_flags=<same flags as 2b>)

    On failure:
      1. Extract failing function(s) from the compiler error.
      2. Re-annotate only those files using clang_annotate_file with
         the failing function excluded (add it to comp_overrides with
         a sentinel, or use exclude).
      3. Re-run syntax check + lint for the fixed file.
      4. Retry session_build_annotated. Max 2 retries.
      5. If still failing → escalate to user with exact error lines.

5c. Run smoke test:

    session_run_smoke_test(run_id=RUN_ID, command=SMOKE_CMD,
      subfolder="build_ann")

    MPI/OpenMPI as root? Add env:
      OMPI_ALLOW_RUN_AS_ROOT=1, OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1

    On failure: if DFTRACER symbols in error → re-annotate + retry.
    Otherwise ask: "Smoke test failed (non-annotation issue). Continue? [yes/stop]"


══════════════════════════════════════════════════════════════════════
STEP 6 — ANNOTATION REPORT + USER CONFIRMATION
══════════════════════════════════════════════════════════════════════

    session_annotation_report(run_id=RUN_ID)

Print:
  ┌─────────────────────────────────────────────────────────┐
  │  ANNOTATION REPORT — <RUN_ID>                           │
  │  Files:     <n> annotated                               │
  │  Functions: <annotated> / <eligible>  skipped: <n>      │
  │  comp:  io=<n>  comm=<n>  mem=<n>  cpu=<n>             │
  │  Build: PASSED   Smoke test: PASSED                     │
  │  Annotated source: workspaces/<RUN_ID>/annotated/       │
  └─────────────────────────────────────────────────────────┘

Ask: "Proceed with dftracer trace run? [yes / no / fix <file> <feedback>]"

  "no"  → stop, print artifact location.
  "fix <file> <feedback>" → re-annotate that file using clang_annotate_file
           with comp_overrides derived from the feedback, re-run lint +
           syntax check, rebuild, re-run smoke test, show updated report,
           ask again.
  "yes" → continue to Step 7.


══════════════════════════════════════════════════════════════════════
STEP 7 — TRACE COLLECTION + ANALYSIS  (MCP tools)
══════════════════════════════════════════════════════════════════════

7a. Create the trace output subdirectory before running:

    mkdir -p <WS>/traces/<app_name>

    (where app_name = first component of RUN_ID before "/")

7b. Run with dftracer:

    session_run_with_dftracer(run_id=RUN_ID, command=SMOKE_CMD,
      subfolder="build_ann",
      env_extra={**DFTRACER_INIT_ENV,
                 "DFTRACER_ENABLE": "1",
                 "DFTRACER_INC_METADATA": "1"},
      data_dir="all")

    Always use data_dir="all" so no I/O path is excluded from tracing.
    Always set DFTRACER_INC_METADATA=1 so UPDATE_STR/UPDATE_INT metadata
    (comp=, filename=, count=) is captured in the trace events.
    Always set DFTRACER_ENABLE=1 to ensure trace files are written.

7c. Copy trace files up to the parent traces/ dir if needed
    (run_id subdirectory layout):

    cp <WS>/traces/<app_name>/*.pfw.gz <WS>/traces/ 2>/dev/null || true

7d. Split and analyze:

    APP_NAME = first component of RUN_ID
    session_split_traces(run_id=RUN_ID, app_name=APP_NAME)
    session_analyze_traces(run_id=RUN_ID, query_type="summary")


══════════════════════════════════════════════════════════════════════
STEP 8 — OPTIMIZATION PIPELINE  (MCP tools)
══════════════════════════════════════════════════════════════════════

Ask: "Generate optimization proposals? [yes / no]"

If yes:

8-PRE. DETECT FILESYSTEM AND STORAGE CONTEXT (mandatory before any proposals)

    Before searching for papers or generating proposals, identify the actual
    filesystem where the application is performing I/O.  This drives which
    L2/L3 strategies are valid and which search terms to use.

    Detect filesystem type of the data directory:
      stat -f --format="%T" <DATA_DIR>   # e.g. "nfs", "ext2/ext3", "lustre"
      df -T <DATA_DIR>                   # shows filesystem type
      lfs getname <DATA_DIR> 2>/dev/null # non-empty → Lustre
      # For VAST: usually shows as NFS mount but df -T shows "nfs" or "tmpfs"
      # Check mount source: mount | grep <DATA_DIR>

    Store the result as FS_TYPE. Classify into one of:
      FS_TYPE = "lustre"      # lfs command works; OST/MDT visible
      FS_TYPE = "vast"        # NVMe-backed NFS or proprietary; no lfs commands
      FS_TYPE = "gpfs"        # IBM Spectrum Scale / GPFS
      FS_TYPE = "beegfs"      # BeeGFS distributed FS
      FS_TYPE = "nfs"         # standard NFS (no parallel I/O tuning)
      FS_TYPE = "local_nvme"  # local NVMe SSD
      FS_TYPE = "local_hdd"   # local spinning disk
      FS_TYPE = "unknown"     # could not determine

    Print: "I/O filesystem detected: <FS_TYPE> at <DATA_DIR>"

    FS_TYPE is used in:
      - Paper search queries (include filesystem name)
      - system_score assignment (papers for wrong FS score 0)
      - L3 strategy filtering (only propose valid strategies for FS_TYPE)

    FILESYSTEM COMPATIBILITY TABLE FOR L3 PROPOSALS:
    ┌──────────────────────────┬─────────────────────────────────────────┐
    │ Strategy                 │ Valid FS_TYPE                           │
    ├──────────────────────────┼─────────────────────────────────────────┤
    │ lfs setstripe / stripe   │ lustre only                             │
    │ lfs mkdir -c (DNE)       │ lustre only                             │
    │ romio_ds_write=disable   │ lustre only (FATAL on vast/NVMe)        │
    │ romio_cb_read=enable     │ lustre, gpfs, nfs (HARMFUL on vast)     │
    │ romio_cb_write=enable    │ all parallel FS (lustre, vast, gpfs…)   │
    │ blockdev --setra          │ local_nvme, local_hdd only              │
    │ I/O scheduler (none/mq)  │ local_nvme, local_hdd only              │
    │ vm.dirty_* sysctl        │ all local filesystems                   │
    │ mmchattr (GPFS)          │ gpfs only                               │
    │ beegfs-ctl tuning        │ beegfs only                             │
    │ NFS rsize/wsize mount    │ nfs only                                │
    └──────────────────────────┴─────────────────────────────────────────┘

    DO NOT propose L3 strategies for a different FS_TYPE.
    For "unknown" FS_TYPE: propose only L1 and L2 strategies; omit L3.

8a. Run the baseline iteration loop (profiling + diagnosis + literature search):

    session_optimization_iteration(run_id=RUN_ID, command=SMOKE_CMD,
      app_name=APP_NAME, data_dir="all",
      env_extra=DFTRACER_INIT_ENV,
      optimization_applied="baseline", rebuild=False)

    This runs the benchmark, splits traces, diagnoses bottlenecks AND
    searches arXiv/Semantic Scholar for papers relevant to each bottleneck.
    Repeat for each optimization iteration.

8b. Generate citation-backed proposals from the iteration results:

    session_generate_optimization_proposals(run_id=RUN_ID, iteration=-1)

    This maps each diagnosed bottleneck to concrete L1/L2/L3 proposals
    backed by papers found in 8a.

    CRITICAL: Every optimization proposal MUST be backed by a paper citation.
    Papers are not rejected by age but are scored and ranked — the
    highest-ranked paper for each bottleneck becomes the citation.

8b-i. PAPER RELEVANCE SCORING

    For every candidate paper, compute a composite relevance score (0–100):

      score = bottleneck_score + system_score + recency_score

    Component definitions:

      bottleneck_score (0–50):
        How directly does the paper address the specific bottleneck metric?
        • 50 — paper directly studies this metric or the exact I/O pattern
                (e.g., paper on "POSIX close latency in HDF5 timestep writes")
        • 35 — paper addresses the broader I/O category
                (e.g., "collective metadata performance in parallel I/O")
        • 20 — paper addresses an adjacent technique that indirectly applies
                (e.g., "MPI-IO collective buffering" for a metadata bottleneck)
        •  0 — paper is topically unrelated to the bottleneck

      system_score (0–30):
        How well does the paper match the ACTUAL detected filesystem and stack?
        This score is filesystem-specific — a paper about Lustre tuning scores 0
        when the detected FS_TYPE is "vast" or "local_nvme", even though both
        are "parallel filesystems."

        • 30 — paper studies the SAME filesystem/storage product as FS_TYPE
                (e.g., FS_TYPE=lustre and paper evaluates on Lustre;
                 FS_TYPE=vast and paper evaluates on VAST or all-NVMe NAS)
        • 25 — same library/runtime regardless of filesystem
                (e.g., paper uses HDF5 + ROMIO; or MPI-IO collective I/O
                 technique validated on multiple filesystems)
        • 15 — same storage technology class (NVMe vs spinning disk vs network)
                (e.g., FS_TYPE=local_nvme and paper studies NVMe-based storage;
                 FS_TYPE=vast and paper studies NVMe-backed parallel storage)
        • 10 — same application domain (scientific HPC I/O, checkpoint)
                on a DIFFERENT storage class
        •  0 — paper is for a DIFFERENT specific filesystem
                (e.g., paper is Lustre-only when FS_TYPE=vast or gpfs;
                 paper is cloud/object-store when FS_TYPE=lustre)
        •  0 — different system class (cloud-only, database, in-memory)

        HARD RULE: A paper that proposes a strategy that is KNOWN TO HARM
        the detected FS_TYPE (see compatibility table in 8-PRE) must score 0
        on system_score, even if it scores high on bottleneck_score.
        Do NOT rank it as a citation for a proposal on this system.

      recency_score (0–20):
        age_years = current_year - publication_year
        • 20 — age_years ≤ 2
        • 15 — age_years ≤ 5
        • 10 — age_years ≤ 10
        •  5 — age_years ≤ 15
        •  0 — age_years > 15

    Rank all candidate papers by score descending.
    Use the top-ranked paper (highest score) as the primary citation for each
    bottleneck. If the top paper scores < 20 total, add a second search pass
    before accepting it.

8b-ii. PAPER SEARCH PROCEDURE

    If session_generate_optimization_proposals returns empty or unsupported
    bottlenecks, search manually.  ALWAYS include FS_TYPE in the queries:

      dftracer__search_arxiv / dftracer__search_semantic_scholar with queries:
        - Bottleneck metric name + I/O domain + FS_TYPE
          (e.g., "posix close latency HDF5 VAST NVMe parallel I/O")
          (e.g., "posix seek ops collective I/O Lustre MPI-IO")
        - Broader technique synonyms + FS_TYPE
          (e.g., "collective buffering MPI-IO NVMe storage")
          (e.g., "metadata caching HDF5 Lustre parallel filesystem")
        - System-specific terms from the detected stack
          (e.g., "ROMIO two-phase I/O VAST" or "ROMIO Lustre striping checkpoint")

      If FS_TYPE is "unknown", omit the filesystem name and search broadly,
      then mark all L3 proposals as "requires filesystem verification."

      Score every result using the rubric above, applying the filesystem filter.
      If top score < 20 after 3 searches → state:
        "Best available citation scores <N>/100 for this bottleneck
         (title: <title>, year: <year>). Proceeding with caveat."
      NEVER propose an optimization with zero candidate papers.

8c. Present proposals in this format (one per bottleneck, top-ranked citation):

    ┌─────────────────────────────────────────────────────────────┐
    │  OPTIMIZATION PROPOSAL — <bottleneck> (<severity>)          │
    │  Filesystem: <FS_TYPE> at <DATA_DIR>                        │
    ├─────────────────────────────────────────────────────────────┤
    │  Evidence: <paper title>, <authors>, <year>                 │
    │  URL: <arxiv or doi url>                                    │
    │  Relevance score: <N>/100  (bottleneck=N, system=N, age=N) │
    ├─────────────────────────────────────────────────────────────┤
    │  L1 (application):  <specific code/config change>           │
    │  L2 (middleware):   <library/runtime tuning>                │
    │  L3 (filesystem):   <FS_TYPE-specific config — or N/A>      │
    └─────────────────────────────────────────────────────────────┘

    If no valid L3 strategy exists for the detected FS_TYPE, write:
      "L3 (filesystem): N/A — no validated strategy for <FS_TYPE>"
    NEVER propose an L3 strategy from the incompatibility table above.

8d. ITERATIVE OPTIMIZATION LOOP  (max 10 iterations)

    Run up to 10 full optimization cycles. Each cycle applies ALL THREE
    layers (L1 → L2 → L3) then re-profiles to discover new bottlenecks.
    Stop early only if the termination conditions in 8f are met.

    Maintain a loop state table across iterations:

      ITER  | applied_opts          | new_bottlenecks | resolved | raw_rate
      ------|-----------------------|-----------------|----------|----------
      0     | baseline              | <list>          | —        | <val>
      1     | <L1+L2+L3 changes>    | <list>          | <list>   | <val>
      ...

    For each iteration i = 1 … 10:

    8d-0.  SMOKE VALIDATION BEFORE PRODUCTION RUN  ← MANDATORY BEFORE EVERY ITER

        Before running any optimization at production scale, validate each
        proposed change with a smoke run using minimal resources and tiny data.
        Test each optimization in isolation (L1 alone, L2 alone, L3 alone,
        then combinations) and scale up gradually until you find the smallest
        configuration that fails OR confirm all pass.

        Gradual scale steps (Tuolumne example):
          Step 1 — 1 node, 4 ranks, DIM_1=1M, TIMESTEPS=2   (~seconds)
          Step 2 — 1 node, 96 ranks, same small DIM          (~seconds)
          Step 3 — 2 nodes, 192 ranks, small DIM             (~seconds)
          Step 4 — 2 nodes, 192 ranks, medium DIM (½ of production)
          Step 5 — production scale only if all above pass

        For each step, test in this order:
          a) baseline (no optimization) — confirm it passes at this scale
          b) L3 only (filesystem: lfs setstripe)
          c) L2 only (middleware: ROMIO hints via wrapper script)
          d) L1 only (app: config changes)
          e) L3 + L2 combined
          f) all three combined

        If a combination fails at step N, binary-search within that combination
        (e.g. remove hints one at a time) to find the specific offending param.

        Smoke run template (h5bench on Tuolumne):
          cat > smoke.cfg << 'EOF'
          MEM_PATTERN=CONTIG
          FILE_PATTERN=CONTIG
          TIMESTEPS=2
          DELAYED_CLOSE_TIMESTEPS=0
          COLLECTIVE_DATA=YES
          COLLECTIVE_METADATA=YES
          NUM_DIMS=1
          DIM_1=1048576
          EOF

          flux proxy $JOB flux run -N 1 -n 4 --env LD_LIBRARY_PATH=$LDPATH \
            bash wrapper.sh $BIN smoke.cfg /p/lustre5/$USER/smoke_test/out.h5

        Record failures in the workload/software skill BEFORE running at scale.
        Only proceed to production run with configurations confirmed at all steps.
        See R11 in [[dftracer-annotation-lessons]] for full rationale.

    8d-i.  Apply optimizations ONE AT A TIME, measure each individually,
        then measure the combined effect.

        !! MANDATORY — never apply all three layers in one shot and compare
        only the combined result. The user must always see per-optimization
        impact so they know which change contributed what. !!

        For each proposed optimization OPT_k in (L3, L2, L1):

          a) Apply OPT_k on top of BASELINE (not on top of previous opts).
          b) Run the benchmark with OPT_k only:
               session_optimization_iteration(run_id=RUN_ID, command=SMOKE_CMD,
                 app_name=APP_NAME, data_dir="all",
                 env_extra=DFTRACER_INIT_ENV,
                 optimization_applied="iter-<i>: <OPT_k name> only",
                 rebuild=<True if L1, False if L2/L3-only>)
             Store trace as TRACE_OPT_k.
          c) Compare OPT_k vs BASELINE using the comparator:
               comparator(trace_a=TRACE_BASELINE, trace_b=TRACE_OPT_k)
             Record impact: IMPROVED / NEUTRAL / REGRESSED per metric.

        After all individual OPT_k runs:

          d) Apply ALL passing opts together (those that were IMPROVED or
             at least NEUTRAL on their own):
               session_optimization_iteration(run_id=RUN_ID, command=SMOKE_CMD,
                 app_name=APP_NAME, data_dir="all",
                 env_extra=DFTRACER_INIT_ENV,
                 optimization_applied="iter-<i>: all combined",
                 rebuild=True)
             Store trace as TRACE_COMBINED.

        Then run the N-WAY COMPARATOR to show all traces at once:

          e) comparator(
               trace_a = TRACE_BASELINE,
               trace_b = TRACE_OPT_1,
               trace_c = TRACE_OPT_2,
               trace_d = TRACE_OPT_3,
               trace_e = TRACE_COMBINED
             )

        This produces a single table showing the contribution of each
        optimization independently AND their synergistic combined effect.

        Present results as:
          OPT          | raw_rate  | delta vs baseline | key bottleneck change        | citation (author, year, score/100)
          -------------|-----------|-------------------|-----------------------------|---------------------------------
          baseline     | <val>     | —                 | —                           | —
          L3 only      | <val>     | +X%               | <metric improved>           | <Author et al., YYYY, NN/100>
          L2 only      | <val>     | +X%               | <metric improved>           | <Author et al., YYYY, NN/100>
          L1 only      | <val>     | +X%               | <metric improved>           | <Author et al., YYYY, NN/100>
          L3+L2+L1     | <val>     | +X%               | cumulative                  | —

        !! MANDATORY: Every optimization row MUST have a citation. If the hint was
        silently dropped or unrecognized, note "N/A — hint unrecognized" in the
        key bottleneck change column but still show the citation that motivated
        the attempt. Never leave the citation column blank for a tested OPT. !!

        Any optimization that is REGRESSED or NEUTRAL individually:
          → Do NOT include in the combined run.
          → Record it immediately as a failed config (see 8d-iii-FAIL).
          → Note in the table with reason (e.g. "SKIP — neutral alone").

        IMPORTANT: Run the individual optimization runs in parallel where
        the allocation has enough nodes (e.g. L3-only and L2-only can run
        simultaneously if nodes are available, since each is applied on
        top of BASELINE, not on top of each other).

        If any layer reports "no applicable optimizations", skip it and
        continue with the remaining layers. Do NOT skip the combined run.

    8d-ii. Re-profile with the combined changes (if step 8d-i-d above ran):

        The TRACE_COMBINED from 8d-i-d is the authoritative profile for
        this iteration. Use it as the basis for next-iteration bottleneck
        analysis and proposals.

        session_optimization_iteration(run_id=RUN_ID, command=SMOKE_CMD,
          app_name=APP_NAME, data_dir="all",
          env_extra=DFTRACER_INIT_ENV,
          optimization_applied="iter-<i>: L1+L2+L3",
          rebuild=True)

        This rebuilds the annotated binary, runs the benchmark, splits
        traces, diagnoses bottlenecks, and searches for new papers.
        Store the trace path for this iteration as TRACE_ITER_<i>.

    8d-iii. Compare this iteration against the previous one using the
        dftracer comparator MCP tool:

        comparator(
          trace_a = TRACE_ITER_<i-1>,   # previous iteration's trace path
          trace_b = TRACE_ITER_<i>       # current iteration's trace path
        )

        The comparator output includes per-metric deltas (improved /
        regressed / unchanged) and a ranked list of changes. Use this
        output — not manual arithmetic — as the authoritative source for:
          • Which bottlenecks were resolved between iterations
          • Which metrics worsened (regression check for 8e)
          • The raw rate and observed completion time delta to show in
            the loop state table

        For the baseline comparison (i = 1), use TRACE_ITER_0 as trace_a.

    8d-iii-FAIL. Record any regressed or neutral configurations IMMEDIATELY.

        After every comparator result, classify each applied change as:
          IMPROVED  — metric improved vs previous iteration
          NEUTRAL   — change within ±5% (no meaningful effect)
          REGRESSED — metric worsened by > 5%

        For every NEUTRAL or REGRESSED change, write a failed-config entry
        to the appropriate skill file RIGHT NOW (do not wait for Step 9).

        Use the ROUTING TABLE from Step 9 to pick the target file.
        Append this block under the "## Failed Configurations" section
        of that skill file (create the section if it does not exist):

          ---
          date: YYYY-MM-DD
          app: <APP_URL>
          workload: <APP_NAME>
          filesystem: <FS_TYPE>
          system: <HPC system, e.g. Tuolumne>
          bottleneck: <bottleneck metric that was targeted>
          config_attempted: |
            <exact env vars, flags, or code changes that were applied>
          result: REGRESSED | NEUTRAL
          metrics_before: <key metric values from TRACE_ITER_<i-1>>
          metrics_after:  <key metric values from TRACE_ITER_<i>>
          delta: <pct change, e.g. "-70% read BW", "+0% write BW">
          root_cause: <why this configuration hurt or had no effect>
          do_not_use_when: <condition under which this config is harmful>
          ---

        IMPORTANT: Roll back the regressed changes before continuing the loop.
        Do NOT carry a known-bad configuration into the next iteration.

    8d-iv. Generate updated proposals for newly surfaced bottlenecks:

        session_generate_optimization_proposals(run_id=RUN_ID, iteration=i)

        Score and rank candidate papers per 8b-i. Present any new
        proposals using the format in 8c. Skip bottlenecks that were
        already fully addressed in a prior iteration (no new proposals
        means no proposal box for that bottleneck).

        BEFORE proposing any configuration: check the "## Failed Configurations"
        section of every relevant skill file (workload, software, filesystem).
        If the proposed config matches a known REGRESSED entry for the same
        FS_TYPE and workload, SKIP that proposal and note:
          "Skipped: <config> previously caused <delta> regression on
           <FS_TYPE>/<workload> — see <skill_file>#Failed Configurations"

    8d-v. Update the loop state table and print a one-line delta summary
        derived from the comparator output:

        "Iter <i>: resolved=<n> bottlenecks, new=<m>, raw_rate <before>→<after> GB/s (comparator: <overall verdict>)"
        "  Applied: <list of changes>"
        "  Regressed/rolled back: <list, or 'none'>"

8e. Check termination conditions after each iteration:

    Stop the loop early (before reaching 10 iterations) if ANY of:

      EXHAUSTED  — All three layers report "no applicable optimizations"
                   for every active bottleneck in this iteration.
      CONVERGED  — No new bottlenecks surfaced AND no resolved bottlenecks
                   changed severity compared to the previous iteration.
      REGRESSED  — The comparator reports that raw write rate or observed
                   completion time worsened by > 5% versus the previous
                   iteration. Use the comparator's metric delta output as
                   the authoritative source — do not recompute manually.
                   (Roll back the last iteration's changes; the failed-config
                   entry was already written in 8d-iii-FAIL. Stop loop.)
      MAX_ITERS  — 10 iterations completed.

    On stopping, print the reason:
      "Optimization loop ended after <i> iterations: <reason>"

8f. Final all-layers summary after the loop:

    Print the completed loop state table (all iterations).
    Identify the iteration with the best raw write rate as the
    "recommended configuration" and note which optimizations it includes.


══════════════════════════════════════════════════════════════════════
STEP 9 — UPDATE LESSONS LEARNED
══════════════════════════════════════════════════════════════════════

Append any new pitfalls discovered this session to the CORRECT file
based on the nature of the lesson:

  ROUTING TABLE
  ─────────────────────────────────────────────────────────────────
  Lesson type                          → Target file
  ─────────────────────────────────────────────────────────────────
  IOR-specific (build, annotation,     → .agents/skills/workload-ior/SKILL.md
    ROMIO tuning, smoke test)
  H5Bench-specific (build, CMake,      → .agents/skills/workload-h5bench/SKILL.md
    config, annotation edge cases)
  MPI/ROMIO software tuning,           → .agents/skills/software-mpi/SKILL.md
    Flux env propagation, Cray MPICH
  HDF5 version, chunk/cache tuning,    → .agents/skills/software-hdf5/SKILL.md
    Cray chid_t, dftracer HDF5 support
  POSIX readahead, Lustre striping,    → .agents/skills/software-posix/SKILL.md
    OS/VM tuning, ops_slope bottlenecks
  New workload (not IOR or H5Bench)    → create .agents/skills/workload-<name>/SKILL.md
  New software (not MPI/HDF5/POSIX)   → create .agents/skills/software-<name>/SKILL.md
  General/cross-cutting annotation     → .agents/skills/dftracer-annotation-lessons/SKILL.md
  ─────────────────────────────────────────────────────────────────

  ALWAYS also append a one-line cross-reference entry to
  dftracer-annotation-lessons/SKILL.md pointing at the target file,
  so agents loading the general lessons file can find the new entry.

  NEW WORKLOAD / SOFTWARE SKILL:
  If the workload or software has no existing skill file, create one:
    1. mkdir -p .agents/skills/workload-<name>/
    2. Write SKILL.md with frontmatter (name, description), cross-reference
       links to [[dftracer-annotation-lessons]] and related skills, and the
       lesson entry below.
    3. Add a cross-reference line to dftracer-annotation-lessons/SKILL.md
       under "Related Skills" pointing at the new skill.

Entry format (same regardless of target file):

  ---
  date: YYYY-MM-DD
  app: <APP_URL>
  context: <one-line description>
  error: |
    <exact error or key excerpt>
  root_cause: <why it happened>
  fix: |
    <exact steps or rule>
  tags: [c|cpp|python, annotation, <keyword>]
  ---

Do not duplicate entries already present.


══════════════════════════════════════════════════════════════════════
STEP 10 — SESSION REPORT
══════════════════════════════════════════════════════════════════════

Generate a Markdown session report and write it to
<WS>/session_report.md using the Write tool.

The report MUST follow this exact structure:

---

# DFTracer Session Report — <RUN_ID>

**Application:** <APP_URL> @ <REF>  
**Date:** <YYYY-MM-DD>  
**Platform:** <OS / arch / HPC system>

---

## What Ran Correctly (Pipeline Flow)

Document each step that succeeded as a numbered flow:

1. **Session setup** — cloned <URL>, built with <build_tool>
2. **dftracer install** — version <X.Y.Z>, installed to <path>
3. **Source copy** — <N> files copied to annotated/
4. **Annotation** — <N> files annotated (<M> functions), <K> skipped
5. **Build** — annotated binary built successfully at <path>
6. **Smoke test** — ran `<command>`, completed in <time>
7. **Trace collection** — <N> trace files, <M> events, <K> KB
8. **Trace analysis** — bottlenecks found: <list>
9. **Optimization loop** — <N> iterations (stopped: <reason>); best speedup at iter <K>

Include the full iteration table:

| Iter | L1 applied | L2 applied | L3 applied | New bottlenecks | Resolved | Raw rate |
|------|-----------|-----------|-----------|-----------------|----------|----------|
| 0    | baseline  | baseline  | baseline  | <list>          | —        | <val>    |
| 1    | <change>  | <change>  | <change>  | <list>          | <list>   | <val>    |
| …    |           |           |           |                 |          |          |

For each iteration's applied optimizations, include a sub-section:

### Iteration <N> — <L1/L2/L3 summary>
- **L1 (app):** <code or config change>
- **L2 (middleware):** <library/runtime tuning>
- **L3 (filesystem):** <storage/system config>
- **Evidence:** <paper title>, <authors>, <year>, score=<N>/100 — <URL>
- **Result:** Before: <metric=val>, After: <metric=val>, Δ = <pct>%
- **Stop reason (if last iter):** <EXHAUSTED / CONVERGED / REGRESSED / MAX_ITERS>

---

## Lessons Learned (Pitfalls)

For each pitfall encountered, include a sub-section:

### Pitfall: <short title>
- **Step:** <which pipeline step this occurred in>
- **Error:** (verbatim excerpt from compiler/linker/runtime output)
  ```
  <error text>
  ```
- **Root cause:** <one paragraph explaining why it happened>
- **Fix:** <exact commands or code changes that resolved it>
- **Lesson added to:** <path to lessons file> as tag `<tag>`

---

## Summary

| Category | Value |
|---|---|
| Files annotated | <N> |
| Functions annotated | <N> |
| Bottlenecks diagnosed | <N> (high: N, medium: N) |
| Optimization iterations | <N> (max 10, stopped: <reason>) |
| Optimizations applied | <N> (L1=N, L2=N, L3=N) |
| Raw write rate improvement | <pct>% |
| Observed completion improvement | <pct>% |
| Papers cited (avg relevance score) | <N> (avg <X>/100) |
| Pitfalls resolved | <N> |

---

## Artifacts

| Artifact | Path |
|---|---|
| Annotated source | workspaces/<RUN_ID>/annotated/ |
| Annotated build | workspaces/<RUN_ID>/build_ann/ |
| Trace files | workspaces/<RUN_ID>/traces/ |
| Split traces | workspaces/<RUN_ID>/traces_split/ |
| Session report | workspaces/<RUN_ID>/session_report.md |

---

After writing session_report.md, print its path and a one-line summary
of the key result (e.g., "Raw write rate improved X% via collective I/O
and ROMIO collective buffering — see <path> for full report").


══════════════════════════════════════════════════════════════════════
TOOL REFERENCE
══════════════════════════════════════════════════════════════════════

| Purpose                          | MCP Tool                        |
|----------------------------------|---------------------------------|
| Create session + clone           | session_create                  |
| Configure original build         | session_configure               |
| Build + install original         | session_build_install           |
| Install dftracer (C/C++ cmake)   | session_install_dftracer        |
| Copy source → annotated/         | session_copy_annotated          |
| Annotate entire project at once  | clang_annotate_project          |
| Annotate one file (with overrides)| clang_annotate_file            |
| Extract function map + line #s   | clang_extract_functions         |
| Estimate per-function cost       | clang_estimate_function_cost    |
| Compiler syntax check            | clang_syntax_check              |
| Lint macro ordering              | clang_lint_annotations          |
| Flush in-memory buffer to disk   | clang_write_annotated_file      |
| Build annotated version          | session_build_annotated         |
| Run smoke test                   | session_run_smoke_test          |
| Annotation coverage report       | session_annotation_report       |
| Run with dftracer tracing        | session_run_with_dftracer       |
| Split trace files                | session_split_traces            |
| Analyze traces                   | session_analyze_traces          |
| Search optimization papers       | session_search_optimization_papers |
| Generate optimization proposals  | session_generate_optimization_proposals |
| Apply L1 app optimizations       | session_optimize_l1_app         |
| Apply L2 software optimizations  | session_optimize_l2_software    |
| Apply L3 filesystem optimizations| session_optimize_l3_filesystem  |
| Compare two trace runs           | comparator                      |

NEVER use:
  • Read / Write / Edit tools to manually insert macros into source files
  • Bash `gcc -fsyntax-only` — use clang_syntax_check instead
  • session_annotate_c_file / session_annotate_cpp_file — superseded by
    clang_annotate_file and clang_annotate_project


══════════════════════════════════════════════════════════════════════
ANNOTATION RULES QUICK REFERENCE
══════════════════════════════════════════════════════════════════════

These are enforced automatically by clang_annotate_file/project but
apply when you use comp_overrides:

  comp= "io"   — POSIX I/O, HDF5, NetCDF, MPI-IO file ops, lifecycle fns
  comp= "comm" — MPI wrappers, network, S3, HDFS, RADOS, DFS
  comp= "mem"  — memcpy, malloc/free of large buffers, mmap setup
  comp= "cpu"  — checksums, compression, encryption, hashing

  ALWAYS annotate (never apply score filter):
    • Lifecycle: *_init, *_final, *_initialize, *_finalize
    • Sync/flush: *_fsync, *_flush, *_sync
    • File ops:   *_delete, *_rename, *_stat, *_mknod, *_getfilesize
    • Vendor FS:  gpfs_*, beegfs_*, lustre_*, hdfs_*, ceph_*, daos_*

  Score ≥ 20 (from clang_estimate_function_cost) → annotate
  Score < 20 and not lifecycle/vendor → skip

  INIT/FINI placement in main() (enforced by is_entry=True):
    DFTRACER_C_INIT(NULL, NULL, NULL)  ← before FUNCTION_START
    DFTRACER_C_FUNCTION_START()
    DFTRACER_C_FUNCTION_UPDATE_STR("comp", "cpu")
    ... benchmark work ...
    DFTRACER_C_FUNCTION_END()
    DFTRACER_C_FINI()                 ← after all benchmark work
    MPI_Finalize();                   ← FINI must precede MPI_Finalize
