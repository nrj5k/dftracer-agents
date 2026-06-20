---
name: dftracer-pipeline
description: >
  Interactive dftracer annotation pipeline for goose session.
  Asks the user for inputs, sets up the workspace via MCP tools,
  annotates all source files in parallel, verifies correctness,
  confirms with the user, then collects and analyzes traces.
---

You are the dftracer annotation pipeline. Follow these steps in order.
Ask questions one at a time and wait for the user to reply before continuing.

Lessons file: /workspaces/dftracer-agents/.agents/skills/dftracer-annotation-lessons/SKILL.md


══════════════════════════════════════════════════════════════════════
STEP 1 — GATHER INPUTS (one question at a time, wait for each answer)
══════════════════════════════════════════════════════════════════════

Q1: "What is the Git URL of the application you want to annotate?"
→ Wait. Store as APP_URL.

Q2: "Which branch or tag? (default: main)"
→ Wait. Store as REF (use "main" if blank).

Q3: "Smoke test command? (leave blank to auto-detect)"
→ Wait. Store as SMOKE_CMD.

Q4: "Extra CMake build flags? (e.g. -DENABLE_MPI=ON — leave blank to skip)"
→ Wait. Store as EXTRA_FLAGS.

Confirm: "Starting pipeline for <APP_URL> @ <REF>"


══════════════════════════════════════════════════════════════════════
STEP 2 — SETUP  (MCP tools)
══════════════════════════════════════════════════════════════════════

Call session_run_pipeline(url=APP_URL, ref=REF,
  smoke_test_command=SMOKE_CMD, extra_cmake_flags=EXTRA_FLAGS,
  skip_annotation=False)

Extract: RUN_ID, LANGUAGES, SMOKE_CMD (use detection value if user left blank).

Print: "Setup complete. Run ID: <RUN_ID>  Languages: <LANGUAGES>"


══════════════════════════════════════════════════════════════════════
STEP 3 — LIST FILES
══════════════════════════════════════════════════════════════════════

Per detected language call session_list_files(run_id=RUN_ID,
  subfolder="annotated", pattern=<pattern>):

  C:      src/**/*.c
  C++:    src/**/*.cpp  src/**/*.cxx  src/**/*.cc
  Python: **/*.py

Exclude paths with: /test/, /tests/, /vendor/, /third_party/,
  /CMakeFiles/, /_build/, conftest.py, setup.py

Print the file counts.


══════════════════════════════════════════════════════════════════════
STEP 4 — ANNOTATE FILES  (all files in parallel)
══════════════════════════════════════════════════════════════════════

These tools live in the DFTracerAnnotation sub-service.

Issue ALL annotation calls simultaneously — one call per file, do not
wait for one to finish before starting the next:

  C files   → session_annotate_c_file(run_id=RUN_ID, filepath=<file>)
  C++ files → session_annotate_cpp_file(run_id=RUN_ID, filepath=<file>)
  Py files  → session_annotate_python_file(run_id=RUN_ID, filepath=<file>)

Each call returns a structured context and a procedure for the agent to
follow for that file. Execute each procedure as directed, then collect
the per-file report it produces.

After all per-file calls have completed and their procedures are done,
call session_annotation_report(run_id=RUN_ID) to generate the summary.

Print the summary table: | File | Status | Annotated | Skipped |


══════════════════════════════════════════════════════════════════════
STEP 5 — BUILD FOR CORRECTNESS  (MCP tools)
══════════════════════════════════════════════════════════════════════

5a. Detect explicit INIT calls:
  ```bash
  grep -r "DFTRACER_C_INIT\|DFTRACER_CPP_INIT\|DFTracer.initialize_log" \
    /workspaces/dftracer-agents/workspaces/<RUN_ID>/annotated/ 2>/dev/null | wc -l
  ```
  count > 0 → DFTRACER_INIT_ENV = {"DFTRACER_INIT": "FUNCTION"}
  count == 0 → DFTRACER_INIT_ENV = {"DFTRACER_INIT": "PRELOAD"}

5b. session_install_dftracer(run_id=RUN_ID)

5c. session_build_annotated(run_id=RUN_ID)
    On failure: re-annotate affected files with the compiler errors as context,
    then retry. Max 2 retries. On third failure: show errors and ask user what to do.

5d. session_run_smoke_test(run_id=RUN_ID, command=SMOKE_CMD, subfolder="build_ann")
    On failure: if DFTRACER symbols in error → re-annotate + retry.
    Otherwise ask user: "Smoke test failed (non-annotation issue). Continue? [yes/stop]"


══════════════════════════════════════════════════════════════════════
STEP 6 — ANNOTATION REPORT + USER CONFIRMATION
══════════════════════════════════════════════════════════════════════

Show:
  ┌─────────────────────────────────────────────────────────┐
  │  ANNOTATION REPORT — <RUN_ID>                           │
  │  <summary table from Step 4>                            │
  │  Coverage: <annotated> / <eligible>                     │
  │  comp:  io=<n>  comm=<n>  mem=<n>  cpu=<n>             │
  │  Build: PASSED   Smoke test: PASSED                     │
  │                                                         │
  │  Annotated source: workspaces/<RUN_ID>/annotated/       │
  └─────────────────────────────────────────────────────────┘

Ask: "Proceed with dftracer trace run? [yes / no / fix <file or function>]"
→ Wait for answer.

  "no"  → stop, print artifact location.
  "fix" → re-annotate the named file(s) with user's feedback, rebuild,
           rerun smoke test, show updated report, ask again.
  "yes" → continue to Step 7.


══════════════════════════════════════════════════════════════════════
STEP 7 — TRACE COLLECTION + ANALYSIS  (MCP tools)
══════════════════════════════════════════════════════════════════════

session_run_with_dftracer(run_id=RUN_ID, command=SMOKE_CMD,
  subfolder="build_ann", env_extra=DFTRACER_INIT_ENV)

APP_NAME = first component of RUN_ID (before "/")
session_split_traces(run_id=RUN_ID, app_name=APP_NAME)
  — this tool is in DFTracerUtilsService.session_subservice

session_analyze_traces(run_id=RUN_ID, query_type="summary")


══════════════════════════════════════════════════════════════════════
STEP 8 — OPTIMIZATION PIPELINE  (optional, MCP tools)
══════════════════════════════════════════════════════════════════════

If the user wants optimization recommendations, run in order:

1. session_diagnose_bottlenecks(run_id=RUN_ID)
     — in DFDiagnoserService.session_subservice

2. session_search_optimization_papers(run_id=RUN_ID)
     — in DFTracerOptimization sub-service

3. session_generate_optimization_proposals(run_id=RUN_ID)
     — in DFTracerOptimization

4. Apply proposals at each layer (can be run in any order or selectively):
   session_optimize_l1_app(run_id=RUN_ID)         — application-level changes
   session_optimize_l2_software(run_id=RUN_ID)    — software/middleware changes
   session_optimize_l3_filesystem(run_id=RUN_ID)  — filesystem/storage changes
     — all three are in DFTracerOptimization


══════════════════════════════════════════════════════════════════════
STEP 9 — DFTRACER PC GENERATION  (if needed)
══════════════════════════════════════════════════════════════════════

To generate a dftracer performance counter configuration:

  session_generate_dftracer_pc(run_id=RUN_ID)
    — registered in DFTracerSessionService.session_subservice
      via register_install_session_tools


══════════════════════════════════════════════════════════════════════
STEP 10 — UPDATE LESSONS LEARNED
══════════════════════════════════════════════════════════════════════

Append any new lessons (annotation errors, pitfalls, fixes discovered
during this session) to the lessons file:

  /workspaces/dftracer-agents/.agents/skills/dftracer-annotation-lessons/SKILL.md

Use the format defined at the top of that file. Do not duplicate existing entries.


══════════════════════════════════════════════════════════════════════
STEP 11 — FINAL REPORT
══════════════════════════════════════════════════════════════════════

  ╔═══════════════════════════════════════════════════════════╗
  ║  DFTRACER PIPELINE COMPLETE — <RUN_ID>                    ║
  ╠═══════════════════════════════════════════════════════════╣
  ║  Application:  <APP_URL> @ <REF>                          ║
  ╠═══════════════════════════════════════════════════════════╣
  ║  ANNOTATION SUMMARY                                       ║
  ║  <per-language annotated/skipped counts>                  ║
  ╠═══════════════════════════════════════════════════════════╣
  ║  TRACE ANALYSIS                                           ║
  ║  <output from session_analyze_traces>                     ║
  ╠═══════════════════════════════════════════════════════════╣
  ║  ARTIFACTS                                                ║
  ║  Annotated source: workspaces/<RUN_ID>/annotated/         ║
  ║  Trace files:      workspaces/<RUN_ID>/traces/            ║
  ║  Split traces:     workspaces/<RUN_ID>/traces_split/      ║
  ╚═══════════════════════════════════════════════════════════╝
