## C / C++ / Python Annotation Rules (dftracer)

These rules apply whenever you manually annotate C, C++, or Python source files.
Violating any rule will cause build failures or missing trace data.

### Rule 0 — Only annotate functions worth tracing (skip trivial ones)

dftracer macros add a small but real overhead per call. Annotating every function
indiscriminately defeats the purpose and pollutes traces with noise.

**Annotate a function if it:**
- Performs file or network I/O (`open`, `read`, `write`, `close`, `fread`, `fwrite`,
  `mmap`, `send`, `recv`, `MPI_File_*`, `MPI_Send`, `MPI_Recv`, …)
- Moves or transforms significant data (memcpy, checksums, compression, serialize)
- Allocates or frees large buffers (`malloc`/`free` of buffers > a few KB)
- Contains a loop that iterates over data (segments, blocks, ranks)
- Is a top-level driver or orchestration function (`main`, `run_test`, `benchmark_*`)
- Takes measurable wall time (anything you would profile with perf or gprof)

**Skip a function if it:**
- Is a simple getter or setter (returns a field, sets one value, ≤ 5 lines)
- Only does arithmetic or string formatting with no I/O
- Is a small utility called in a tight inner loop (would add overhead per iteration)
- Is a constructor/destructor with no I/O side effects
- Is already wrapped by a larger annotated function that covers the same work

**Always annotate, even if they look small:**
- Backend lifecycle functions (`*_init`, `*_final`, `*_initialize`, `*_finalize`,
  `*_open_backend`, `*_close_backend`) — these mark framework entry/exit points and
  appear in traces even if the body is short. **Never apply Rule 0 to lifecycle functions.**
- Sync/flush functions (`*_Fsync`, `*_Sync`, `*_Flush`) — directly observable I/O ops
- Delete/rename/stat functions — file metadata operations worth tracing (`*_Delete`,
  `*_Rename`, `*_Mknod`, `*_GetFileSize`)
- **Vendor-specific filesystem helpers** with prefixes `gpfs_*`, `beegfs_*`,
  `lustre_*`, `hdfs_*`, `ceph_*`, `daos_*` — these call kernel-level filesystem
  APIs (gpfs_fcntl, ioctl, beegfs_getStripeInfo, llapi_*) and are I/O operations
  regardless of their unfamiliar prefix. Annotate as `comp="io"`.

**Examples:**

```c
// ✅ Annotate — does file I/O, has filename and size params
ssize_t POSIX_read(int fd, void *buf, size_t count, off_t offset) { ... }

// ✅ Annotate — top-level benchmark driver, takes real time
void IOR_TestIO(IOR_param_t *params) { ... }

// ❌ Skip — trivial getter, no I/O, called millions of times
static inline int get_rank() { return rank; }

// ❌ Skip — 3-line string helper, no data movement
static const char *mode_to_str(int mode) { return mode == 0 ? "r" : "w"; }
```

### Rule F — Scan the entire file for internal helpers, not just the named API

After annotating the public/named backend functions (e.g., `POSIX_Create`, `POSIX_Xfer`),
always scan the **whole file** for unannotated helper functions. Internal helpers with
non-obvious names are often the most interesting functions to trace.

**Syscall triggers — if a function body contains any of these, it is an I/O function:**

| Trigger call | Category | Example function |
|-------------|----------|-----------------|
| `open()`, `read()`, `write()`, `close()`, `stat()` | POSIX syscall | Any internal read/write helper |
| `mknod()`, `mkstemp()`, `unlink()`, `rename()` | File lifecycle | `POSIX_Mknod`, `mkTempInDir` |
| `ioctl()`, `fcntl()` | Kernel I/O control | Any hint/lock setter |
| `gpfs_fcntl()` | GPFS control | `gpfs_access_start`, `gpfs_free_all_locks` |
| `beegfs_getStripeInfo()`, `beegfs_createFile()` | BeeGFS | `beegfs_createFilePath` |
| `llapi_*()` | Lustre | Any lustre helper |
| `cuFileHandleRegister()`, `cuFileRead()`, `cuFileWrite()` | GPU Direct | `init_cufile` |
| `MPI_File_*()` | MPI-IO | Any MPI file wrapper |

**Functions that call only these are Rule 0 safe to skip:**
- Pure setters: `hints = param;`
- Parameter validation: `if (x != valid) ERR(...)` with no filesystem call
- Error string formatters: `return strerror(err);`

**Workflow:**
```bash
# Step 1 — list definitions
grep -n "^[a-zA-Z_].*(.*)$" src/foo.c | grep -v ";"

# Step 2 — cross-reference against annotated list
awk '/^[a-zA-Z_].*\(/ {func=NR": "$0} /DFTRACER_C_FUNCTION_START/ {print func}' annotated/src/foo.c

# Step 3 — for any definition not in Step 2, check body for syscall triggers
# If trigger found → annotate; if no trigger → Rule 0 skip
```

### Rule D — Complete coverage: annotate everything, skip only trivial functions

**The default is to annotate. Skip requires justification.**

Every function is annotated unless it meets a specific skip condition. Do not
skip a function because it is complex, has many returns, or uses unfamiliar
patterns — those are reasons to annotate carefully, not reasons to skip.

**Permanent skip conditions (Rule 0):**
- Getter or setter returning a single field, ≤ 5 lines, no I/O
- Pure arithmetic, string formatting, or logging with no data movement
- Small utility called inside a tight inner loop (per-iteration overhead)
- A function whose entire body is delegated to a single call that is itself annotated

**Every other function gets annotated**, including:
- Complex functions with many branches — annotate the happy path (see Rule E)
- Large functions with 100+ lines — still annotate, but work section by section
- Functions whose bodies contain library macros (MPI_CHECK, H5CHECK) — still annotate

**After annotating a file**, verify coverage:
```bash
# Count function definitions (rough)
grep -c "^[a-zA-Z].*(.*)$\|^[a-zA-Z].* (\*" src/foo.c

# Count STARTs
grep -c "DFTRACER_C_FUNCTION_START" annotated/src/foo.c
```
If the counts are far apart, find the unannotated functions and annotate them.

### Rule E — Error paths: annotate happy path only; END only before visible return/throw

For complex error-checking code, DO NOT attempt to place END inside macro
expansions or before every error-checking macro call. Only END where you can
see an explicit `return`, `throw`, or the function-closing `}` in the source.

**Apply this rule to:**
- `MPI_CHECK(...)`, `NCMPI_CHECK(...)`, `H5EPRINT(...)`, `ERRNO_CHECK(...)` macros
- `if (rc != 0) { log_error(...); return -1; }` blocks
- Any error path that ultimately reaches a visible `return` or `goto label`

**Simple rule for error paths:**
```c
// ✅ Correct — END before the explicit return you can see
int my_fn(const char *path) {
  DFTRACER_C_FUNCTION_START();
  int fd = open(path, O_RDONLY);
  if (fd < 0) {
    DFTRACER_C_FUNCTION_END();   // ← explicit return you can see
    return -1;
  }
  // ... happy path ...
  DFTRACER_C_FUNCTION_END();
  return 0;
}

// ✅ Correct — don't add END before macro; the macro's internal return is invisible
int mpi_fn(MPI_File *fh, const char *path) {
  DFTRACER_C_FUNCTION_START();
  MPI_CHECK(MPI_File_open(...), "open failed");  // ← no END here; macro hides return
  // ... happy path ...
  DFTRACER_C_FUNCTION_END();
  return 0;
}

// ✅ Correct — goto pattern: single END at the label covers all paths
int hdf5_fn(const char *path) {
  DFTRACER_C_FUNCTION_START();
  if ((fid = H5Fopen(...)) < 0) goto done;
  // ... happy path ...
done:
  DFTRACER_C_FUNCTION_END();   // ← one END at label covers happy + error goto
  return ret;
}
```

**Exception:** if an error-checking macro is immediately followed by an explicit
`return` in the source (i.e., the return is visible), treat that return normally.

### Rule A — Annotate at every abstraction layer (wrapper calls)

When a higher-level function delegates to a lower-level function that is also
annotated, annotate BOTH. Do not skip the wrapper just because the underlying
call is already traced.

```
MMAP_Xfer()      ← annotate (captures MMAP-level intent)
  └─ POSIX_Xfer() ← also annotated (captures syscall-level detail)
```

This is intentional: traces show the full call hierarchy, which reveals how
much time is spent in the abstraction layer vs. the actual I/O call. If double-
tracing appears in the output, it is correct and expected — not a bug.

**Exception:** if function A is a one-line passthrough with no logic of its own
and no parameters to capture, and the wrapper overhead would be misleading,
skip it (Rule 0 trivial wrapper).

### Rule B — Annotation order within a file: simplest functions first

When annotating a file with many functions, work in this order:
1. Functions with a **single return statement** — lowest risk, easiest END placement
2. Functions with **2–3 returns** in shallow control flow
3. Functions with **multiple returns in nested blocks** (switch/case, error chains)
4. Functions with **error-checking macros** that may hide exits (see C rules)

This order means the build is passing for the easy functions before you tackle
the complex ones. If a complex function fails, the rest of the file still compiles.

### Rule C — Backend annotation priority order

When a project has multiple storage backends, annotate them in this order:
1. **Reference / POSIX backend** — simplest, establishes the pattern
2. **Wrapper backends** (MMAP, AIO) — wrap POSIX, easy to verify against it
3. **MPI-based backends** (MPIIO, NCMPI, parallel HDF5) — more complex control flow
4. **High-level library backends** (HDF5, NetCDF) — property lists, error hierarchies
5. **Distributed / cloud backends** (HDFS, DFS, S3, CEPHFS, RADOS) — JNI or REST calls
6. **Specialized / research backends** (PMDK, IME, Gfarm, CHFS, aio) — last

Build and run a smoke test for each backend immediately after annotating it —
do not batch multiple backends before testing.

### Quick checklist before writing annotated code

- [ ] Language-specific include / import added (see per-language rules)
- [ ] Only functions with I/O, data movement, or measurable runtime are annotated (Rule 0)
- [ ] Backend lifecycle functions (`*_init`, `*_final`, `*_Sync`, `*_Delete`, `*_Rename`, `*_Mknod`) always included
- [ ] Vendor filesystem helpers (`gpfs_*`, `beegfs_*`, `lustre_*`, etc.) included as `comp="io"`
- [ ] Wrapper functions annotated even if the underlying call is also annotated (Rule A)
- [ ] Annotation order: single-return functions first (Rule B)
- [ ] No annotation inside header files, macro bodies, or **forward declarations**
- [ ] For any function name that appears twice: annotated only the definition (the one with a body), not the forward declaration (see C Rule 7)
- [ ] After each file: verify definition count ≈ START count to confirm full coverage (C Rule 9)
- [ ] Build passes after each file — fix errors before moving to the next file
- [ ] Line count of annotated file is ≥ original (truncation check)
