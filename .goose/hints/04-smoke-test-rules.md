## Smoke Test Rules

**Always run smoke tests as a single process — never with MPI or any parallelism.**

- `session_run_smoke_test` automatically strips MPI launchers (`mpirun`, `mpiexec`,
  `srun`, `jsrun`, `aprun`, `flux run`) and their flags (`-np`, `-n`, `--ntasks`, etc.)
  from the command before running. You do not need to clean the command yourself, but
  **do not intentionally pass multi-process flags** — the goal is a clean, reproducible
  single-process baseline.
- Never use `-j N` (parallel make) inside a smoke test command.
- Never use `OMP_NUM_THREADS > 1` or `GOMP_SPINCOUNT` in smoke test env vars.
- If the application requires at least one MPI rank to start (e.g., calls
  `MPI_Init` unconditionally), run it with `mpirun -np 1 <binary> <args>` so the
  tool strips it to a single rank — NOT with multiple ranks.
- The smoke test command should exercise the minimal happy path: one input file,
  one iteration, smallest possible data size, no checksum or verification flags that
  require multiple processes.

**Example for IOR:**
```
# Good — single process, minimal run
./src/ior -a POSIX -b 1m -t 1m -s 1 -F -C -o /tmp/ior_smoke_test

# Bad — MPI multi-process
mpirun -np 4 ./src/ior -a POSIX -b 1m -t 1m -s 1 -F -C -o /tmp/ior_smoke_test
```

## Key DFTracer Environment Variables (for smoke tests / session_run_with_dftracer)

Always set all of these when running an application with dftracer enabled:
- `DFTRACER_ENABLE=1`        — activate tracing (required)
- `DFTRACER_INC_METADATA=1`  — include process/thread metadata in trace output
- `DFTRACER_LOG_FILE=<path>` — prefix path for .pfw trace files
- `DFTRACER_DATA_DIR=<path>` — directory to monitor for I/O events
- `DFTRACER_INIT=1`          — auto-initialise without an explicit API call in source
