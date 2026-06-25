# System: Tuolumne

AMD MI300A APU cluster at LLNL. Uses Cray PE with ROCm.

## Key Constraints
- **No sudo** — unprivileged user environment only.
- MPI uses `cray-mpich/9.0.1` via OFI fabric (`craype-network-ofi`, `libfabric/match_SHS`).
- Compiler: CCE 20.0.0 (`cce/20.0.0`) under `PrgEnv-cray/8.7.0`.
- Python: `python/3.13.2`

## Module Load Sequence

Load modules in this order (order matters for Cray PE):

```bash
module load craype-x86-trento
module load libfabric/match_SHS
module load craype-network-ofi
module load perftools-base/25.09.0
module load craype/2.7.35
module load PrgEnv-cray/8.7.0
module load flux_wrappers/0.1
module load xpmem/2.6.5
module load cce/20.0.0
module load cray-libsci/25.09.0
module load cray-mpich/9.0.1
module load python/3.13.2
```

StdEnv (S) is loaded by default — do not reload it.

## LD_LIBRARY_PATH Fix for CCE

After loading modules, set:

```bash
GCC_MODULE="cce/20.0.0"
export LD_LIBRARY_PATH="/opt/cray/pe/${GCC_MODULE}/cce/x86_64/lib:/opt/cray/pe/${GCC_MODULE}/cce/x86_64/lib/default64:${LD_LIBRARY_PATH}"
echo "Updated LD_LIBRARY_PATH for CCE: $LD_LIBRARY_PATH"
```

## I/O and Workspace

- **All benchmark I/O must target Lustre**: use `/p/lustre5/$USER/workspaces/ior/` as the data directory for IOR runs and trace output. This path is read/write accessible and is the correct path for high-bandwidth parallel I/O workloads.
- Smoke tests and benchmark runs must pass `DATA_DIR=/p/lustre5/$USER/workspaces/ior` (or the equivalent `--output-file` / `-o` flag for IOR) so that files land on Lustre, not on the shared home filesystem.
- Use `workspaces/<session>/tmp/` inside the project directory for build artifacts and temporary files.
- Never write to `/tmp` directly.

```bash
# Create Lustre workdir before any IOR run
mkdir -p /p/lustre5/$USER/workspaces/ior
```

## Scheduler: Flux

Tuolumne uses **Flux** as its job scheduler. Do not use `srun` or `mpirun` directly.

- Allocate nodes, list queues, and run jobs via Flux — see the `/flux-alloc` skill for full syntax.
- `flux_wrappers/0.1` must be loaded (included in the module sequence above) before any `flux` command.
- MPI jobs are launched with `flux run -N <nodes> -n <tasks> <cmd>` inside an allocated Flux instance.

Quick reference:

```bash
flux queue list                               # list queues and availability
flux resource info                            # total nodes/cores/GPUs
flux alloc -N <N> -q <QUEUE> -t <TIME>       # allocate nodes interactively
flux alloc --bg -N <N> -q <QUEUE> -t <TIME>  # allocate in background → JOBID
flux proxy <JOBID>                            # connect to the allocation
flux run -N <N> -n <TASKS> <CMD>             # run inside the allocation
```

For a full workflow (queue discovery → alloc → proxy → job submission), invoke the `/flux-alloc` skill.

## ROMIO / MPICH_MPIIO_HINTS on Cray MPICH 9.0.1

### How to discover available hints on a new MPI

Always query the MPI library first when on a new system or MPI version:

```bash
strings /opt/cray/pe/mpich/9.0.1/ofi/cray/20.0/lib/libmpi_cray.so \
  | grep -E "^romio_|^cb_|^striping_|^ind_|^ds_" | sort -u
```

### Cray MPICH 9.0.1 — confirmed available hints

Verified by `strings` on `libmpi_cray.so`:

| Hint | Description |
|------|-------------|
| `romio_cb_write` | Enable collective buffering for writes (`enable`/`disable`) |
| `romio_cb_read` | Enable collective buffering for reads |
| `romio_ds_write` | Enable data sieving for writes (`enable`/`disable`) |
| `romio_ds_read` | Enable data sieving for reads |
| `cb_buffer_size` | Collective buffer size in bytes (e.g. `67108864` = 64 MB) |
| `cb_nodes` | Number of aggregator processes |
| `cb_config_list` | Explicit aggregator list |
| `striping_factor` | Lustre stripe count (match OST count) |
| `striping_unit` | Lustre stripe size in bytes (e.g. `4194304` = 4 MB) |
| `ind_rd_buffer_size` | Independent read buffer size |
| `ind_wr_buffer_size` | Independent write buffer size |
| `romio_lustre_cb_lock_ahead_write` | Lock-ahead for collective writes |
| `romio_lustre_cb_lock_ahead_read` | Lock-ahead for collective reads |
| `romio_lustre_cb_lock_ahead_num_extents` | Number of lock-ahead extents |
| `romio_no_indep_rw` | Force collective I/O even for independent calls |

### MPICH_MPIIO_HINTS format

**Syntax:** `pathname_pattern:key=value:key=value,...` (colon-separated key=value pairs per file pattern; comma to separate multiple file patterns)

```bash
# Match all files with wildcard, colon-separated hints:
export MPICH_MPIIO_HINTS="*:romio_cb_write=enable:cb_buffer_size=67108864:cb_nodes=16:romio_ds_write=disable:striping_factor=16:striping_unit=4194304"

# Different hints per file:
export MPICH_MPIIO_HINTS="/path/to/file1:cb_nodes=8,/path/to/file2:cb_nodes=16"
```

**Always use a wrapper script** — never pass `MPICH_MPIIO_HINTS` via `--env` to `flux proxy flux run` because the colons are misinterpreted. See `/flux-alloc` skill for the wrapper script pattern.

**To display which hints MPICH actually applied**, set before running:
```bash
export MPICH_MPIIO_HINTS_DISPLAY=1
```

---

## MPI Library Path (Pitfall)

When running inside a flux allocation (`flux proxy <JOBID> flux run ...`), the compute nodes do NOT inherit a full `LD_LIBRARY_PATH`. The Cray MPI library must be added explicitly.

**Required path for cray-mpich/9.0.1:**

```bash
MPI_LIB=/opt/cray/pe/mpich/9.0.1/ofi/cray/20.0/lib
CCE_LIB=/opt/cray/pe/cce/20.0.0/cce/x86_64/lib
```

Always pass LD_LIBRARY_PATH explicitly with `--env` to `flux run`:

```bash
flux run -N <N> -n <TASKS> \
  --env LD_LIBRARY_PATH=${CCE_LIB}:${CCE_LIB}/default64:${MPI_LIB}:/opt/cray/pe/lib64:${EXTRA_LIBS} \
  <CMD>
```

Without this, all 768 ranks will fail with: `libmpi_cray.so.12: cannot open shared object file`.

## MPI Scaling (Default Behavior)

For MPI runs inside a flux allocation, always use all available physical cores:

- Tuolumne nodes have **96 physical cores** (no hyperthreading on MI300A).
- Default: `-N <all_nodes> -n <N_nodes × 96>`.
- Discover at runtime:

  ```bash
  N_NODES=$(flux proxy <JOBID> flux resource list --format="{nnodes}" -s free | tail -1)
  N_TASKS=$((N_NODES * 96))
  flux proxy <JOBID> flux run -N $N_NODES -n $N_TASKS <CMD>
  ```

When connecting via `flux proxy <JOBID>`, use all nodes in the allocation by default — do not request fewer nodes unless the user explicitly asks.

## Debugging: Core Dumps

On all Livermore systems (Tuolumne and others), enable core dumps in the directory where the job runs to capture crash state:

```bash
ulimit -c unlimited
cd /path/to/run/directory   # core file lands here
# then run the application
```

The core file appears as `core.<pid>` (or just `core`) in the current directory. Inspect with:

```bash
gdb <binary> core.<pid>
# or with ROCm for GPU faults:
rocgdb <binary> core.<pid>
```

**Why this works:** Livermore compute nodes have `ulimit -c 0` by default (no core dumps). Setting `ulimit -c unlimited` before the run re-enables them for the shell and all child processes, including MPI ranks launched via `flux run`. The core file is written to the working directory of the crashing process — make sure that path is on Lustre (`/p/lustre5/$USER/...`) and has sufficient space.

For MPI jobs where all ranks may crash, expect one core file per crashing rank.

## Notes

- APU means CPU and GPU share memory — no explicit data transfer needed between host and device.
- If `module` commands fail, ensure `StdEnv` is active: `module list | grep StdEnv`.
