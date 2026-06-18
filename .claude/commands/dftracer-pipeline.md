You are running the dftracer annotation pipeline.

Before doing anything else, read the annotation skill files:
- /workspaces/dftracer-agents/.agents/skills/dftracer-annotate-general/SKILL.md
- /workspaces/dftracer-agents/.agents/skills/dftracer-annotation-lessons/SKILL.md (if it exists)

These contain the rules and lessons-learned that govern every annotation decision. Apply them throughout.

SKILL_FILE=/workspaces/dftracer-agents/.agents/skills/dftracer-annotation-lessons/SKILL.md

---

## TOOL NAMES — CLAUDE CODE ENVIRONMENT

| Task                    | Tool to use                                 |
|-------------------------|---------------------------------------------|
| Read a file             | Read                                        |
| Write / overwrite file  | Write                                       |
| Edit a region           | Edit                                        |
| Run a shell command     | Bash                                        |
| dftracer MCP setup      | dftracer__session_run_pipeline              |
| Install dftracer        | dftracer__session_install_dftracer          |
| Build annotated/        | dftracer__session_build_annotated           |
| Run smoke test          | dftracer__session_run_smoke_test            |
| List files in workspace | dftracer__session_list_files               |
| Annotation report       | dftracer__session_annotation_report        |
| Run with dftracer       | dftracer__session_run_with_dftracer        |
| Split traces            | dftracer__session_split_traces             |
| Analyze traces          | dftracer__session_analyze_traces           |
| Fetch dftracer API docs | dftracer__docs_search                      |

---

## STEP 0.5 — FETCH DFTRACER API DOCS

Before looking at any source files, call:
```
dftracer__docs_search(query="DFTRACER_C_FUNCTION_START DFTRACER_C_FUNCTION_END UPDATE_STR UPDATE_INT INIT FINI C API", source="dftracer", fetch_content=True)
dftracer__docs_search(query="annotation macros comp metadata io comm mem cpu", source="dftracer", fetch_content=True)
```

Record the exact macro signatures and include paths. Do not guess — use what the docs say.

---

## STEP 1 — GATHER INPUTS

If not already provided, ask:
- Q1: Git URL of the application (store as APP_URL)
- Q2: Branch or tag (default: main → store as REF)
- Q3: Smoke test command (leave blank to auto-detect → store as SMOKE_CMD)
- Q4: Extra build flags (leave blank to skip → store as EXTRA_FLAGS)

If the user invoked this command with arguments like `url=... ref=...`, use those directly.

---

## STEP 2 — SETUP (MCP)

Call:
```
dftracer__session_run_pipeline(url=APP_URL, ref=REF,
  smoke_test_command=SMOKE_CMD, extra_cmake_flags=EXTRA_FLAGS)
```

Extract from the response:
- RUN_ID ← run_id
- LANGUAGES ← languages list
- BUILD_TOOL ← build_tool
- SMOKE_CMD ← step_reports.step_5_smoke_test.command (if not set by user)
- WS ← workspace path

Install dftracer (must happen before annotation loop so incremental builds work):
```
dftracer__session_install_dftracer(run_id=RUN_ID)
```
On failure → print error and stop.

Baseline annotated build (verifies build system patch, no macros yet):
```
dftracer__session_build_annotated(run_id=RUN_ID)
```
On failure → print cmake/make error and stop.

Store: BUILD_ANN = <WS>/build_ann

Print: "Setup complete. RUN_ID=<RUN_ID>  Languages=<LANGUAGES>  dftracer installed. Baseline build passed."

---

## STEP 3 — LIST AND ORDER FILES

Use dftracer__session_list_files to collect:
- C_FILES   ← annotated/**/*.c  (skip *.h)
- CPP_FILES ← annotated/**/*.cpp + *.cxx + *.cc
- PY_FILES  ← annotated/**/*.py

Exclude paths containing: /test/, /tests/, /vendor/, /third_party/, /CMakeFiles/, /.git/, conftest.py, setup.py

Order (process in this sequence):
1. Library/inner files first (no `main()` / no `if __name__`)
2. Entry-point files last

Detect entry points:
```bash
grep -rl "^int main\b" <WS>/annotated/ --include="*.c" --include="*.cpp" 2>/dev/null
grep -rl "if __name__" <WS>/annotated/ --include="*.py" 2>/dev/null
```

Print: "Files to annotate — C: <n>  C++: <n>  Python: <n>  (entry-points last)"

---

## STEP 4 — SEQUENTIAL ANNOTATION WITH INCREMENTAL BUILDS

**Process ONE file at a time.** Do NOT move to the next file until the current one builds.

For each FILE in order:

### 4a. Read the file
```
Read(<WS>/annotated/<FILE>)
```

Also prepare the syntax-check stub (once, reuse for all C/C++ files):
```bash
cat > /tmp/dftracer_stub.h << 'EOF'
#ifndef DFTRACER_STUB_H
#define DFTRACER_STUB_H
#define DFTRACER_C_INIT(a,b,c)        do{}while(0)
#define DFTRACER_C_FINI()             do{}while(0)
#define DFTRACER_C_FUNCTION_START()   do{}while(0)
#define DFTRACER_C_FUNCTION_END()     do{}while(0)
#define DFTRACER_C_FUNCTION_UPDATE_STR(k,v) do{}while(0)
#define DFTRACER_C_FUNCTION_UPDATE_INT(k,v) do{}while(0)
#define DFTRACER_CPP_INIT(a,b,c)     do{}while(0)
#define DFTRACER_CPP_FINI()          do{}while(0)
#define DFTRACER_CPP_FUNCTION()      do{}while(0)
#define DFTRACER_CPP_FUNCTION_UPDATE(k,v) do{}while(0)
#define DFTRACER_CPP_REGION_START(n) do{}while(0)
#define DFTRACER_CPP_REGION_END(n)   do{}while(0)
#endif
EOF
```

### 4b. Classify every function (apply SKILL.md rules)

Build a function inventory using the annotation rules from the skill files:
- ANNOTATE if: performs I/O, data movement, MPI comms, lifecycle, vendor FS calls, entry points
- SKIP (Rule 0) if: trivial getter/setter, string formatter, ≤5 lines no I/O, tight-loop helper

**comp= mapping:**
- POSIX/HDF5/MPIIO/MMAP file transfer → "io"
- MPI/network/S3/HDFS/RADOS transfer → "comm"
- memcpy into mmap/buffer region → "mem"
- CPU-bound computation → "cpu"

### 4c. Annotate function by function (per-function incremental loop)

For **each function** to annotate (one at a time):

**C function pattern:**
```c
return_type function_name(params)
{
  DFTRACER_C_FUNCTION_START();
  DFTRACER_C_FUNCTION_UPDATE_STR("comp", "<type>");
  DFTRACER_C_FUNCTION_UPDATE_STR("filename", path_param);  /* key string params */
  DFTRACER_C_FUNCTION_UPDATE_INT("count", (int)n);         /* key numeric params */
  ...
  if (err) {
    DFTRACER_C_FUNCTION_END();
    return -1;
  }
  DFTRACER_C_FUNCTION_END();
  return result;
}
```

**C++ regular function (RAII — no manual END):**
```cpp
#include <dftracer/dftracer.h>   // last include in .cpp only
void my_fn(const char *path) {
  DFTRACER_CPP_FUNCTION();
  DFTRACER_CPP_FUNCTION_UPDATE("comp", "io");
  DFTRACER_CPP_FUNCTION_UPDATE("path", path);
}
```

**main() — C:**
```c
int main(int argc, char **argv) {
  DFTRACER_C_INIT(NULL, NULL, NULL);
  DFTRACER_C_FUNCTION_START();
  ...
  DFTRACER_C_FUNCTION_END();
  DFTRACER_C_FINI();
  return 0;
}
```

**main() — C++ (REGION, not FUNCTION):**
```cpp
int main(int argc, char **argv) {
  DFTRACER_CPP_INIT(nullptr, nullptr, nullptr);
  DFTRACER_CPP_REGION_START(main_region);
  ...
  DFTRACER_CPP_REGION_END(main_region);
  DFTRACER_CPP_FINI();
  return 0;
}
```

**Python:**
```python
from dftracer.logger import dftracer_fn, DFTracer

@dftracer_fn(cat="IO", comp="io")
def read_data(path):
    ...
```

Absolute rules:
- START is the very FIRST statement inside `{` — nothing before it
- For C: map ALL exit paths (each `return` needs an END before it; goto labels get ONE END at the label)
- For C++: RAII destructor handles END automatically; never add manual END after CPP_FUNCTION()
- Never annotate `.h`/`.hpp` header files
- comp= UPDATE is the FIRST UPDATE after START
- Parameter names must come from the function DEFINITION body (not forward declarations)
- MPI_CHECK/NCMPI_CHECK/HGOTO_ERROR macros hide returns — do NOT add END before them

After writing each function's annotation, immediately run the syntax check:

**C syntax check:**
```bash
gcc -include /tmp/dftracer_stub.h -fsyntax-only -w -x c <WS>/annotated/<FILE> 2>&1
```

**C++ syntax check:**
```bash
g++ -include /tmp/dftracer_stub.h -fsyntax-only -w -std=c++14 <WS>/annotated/<FILE> 2>&1
```

**Python syntax check:**
```bash
python3 -c "import ast, sys; ast.parse(open('<WS>/annotated/<FILE>').read())" 2>&1
```

If syntax check **passes** → continue to next function.

If syntax check **fails**:
- Fix ONLY the failing function (max 2 retries)
- Do NOT remove the macro — simplify instead:
  - Remove UPDATE calls for edge-case parameters
  - Cover only the happy-path exit (single return/END pair)
  - Remove FINI/INIT only if that specific call causes the error
- On 2nd failure → strip macros from this function ONLY, mark it as `/* DFTRACER_PENDING: <reason> */`
- Write the new pitfall to SKILL_FILE immediately (see template below)

### 4d. Write the annotated file

After annotating all functions in the file:
- Write the complete annotated file using Write or Edit
- Verify it's not truncated: line count should be ≥ original

### 4e. Incremental build
```bash
make -C <BUILD_ANN> -j4 2>&1
```

If build **passes** → print `✓ <FILE>  (<n> functions annotated)` and move to next file.

If build **fails**:
1. Extract failing lines: `echo "<BUILD_OUT>" | grep "error:" | head -20`
2. Identify the failing function(s) from the error
3. Fix ONLY those functions (same simplify-not-remove rule as syntax fix)
4. Rebuild. Max 2 retries.
5. If still failing after 2 retries → **ESCALATE TO USER**:

```
┌──────────────────────────────────────────────────────────┐
│  ANNOTATION HELP NEEDED                                   │
│  File:     <file>                                         │
│  Function: <name>                                         │
│  Current annotation:                                      │
│  <show 10-20 lines around the annotated function>         │
│  Build error:                                             │
│  <exact compiler error lines>                             │
└──────────────────────────────────────────────────────────┘
How should I handle this?
  [1] Give me guidance — describe what the annotation should look like
  [2] Skip this function only (mark PENDING, keep rest)
  [3] Remove annotation from this function only
```

Wait for user response. Apply it. Write the new rule to SKILL_FILE immediately.

### Lessons-learned template (write immediately when a pitfall is discovered):
```bash
cat >> "$SKILL_FILE" << 'ENDLESSON'
---
date: <YYYY-MM-DD>
app: <APP_URL>
file: <failing file>
function: <function name>
context: <one-line description>
error: |
  <exact compiler error>
root_cause: <why the macro placement failed>
fix: |
  <what resolved it or what the user said>
tags: [<c|cpp|python>, annotation, build-error]
---
ENDLESSON
```

---

## STEP 5 — FINAL SMOKE TEST

All files now build. Detect INIT usage:
```bash
grep -r "DFTRACER_C_INIT\|DFTRACER_CPP_INIT\|DFTracer.initialize_log" \
  <WS>/annotated/ 2>/dev/null | wc -l
```
count > 0 → DFTRACER_INIT_ENV = `{"DFTRACER_INIT": "FUNCTION"}`
count == 0 → DFTRACER_INIT_ENV = `{"DFTRACER_INIT": "PRELOAD"}` but you need to se LD_PRELOAD to dftracer_preload.so

Run smoke test:
```
dftracer__session_run_smoke_test(run_id=RUN_ID, command=SMOKE_CMD, subfolder="build_ann")
```

On dftracer-related failure → targeted fix (same loop as 4e). Otherwise ask user to proceed or stop.

---

## STEP 6 — ANNOTATION REPORT AND USER CONFIRMATION

```
dftracer__session_annotation_report(run_id=RUN_ID)
```

Print summary and ask: "Proceed with dftracer trace run? [yes / no / fix <file> <feedback>]"

If "fix <file>": re-annotate that file with the feedback, rebuild, re-run smoke test, ask again.

---

## STEP 7 — TRACE COLLECTION AND ANALYSIS

```
dftracer__session_run_with_dftracer(run_id=RUN_ID, command=SMOKE_CMD,
  subfolder="build_ann", env_extra=DFTRACER_INIT_ENV)

APP_NAME = first part of RUN_ID (before "/")
dftracer__session_split_traces(run_id=RUN_ID, app_name=APP_NAME)
dftracer__session_analyze_traces(run_id=RUN_ID, query_type="summary")
```

---

## STEP 8 — FLUSH LESSONS TO SKILL FILE

Collect any LESSONS not yet written inline. Append new ones to SKILL_FILE.
Deduplicate: skip if the same error pattern is already present.

---

## PITFALLS (apply to all annotations)

| Code | Pitfall | Fix |
|------|---------|-----|
| PG1  | File truncated after write | Re-read and rewrite the complete file |
| PG2  | Annotated a .h/.hpp header | Move macros to .c/.cpp source |
| PG3  | comp= missing | Add UPDATE_STR("comp",...) after every START |
| PG4  | Lifecycle fn skipped | *_init/*_finalize → always annotate |
| PG5  | Vendor fn skipped | gpfs_*/beegfs_*/lustre_* → comp="io", always |
| PG6  | Re-annotating dirty file | Check for existing macros; restore original first |
| PG7  | Coverage check skipped | Always verify START count == comp count |
| PG8  | UPDATE uses forward-decl param | Read definition body for param names |
| PG9  | END at column 0 | Match indentation of the return line |
| PG10 | Revert-all on build error | Strip ONLY the failing function, mark PENDING |
| PG11 | END after return | Swap order: END must precede the return |
| PG12 | START before { | Move START to first line INSIDE the braces |
| PG13 | MPI_CHECK hides a return | Do NOT add END before MPI_CHECK — only before visible returns |
