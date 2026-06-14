### Manual Annotation Process (gradual, iterative)

`session_annotate_source` returns a **plan** — it does NOT write files.
Annotate manually using `session_read_file` + `session_write_file`.

---

## Coverage Requirement

**All storage backend functions MUST be annotated. Skipping due to difficulty is not acceptable.**

A function may only be permanently skipped if it qualifies under **Rule 0**
(trivial — getter/setter, ≤5 lines, no I/O, no data movement). Every other
function, regardless of complexity, MUST eventually be annotated. If the first
attempt fails, fix the error and retry — do not move on and leave it skipped.

**Storage backends that always require full annotation** (every I/O function):
- `aiori-POSIX.c` — POSIX_Create, POSIX_Open, POSIX_Xfer, POSIX_Close, POSIX_Delete,
  POSIX_Rename, POSIX_Fsync, POSIX_Sync, POSIX_GetFileSize, POSIX_Mknod
- `aiori-MPIIO.c` — MPIIO_Open, MPIIO_Xfer, MPIIO_Close, MPIIO_Delete, MPIIO_Fsync,
  MPIIO_GetFileSize
- `aiori-HDF5.c`, `aiori-NCMPI.c`, `aiori-DFS.c`, `aiori-S3*.c` — all I/O functions
- Any other `aiori-*.c` file — treat every non-trivial function as mandatory
- **Lifecycle functions always included:** `*_init`, `*_final`, `*_initialize`, `*_finalize`,
  `*_Sync`, `*_Fsync`, `*_Delete`, `*_Rename` — even if the body is short

**Mandatory POSIX backend function list** (annotate every one that has a real body):
- `POSIX_Create`, `POSIX_Open`, `POSIX_Xfer`, `POSIX_Close`, `POSIX_Delete`
- `POSIX_Fsync`, `POSIX_Sync`, `POSIX_GetFileSize`, `POSIX_Mknod` (even if "deprecated")
- `POSIX_Rename`, `POSIX_Initialize`, `POSIX_Finalize`
- `gpfs_free_all_locks`, `gpfs_access_start`, `gpfs_access_end`, `gpfs_fineGrainWriteSharing`, `gpfs_fineGrainReadSharing`
- `beegfs_getStriping`, `beegfs_compatibleFileExists`, `beegfs_createFilePath`, `beegfs_isOptionSet`
- `lustre_disable_file_locks`

**Note:** `gpfs_*`, `beegfs_*`, and `lustre_*` functions are guarded by `#ifdef` and
are only invoked on those specific filesystems. If they don't appear in smoke-test
traces, that is expected — record as "annotated, not traced (filesystem unavailable)"
in the coverage table. This is NOT a sign of broken annotation.

**Backend annotation order** (from General Rule C):
1. POSIX / simplest reference backend
2. Wrapper backends that delegate to POSIX (MMAP, AIO)
3. MPI-based backends (MPIIO, NCMPI)
4. High-level library backends (HDF5, NetCDF)
5. Distributed / cloud (HDFS, DFS, S3, CEPHFS, RADOS)
6. Specialized / research backends last

Annotate and smoke-test each backend **before moving to the next**.
When a wrapper backend (e.g. MMAP) calls a lower-level annotated function (e.g. POSIX_Create),
both will appear in traces — this is correct and expected (double-tracing shows call hierarchy).

For complex functions with multiple returns or nested control flow (e.g. MPIIO_Xfer,
MPIIO_Open), work through them one exit path at a time. Before placing any macro, grep
for all error-checking macros (MPI_CHECK, NCMPI_CHECK, HGOTO_ERROR) — they are hidden
exit points. See pitfall entries for details.

---

## annotation_logs/ Folder (mandatory — create before Pass 1)

Create this folder at the start of annotation and maintain it throughout:

```
workspaces/<run_id>/annotation_logs/
  annotation_process.log   ← running log of every action taken (append-only)
  annotation_status.md     ← per-function status table (update after every function)
  annotation_report.md     ← final summary (written after all passes complete)
```

### annotation_process.log format (append a line after every action)

```
[PASS1] src/ior.c — added INIT/FINI/START/END — build: SUCCESS
[PASS2] src/aiori-POSIX.c — POSIX_Create — START+END×2+UPDATE×2 — build: SUCCESS
[PASS2] src/aiori-MPIIO.c — MPIIO_Xfer — attempt 1 FAILED: data_fn undeclared
[PASS2] src/aiori-MPIIO.c — MPIIO_Xfer — attempt 2 — fixed END order — build: SUCCESS
[SKIP]  src/aiori-POSIX.c — POSIX_options — Rule 0: trivial, no I/O
[ERROR] src/aiori-MPIIO.c — MPIIO_Open — FAILED 2× — kept include-only — PENDING RETRY
```

### annotation_status.md format (update after every function)

```markdown
# Annotation Status — <run_id>

## Storage Backends (mandatory coverage)

| File | Function | Status | comp | Traced in Test | Attempts | Notes |
|------|----------|--------|------|----------------|----------|-------|
| aiori-POSIX.c | POSIX_Create | ✅ DONE | io | ✅ 2 events | 1 | START+END×2+UPDATE(filename,flags) |
| aiori-POSIX.c | POSIX_Xfer | ✅ DONE | io | ✅ 5 events | 2 | 5 returns — fixed END order attempt 2 |
| aiori-POSIX.c | POSIX_Sync | ✅ DONE | io | ⚠️ not called | 1 | Requires --sync flag |
| aiori-POSIX.c | gpfs_access_start | ✅ DONE | io | ⚠️ no GPFS | 1 | #ifdef guarded; traced on GPFS only |
| aiori-MPIIO.c | MPIIO_Open | ⚠️ PENDING | — | — | 2 | include-only; complex control flow |
| aiori-MPIIO.c | MPIIO_Xfer | ⚠️ PENDING | — | — | 1 | multi-line sig; needs retry |

## Entry Points

| File | Function | Status | Notes |
|------|----------|--------|-------|
| ior-main.c | main | ✅ DONE | INIT+FINI+START+END |
| ior.c | ior_main | ✅ DONE | MPI: INIT after MPI_Init |

## Skipped (Rule 0 — permanent)

| File | Function | Reason |
|------|----------|--------|
| aiori-POSIX.c | POSIX_options | Trivial option registration helper, no I/O |
| utilities.c | get_time_string | ≤5-line string formatter, no data movement |

## Coverage Summary

| Category | Total | Annotated | Annotated % | Traced in Test | Notes |
|----------|-------|-----------|-------------|----------------|-------|
| Core I/O | 9 | 9 | 100% | 6 | Fsync/Sync/Rename not in basic test |
| Lifecycle | 2 | 2 | 100% | 2 | Init/Finalize always traced |
| GPFS | 5 | 5 | 100% | 0 | #ifdef; GPFS filesystem not available |
| BeeGFS | 4 | 4 | 100% | 0 | #ifdef; BeeGFS not available |
| Lustre | 1 | 1 | 100% | 0 | #ifdef; Lustre not available |
```

**The `comp` column must be filled for every ✅ DONE row** — a function with no comp
is incomplete annotation. If you find DONE rows with `—` in comp, add the
`DFTRACER_C_FUNCTION_UPDATE_STR("comp", "<type>")` line before moving on.

"Traced in Test" tracks whether the function appeared in the smoke test output.
`⚠️ not called` and `⚠️ no GPFS` are acceptable — the annotation is still correct
and will produce traces when those code paths are exercised.

Status values:
- `✅ DONE` — annotated and build passes
- `⚠️ PENDING` — not yet annotated or annotation failed; MUST be retried
- `❌ INCLUDE-ONLY` — only `#include` added; annotation failed and was reset; MUST retry in next pass
- `⏭️ SKIPPED (Rule 0)` — permanently exempt; no retry needed

**PENDING and INCLUDE-ONLY functions are unfinished work, not completed work.**

---

## Build Prep — BEFORE Pass 1 (Makefile patching)

Before annotating any file, patch the build system so `#include <dftracer/dftracer.h>`
compiles without error. Do this immediately after `session_copy_annotated`.

For autotools projects (Makefile-based):
```bash
# 1. Find install_ann path (recorded in session state)
INSTALL_ANN=<workspace>/install_ann

# 2. Patch src/Makefile (not just the top-level)
sed -i "s|^CFLAGS = |CFLAGS = -I${INSTALL_ANN}/include |" annotated/src/Makefile
sed -i "s|^LIBS = |LIBS = -ldftracer_core -lcpp-logger -lbrahma |" annotated/src/Makefile
sed -i "s|^LDFLAGS = |LDFLAGS = -L${INSTALL_ANN}/lib -Wl,-rpath,${INSTALL_ANN}/lib |" annotated/src/Makefile

# 3. Clean stale dependency files — ALWAYS after a Makefile patch
rm -rf annotated/.deps annotated/src/.deps && make -C annotated clean
```

Verify the patch works by adding ONLY the include to one file and building:
```bash
# Quick sanity check — add include to ior-main.c, build, confirm no header errors
```

---

## Pass 1 — INIT and FINI in main (entry files only)

For each file where `is_entry: true`:
1. Read the file with `session_read_file`
2. Add `#include <dftracer/dftracer.h>` after the last existing `#include`
3. Check whether `main()` calls `MPI_Init` or `MPI_Init_thread`:
   - **No MPI**: place `DFTRACER_C_INIT(NULL, NULL, NULL);` as the first statement
     after the opening `{` of main
   - **Has MPI**: place `DFTRACER_C_INIT(NULL, NULL, NULL);` on the line immediately
     **after** the `MPI_Init(...)` or `MPI_Init_thread(...)` call
   - C++: same logic but use `DFTRACER_CPP_INIT(nullptr, nullptr, nullptr);` then
     `DFTRACER_CPP_REGION_START(main_region);`
4. Before every `return` in main AND before any `exit()`/`abort()`/`MPI_Abort()` call:
   - C (no MPI): `DFTRACER_C_FINI();` (FUNCTION_END comes in Pass 2)
   - C (with MPI): `DFTRACER_C_FINI();` then `MPI_Finalize();` then `return`
   - C++: `DFTRACER_CPP_REGION_END(main_region); DFTRACER_CPP_FINI();`
5. **If code has explicit DFTRACER_C_INIT(), do NOT set DFTRACER_INIT=1 in the env**
   when running with dftracer. They conflict and produce an empty trace file.
6. Write the file, build, fix errors, log to `annotation_process.log`.

---

## Pass 2 — FUNCTION_START and FUNCTION_END

For each backend file, work through ALL I/O functions — start with the simplest
(Fsync, Close, Delete) and work up to the most complex (Xfer, Open):

1. Read the file.
2. For each function definition:
   - **Rule D**: skip only pure getters/setters/string-formatters ≤5 lines with no I/O — log as `⏭️ SKIPPED (Rule 0)`
   - For storage backend files (`aiori-*.c`): all named functions qualify; complexity is not a skip reason
3. For each function, determine its `comp` type BEFORE writing any macro:
   - `"io"`: open/read/write/close/create/delete/fsync/stat/rename/getfilesize/lifecycle
   - `"mem"`: memcpy-heavy, mmap region setup, large buffer management
   - `"cpu"`: checksums, compression, data encoding/transformation
   - `"comm"`: MPI_File_*, network I/O, REST API, distributed FS operations
4. Annotate the function:
   a. Count VISIBLE `return` statements: `grep -n "return" <function_lines>`
   b. Identify multi-line signatures by scanning upward from `{`
   c. Insert `DFTRACER_C_FUNCTION_START();` as FIRST statement after `{`
   d. Insert `DFTRACER_C_FUNCTION_UPDATE_STR("comp", "<type>");` immediately after START
   e. Insert `DFTRACER_C_FUNCTION_END();` before every VISIBLE `return` (Rule E: not before error macros)
   f. For goto-based error handling: single END at the goto label
   g. For void functions: END as last statement before `}`
5. Write the file, run `rm -rf .deps src/.deps` first if the Makefile was touched,
   then build. Fix errors before the next function.
6. **On failure**: do NOT reset and skip. Instead:
   - Read the exact error (line number + message)
   - Identify which END or START caused the problem
   - Fix the specific macro and rebuild
   - Only after 3 separate fix attempts on the same function should you reset that
     function to include-only and mark it `❌ INCLUDE-ONLY` (PENDING retry)
7. Log every attempt to `annotation_process.log`.
8. Update `annotation_status.md` (including `comp` column) after every function.
9. **After every file** — run the two-count verification before moving to the next file:
   ```bash
   # Counts must be equal — mismatch means some functions are missing comp=TYPE
   grep -c "DFTRACER_C_FUNCTION_START"               annotated/src/foo.c
   grep -c 'DFTRACER_C_FUNCTION_UPDATE_STR.*"comp"'  annotated/src/foo.c

   # List all annotated functions — review for coverage gaps
   awk '/^[a-zA-Z].*\(/ {func=$0} /DFTRACER_C_FUNCTION_START/ {print NR": "func}' \
       annotated/src/foo.c
   ```

---

## Pass 3 — FUNCTION_UPDATE for I/O metadata

For functions that have meaningful I/O parameters (filenames, sizes, offsets, fds):
1. After `DFTRACER_C_FUNCTION_START();`, add:
   - `DFTRACER_C_FUNCTION_UPDATE_STR("name", param)` for `const char *` params
     with names containing: file, path, name, dir, mode, cmd
   - `DFTRACER_C_FUNCTION_UPDATE_INT("name", (int)param)` for numeric params
     with names containing: size, count, len, length, offset, fd, flags, bytes, access
   - Always read parameter names from the **definition**, not a forward declaration
2. Build and fix after every file.
3. Log to `annotation_process.log`.

---

## Pass 4 — Retry PENDING / INCLUDE-ONLY functions

After Pass 3 builds cleanly, go back to every function marked `⚠️ PENDING` or
`❌ INCLUDE-ONLY` in `annotation_status.md` and retry:

1. Read the function carefully — treat it as a fresh annotation, not a continuation
2. For complex control flow (switch/case, nested MPI error checks, multi-return):
   - List every exit path on paper before writing any macro
   - For `switch` statements: place END before each `case`'s `break` or `return`
   - For nested `if/else` with returns: trace the deepest nesting level first
3. Build after each retry
4. If it passes: update status to `✅ DONE`
5. If it fails again: ask the user for guidance, then add a pitfall entry

**No function should remain PENDING at the end of the session unless the user
explicitly approves leaving it incomplete.**

---

## Key safety rules for every pass

- Verify line count: annotated file must have MORE lines than original (never fewer)
- Work one function at a time — build after every function in complex files
- After Makefile changes: always `rm -rf .deps src/.deps` before rebuilding
- Never move to the next function while the current file has compile errors
- Update `annotation_status.md` after every function — do not batch updates

---

## When uncertain, ask the user — then record the answer

If you encounter a pattern not covered by the rules above (ambiguous brace,
unusual function signature, preprocessor gymnastics, K&R-style params, etc.)
and you are not confident:

1. **Stop and ask the user.** Describe the specific code pattern.
2. **Apply the user's answer** to the current file.
3. **Immediately add a new entry to `02-annotation-pitfalls.md`** using the format:
   ```
   ---
   context: <one-line description>
   error: |
     <what would go wrong>
   root_cause: <why it was ambiguous>
   fix: |
     <the rule, phrased for future Goose>
   tags: [c, annotation, <keywords>]
   ```
4. Continue annotation.

Never silently guess on an ambiguous pattern — a wrong guess causes a build
error that is harder to diagnose than a one-line clarifying question.

---

## After all passes — mandatory artifacts

Once the build passes and dftracer run succeeds, write to `workspaces/<run_id>/`:

### 1. `annotation.patch`

```bash
diff -urN source/ annotated/ > workspaces/<run_id>/annotation.patch
```

### 2. `annotation_logs/annotation_report.md`

Final report with:
- Total files modified, lines added, macros inserted
- Per-pass build history (attempt number, files, status, first error if any)
- Table of annotated functions (file, function, macros, notes)
- Table of skipped functions (file, function, reason — Rule 0 only)
- Table of INCLUDE-ONLY functions (file, function, reason, retry plan)
- List of errors encountered and fixes applied
- Pending work (functions that could not be completed this session)

**Both files must exist before reporting the pipeline as complete.**
