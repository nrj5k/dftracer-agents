### Annotation Pitfalls (lessons learned)

These are real mistakes that caused build failures. Check each one before writing any annotated file.

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
