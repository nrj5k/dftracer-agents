## Python Annotation Rules (dftracer)

### Python Rule 1 — Use the decorator for regular functions

```python
from dftracer.logger import dftracer_fn, dft_fn

@dftracer_fn(cat="IO")            # cat groups functions in the trace viewer
def my_read(path: str, size: int) -> bytes:
    ...
```

- The `@dftracer_fn` decorator wraps the function with START/END automatically.
- `cat` (category) is required — use a meaningful group name: `"IO"`, `"Compute"`,
  `"MPI"`, `"Data"`, `"Init"`, etc.
- `dft_fn` is an alias for `dftracer_fn` — prefer `dftracer_fn` for clarity.
- Apply at every function that qualifies under **Rule 0** (see general rules).

### Python Rule 2 — Initialize and finalize the tracer

```python
from dftracer.logger import DFTracer

# At program entry (after MPI.Init if using mpi4py)
tracer = DFTracer.initialize_log(
    log_file="traces/my_app",    # prefix for .pfw trace files
    data_dir="/data",            # directory to monitor for I/O events
    process_id=rank,             # MPI rank or 0 for single-process
)

# ... application code ...

# At program exit (before MPI.Finalize if using mpi4py)
DFTracer.finalize_log()
```

- **With mpi4py**: call `DFTracer.initialize_log` AFTER `MPI.Init()` (or use
  `MPI.COMM_WORLD.Get_rank()` for `process_id`).
- **With mpi4py**: call `DFTracer.finalize_log()` BEFORE `MPI.Finalize()`.
- Use `process_id=0` for single-process runs.

### Python Rule 3 — Environment-variable initialization (alternative)

If the application uses `DFTRACER_INIT=1` via environment variable (no explicit API call),
you still need the decorator to trace individual functions. The env var only handles
INIT/FINI — it does not add function-level spans.

```bash
export DFTRACER_ENABLE=1
export DFTRACER_INIT=1
export DFTRACER_LOG_FILE=/tmp/traces/my_app
export DFTRACER_DATA_DIR=/data
python my_app.py
```

### Python Rule 4 — Context manager for ad-hoc regions

For code blocks that are not functions (e.g., a critical loop body), use the
context manager:

```python
from dftracer.logger import DFTracer

tracer = DFTracer.get_instance()
with tracer.get_time("IO", "read_loop"):
    for chunk in data:
        process(chunk)
```

- Only use context managers when the code block is too coarse-grained to fit a function
  decorator — prefer decorators for most cases.
- Name the region descriptively: `"category"`, `"name"` pair appears in the trace.

### Python Rule 5 — Skip trivial functions (Rule 0 applies)

```python
# ✅ Annotate — does real file I/O
@dftracer_fn(cat="IO")
def read_checkpoint(path: str, rank: int) -> dict:
    with open(path, "rb") as f:
        return pickle.load(f)

# ✅ Annotate — large data movement
@dftracer_fn(cat="Data")
def preprocess_batch(data: np.ndarray) -> np.ndarray:
    return normalize(data)

# ❌ Skip — trivial property
@property
def rank(self) -> int:
    return self._rank

# ❌ Skip — one-liner string helper
def _fmt_path(self, p: str) -> str:
    return str(Path(p).resolve())
```

### Python Rule 6 — Class methods

Apply the decorator directly to instance and class methods:

```python
class DataLoader:
    @dftracer_fn(cat="IO")
    def load(self, path: str, batch_size: int) -> list:
        ...

    @dftracer_fn(cat="IO")
    def write(self, path: str, data: bytes) -> None:
        ...

    def _validate(self, x):      # ❌ skip — trivial helper
        return x is not None
```

- `self`/`cls` parameters are not useful for UPDATE — skip them.
- Only annotate methods that perform I/O, data movement, or take measurable time.

### Python Quick checklist

- [ ] `from dftracer.logger import dftracer_fn` imported at top of file
- [ ] `DFTracer.initialize_log(...)` called at program entry (after MPI.Init if applicable)
- [ ] `DFTracer.finalize_log()` called at program exit (before MPI.Finalize if applicable)
- [ ] `@dftracer_fn(cat="<category>")` applied to all Rule-0-qualifying functions
- [ ] No decorator on trivial getters, setters, or one-liners
- [ ] Category names are meaningful and consistent across the codebase
- [ ] For mpi4py: initialize AFTER MPI.Init(), finalize BEFORE MPI.Finalize()
