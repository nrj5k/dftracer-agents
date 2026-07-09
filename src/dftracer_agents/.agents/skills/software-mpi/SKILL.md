---
name: software-mpi
description: >
  MPI and MPI-IO (ROMIO) optimization strategies, Cray MPICH environment
  variable wiring, flux proxy env propagation rules, and dftracer MPI tracing
  requirements.  Load this skill for any MPI or collective I/O work.
---

Cross-references: [[dftracer-io-optimization]] [[dftracer-preload-run]] [[workload-ior]] [[software-hdf5]]

---

## L2 Software — MPI-IO / ROMIO

Applies when bottleneck is `small_io_pct` or `rand_pct` high (metric: bandwidth or time).

### Hints file approach

```bash
cat > <WS>/romio_hints.txt << 'EOF'
cb_buffer_size 67108864
romio_cb_read enable
romio_ds_read enable
romio_ds_write enable
cb_nodes <num_nodes>
EOF
export ROMIO_HINTS=<WS>/romio_hints.txt
```

### Cray MPICH environment variable (preferred over hints file)

**Format:** `pathname_pattern:key=value:key=value` — colon-separated key=value pairs per pattern.
Multiple file patterns are comma-separated. Use `*` to match all files.

```bash
# Correct format — colons between key=value pairs:
export MPICH_MPIIO_HINTS="*:romio_cb_write=enable:cb_buffer_size=67108864:cb_nodes=16:romio_ds_write=disable:striping_factor=16:striping_unit=4194304"

# To display hints actually applied (set before run):
export MPICH_MPIIO_HINTS_DISPLAY=1
```

**NEVER pass via `--env` to `flux proxy flux run`** — colons and semicolons in the value
cause flux run to lose the binary argument entirely. Always use a wrapper script:

```bash
cat > wrapper.sh << 'EOF'
#!/bin/bash
export MPICH_MPIIO_HINTS="*:romio_cb_write=enable:cb_buffer_size=67108864:cb_nodes=16"
exec "$@"
EOF
chmod +x wrapper.sh
flux proxy $JOB flux run -N 2 -n 192 --env LD_LIBRARY_PATH=<libs> bash wrapper.sh /path/to/binary args
```

**How to discover available hints on a new Cray MPICH version:**

```bash
strings /opt/cray/pe/mpich/<version>/ofi/cray/<ver>/lib/libmpi_cray.so \
  | grep -E "^romio_|^cb_|^striping_|^ind_|^ds_" | sort -u
```

### Cray MPICH 9.0.1 — confirmed available hints

Extracted from `libmpi_cray.so` on Tuolumne (cray-mpich/9.0.1, OFI path):

| Hint | Recommended value | Effect |
|------|------------------|--------|
| `romio_cb_write` | `enable` | Collective buffering for writes |
| `romio_cb_read` | `enable` | Collective buffering for reads (disable on VAST/NVMe) |
| `romio_ds_write` | `disable` | Data sieving writes (disable to force 2-phase I/O) |
| `romio_ds_read` | `disable` | Data sieving reads |
| `cb_buffer_size` | `67108864` | 64 MB collective buffer per aggregator |
| `cb_nodes` | `16` | Aggregator count (match Lustre stripe count) |
| `cb_config_list` | `*:1` | Explicit aggregator mapping |
| `striping_factor` | `16` | Lustre stripe count (match with `lfs getstripe`) |
| `striping_unit` | `4194304` | 4 MB stripe size (match Lustre layout) |
| `ind_rd_buffer_size` | `4194304` | Independent read buffer |
| `ind_wr_buffer_size` | `4194304` | Independent write buffer |
| `romio_lustre_cb_lock_ahead_write` | `enable` | Lock-ahead for collective writes on Lustre |
| `romio_lustre_cb_lock_ahead_read` | `enable` | Lock-ahead for collective reads on Lustre |
| `romio_lustre_cb_lock_ahead_num_extents` | `4` | Number of lock-ahead extents |
| `romio_no_indep_rw` | `true` | ❌ NOT SUPPORTED on cray-mpich 9.0.1 — hint silently dropped with "unrecognized value" warning |

### cb_nodes guidance for Lustre + cray-mpich 9.0.1

Per-workload rules validated on Tuolumne lustre5 (28 OSTs), 2 nodes × 192 ranks:

| Workload type | cb_nodes rule | Rationale |
|---|---|---|
| Contiguous fixed-size writes | Do NOT set (use default=2) | Default aggregators work well; adding more aggregators doesn't improve throughput and can cause pwrite stalls |
| Variable-size writes (e.g. normal-dist) | Do NOT set (use default=2) | More aggregators fragment variable chunks → massive pwrite p95 regression (+2650%) |
| Reads (untested on lustre5) | 2× stripe_count recommended per literature | Follows user's 2× rule |

The user's rule (cb_nodes = 2× stripe_count) is theoretically correct but empirically neutral-or-worse on Tuolumne at the tested scale (DIM_1=16M, 4 TS, 192 ranks). The close overhead savings from more aggregators are cancelled by pwrite stalls caused by buffer fragmentation.

If cb_nodes is needed: test at each scale step (R11) before production. Values tested:
- cb_nodes=16 + cb_buffer_size + striping hints → segfault (see workload-h5bench SKILL.md)
- cb_nodes=32 alone (write, 16-OST) → neutral (open −49%, overall ±0%)
- cb_nodes=56 alone (ND, 28-OST) → neutral (close −96%, pwrite p95 +2650%)

### Common ROMIO parameters (all MPI implementations)

| Parameter | Recommended value | Effect |
|-----------|------------------|--------|
| `cb_buffer_size` | `67108864` (64 MB) | Collective I/O buffer per aggregator |
| `romio_cb_read` | `enable` | Aggregate reads via cb (helps Lustre; hurts VAST — see below) |
| `romio_cb_write` | `enable` | Aggregate writes via cb (beneficial on most systems) |
| `romio_ds_read` | `enable` | Data sieving for reads |
| `romio_ds_write` | `enable` | Data sieving for writes (default; disabling is FATAL on VAST) |
| `cb_nodes` | `<num_nodes>` | Number of aggregator processes |

### Storage-specific ROMIO guidance (check FS_TYPE before applying — see Step 8-PRE of [[dftracer-pipeline]])

See [[workload-ior]] for quantified results on VAST NVMe (Tuolumne):
- `romio_cb_write=enable` → **+58% write BW** (always beneficial)
- `romio_cb_read=enable` → **−70% read BW** on VAST (AVOID on NVMe)
- `romio_ds_write=disable` → **FATAL** on VAST (352→95 MiB/s write collapse)

General rule: collective read buffering helps on high-latency storage (Lustre,
spinning disk). It hurts on parallel NVMe where concurrent reads are cheap.

---

## Forwarding Environment Variables to MPI Ranks

`mpirun` does **not** automatically forward shell environment. Export + `-x`:

```bash
export DFTRACER_ENABLE=1
export DFTRACER_INIT=PRELOAD
export DFTRACER_DATA_DIR=/tmp
export DFTRACER_LOG_FILE=/tmp/traces/myapp
export MPICH_MPIIO_HINTS="*:romio_cb_write=enable"
export LD_PRELOAD=<dftracer_lib>/libdftracer_preload.so

mpirun --allow-run-as-root -np 4 \
  -x DFTRACER_ENABLE \
  -x DFTRACER_INIT \
  -x DFTRACER_DATA_DIR \
  -x DFTRACER_LOG_FILE \
  -x MPICH_MPIIO_HINTS \
  -x LD_LIBRARY_PATH \
  -x LD_PRELOAD \
  ./my_app
```

---

## Flux Proxy — Environment Variable Propagation

`flux proxy` does NOT export the current shell's environment to the broker.
Variables set after `flux alloc` or `flux proxy` are invisible to job tasks.

**Always use `--env` flags with `flux run`:**

```bash
flux proxy <JOBID> flux run \
  -N 2 -n 192 \
  --env MPICH_MPIIO_HINTS="*:romio_cb_write=enable" \
  --env DFTRACER_ENABLE=1 \
  --env DFTRACER_LOG_FILE=<prefix> \
  --env DFTRACER_DATA_DIR=all \
  --env DFTRACER_INC_METADATA=1 \
  --env DFTRACER_INIT=FUNCTION \
  --env LD_LIBRARY_PATH=<libs> \
  <command>
```

Do NOT rely on `export VAR=value` before `flux proxy` — it will NOT propagate.

---

## MPI tracing in dftracer

dftracer must be built with MPI support to capture `MPI_File_write/read` events:

```bash
cmake -DDFTRACER_ENABLE_MPI=ON ...
# Verify:
grep DFTRACER_MPI_ENABLE <prefix>/include/dftracer/core/dftracer_config.hpp
# Expected: #define DFTRACER_MPI_ENABLE 1
```

Runtime: the MPI shared library at run time must match the version dftracer was
compiled against:
```bash
strings libdftracer_core.so | grep "openmpi/include"  # must match system mpi.h path
```

### Cray PE / MPICH include path issue in clang_syntax_check

```
fatal error: mpi.h: No such file or directory
```

Cray PE `mpicc --showme:incdirs` outputs `-I/path` (with `-I` prefix) instead of
a plain path, so auto-detection fails. Always pass explicitly:

```python
clang_syntax_check(run_id=..., filepath=...,
  extra_include_dirs=[
    "/opt/cray/pe/mpich/<version>/ofi/cray/<ver>/include",
    "<ws>/venv/lib/python3.*/site-packages/dftracer/include"
  ])
```

Get the exact MPI path: `mpicc -show | grep -o '\-I[^ ]*' | head -1`

---

## NUMA Memory Binding (MPI processes)

```bash
numactl --hardware   # check topology
numactl --cpunodebind=0 --membind=0 <run_command>   # pin to NUMA node (no persistent side effect)
# For MPI:
mpirun --map-by numa:pe=<cores_per_node>   # OpenMPI
```

---

## Citation

**WisIO (Yildirim et al., ICS 2025)** — covers `small_io_pct`, `rand_pct`, `read_time`, `write_time`
URL: https://dl.acm.org/doi/10.1145/3721145.3730395

**Drishti (Bez et al., PDSW 2022)** — L1/L2/L3 suggestion model including collective I/O
URL: https://ieeexplore.ieee.org/document/10027503

## Failed Configurations

Entries below were applied during optimization loops and caused regressions or had no effect.
Check this section before proposing any configuration for this workload/software/filesystem.

Format per entry:
  date, app, workload, filesystem, system, bottleneck,
  config_attempted, result, metrics_before, metrics_after, delta,
  root_cause, do_not_use_when

<!-- New failed-config entries are appended below by the optimization loop (Step 8d-iii-FAIL) -->

---

## Cray MPICH: `cb_nodes` is accepted but IGNORED (2026-07-08, measured)

Setting `cb_nodes` via `MPICH_MPIIO_HINTS` (or a `ROMIO_HINTS` file) does **not**
by itself increase the number of MPI-IO collective-buffering aggregators on Cray
MPICH. `MPICH_MPIIO_HINTS_DISPLAY=1` will happily echo back `cb_nodes = 8`, while
the runtime keeps using only **2** aggregators.

**Do not trust the hints display — verify the real aggregator count from traces:**
count the distinct ranks that issue writes `>= 1 MB` (the collective-buffer
flushes). Tiny writes happen on every rank (logs), so counting "ranks that write"
is useless; count ranks doing *large* writes.

```bash
dftracer_view -d <split_dir> --query 'cat == "POSIX"' --stream --no-metadata \
  | python3 -c 'import sys,json;p=set()
for l in sys.stdin:
  if "\"name\":\"pwrite\"" not in l and "\"name\":\"write\"" not in l: continue
  e=json.loads(l); r=e.get("args",{}).get("ret",0)
  if isinstance(r,int) and r>=1048576: p.add(e["pid"])
print("aggregators:",len(p))'
```

**What actually raises the aggregator count:** the Cray-specific
`CRAY_CB_NODES_MULTIPLIER` env var (used together with `cb_nodes`), plus a Lustre
stripe count wide enough to absorb them.

Measured on Flash-X (384 ranks / 8 nodes, 18 checkpoints, ~6.7 GB, Lustre):

| Config | aggregators | critical-path write | avg large write |
| --- | --- | --- | --- |
| parallel HDF5, no hints | 2 | 6.80 s | 1.05 MB |
| `+ cb_nodes=8` (hint echoed, ignored) | 2 | 5.53 s | 1.05 MB |
| `+ cb_nodes=16` + `CRAY_CB_NODES_MULTIPLIER=2` + 16×4 MB stripes | **16** | **1.45 s** | **3.76 MB** |

Top-1 rank's share of write bytes fell 52% → 6.8% (balanced parallel I/O).

**Ordering matters:** collective-buffering hints are worthless until the
application actually performs parallel writes. If one rank holds ~all write
bytes, fix that first (see [[workload-flashx]] serial-HDF5 case) — hints cannot
parallelize a single writer.

