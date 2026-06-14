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

### Quick checklist before writing annotated code

- [ ] Language-specific include / import added (see per-language rules)
- [ ] Only functions with I/O, data movement, or measurable runtime are annotated (Rule 0)
- [ ] No annotation inside header files, macro bodies, or forward declarations
- [ ] Build passes after each file — fix errors before moving to the next file
- [ ] Line count of annotated file is ≥ original (truncation check)
