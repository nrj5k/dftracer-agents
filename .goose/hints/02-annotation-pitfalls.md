### Annotation Pitfalls (lessons learned)

> **Quick navigation:** For the top-10 most common mistakes and critical rules,
> see `00-critical-cheatsheet.md` first. This file contains the full detail for
> every pitfall, organized in the order they were discovered.

These are real mistakes that caused build failures or missing trace data in actual
annotation sessions. Check pitfalls matching your current file type before writing.

**Standing instruction — update this file whenever something fails and gets fixed:**

When you encounter ANY error during annotation, build, or run that is not already
covered by an entry below, and you fix it, you MUST immediately append a new entry
to THIS file (`.goose/hints/02-annotation-pitfalls.md`) using the format:

```
---
context: <one-line description of what was being attempted>
error: |
  <exact error message or key excerpt>
root_cause: <why it happened>
fix: |
  <exact rule or steps that resolved it — written so future Goose can apply it
  without asking>
tags: [<language>, annotation, <error-keyword>, ...]
---
```

Do this **immediately after fixing** — not at the end of the session, not as an
optional step. The entry must be appended before continuing to the next file or step.
Because `.goosehints` loads this file via `@` reference, no rebuild is needed — the
new rule is active for the rest of the current session and all future sessions.

---
context: Annotating C source with fprintf multi-line strings
error: |
  aiori-MPIIO.c:241: error: missing terminating " character
  fprintf(stdout, "\nhints passed to MPI_File_open() {
    DFTRACER_C_FUNCTION_START();\n");
root_cause: A `{` character inside a string literal matched a function-header
  pattern. The macro was injected into the string content, breaking the string
  and producing unterminated-string errors and stray-backslash errors.
fix: |
  Before placing any macro after a `{`, verify the `{` is a REAL function body
  opener, not a character inside a string or format specifier.
  A real function body `{` must:
    1. Follow a closing `)` that ends a parameter list (possibly on previous line)
    2. Be the last non-whitespace/non-comment character on its line (or on its own line)
    3. NOT be preceded by `"` (i.e., not inside a string literal)
  If the line containing `{` also contains `"..."` or is part of a fprintf/printf
  argument, it is NOT a function body — skip it.
tags: [c, annotation, string-literal, fprintf, brace]

---
context: Macro placed inside a `#define` body or macro expansion
error: |
  error: 'data_fn' undeclared — DFTRACER_C_FUNCTION_END expanded inside a macro
  that has no surrounding DFTRACER_C_FUNCTION_START scope
root_cause: DFTRACER_C_FUNCTION_END uses `data_fn` which is defined by
  DFTRACER_C_FUNCTION_START. Placing END in a macro body or standalone helper
  without a matching START causes "undeclared" errors for `data_fn`.
fix: |
  Never annotate:
    - `#define` macro bodies (lines starting with `#define ...`)
    - Inline lambdas or function-like macros
    - Functions whose entire body is a single macro expansion
  Only annotate real C function definitions (have a return type, a name,
  `(params)`, and a `{...}` body visible in the source).
tags: [c, annotation, define, macro, data_fn]

---
context: Macro inserted inside a block comment `/* ... */`
error: |
  Compiler sees DFTRACER_C_FUNCTION_START() as part of a comment — no error
  but the function is never actually traced (silent miss).
root_cause: The annotation pattern matched text inside a `/* */` block comment.
fix: |
  Skip any line where the `{` appears after `/*` with no intervening `*/`.
  Track comment depth as you scan: if you are inside a block comment, do not
  annotate.
tags: [c, annotation, block-comment, silent-miss]

---
context: DFTRACER_C_FUNCTION_START placed inside a control-flow block
error: |
  Multiple START macros per function when the function contains `if(...) {`
  or `for(...) {` — the inner `{` matched as another function opener.
root_cause: Pattern matched `if (condition) {` or `for (init; cond; step) {`
  as a function header, inserting a second START inside the already-annotated
  function body.
fix: |
  A function-header `{` is ONLY valid when the token immediately before `(`
  is a plain identifier that is NOT a C/C++ keyword. Reject any match where
  the "function name" is: if, else, for, while, do, switch, case, return,
  sizeof, typeof, namespace, class, struct, union, enum, template, catch, try.
  Also reject when the line contains `else {` or `} else {`.
tags: [c, annotation, control-flow, double-start]

---
context: Forward declaration annotated as if it were a function body
error: |
  DFTRACER_C_FUNCTION_START injected after a semicolon-terminated prototype:
    int foo(int x);
    DFTRACER_C_FUNCTION_START();   ← wrong: foo is declared, not defined here
root_cause: Pattern matched `int foo(int x)` before the trailing `;` was checked.
fix: |
  A function DEFINITION must have `{` (not `;`) after the parameter list.
  Never annotate a line that ends with `;` — that is a declaration/prototype.
tags: [c, annotation, forward-declaration, prototype]

---
context: struct/union/enum definition annotated as function
error: |
  DFTRACER_C_FUNCTION_START() inserted after `struct IOR_param_t {`
root_cause: `struct Foo {` has the same surface syntax as `type name {`.
fix: |
  Reject any match where the return-type token is `struct`, `union`, `enum`,
  `typedef`, `class`, or `namespace`. These are type definitions, not functions.
tags: [c, annotation, struct, typedef, false-positive]

---
context: File was truncated when writing annotated version
error: |
  ior.c went from 1869 lines to 960 lines after session_write_file.
  Compiler reported "expected declaration or statement at end of input".
root_cause: session_write_file was called with a partial file content — only
  the first half of the annotated text was provided to the tool.
fix: |
  Always write the COMPLETE file. Read the original, build the full annotated
  content in memory, verify the line count is >= original line count, then
  write. Never split a file write across multiple tool calls. If the file is
  large, read it in sections but write the full content in one call.
tags: [c, annotation, truncation, write, session_write_file]

---
context: Annotating header files (.h / .hpp)
error: |
  DFTRACER_C_FUNCTION_START injected into inline function in a header included
  by many translation units — START/END mismatch when header included multiple
  times, or data_fn redeclared.
root_cause: Header files often contain inline functions or static inline
  functions. Annotating these causes the macro to expand in every TU that
  includes the header, leading to redeclaration or scope errors.
fix: |
  Do NOT annotate header files (.h, .hpp). Only annotate .c / .cpp / .cxx
  source files. Add `#include <dftracer/dftracer.h>` to the .c/.cpp file
  that includes the header, not to the header itself.
tags: [c, annotation, header, inline, redeclaration]

---
context: UPDATE macro uses parameter name from forward declaration, not definition
error: |
  aiori-POSIX.c:687:50: error: 'count' undeclared (first use in this function)
      DFTRACER_C_FUNCTION_UPDATE_INT("count", (int)count);
root_cause: The forward declaration `static IOR_offset_t POSIX_Xfer(int, aiori_fd_t *,
  IOR_size_t *, IOR_offset_t, IOR_offset_t, aiori_mod_opt_t *)` has no parameter
  names. The actual definition uses `length` and `offset`. UPDATE was written using
  a guessed name `count` that doesn't exist in the definition's scope.
fix: |
  Always locate the function DEFINITION (the one with a `{` body) to read the
  exact parameter names. The forward declaration prototype often omits names or
  uses different names than the definition. Search for the definition separately:
    grep -n "POSIX_Xfer" file.c   # find both — check which one has {
  Then write UPDATE using the names from the definition only.
tags: [c, annotation, update, parameter-name, forward-declaration, undeclared]

---
context: START macro placed before the opening { instead of after it
error: |
  DFTRACER_C_FUNCTION_START() appears on the line before `{`, outside the
  function body — the macro expands with no enclosing scope, causing
  `data_fn undeclared` or duplicate-START errors.
root_cause: Script-based annotation matched the function header line and inserted
  the macro before the `{` rather than on the first line inside the body.
fix: |
  START must go on the FIRST LINE AFTER the opening `{`, not before it.
  The correct pattern:
    void foo(int x) {
        DFTRACER_C_FUNCTION_START();   ← after {
        ...
  Never:
    void foo(int x)
    DFTRACER_C_FUNCTION_START();       ← before {
    {
tags: [c, annotation, start, brace, placement]

---
context: END macro at column 0 instead of indented to match surrounding code
error: |
  Indentation mismatch — `DFTRACER_C_FUNCTION_END();` appears at column 0
  while all surrounding code is indented by 4 spaces. Not a compile error
  but causes diff noise and confuses future readers.
root_cause: Shell `sed` or string-replace inserted the macro without inheriting
  the indentation of the surrounding lines.
fix: |
  END indentation must match the line it precedes (the `return` or `}`).
  Before writing, check the indentation of the target line and use the same
  leading whitespace for the END macro.
  Example: if `return 0;` is at 4-space indent, write:
      DFTRACER_C_FUNCTION_END();
      return 0;
tags: [c, annotation, indentation, end, whitespace]

---
context: Script-based annotation adds duplicate STARTs or misplaced ENDs
error: |
  Running annotation scripts iteratively (restore → annotate → check → fix → repeat)
  resulted in duplicate DFTRACER_C_FUNCTION_START() calls and END macros placed
  outside the correct function scope after multiple script runs on the same file.
root_cause: Each script pass operated on the already-annotated file from the
  previous pass, compounding insertions rather than starting clean.
fix: |
  Always restore from the original unannotated source before re-running any
  annotation script:
    cp source/src/foo.c annotated/src/foo.c
  Never run an annotation script on a file that already contains dftracer macros
  from a previous pass — always start from the clean original.
tags: [c, annotation, script, idempotency, duplicate-start]

---
context: END placed after return statement (unreachable code) in MPI-IO style functions
error: |
  aiori-MPIIO.c build attempt 3:
    error: 'data_fn' undeclared — DFTRACER_C_FUNCTION_END placed after a return
    statement or in a branch where DFTRACER_C_FUNCTION_START was never reached.
root_cause: MPI-IO functions use complex control flow: switch statements with
  per-case returns, error-handling blocks with early exits, and nested conditions.
  Automated placement scanned for `return` keywords but inserted END on the line
  AFTER the return instead of immediately BEFORE it, producing unreachable code.
  In other cases, a return inside a nested block was outside the annotation scope
  (function lacked START), so END referenced data_fn that did not exist.
fix: |
  For functions with complex control flow (switch/case, nested if/else, MPI error
  codes), manually trace EVERY exit path before placing END:
    1. List all return statements with their line numbers (grep -n "return" file.c)
    2. For each return, identify the enclosing function by scanning upward for the
       function header — confirm START exists in that function's body
    3. Place END on the line IMMEDIATELY BEFORE the return keyword, same indentation
    4. For switch/case patterns:
         case MPI_SUCCESS:
           DFTRACER_C_FUNCTION_END();   ← before break or return
           return result;
    5. After writing, verify: count of END == count of return statements within the
       annotated function (grep -c for both within the function range)
  If the function has more than 5 exit points or deeply nested control flow, annotate
  it last — after all simpler functions in the file are done and the build passes.
tags: [c, annotation, end, unreachable, return, mpiio, control-flow, switch]

---
context: Duplicate END macros from iterative annotation or deduplication failure
error: |
  grep shows two DFTRACER_C_FUNCTION_END() calls immediately before a single return
  statement. Compiler sees no error but trace data has double-END events.
root_cause: Two separate annotation passes (or two attempts to fix a missing END)
  both inserted an END before the same return without checking if one was already there.
fix: |
  Before inserting any END, grep the surrounding 3 lines above the return to check
  whether an END is already present:
    awk "NR>=$(( LINE-3 )) && NR<$LINE" file.c | grep -c "DFTRACER_C_FUNCTION_END"
  If the count is already 1, skip the insertion. Never add END without checking first.
  After completing annotation for a function, scan the whole function for duplicate
  adjacent ENDs:
    grep -n "DFTRACER_C_FUNCTION_END" file.c
  If any two ENDs appear on consecutive lines, remove the second one.
tags: [c, annotation, end, duplicate, deduplication]

---
context: DFTRACER_INIT=1 env var conflicts with explicit DFTRACER_C_INIT() in code
error: |
  Trace file created but is empty (0 bytes / 580 bytes with no events).
  DFTRACER_INIT=1 was set in the environment AND the annotated source calls
  DFTRACER_C_INIT(NULL, NULL, NULL) explicitly.
root_cause: When both are present, the library initializes twice. The second
  init call sees the tracer already running and silently resets internal state,
  producing an empty or corrupted trace file.
fix: |
  Rule: NEVER set DFTRACER_INIT=1 in the environment when the source already
  contains explicit DFTRACER_C_INIT() calls (Pass 1 annotation adds these).
  The `session_run_with_dftracer` tool sets DFTRACER_INIT=1 by default — override
  it by passing env_extra='{"DFTRACER_INIT": "0"}' if the code has manual INIT.
  Heuristic: if grep finds DFTRACER_C_INIT in annotated/, unset DFTRACER_INIT.
tags: [c, annotation, dftracer-init, env, empty-trace, conflict]

---
context: Autotools .deps missing separator error after Makefile patching
error: |
  .deps/IOR-aiori-DUMMY.Po:1: *** missing separator.  Stop.
  make: *** [Makefile:382: all-recursive] Error 1
root_cause: Autotools generates dependency files (.deps/*.Po) that reference the
  original object names. After patching the Makefile or running make clean in the
  wrong order, these files become corrupted or mismatched.
fix: |
  Always purge .deps before rebuilding after a Makefile patch:
    rm -rf .deps src/.deps && make clean && make -j4
  This is the FIRST thing to try when a build fails immediately with
  "missing separator" — it is almost always a stale .deps file, not a code error.
  Add this as step 1 of every re-build after patching.
tags: [c, build, autotools, deps, makefile, missing-separator]

---
context: dftracer.h not found and linker undefined references after Makefile patching
error: |
  fatal error: dftracer/dftracer.h: No such file or directory
  undefined reference to `initialize_main'
  undefined reference to `initialize_region'
  undefined reference to `finalize_region'
root_cause: The build system (Makefile / configure) does not know about the dftracer
  installation path. Compiler can't find the header; linker can't find the library.
fix: |
  Patch the build system after session_copy_annotated and BEFORE annotating any file:
  1. Add to CFLAGS/CXXFLAGS: -I<install_ann>/include
  2. Add to LDFLAGS: -L<install_ann>/lib -Wl,-rpath,<install_ann>/lib
  3. Add to LIBS: -ldftracer_core -lcpp-logger -lbrahma
  For autotools projects, patch src/Makefile (not just the top-level):
    sed -i 's|^CFLAGS =|CFLAGS = -I<install_ann>/include|' src/Makefile
    sed -i 's|^LIBS =|LIBS = -ldftracer_core -lcpp-logger -lbrahma|' src/Makefile
  Verify with: make -n 2>&1 | grep dftracer (should show the flags)
  Then run: rm -rf .deps src/.deps && make -j4
tags: [c, build, makefile, linker, dftracer-h, undefined-reference, autotools]

---
context: conflicting types for finalize_region from duplicate END macros in sequence
error: |
  error: conflicting types for 'finalize_region'
  note: previous declaration of 'finalize_region' was here
root_cause: Two DFTRACER_C_FUNCTION_END() macros on consecutive lines both expand
  to a `finalize_region(...)` declaration, causing a redeclaration conflict.
fix: |
  Same root cause as duplicate END — see "Duplicate END macros" pitfall entry.
  Specific detection: grep -n "DFTRACER_C_FUNCTION_END" file.c | awk -F: 'prev && $1-prev==1 {print "duplicate at lines " prev " and " $1} {prev=$1}'
  Remove the second END in any pair that appears on adjacent or near-adjacent lines.
tags: [c, annotation, end, duplicate, finalize-region, conflicting-types]

---
context: Smoke test binary not found (return code 127) — wrong path
error: |
  ./ior: not found (return code 127)
  /bin/sh: 1: ./ior: not found
root_cause: The command uses a relative path like `./ior` but the working directory
  is not the directory that contains the binary. Binary may be in build/src/, install/bin/,
  or annotated/src/ depending on the step.
fix: |
  Always use the FULL ABSOLUTE PATH to the binary in all tool calls:
    Bad:  command="./ior -a POSIX ..."
    Good: command="/workspaces/.../install/bin/ior -a POSIX ..."
  Find the binary first: find <workspace> -name "ior" -type f -not -path "*/source/*"
  The install path is recorded in session state — read it with session_list_files.
tags: [c, smoke-test, binary, path, 127, not-found]

---
context: Error-checking macro (MPI_CHECK, NCMPI_CHECK, H5EPRINT) hides early return
error: |
  After annotation, a function that uses NCMPI_CHECK() exits without hitting
  DFTRACER_C_FUNCTION_END — traces show START with no corresponding END,
  causing span imbalance. No compile error but incorrect trace data.
root_cause: Error-checking macros expand to `if (rc) { fprintf(stderr, ...); return; }`
  or similar. The `return` inside the macro expansion is invisible when reading
  the source — it looks like a normal function call but is an exit point.
fix: |
  Before annotating any function, grep for all error-checking macro calls:
    grep -n "MPI_CHECK\|NCMPI_CHECK\|H5_CHECK\|HGOTO_ERROR\|ERR_GOTO\|ERRNO_CHECK" fn.c
  Treat each one as a potential `return` and place END immediately before it.
  For libraries that use MPI_CHECK / NCMPI_CHECK, the pattern is:
    DFTRACER_C_FUNCTION_END();
    NCMPI_CHECK(ncmpi_create(...), "msg");   ← END before this
  Verify after annotation: count of END >= count of (return + CHECK_MACROS) in the function.
tags: [c, annotation, end, mpi-check, ncmpi-check, hidden-return, macro]

---
context: goto-based error handling (HDF5 style) — END before goto vs at label
error: |
  Placed END before each individual `goto done` statement, resulting in N
  duplicate END macros when a function has N goto-to-done jumps. Compiler
  reports "conflicting types for finalize_region" or duplicate scope errors.
root_cause: HDF5 and similar libraries use goto-based cleanup:
    if (err) goto done;
    ...
  done:
    H5Eclear2(...);
    return ret;
  Placing END before each `goto done` adds multiple ENDs for a single function exit.
fix: |
  Place a SINGLE END at the goto label, not before each goto:
    /* function body with multiple goto done */
    if (err) goto done;
    ...
  done:
    DFTRACER_C_FUNCTION_END();   ← ONE END here covers all goto paths
    H5Eclear2(...);
    return ret;
  This way, regardless of which path reaches `done:`, exactly one END fires.
  Exception: if the function has BOTH a normal return path AND a goto path that
  don't converge, place END before each separately.
tags: [c, annotation, end, goto, hdf5, label, cleanup]

---
context: Multi-line function signature not matched during annotation
error: |
  MPIIO_Xfer annotation skipped / START placed inside wrong line when function
  signature spans two or more source lines:
    static IOR_offset_t MPIIO_Xfer(int access, aiori_fd_t *file,
                                   IOR_size_t *buffer, IOR_offset_t length,
                                   IOR_offset_t offset, aiori_mod_opt_t *param)
    {
root_cause: Pattern matching on a single line can't detect that the `{` opening
  the function body belongs to a signature that started 2-3 lines earlier.
fix: |
  Treat ANY `{` that appears on its own line (only whitespace before it) as a
  potential function-body opener. Look UPWARD from that `{` to find the function
  signature — scan back until you find a line ending with `)` or containing `)`.
  The parameter names for UPDATE must come from the FULL multi-line signature.
  For MPIIO_Xfer specifically:
    static IOR_offset_t MPIIO_Xfer(int access, aiori_fd_t *file,
                                   IOR_size_t *buffer, IOR_offset_t length,
                                   IOR_offset_t offset, aiori_mod_opt_t *param)
    {
      DFTRACER_C_FUNCTION_START();                         // ← after the {
      DFTRACER_C_FUNCTION_UPDATE_INT("access", (int)access);
      DFTRACER_C_FUNCTION_UPDATE_INT("length", (int)length);
      DFTRACER_C_FUNCTION_UPDATE_INT("offset", (int)offset);
tags: [c, annotation, multi-line-signature, start, mpiio, xfer]

---
context: MPI-IO backend (aiori-MPIIO.c style) annotation fails and must be reset
error: |
  Build attempt with annotated aiori-MPIIO.c:
    error: 'data_fn' undeclared (first use in this function)
  File reset to unannotated (include-only) to preserve the build.
root_cause: MPI-IO backends combine: (1) complex switch/case dispatch, (2) MPI
  return code checks as early exits, (3) C99 variable-length goto chains, and
  (4) callback function pointers. These patterns make it extremely easy to place
  START/END in the wrong scope when annotating non-interactively.
fix: |
  Annotate MPI-IO style backends LAST and one function at a time:
    1. Pick the simplest function first (MPIIO_Close, MPIIO_Delete).
    2. Read the entire function body before writing a single macro.
    3. Count returns: grep -c "return" <function_lines>
    4. Write START, then add END×N (one before each return), then build immediately.
    5. Only proceed to the next function after the build passes.
  If the file fails after 2 separate single-function attempts, add only the
  `#include <dftracer/dftracer.h>` line (so the file compiles cleanly) and
  mark it as "include-only" in the annotation_summary.md — partial coverage is
  better than a broken build.
tags: [c, annotation, mpiio, mpi, switch, reset, include-only]

---
context: Annotating POSIX_Xfer — forward declaration and definition have same name
error: |
  Goose annotated the forward declaration at the top of the file (line ~115) instead
  of the real function definition (line ~735). The forward declaration ends with `;`
  and has no parameter names. Placing START there causes "expected ';' before..."
  errors because a declaration has no body.
root_cause: |
  C source files often have forward declarations near the top so internal functions
  can be called before their definitions. The declaration looks like a function
  signature but ends with `;` and has no `{...}` body. Annotating it is wrong.
fix: |
  Before annotating ANY function, search for ALL occurrences of its name in the file:
    grep -n "POSIX_Xfer\|FUNCTION_NAME" file.c
  If you find TWO hits:
  - The hit with no parameter names and a trailing `;` → FORWARD DECLARATION — DO NOT ANNOTATE
  - The hit with named parameters and a following `{` body → DEFINITION — annotate this one
  The definition always has:
    1. Named parameters (e.g., `int access, aiori_fd_t *file, ...`)
    2. A `{` on the same or next line (NOT a `;`)
    3. Appears lower in the file, usually with a comment block above it
  Quick check: `grep -n "FUNCTION_NAME" file.c | grep -v ";$"` shows only definitions.
tags: [c, annotation, forward-declaration, posix, duplicate-signature]

---
context: Missing annotation for vendor-specific filesystem helper functions (gpfs_*, beegfs_*, lustre_*)
error: |
  Coverage check after annotating aiori-POSIX.c showed functions
  gpfs_free_all_locks, gpfs_access_start, gpfs_access_end,
  gpfs_fineGrainWriteSharing, gpfs_fineGrainReadSharing,
  beegfs_getStriping, beegfs_compatibleFileExists, beegfs_createFilePath,
  lustre_disable_file_locks were unannotated.
root_cause: |
  These functions have vendor-specific prefixes (gpfs_, beegfs_, lustre_) and
  are not in the named list of "POSIX_*" functions. Goose stopped at POSIX_*
  without scanning for other function definitions in the same file.
fix: |
  After annotating the named backend functions (POSIX_Create, POSIX_Xfer, etc.)
  always scan the ENTIRE file for unannotated definitions:
    grep -n "^[a-zA-Z].*(.*)$\|^static.*(.*)$" file.c | grep -v ";" | grep -v "DFTRACER"
  Any definition that calls gpfs_fcntl(), beegfs_*(), or filesystem ioctls is an
  I/O operation and must be annotated with comp="io". The unfamiliar prefix does
  not mean it is trivial — it controls filesystem behavior at the kernel level.
  Rule: if a function calls filesystem-specific APIs (gpfs_fcntl, ioctl,
  beegfs_getStripeInfo, llapi_*) it is an I/O function regardless of its prefix.
tags: [c, annotation, coverage, gpfs, beegfs, lustre, vendor-filesystem]

---
context: Missing POSIX_Initialize and POSIX_Finalize because they are short
error: |
  After annotating the main I/O functions in aiori-POSIX.c, POSIX_Initialize
  and POSIX_Finalize were not annotated because their bodies are short
  (only a HAVE_GPU_DIRECT ifdef block).
root_cause: |
  Short body length was mistakenly treated as a skip condition (Rule 0). But
  lifecycle functions are ALWAYS annotated regardless of body size — they mark
  backend entry and exit points, which is critical for trace correlation.
fix: |
  Backend lifecycle functions — *_Initialize, *_Finalize, *_init, *_final,
  *_open_backend, *_close_backend — are ALWAYS annotated even if their body
  appears to be empty or only contains #ifdef guards. The START/END pair records
  the lifecycle event in the trace. Never apply Rule 0 to lifecycle functions.
tags: [c, annotation, lifecycle, initialize, finalize, rule0-exception]

---
context: Missing POSIX_Sync and POSIX_Rename because they are not "read/write" functions
error: |
  Coverage check found POSIX_Sync and POSIX_Rename not annotated. They were
  not in the initial pass because they are not data-transfer functions.
root_cause: |
  Annotation focused on data-path functions (Create/Open/Xfer/Close) and missed
  the sync/flush and rename/metadata functions. These are file system operations
  that directly affect data durability and file organization.
fix: |
  The mandatory "always annotate" list for any POSIX-style backend is:
    Create, Open, Xfer, Close, Delete, Fsync, Sync, Rename, GetFileSize, Mknod,
    Initialize, Finalize
  Sync/flush functions (Sync, Fsync) → comp="io" — they trigger kernel writebacks.
  Rename/metadata functions (Rename, Mknod, GetFileSize) → comp="io" — file metadata ops.
  Run `grep -n "^[a-zA-Z_]" aiori-POSIX.c | grep -v ";"` to get the full function list
  and verify every backend-level function is covered before marking the file DONE.
tags: [c, annotation, posix, sync, rename, coverage, metadata]

---
context: Skipping POSIX_Mknod because documentation calls it "deprecated"
error: |
  POSIX_Mknod was left unannotated because comments or documentation described
  it as deprecated or not used in the default test. Coverage check showed a gap.
root_cause: |
  "Deprecated" or "not called in the current test" does not mean a function has
  no body or is trivial. POSIX_Mknod calls the mknod() syscall — a real filesystem
  operation. It qualifies as an I/O function under Rule 0.
fix: |
  Never skip a function solely because:
    - It is described as "deprecated"
    - It is not called in the basic smoke test
    - Its name suggests it might be unused
  Only skip under Rule 0 conditions (pure getter/setter ≤5 lines, no I/O).
  If the function has a syscall or filesystem operation in its body, annotate it.
  "Deprecated" annotations only affect the caller's code, not the callee's coverage.
tags: [c, annotation, posix, mknod, deprecated, rule0]

---
context: Completing annotation pass without adding comp=TYPE to any function
error: |
  After a full annotation session, grep for comp= updates finds zero results:
    grep -c 'DFTRACER_C_FUNCTION_UPDATE_STR.*comp' annotated/src/aiori-POSIX.c
    → 0
  But grep for START finds 21. Every function was traced but none is classified.
root_cause: |
  The comp=TYPE rule (C Rule 4) was added AFTER the annotation session started,
  or was missed during Pass 2. When working function-by-function under time pressure
  it is easy to add START+END without the mandatory UPDATE_STR("comp", ...).
fix: |
  After annotating a file, always run the two-count check BEFORE moving to the next file:
    grep -c "DFTRACER_C_FUNCTION_START" file.c
    grep -c 'DFTRACER_C_FUNCTION_UPDATE_STR.*comp' file.c
  If the numbers differ, find each START without a matching comp= by looking at the
  lines immediately following every DFTRACER_C_FUNCTION_START occurrence:
    grep -A2 "DFTRACER_C_FUNCTION_START" file.c | grep -v "comp"
  Add the missing UPDATE_STR("comp", "<type>") immediately after each bare START.
  This must be done before Pass 3 — retrofitting it after UPDATE metadata calls are
  in place is harder because you need to reorder lines.
tags: [c, annotation, comp, classification, missing-comp, rule4]

---
context: Vendor-specific functions (gpfs_*, beegfs_*, lustre_*) not appearing in trace
error: |
  After annotating gpfs_access_start, beegfs_getStriping, lustre_disable_file_locks
  etc., they do NOT appear in trace output from the smoke test.
root_cause: |
  These functions are guarded by #ifdef HAVE_GPFS_FCNTL_H, #ifdef HAVE_BEEGFS_BEEGFS_H,
  etc. They are also only called when the program runs on those specific filesystems.
  In a standard smoke test on a local POSIX fs, none of them will be invoked.
fix: |
  This is EXPECTED and CORRECT — the annotation is still valuable. The functions
  will appear in traces when:
  1. The code is compiled with the appropriate #define (HAVE_GPFS_FCNTL_H, etc.)
  2. The test runs on the corresponding filesystem (GPFS mount, BeeGFS, Lustre)
  3. The runtime options that trigger those code paths are enabled
  Do NOT conclude the annotation is broken because the functions don't appear in
  the basic smoke test. Report these as "annotated, not traced (filesystem not
  available in test environment)" in the coverage table.
tags: [c, annotation, gpfs, beegfs, lustre, trace, missing-in-trace, ifdef]

---
