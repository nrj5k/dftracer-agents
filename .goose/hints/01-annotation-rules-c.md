## C Annotation Rules (dftracer)

> **Before reading the detail rules below, check `00-critical-cheatsheet.md`** for
> the five critical rules (C1–C5), seven corner cases (CC1–CC7), and the top-10
> known mistakes (M1–M10). The rules below provide the full explanation and examples.

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

### C Rule 4 — Classify every annotated function with comp=TYPE

Every annotated function MUST include a `comp` classification UPDATE immediately
after `DFTRACER_C_FUNCTION_START()`. This tags functions in the trace so analysis
tools can group and filter by operation type.

**The four types:**

| Type | When to use | Typical functions |
|------|------------|-------------------|
| `"io"` | File system I/O: open, read, write, close, fsync, stat, delete, rename, getfilesize, mknod | POSIX_Create, POSIX_Xfer, POSIX_Fsync, POSIX_Close, POSIX_Delete, HDF5_Xfer, MMAP_Open |
| `"mem"` | Memory operations: large memcpy, mmap region setup, buffer alloc/free, data copy between memory regions | MMAP_Xfer (memcpy path), any malloc/free of large buffers |
| `"cpu"` | Compute: checksums, compression, encryption, hash, data encoding, format conversion | Any function that transforms data without doing file I/O |
| `"comm"` | Communication: MPI sends/recvs, network I/O, REST API calls, distributed FS ops | MPIIO_Xfer, NCMPI_Xfer, HDFS_Xfer, S3_Xfer, RADOS ops, DFS ops, any MPI_File_* wrapper |

**Usage — always the first UPDATE after START:**

```c
// File I/O function
aiori_fd_t *POSIX_Create(char *testFileName, int flags, aiori_mod_opt_t *param)
{
  DFTRACER_C_FUNCTION_START();
  DFTRACER_C_FUNCTION_UPDATE_STR("comp", "io");            // ← type first
  DFTRACER_C_FUNCTION_UPDATE_STR("filename", testFileName); // then params
  DFTRACER_C_FUNCTION_UPDATE_INT("flags", flags);
  ...
}

// MPI communication function
IOR_offset_t MPIIO_Xfer(int access, aiori_fd_t *fd, IOR_size_t *buf,
                        IOR_offset_t length, IOR_offset_t offset, ...)
{
  DFTRACER_C_FUNCTION_START();
  DFTRACER_C_FUNCTION_UPDATE_STR("comp", "comm");          // ← MPI = comm
  DFTRACER_C_FUNCTION_UPDATE_INT("access", (int)access);
  DFTRACER_C_FUNCTION_UPDATE_INT("length", (int)length);
  DFTRACER_C_FUNCTION_UPDATE_INT("offset", (int)offset);
  ...
}

// Memory-mapped transfer
IOR_offset_t MMAP_Xfer(int access, aiori_fd_t *fd, IOR_size_t *buf, ...)
{
  DFTRACER_C_FUNCTION_START();
  DFTRACER_C_FUNCTION_UPDATE_STR("comp", "mem");           // ← memcpy = mem
  DFTRACER_C_FUNCTION_UPDATE_INT("access", (int)access);
  ...
}

// Backend lifecycle (maps to io — it's part of the I/O stack setup)
void POSIX_initialize(aiori_mod_opt_t *options)
{
  DFTRACER_C_FUNCTION_START();
  DFTRACER_C_FUNCTION_UPDATE_STR("comp", "io");
  ...
}
```

**Classification guide for common patterns:**
- `Create`, `Open`, `Close`, `Fsync`, `Sync`, `Delete`, `Rename`, `GetFileSize`, `Mknod` → `"io"`
- `Xfer` in POSIX, MMAP, HDF5, NCMPI where data goes to/from a file → `"io"`
- `Xfer` in MPIIO, S3, HDFS, RADOS, DFS, CEPHFS where a network/RPC call is made → `"comm"`
- `Xfer` in MMAP where the transfer is a `memcpy` into the mmap region → `"mem"`
- `init`, `final`, `initialize`, `finalize` for any backend → `"io"` (I/O stack lifecycle)
- Functions doing checksums, compression, encryption, hashing → `"cpu"`

When a function spans multiple types (e.g., reads from file then checksums):
use the **primary** type — the one that dominates wall time.

### C Rule 5 — Track important I/O metadata with FUNCTION_UPDATE (after comp=TYPE)

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
- **Opaque handle typedefs** (`MPI_File`, `hid_t`, `ncid`, `hsize_t`, `herr_t`,
  `MPI_Comm`) are integer-backed types. Use `DFTRACER_C_FUNCTION_UPDATE_INT("name", (int)val)` —
  cast is safe since the value is just an ID/descriptor used for correlation.

### C Rule 5 — Error-checking macros that embed early exits

Many C libraries define macros that check return codes and jump or return on error:

```c
// MPI pattern
MPI_CHECK(MPI_File_open(...), "cannot open file");

// NCMPI pattern
NCMPI_CHECK(ncmpi_create(...), "cannot create file");

// HDF5 pattern
if ((fid = H5Fcreate(...)) < 0) HGOTO_ERROR(...)
```

These macros often expand to `if (rc != 0) { ... return; }` or `goto err_label`.
They are **hidden exit points** that need END before them.

**Rule:** Before placing any START/END, scan the function for ALL exit-point macros:
```bash
grep -n "MPI_CHECK\|NCMPI_CHECK\|H5_CHECK\|HGOTO_ERROR\|ERR_GOTO\|goto.*err\|return" fn_lines
```

For each hidden-return macro, place END immediately before it:
```c
ssize_t my_write(...) {
  DFTRACER_C_FUNCTION_START();
  ...
  DFTRACER_C_FUNCTION_END();          // ← before macro that may return/goto
  NCMPI_CHECK(ncmpi_put_vara(...), "write failed");
  ...
  DFTRACER_C_FUNCTION_END();          // ← before normal return
  return 0;
err:
  DFTRACER_C_FUNCTION_END();          // ← before goto-target fallthrough
  return -1;
}
```

For **`goto`-based error handling** (common in HDF5 code):
```c
ssize_t hdf5_write(...) {
  DFTRACER_C_FUNCTION_START();
  ...
  if ((fid = H5Fcreate(...)) < 0) goto done;   // hidden exit
  ...
done:
  DFTRACER_C_FUNCTION_END();   // ← place END at the goto label, not before each goto
  return ret;
}
```
Place a single END at the `done:` / `err:` / `out:` label rather than before
each individual `goto` — this avoids duplicate ENDs and handles all goto paths.

### C Rule 7 — Forward declarations vs definitions: only annotate definitions

C source files often declare internal functions near the top of the file so they
can be called before their definitions. A **forward declaration** looks like a
function signature but:
- Ends with `;` (no body)
- Often has NO parameter names (e.g., `static IOR_offset_t POSIX_Xfer(int, aiori_fd_t *, IOR_size_t *, IOR_offset_t, IOR_offset_t, aiori_mod_opt_t *);`)
- Cannot hold any code

A **definition** always has:
- Named parameters
- A `{...}` body following the signature
- Usually a comment block above it explaining what it does

**Before annotating any function**, check for multiple occurrences:
```bash
grep -n "POSIX_Xfer\|FUNCTION_NAME" file.c
```
If there are two hits, annotate ONLY the definition:
```c
// ❌ DO NOT annotate — forward declaration (no body, no param names)
static IOR_offset_t POSIX_Xfer(int, aiori_fd_t *, IOR_size_t *,
                               IOR_offset_t, IOR_offset_t, aiori_mod_opt_t *);

// ✅ Annotate this one — definition with named params and body
static IOR_offset_t POSIX_Xfer(int access, aiori_fd_t *file, IOR_size_t *buffer,
                               IOR_offset_t length, IOR_offset_t offset,
                               aiori_mod_opt_t *param)
{
  DFTRACER_C_FUNCTION_START();
  ...
}
```

Quick filter to find only definitions (no trailing `;`):
```bash
grep -n "FUNCTION_NAME" file.c | grep -v ";$"
```

### C Rule 8 — Vendor-specific filesystem functions are I/O functions

Files like `aiori-POSIX.c` contain helper functions with vendor-specific prefixes
(`gpfs_*`, `beegfs_*`, `lustre_*`, `hdfs_*`, `ceph_*`, `daos_*`). These are
**filesystem-level I/O operations** that must be annotated with `comp="io"` — they
are not trivial utilities.

| Prefix | Filesystem | Examples |
|--------|-----------|---------|
| `gpfs_*` | IBM GPFS/Spectrum Scale | `gpfs_free_all_locks`, `gpfs_access_start`, `gpfs_access_end`, `gpfs_fineGrainWriteSharing` |
| `beegfs_*` | BeeGFS (ThinkParQ) | `beegfs_getStriping`, `beegfs_compatibleFileExists`, `beegfs_createFilePath` |
| `lustre_*` | Lustre | `lustre_disable_file_locks` |
| `hdfs_*` | Hadoop HDFS | Any HDFS JNI wrapper |
| `ceph_*` | Ceph | Any libcephfs wrapper |
| `daos_*` | DAOS | Any DAOS API wrapper |

These functions call kernel-level filesystem APIs (`gpfs_fcntl`, `ioctl`,
`beegfs_getStripeInfo`, `llapi_*`) that control I/O behavior. They must be traced
to observe filesystem-specific optimization paths (striping, lock management,
fine-grain sharing).

**How to find them**: after annotating the primary backend functions, scan for ALL
definitions in the file:
```bash
grep -n "^[a-zA-Z].*(.*)$\|^static.*(.*)$" aiori-POSIX.c | grep -v ";"
```
Any function that calls a vendor filesystem API is a mandatory annotation target
regardless of body size.

### C Rule 9 — Coverage verification: scan all definitions after each file

After annotating a file, run TWO checks before marking it DONE:

**Check 1 — list every annotated function** (the `awk` command from the report):
```bash
awk '/^[a-zA-Z].*\(/ {func=$0} /DFTRACER_C_FUNCTION_START/ {print NR": "func}' \
    annotated/src/foo.c
```
This prints one line per START with the function signature. Review it against the
full function list — any function missing from the output is unannotated.

**Check 2 — verify comp=TYPE is present in every annotated function:**
```bash
grep -c "DFTRACER_C_FUNCTION_START"          annotated/src/foo.c
grep -c 'DFTRACER_C_FUNCTION_UPDATE_STR.*comp' annotated/src/foo.c
```
Both counts **must be equal**. If START count > comp count, some functions were
annotated without the mandatory classification. Fix them before proceeding.

**Check 3 — quick definition vs START sanity count:**
```bash
grep -c "^[a-zA-Z_].*(" annotated/src/foo.c   # rough definition count
grep -c "DFTRACER_C_FUNCTION_START" annotated/src/foo.c  # START count
```
If counts differ significantly, use Check 1 to find the gap.

### C Rule 10 — Header include and linker setup (must be done BEFORE any macro compiles)

Before the first annotated file can compile, two things must be in place:
the dftracer header and the dftracer shared library. Neither is optional — missing
either produces hard compile or link errors.

---

#### 10a — Header: `#include <dftracer/dftracer.h>`

Add this line to **every `.c` or `.cpp` file that contains annotations**.

Placement rule:
- Add it as the **last `#include`** in the existing include block of that file.
- **Never add it to a `.h` header file** — doing so forces dftracer as a
  compile-time dependency on every translation unit that includes that header,
  which breaks builds that don't link dftracer.
- If the file has no existing includes, place it at the very top.

```c
/* ✅ Correct — in a .c file, after existing includes */
#include <stdio.h>
#include <stdlib.h>
#include "myproject.h"
#include <dftracer/dftracer.h>   /* ← dftracer last */
```

```c
/* ❌ Wrong — in a .h header file */
#ifndef MYMODULE_H
#define MYMODULE_H
#include <dftracer/dftracer.h>   /* ← will break all includers */
...
```

---

#### 10b — Linker: `-ldftracer_core` and runtime path

The annotation macros expand to calls into `libdftracer_core.so`.
**The library must be on both the link path and the runtime library path.**

The shared library name is `dftracer_core` (not `dftracer`). The install prefix
is wherever dftracer was built/installed — typically `workspaces/<run_id>/install_ann/`
for pipeline runs, or a system path like `/usr/local`.

**For Makefile / autotools projects** — patch `src/Makefile` (not just the
top-level Makefile):

```makefile
DFTRACER_PREFIX = /path/to/install_ann

CFLAGS  += -I$(DFTRACER_PREFIX)/include
LDFLAGS += -L$(DFTRACER_PREFIX)/lib -Wl,-rpath,$(DFTRACER_PREFIX)/lib
LIBS    += -ldftracer_core
```

`-Wl,-rpath,...` embeds the library search path into the binary so it does not
depend on `LD_LIBRARY_PATH` at run time.

**For CMake projects** — add to `CMakeLists.txt` before the target definitions:

```cmake
find_package(dftracer REQUIRED
    HINTS ${DFTRACER_PREFIX}/lib/cmake/dftracer
          ${DFTRACER_PREFIX}/share/cmake/dftracer)

target_link_libraries(${MY_TARGET} PRIVATE dftracer::dftracer_core)
target_include_directories(${MY_TARGET} PRIVATE
    ${DFTRACER_PREFIX}/include)
```

If `find_package` is not available (dftracer built without cmake export), fall
back to manual flags:

```cmake
target_include_directories(${MY_TARGET} PRIVATE ${DFTRACER_PREFIX}/include)
target_link_directories(${MY_TARGET} PRIVATE ${DFTRACER_PREFIX}/lib)
target_link_libraries(${MY_TARGET} PRIVATE dftracer_core)
set_target_properties(${MY_TARGET} PROPERTIES
    BUILD_RPATH "${DFTRACER_PREFIX}/lib"
    INSTALL_RPATH "${DFTRACER_PREFIX}/lib")
```

**For pkg-config** (when dftracer installs a `.pc` file):

```bash
pkg-config --cflags dftracer    # → -I/path/to/include
pkg-config --libs   dftracer    # → -L/path/to/lib -ldftracer_core
```

**When dftracer was installed via pip / venv** — the shared library and headers
live inside the Python package tree, not in a system prefix:

```
<venv>/lib/python<ver>/site-packages/dftracer/lib/libdftracer_core.so
<venv>/lib/python<ver>/site-packages/dftracer/include/dftracer/dftracer.h
```

Find the paths at build time:

```bash
DFTRACER_SITE=$(python3 -c \
    "import importlib.util, pathlib; \
     p=importlib.util.find_spec('dftracer'); \
     print(pathlib.Path(p.origin).parent)")

DFTRACER_INC="${DFTRACER_SITE}/include"
DFTRACER_LIB="${DFTRACER_SITE}/lib"
```

Then set flags as normal:

```makefile
CFLAGS  += -I$(DFTRACER_INC)
LDFLAGS += -L$(DFTRACER_LIB) -Wl,-rpath,$(DFTRACER_LIB)
LIBS    += -ldftracer_core
```

Use the **venv's python3** when querying — not the system python — to get the
path for the venv that will actually be on `LD_LIBRARY_PATH` at runtime.

---

#### 10c — Dependency libraries (if linker reports undefined symbols)

`libdftracer_core.so` may depend on `libcpp-logger` and `libbrahma`. On systems
without `RUNPATH` in dftracer's own binary, the linker may need these explicitly:

```makefile
LIBS += -ldftracer_core -lcpp-logger -lbrahma
```

Only add these if the link fails with `undefined reference to …` symbols from those
libraries. If dftracer was built with `-Wl,--as-needed`, they are already pulled in
transitively and adding them explicitly causes no harm.

---

#### 10d — Verify the build setup before annotating any source

Before writing the first annotation macro, confirm the build setup works:

```bash
# 1. Add ONLY the include to one .c file and build it
echo '#include <dftracer/dftracer.h>' >> annotated/src/one_file.c
make -C annotated/src 2>&1 | grep -i "error\|cannot find"

# 2. If it compiles cleanly, the include path is correct.
# 3. If "dftracer.h: No such file or directory", fix CFLAGS first.
# 4. Link a minimal program to confirm the .so is found:
gcc -o /tmp/dft_probe /dev/stdin \
    -I${DFTRACER_PREFIX}/include \
    -L${DFTRACER_PREFIX}/lib -ldftracer_core \
    -Wl,-rpath,${DFTRACER_PREFIX}/lib <<'C'
#include <dftracer/dftracer.h>
int main(void){ return 0; }
C
```

If the probe compiles and links, the environment is ready. Do not start annotation
until this probe succeeds — a broken build environment means every subsequent build
failure will be ambiguous.

### C Quick checklist

- [ ] **Build setup verified before first annotation** (Rule 10d): header probe compiles clean, linker probe links without errors
- [ ] `#include <dftracer/dftracer.h>` added to every annotated `.c`/`.cpp` file — as the last include, never in `.h` headers (Rule 10a)
- [ ] Linker flags set: `-ldftracer_core -L<prefix>/lib -Wl,-rpath,<prefix>/lib` (Rule 10b)
- [ ] For CMake: `target_link_libraries(... dftracer::dftracer_core)` or manual `-ldftracer_core` added (Rule 10b)
- [ ] If linker reports missing symbols: `-lcpp-logger -lbrahma` added after `-ldftracer_core` (Rule 10c)
- [ ] ALL non-trivial functions annotated — skip only pure getters/setters/formatters (Rule D)
- [ ] START is the first statement after `{` (not before `{`)
- [ ] START is in the function definition body, not a forward declaration (Rule 7)
- [ ] `DFTRACER_C_FUNCTION_UPDATE_STR("comp", "<type>")` is FIRST UPDATE after every START
- [ ] `comp` type is one of: `"io"`, `"mem"`, `"cpu"`, `"comm"` — see Rule 4 classification table
- [ ] Parameter names in UPDATE match the definition, not the declaration (Rule 5)
- [ ] Opaque handles (MPI_File, hid_t, ncid) tracked with UPDATE_INT using (int) cast
- [ ] END placed before every VISIBLE `return` in source (same indentation level)
- [ ] Error-checking macros (MPI_CHECK, NCMPI_CHECK): no END before them unless visible return follows (Rule E)
- [ ] goto-based error handling: single END at the label, not before each goto (Rule E)
- [ ] Void functions: END placed as last statement before `}`
- [ ] END indentation is not at column 0 — matches surrounding code
- [ ] main: MPI_Init (if present) → DFTRACER_C_INIT → ... → DFTRACER_C_FINI → MPI_Finalize → return
- [ ] No macros inside control-flow blocks, struct definitions, or `#define` bodies
- [ ] Checked for duplicate function names — annotated only the DEFINITION, not any forward declaration (Rule 7)
- [ ] Vendor-specific functions (gpfs_*, beegfs_*, lustre_*, daos_*, ceph_*) annotated as comp="io" (Rule 8)
- [ ] Coverage verified: definition count ≈ START count; no missed functions (Rule 9)
