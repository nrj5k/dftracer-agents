### Manual Annotation Process (gradual, iterative)

`session_annotate_source` returns a **plan** — it does NOT write files.
Annotate manually using `session_read_file` + `session_write_file`.

Work in three passes. After each pass, run `session_build_annotated` to catch
errors before proceeding. Fix errors before moving to the next pass.

**Pass 1 — INIT and FINI in main (entry files only)**

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
   - C (with MPI): `DFTRACER_C_FINI();` then `MPI_Finalize();` (if not already there),
     then `return` — order is FINI → MPI_Finalize → return
   - C++: `DFTRACER_CPP_REGION_END(main_region); DFTRACER_CPP_FINI();` before
     `MPI_Finalize()` if present
5. Write the file, build, fix errors.

**Pass 2 — FUNCTION_START and FUNCTION_END for selected C functions**

For each C file (start with small files, then larger ones):
1. Read the file.
2. For each function definition, apply **Rule 0** first — skip trivial functions
   (getters, setters, ≤5-line helpers, tight-loop utilities). Only annotate
   functions that do I/O, data movement, or take measurable time.
3. For each selected function:
   a. Insert `DFTRACER_C_FUNCTION_START();` as the FIRST statement after `{`
   b. Insert `DFTRACER_C_FUNCTION_END();` before EVERY `return` statement
   c. For void functions (no return): insert `DFTRACER_C_FUNCTION_END();` as the
      last statement before the closing `}`
   d. For main (already has FINI from Pass 1): insert `DFTRACER_C_FUNCTION_END();`
      immediately BEFORE each `DFTRACER_C_FINI();` call
4. Write the file, build, fix errors before moving to the next file.

**Pass 3 — FUNCTION_UPDATE for I/O metadata (non-blocking)**

For functions that have meaningful I/O parameters (filenames, sizes, offsets, fds):
1. After `DFTRACER_C_FUNCTION_START();`, add:
   - `DFTRACER_C_FUNCTION_UPDATE_STR("name", param)` for `const char *` params
     with names containing: file, path, name, dir, mode, cmd
   - `DFTRACER_C_FUNCTION_UPDATE_INT("name", (int)param)` for numeric params
     with names containing: size, count, len, offset, fd, flags, bytes
2. Skip functions where no parameters carry I/O meaning.
3. Build and fix any errors.

**Key safety rules for every pass:**

- Verify line count: annotated file must have MORE lines than original (never fewer)
- Work one file at a time — do not batch multiple files in one write
- After each file write, run `session_build_annotated` and resolve errors before continuing
- If a file keeps failing after two attempts, reset it to the unannotated original
  (`cp source/src/foo.c annotated/src/foo.c`) and move on — partial coverage is
  better than a broken build

**When uncertain, ask the user — then record the answer:**

If you encounter a pattern not covered by the rules above (ambiguous brace,
unusual function signature, preprocessor gymnastics, C11 generics, etc.) and
you are not confident your annotation will be correct:

1. **Stop and ask the user.** Describe the specific code pattern and your uncertainty.
   Example:
   ```
   I'm unsure how to annotate this K&R-style function in ior.c:
     void old_style(x, y)
     int x; int y;
     {
   Should I treat the `{` on its own line as the function body opener? (y/n)
   ```
2. **Apply the user's answer** to the current file.
3. **Immediately add a new entry to the Annotation Pitfalls section** in
   `.goose/hints/02-annotation-pitfalls.md` using the same lessons-learned format:
   ```
   ---
   context: <one-line description of the pattern>
   error: |
     <what would go wrong without this rule>
   root_cause: <why it was ambiguous>
   fix: |
     <the rule the user gave, phrased so future Goose can apply it without asking>
   tags: [c, annotation, <relevant keywords>]
   ```
   Then run `scripts/build-hints.sh` to regenerate `.goosehints`.
4. Continue annotation.

Never silently guess on an ambiguous pattern and move on — a wrong guess causes
a build error that is harder to diagnose than a one-line clarifying question.
