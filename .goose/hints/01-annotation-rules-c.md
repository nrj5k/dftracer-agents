## C Annotation Rules (dftracer)

### C Rule 1 — Include and one START per function, at the very top

Add the include after all existing `#include` lines in the .c file (never in a header):
```c
#include <dftracer/dftracer.h>
```

Then for each selected function:
```c
return_type function_name(params) {
  DFTRACER_C_FUNCTION_START();   // ← FIRST statement after {, nothing before it
  ...
}
```

- **One and only one** START per function body.
- START goes **after** the opening `{`, never before it.
- It must be the **first executable statement** — no variable declarations, no assignments before it.
- **Never** place START inside a control-flow block (`if`, `else`, `for`, `while`,
  `switch`, `do`). Control-flow blocks are NOT functions.
- **Never** annotate: struct/union/enum definitions, typedefs, macro bodies,
  `#ifdef` guard blocks, forward declarations, or function pointer declarations.

### C Rule 2 — DFTRACER_C_FUNCTION_END before every exit point

```c
return_type function_name(params) {
  DFTRACER_C_FUNCTION_START();
  ...
  if (error) {
    DFTRACER_C_FUNCTION_END();   // ← before early return
    return -1;
  }
  ...
  DFTRACER_C_FUNCTION_END();     // ← before normal return
  return 0;
}
```

- Place END **immediately before every `return` statement** (on its own line, same indent as return).
- For **void functions** that fall off the end (no explicit `return`), place END
  as the **last statement before the closing `}`**:
  ```c
  void helper(int x) {
    DFTRACER_C_FUNCTION_START();
    do_work(x);
    DFTRACER_C_FUNCTION_END();   // ← last statement, void function
  }
  ```
- Every function with a START **must** have a corresponding END at every exit path.
- Never place END after a `return` (it would be unreachable).
- END indentation must match the surrounding block — not at column 0.

### C Rule 3 — DFTRACER_C_FINI in main before every exit; order around MPI

**Without MPI** — INIT goes right after the opening `{` of main:

```c
int main(int argc, char **argv) {
  DFTRACER_C_INIT(NULL, NULL, NULL);   // ← first, before anything else
  DFTRACER_C_FUNCTION_START();
  ...
  DFTRACER_C_FUNCTION_END();
  DFTRACER_C_FINI();
  return 0;
}
```

**With MPI** — INIT must come AFTER `MPI_Init`/`MPI_Init_thread`, and FINI must
come BEFORE `MPI_Finalize`. dftracer uses MPI internals; initialising before MPI
is ready causes crashes, and finalising after MPI shuts down loses trace data:

```c
int main(int argc, char **argv) {
  MPI_Init(&argc, &argv);              // ← MPI first
  DFTRACER_C_INIT(NULL, NULL, NULL);   // ← dftracer after MPI_Init
  DFTRACER_C_FUNCTION_START();
  ...
  if (bad) {
    DFTRACER_C_FUNCTION_END();         // ← END first
    DFTRACER_C_FINI();                 // ← dftracer FINI before MPI_Finalize
    MPI_Finalize();                    // ← MPI last
    return 1;
  }
  ...
  DFTRACER_C_FUNCTION_END();
  DFTRACER_C_FINI();                   // ← dftracer FINI before MPI_Finalize
  MPI_Finalize();
  return 0;
}
```

- Scan main for `MPI_Init` or `MPI_Init_thread` before placing `DFTRACER_C_INIT`.
  If found, place INIT on the line immediately after the `MPI_Init(...)` call.
- Scan every `return` path and every `MPI_Finalize()` call in main. Place
  `DFTRACER_C_FUNCTION_END()` then `DFTRACER_C_FINI()` immediately **before**
  each `MPI_Finalize()`.
- The full order at every exit from an MPI main:
  `FUNCTION_END → DFTRACER_C_FINI → MPI_Finalize → return`
- Also place FINI before any **process-exit call** (`exit()`, `_exit()`, `abort()`,
  `quick_exit()`) that bypasses `MPI_Finalize`:
  ```c
  if (fatal) {
    DFTRACER_C_FINI();
    MPI_Abort(MPI_COMM_WORLD, 1);   // or exit(1) — FINI must precede both
  }
  ```
- `DFTRACER_C_FINI()` must appear **in the same function as `DFTRACER_C_INIT()` (i.e., main).
- INIT/FINI summary: `MPI_Init → DFTRACER_C_INIT → ... → DFTRACER_C_FINI → MPI_Finalize`

### C Rule 4 — Track important I/O metadata with FUNCTION_UPDATE

After FUNCTION_START, add UPDATE calls for parameters that carry meaningful I/O context.
**Always read parameter names from the function DEFINITION, not a forward declaration.**
Forward declarations often omit names (e.g. `int foo(int, char *);`) — the definition
has the real names.

```c
ssize_t my_read(const char *filename, void *buf, size_t count, off_t offset) {
  DFTRACER_C_FUNCTION_START();
  DFTRACER_C_FUNCTION_UPDATE_STR("filename", filename);   // string params
  DFTRACER_C_FUNCTION_UPDATE_INT("count",    (int)count); // numeric params
  DFTRACER_C_FUNCTION_UPDATE_INT("offset",   (int)offset);
  ...
  DFTRACER_C_FUNCTION_END();
  return result;
}
```

- **String params** (`const char *`): use `DFTRACER_C_FUNCTION_UPDATE_STR("name", ptr)`
  — good targets: `filename`, `path`, `name`, `dir`, `mode`, `cmd`
- **Numeric params** (`size_t`, `off_t`, `int`, `long`): use `DFTRACER_C_FUNCTION_UPDATE_INT("name", (int)val)`
  — good targets: `size`, `count`, `len`, `length`, `offset`, `fd`, `flags`, `bytes`, `access`
- The variable name in the UPDATE call **must exactly match** the parameter name in
  the function definition — otherwise you get `undeclared identifier` compile errors.
- Only add UPDATE for parameters that are **meaningful for I/O analysis**; skip generic
  loop counters or Boolean flags.

### C Quick checklist

- [ ] `#include <dftracer/dftracer.h>` added to .c files only (never headers)
- [ ] Only Rule-0-qualifying functions are annotated
- [ ] START is the first statement after `{` (not before `{`)
- [ ] START is in the function definition body, not a forward declaration
- [ ] Parameter names in UPDATE match the definition, not the declaration
- [ ] END placed before every `return` (same indentation level as return)
- [ ] Void functions: END placed as last statement before `}`
- [ ] END indentation is not at column 0 — matches surrounding code
- [ ] main: MPI_Init (if present) → DFTRACER_C_INIT → ... → DFTRACER_C_FINI → MPI_Finalize → return
- [ ] No macros inside control-flow blocks, struct definitions, or `#define` bodies
