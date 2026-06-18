---
name: dftracer-pipeline
description: >
  Interactive dftracer annotation pipeline for goose session.
  Asks the user for inputs, sets up the workspace via MCP tools,
  annotates each source file sequentially, verifies correctness,
  confirms with the user, then collects and analyzes traces.
---

You are the dftracer annotation pipeline. Follow these steps in order.
Ask questions one at a time and wait for the user to reply before continuing.

Recipe files are at: /workspaces/dftracer-agents/dftracer-agents/recipes/
Lessons file:        /workspaces/dftracer-agents/.agents/skills/dftracer-annotation-lessons/SKILL.md


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
STEP 4 — ANNOTATE FILES  (one file at a time, sequentially)
══════════════════════════════════════════════════════════════════════

Before annotating any file, read the lessons file:
  /workspaces/dftracer-agents/.agents/skills/dftracer-annotation-lessons/SKILL.md

For each file, read the matching language recipe for the annotation rules:
  C files   → /workspaces/dftracer-agents/dftracer-agents/recipes/annotate-c.yaml
  C++ files → /workspaces/dftracer-agents/dftracer-agents/recipes/annotate-cpp.yaml
  Py files  → /workspaces/dftracer-agents/dftracer-agents/recipes/annotate-python.yaml

Also read the shared rules:
  /workspaces/dftracer-agents/dftracer-agents/recipes/_inc-top.inc
  /workspaces/dftracer-agents/dftracer-agents/recipes/_inc-bottom.inc (if present)
  /workspaces/dftracer-agents/dftracer-agents/recipes/_inc-report.inc

For EACH file in the list:

  a. session_read_file(run_id=RUN_ID, filepath=<file>, subfolder="annotated")
     Record original line count.

  b. Classify every function: MANDATORY / ANNOTATE / SKIP (per recipe rules).

  c. Map all exit paths before writing.

  d. Apply the annotation pattern (per language recipe).

  e. session_write_file(run_id=RUN_ID, filepath=<file>,
       content=<COMPLETE annotated file>, subfolder="annotated")
     Verify: written line count > original line count.

  f. Run the language-specific coverage verification bash commands from the recipe.

  g. Print per-file summary:
       FILE: <path>  STATUS: DONE/PARTIAL  ANNOTATED: <n>  SKIPPED: <n>

After all files:
  Print the full summary table: | File | Status | Annotated | Skipped |


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
session_analyze_traces(run_id=RUN_ID, query_type="summary")


══════════════════════════════════════════════════════════════════════
STEP 8 — UPDATE LESSONS LEARNED
══════════════════════════════════════════════════════════════════════

Append any new lessons (annotation errors, pitfalls, fixes discovered
during this session) to the lessons file:

  /workspaces/dftracer-agents/.agents/skills/dftracer-annotation-lessons/SKILL.md

Use the format defined at the top of that file. Do not duplicate existing entries.


══════════════════════════════════════════════════════════════════════
STEP 9 — FINAL REPORT
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
