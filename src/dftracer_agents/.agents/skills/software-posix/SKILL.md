---
name: software-posix
description: >
  POSIX I/O optimization strategies: application-level buffering (L1),
  readahead and library hints (L2), OS/filesystem tuning (L3), and
  dftracer DATA_DIR configuration for capturing POSIX events.
  Load this skill for any POSIX I/O bottleneck work.
---

Cross-references: [[dftracer-io-optimization]] [[dftracer-preload-run]] [[software-mpi]] [[software-hdf5]]

---

## L1 Application — POSIX I/O Strategies

### Small I/O (`small_io_pct` high — metric: bandwidth or iops)

- Coalesce small reads into a single larger read with a staging buffer
- Replace per-element writes with batch writes to a pre-allocated buffer
- Use `readv`/`writev` (scatter/gather) instead of loops of `read`/`write`
- For Python: increase `chunk_size`; use `np.fromfile` over loops

### Sequential access (`rand_pct` high — metric: bandwidth or time)

- Reorder access so items are read in file-offset order (sort indices before I/O)
- Add `posix_fadvise(fd, 0, 0, POSIX_FADV_SEQUENTIAL)` before sequential reads
- Use `mmap` for random access patterns to let the OS handle page faults
- For deep learning: sort dataset by file size, shuffle only indices in memory

### Read/write time (`read_time_pct` or `write_time_pct` high — metric: time)

- Pre-open files and keep file descriptors open across iterations
- Use `O_DIRECT` for cache-bypassing when data reuse is low
- Add async I/O: `io_uring` (C/C++) or `aiofiles`/`asyncio` (Python)
- Pre-allocate file size with `fallocate` before writing to avoid fragmentation

### Metadata time (`metadata_time_pct` high — metric: metadata_ops)

- Cache `stat()` results; avoid repeated `lstat`/`stat` on the same paths
- Open files once per epoch, not once per sample
- Replace `readdir()` loops with pre-built file-list caches
- For Python: replace `os.path.exists()` in hot loops with `try`/`except`

---

## L2 Software — POSIX Readahead and Library Hints

### Per-process readahead (no sudo)

```c
// Add to source before sequential reads:
posix_fadvise(fd, 0, 0, POSIX_FADV_SEQUENTIAL);
posix_fadvise(fd, 0, 0, POSIX_FADV_WILLNEED);
```

### General I/O library environment variables

```bash
# Reduce glibc arena fragmentation for I/O-heavy apps:
export MALLOC_ARENA_MAX=2

# OMP threading for I/O threads:
export OMP_NUM_THREADS=<physical_core_count>
export MKL_NUM_THREADS=<physical_core_count>

# High-bandwidth NUMA memory (when available):
export HBWMALLOC_POLICY=transparent
```

---

## L3 OS/Filesystem — Lustre Parallel Filesystem (FS_TYPE: lustre ONLY)

**Do NOT apply these on VAST, NFS, GPFS, BeeGFS, or local filesystems.**
`lfs` commands are no-ops or errors on non-Lustre mounts.
`romio_ds_write=disable` is catastrophic on VAST NVMe (4× write regression).

These require no sudo (per-directory settings):

```bash
# Stripe count (N = OST count, capped at 8 for files < 1 GB; 16+ for large):
lfs setstripe -c <N> <WS>/traces/
# Rollback:
lfs setstripe -c 1 <WS>/traces/

# Stripe size (4 MB aligns to ROMIO collective buffer):
lfs setstripe -S 4m <WS>/traces/
# Rollback:
lfs setstripe -S 1m <WS>/traces/

# Per-file stripe before write:
lfs setstripe -c 4 -S 4m <WS>/traces/<file>
```

### Lustre metadata (may require admin)

```bash
# Use DNE (Distributed Namespace) — admin-only:
lfs mkdir -c <N> <dir>
```

---

## L3 OS/Filesystem — Linux Kernel Readahead (FS_TYPE: local_nvme, local_hdd ONLY)

```bash
# Per-device (sudo required):
sudo blockdev --setra <KB> /dev/<dev>
# Default: 128 (64 KB); recommended: 4096–16384 (2–8 MB)
# Rollback:
sudo blockdev --setra 128 /dev/<dev>

# Check current:
blockdev --getra /dev/<dev>
```

Side effect: affects all processes reading from this device.

---

## L3 OS/Filesystem — VM and Page Cache Tuning

```bash
# Flush dirty pages (sudo required):
sudo sysctl -w vm.dirty_ratio=20           # flush at 20% of RAM dirty
sudo sysctl -w vm.dirty_background_ratio=5 # start background flush at 5%
sudo sysctl -w vm.dirty_expire_centisecs=3000   # 30s dirty page lifetime
sudo sysctl -w vm.dirty_writeback_centisecs=500  # flush every 5s

# Read-heavy: reduce page cache reclaim:
sudo sysctl -w vm.vfs_cache_pressure=50    # default 100
```

---

## L3 OS/Filesystem — I/O Scheduler

```bash
# Check current:
cat /sys/block/<dev>/queue/scheduler

# For NVMe (no scheduler overhead needed):
echo none | sudo tee /sys/block/<dev>/queue/scheduler

# For HDD:
echo mq-deadline | sudo tee /sys/block/<dev>/queue/scheduler

# Rollback:
echo <original> | sudo tee /sys/block/<dev>/queue/scheduler
```

---

## dftracer POSIX Configuration

### DFTRACER_DATA_DIR

Must be a **real filesystem path** (not the string `"all"` at the C++ layer):

| Goal | Value |
|------|-------|
| Capture all I/O on any file | `/` or leave empty → use `/` |
| Capture I/O under /tmp | `DFTRACER_DATA_DIR=/tmp` |
| Two data dirs | `DFTRACER_DATA_DIR=/data:/scratch` |

The string `"all"` is only understood by the Python helper layer and causes
`Code 2001` at the C++ runtime.

### `posix_*_ops_slope` bottlenecks

`session_generate_optimization_proposals` does not support slope metrics.
Derive proposals manually:

| Metric | Cause | Fix |
|--------|-------|-----|
| `posix_data_ops_slope` | Bursty data I/O | Increase transfer size (L1); ROMIO hints (L2); stripe tuning (L3) |
| `posix_close_ops_slope` | Bursty close pattern | Stagger close timing (L1); `ind_wr_buffer_size` (L2); client cache (L3) |
| `posix_metadata_ops_slope` | Bursty metadata | Shared file instead of file-per-process (L1); pre-create (L2); DNE (L3) |
| `posix_seek_ops_slope` | Scattered seek+write | Larger transfer size + `romio_cb_write=enable` (see [[software-mpi]]) |

---

## Bottleneck → Optimization Mapping

| Bottleneck | Primary fix | Layer |
|------------|------------|-------|
| `small_io_pct` | Buffer reads/writes | L1 |
| `rand_pct` / `seq_pct` low | Sort access order | L1 |
| `read_time_pct` | Async I/O, O_DIRECT | L1 / L2 |
| `write_time_pct` | Async write, fallocate | L1 / L2 |
| `metadata_time_pct` | Cache stat(), open once | L1 |
| `posix_seek_ops_slope` | ROMIO cb_write, larger -t | L2 |

Full bottleneck → optimization table: [[dftracer-io-optimization]]

---

## Citations

**WisIO (Yildirim et al., ICS 2025)**
URL: https://dl.acm.org/doi/10.1145/3721145.3730395
Covers: small_io, rand/seq (sequentiality), read_time, write_time, metadata_time

**Drishti (Bez et al., PDSW 2022)**
URL: https://ieeexplore.ieee.org/document/10027503
Covers: small-io buffering, metadata caching, sequentiality hints

## Failed Configurations

Entries below were applied during optimization loops and caused regressions or had no effect.
Check this section before proposing any configuration for this workload/software/filesystem.

Format per entry:
  date, app, workload, filesystem, system, bottleneck,
  config_attempted, result, metrics_before, metrics_after, delta,
  root_cause, do_not_use_when

<!-- New failed-config entries are appended below by the optimization loop (Step 8d-iii-FAIL) -->

