══════════════════════════════════════════════════════
TOOL NAMES — GOOSE ENVIRONMENT (read before calling anything)
══════════════════════════════════════════════════════

In this goose session tools use double-underscore namespacing, NOT dot notation.
Calling a tool with the wrong name returns -32002 "Tool not found" — recover by
using the correct name below.

  WRONG                    CORRECT
  todo.todoWrite           todo__todo_write
  read_file                load              (developer extension read tool)
  write_file               write
  run_shell / bash         shell
  session_read_file        dftracer__session_read_file
  session_write_file       dftracer__session_write_file
  session_annotation_report  dftracer__session_annotation_report
  docs_search              dftracer__docs_search
  docs_fetch_page          dftracer__docs_fetch_page

Rule: MCP tools from the dftracer extension are always prefixed dftracer__.
      Built-in developer tools (load, write, edit, shell, tree) have no prefix.
      Platform extension tools use their extension name + __ (e.g. todo__todo_write).

══════════════════════════════════════════════════════
STEP 0 — LOAD LESSONS LEARNED (do this first)
══════════════════════════════════════════════════════

Read past annotation lessons before touching any code:
```bash
cat /workspaces/dftracer-agents/.agents/skills/dftracer-annotation-lessons/SKILL.md \
  2>/dev/null || echo "(no lessons file yet)"
```
Apply every applicable lesson to this file before writing anything.

══════════════════════════════════════════════════════
STEP 0.5 — FETCH DFTRACER ANNOTATION API DOCS
══════════════════════════════════════════════════════

Before writing a single macro, fetch the official dftracer documentation.
The live docs are the authoritative source — if they differ from the patterns
in these instructions, follow the live docs.

Call ALL of these and read the returned code examples carefully:

  docs_search(query="annotation macros FUNCTION_START FUNCTION_END INIT FINI include", source="dftracer", fetch_content=True)
  docs_search(query="comp metadata UPDATE_STR UPDATE_INT io comm mem cpu types", source="dftracer", fetch_content=True)

Note from the results:
  • The exact #include path for the C/C++ header
  • The exact macro names and argument counts
  • Which macros require explicit END vs RAII
  • The correct comp= string values

══════════════════════════════════════════════════════
GENERAL RULES (apply to every language)
══════════════════════════════════════════════════════

ALWAYS ANNOTATE (mandatory — never apply Rule 0 skip):
  • Lifecycle:   *_init, *_final, *_initialize, *_finalize
  • Sync/flush:  *_fsync, *_flush, *_sync
  • File ops:    *_delete, *_rename, *_stat, *_mknod, *_getfilesize
  • Vendor FS:   gpfs_*, beegfs_*, lustre_*, hdfs_*, ceph_*, daos_*

ANNOTATE if the function:
  • Does file or network I/O (open/read/write/close/stat/mmap/send/recv)
  • Moves or transforms significant data (memcpy, checksums, compression)
  • Allocates or frees large buffers
  • Is a driver, benchmark, or orchestration function
  • Contains a data loop across blocks or ranks
  • Takes measurable wall time

SKIP (Rule 0) ONLY if ALL of these are true:
  • Pure getter/setter ≤ 5 lines, returns a single field
  • No I/O, no data movement, no syscall
  → Justify every skip by function name in the report.

COMP= TYPES:
  "io"   → file I/O, backend lifecycle, vendor FS helpers
  "comm" → MPI wrappers, network I/O, distributed FS (S3/HDFS/RADOS/DFS)
  "mem"  → memcpy, large buffer alloc/free, mmap region setup
  "cpu"  → checksums, compression, encryption, hashing

RULE A — Annotate at every abstraction layer (wrapper AND callee).
RULE B — Simplest functions first (single-exit → nested → macros).
RULE D — Default is to annotate; skip requires Rule 0 justification.

══════════════════════════════════════════════════════
STEP 1 — READ THE FILE
══════════════════════════════════════════════════════

dftracer__session_read_file(run_id="{{ run_id }}", filepath="{{ filepath }}",
  subfolder="annotated")

Record the original line count — the written file must have MORE lines.

For C/C++ files — run this pre-scan before writing any macros:
```bash
WS=/workspaces/dftracer-agents/workspaces/{{ run_id }}
F=${WS}/annotated/{{ filepath }}

echo "=== Braceless early-exit lines (must add braces before inserting END) ==="
grep -n "if.*return\|if.*continue\|if.*break" "$F" | grep -v "{" | grep -v "//"

echo "=== Forward declarations (do NOT annotate these) ==="
grep -n "^[a-zA-Z_].*(.*);" "$F" | grep -v "^\s*//"
```
For every braceless line in the first check: add `{` / `}` around the body FIRST,
then annotate. Skipping this step caused build failures in the IOR session (POSIX_Create,
POSIX_Open, HDF5_GetFileSize, HDF5_Access — all had braceless dryRun returns).

══════════════════════════════════════════════════════
STEP 1.5 — PREPARE SYNTAX CHECK (runs once per file)
══════════════════════════════════════════════════════

Create a macro-stub header so syntax checks work even before dftracer is installed.
This is what enables the per-function incremental loop in STEP 4.

```bash
cat > /tmp/dftracer_stub.h << 'STUB'
#define DFTRACER_C_FUNCTION_START()
#define DFTRACER_C_FUNCTION_END()
#define DFTRACER_C_FUNCTION_UPDATE_STR(k,v)
#define DFTRACER_C_FUNCTION_UPDATE_INT(k,v)
#define DFTRACER_C_INIT(a,b,c)
#define DFTRACER_C_FINI()
#define DFTRACER_CPP_FUNCTION()
#define DFTRACER_CPP_FUNCTION_UPDATE(k,v)
#define DFTRACER_CPP_INIT(a,b,c)
#define DFTRACER_CPP_FINI()
#define DFTRACER_CPP_REGION_START(x) do{(void)(x);}while(0)
#define DFTRACER_CPP_REGION_END(x)   do{(void)(x);}while(0)
STUB
```

══════════════════════════════════════════════════════
PER-FUNCTION INCREMENTAL ANNOTATION LOOP
(governs how STEP 4 executes in every recipe)
══════════════════════════════════════════════════════

Annotate ONE function at a time. After each function:

  a. Write the file:
       dftracer__session_write_file(run_id="{{ run_id }}",
         filepath="{{ filepath }}", content=<full file text>,
         subfolder="annotated")

  b. Run the language-specific syntax check (see STEP 4 in this recipe).

  c. PASS → mark function as annotated, move to the next function.

  d. FAIL →
       i.   Identify the exact line and macro from the error message.
      ii.   Fix ONLY that macro — do not touch functions that already passed.
     iii.   Write the file and re-check (max 2 retries for this function).
      iv.   After 2 failed retries: strip macros from THIS function only,
            mark it PENDING with reason, continue to the next function.
       v.   Write the new pitfall to lessons-learned IMMEDIATELY (not at Step 7):
            ```bash
            SKILL=/workspaces/dftracer-agents/.agents/skills/dftracer-annotation-lessons/SKILL.md
            mkdir -p "$(dirname $SKILL)"
            cat >> "$SKILL" << 'ENDLESSON'

            ---
            date: <YYYY-MM-DD>
            file: {{ filepath }}
            context: <one-line description>
            error: |
              <exact compiler/runtime error>
            root_cause: <why the macro placement failed>
            fix: |
              <what resolved it or why the function was marked PENDING>
            tags: [<c|cpp|python>, annotation, <keyword>]
            ---
            ENDLESSON
            ```

RULES that must never be broken during the loop:
  • NEVER revert annotations from functions that already passed their check.
  • NEVER try to fix an error by reverting the whole file.
  • NEVER skip the syntax check step — it catches errors before the full build.
  • If BUILD ERROR MODE is active (build_errors param is set): process only the
    functions named in the errors first, using the same per-function loop,
    before continuing with any unannotated functions.


