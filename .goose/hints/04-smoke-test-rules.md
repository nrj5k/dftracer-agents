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

## Key DFTracer Environment Variables (for session_run_with_dftracer)

`session_run_with_dftracer` sets all of these automatically — **do not pass them
in `env_extra` unless you have a specific override reason**:

| Variable | Value set by the tool | Purpose |
|---|---|---|
| `DFTRACER_ENABLE` | `1` | Activate tracing (required) |
| `DFTRACER_INC_METADATA` | `1` | Include process/thread metadata |
| `DFTRACER_LOG_FILE` | `workspaces/<run_id>/traces/<run_id>` | Trace file prefix — dftracer appends `.<pid>.pfw` |
| `DFTRACER_DATA_DIR` | `workspaces/<run_id>/source` (or caller-supplied path) | Directory to monitor for I/O events |
| `DFTRACER_INIT` | `1` *(see note below)* | Auto-initialise without an explicit API call in source |

**DFTRACER_INIT conflict warning:** If the annotated source already contains explicit
`DFTRACER_C_INIT()` / `DFTRACER_CPP_INIT()` calls (added during Pass 1), do NOT set
`DFTRACER_INIT=1`. Both active simultaneously causes double-init, producing an empty
or corrupted trace file. Pass `env_extra='{"DFTRACER_INIT":"0"}'` in that case.
Heuristic: `grep -r "DFTRACER_C_INIT\|DFTRACER_CPP_INIT" annotated/` — if any matches,
set `DFTRACER_INIT=0`.

**`DFTRACER_LOG_FILE` must always be an absolute path inside the workspace run directory.**
Trace files land at `workspaces/<run_id>/traces/<run_id>.<pid>.pfw` and are picked
up by `session_split_traces`, which reads all `*.pfw` / `*.pfw.gz` from that folder.

**Never set `DFTRACER_LOG_FILE` to `/tmp/` or any path outside the workspace.**
Traces written outside the workspace will not be found by `session_split_traces`.

If you run the application manually (outside the MCP tool), replicate the same env:
```bash
export DFTRACER_ENABLE=1
export DFTRACER_INC_METADATA=1
export DFTRACER_LOG_FILE=/absolute/path/to/workspaces/<run_id>/traces/<run_id>
export DFTRACER_DATA_DIR=/absolute/path/to/workspaces/<run_id>/source
export DFTRACER_INIT=1
```

## DFTRACER_LOG_FILE Naming Convention (Critical)

**`DFTRACER_LOG_FILE` must be a PREFIX, not a complete filename.**

dftracer automatically appends `.<pid>-<appid>.pfw.gz` (or similar suffix) to the
prefix you provide. This allows multiple processes to write separate trace files
without collisions.

**Correct usage:**
```bash
export DFTRACER_LOG_FILE=/workspace/run_id/traces/trace
# dftracer creates: /workspace/run_id/traces/trace.12345-app.pfw.gz
```

**Incorrect usage:**
```bash
export DFTRACER_LOG_FILE=/workspace/run_id/traces/run_id
# May create empty file or fail silently
```

**For sessionRunWithDftracer:**
The tool should set `DFTRACER_LOG_FILE` to a prefix like:
- `traces/trace` (simple)
- `traces/<run_id>/trace` (organized by run)

After the run, find all trace files with:
```bash
find <workspace>/traces -name "*.pfw*" -type f
```

**sessionSplitTraces compatibility:**
The split tool reads all `*.pfw` / `*.pfw.gz` files from the traces directory.
If dftracer creates files in a subfolder (e.g., `traces/ior/`), you may need to
move them to `traces/` root before splitting, or configure the tool to look in
the correct subdirectory.
