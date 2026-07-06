# ScaFFold — final optimized artifacts

Run `scaffold/20260705_175606` · Tuolumne (AMD MI300A, ROCm 7.1.1, Cray PE, Flux).
This folder is the reproducible end-state: one patch that turns a pristine
[LBANN/ScaFFold](https://github.com/LBANN/ScaFFold.git) checkout into the fully
dftracer-annotated **and** optimized version, plus every install/run script and
config used. See `../session_report.md` for the full narrative + measurements.

## Contents

| Path | What it is |
|---|---|
| `session_report.md` | Full session report (conversation narrative + all measurements). |
| `scaffold_optimized.patch` | Unified diff over `ScaFFold/` (29 `.py` files): dftracer AI/ML annotations + all optimizations. Applies from the repo root. |
| `scripts/install_app.sh` | ROCm torch + mpi4py install into the session venv (`.[rocm]`, Cray MPICH patchelf). |
| `scripts/install_dftracer.sh` | dftracer (HIP tracing + MPI) + `pydftracer[dynamo]` install. |
| `scripts/run_genfractals.sh` | Generate fractal instances (regenerate per `problem_scale`; np = vol_size³/256). |
| `scripts/run_benchmark_dftracer.sh` | 1-node smoke run with dftracer tracing. |
| `scripts/run_benchmark_s7*.sh` | 8-node (32-rank) scale-7 runs (baseline / opt / bigger-data). |
| `scripts/run_ab_{off,on}.sh`, `run_ckpt.sh` | A/B + checkpoint experiment wrappers. |
| `scripts/pack_dataset.py` | Builds packed stacked-memmap archives (kept for reference; **not recommended** — see below). |
| `configs/*.yml` | Smoke / scale-7 / opt / bigger / checkpoint configs. |

## What the patch changes (impact-ranked)

1. **dftracer AI/ML annotations** across 29 files — `pipeline/compute/data/dataloader/
   comm/checkpoint` regions + generic `@_dlp.log`; entry `initialize_log`/`finalize`
   in `worker.py`. Includes the required pitfall fixes (numba `@njit` left
   un-annotated, `@staticmethod` order, `__len__` not annotated, hot-loop region
   helpers stripped).
2. **`losses.py` — CE class-weight disk cache**: `_compute_ce_class_weights` result
   is memoised beside the dataset. **1.18 s → 31 ms (−97 %)** startup, ~1.25 GB mask
   I/O eliminated per run.
3. **`trainer.py` — DataLoader overlap**: `prefetch_factor` is now a configurable knob
   (default **4**); combined with `dataloader_num_workers` (use 24) it cut
   `fetch:compute` **4.70 → 3.39 (−28 %)**.
4. **`trainer.py` — PyTorch Profiler** (opt-in `DFT_TORCH_PROFILER=1`): kernel-level
   events under trace category `PP`.
5. **`data_loading.py` — per-sample glob removed**: `__init__` builds `id→path` dicts
   once; `__getitem__` does O(1) lookups → **−27 % `opendir`** syscalls.
6. **`config_utils.py` — `async_save`** config key added → enables the existing
   background-thread checkpoint offload.

## Environment toggles added by the patch

| Env / config | Default | Effect |
|---|---|---|
| `DFT_TORCH_PROFILER=1` | off | PyTorch Profiler → `PP` category |
| `async_save: true` (yaml) + `checkpoint_interval>0` | off | overlapped checkpoint |
| `DFT_PACKED=1` | off | packed memmap archives — **leave off** (A/B: +24 % slower, random access) |
| `DFT_USE_GLOB=1` | off | reverts to old per-item glob — for A/B only |

## How to reproduce

```bash
# 1. clone + apply the patch
git clone https://github.com/LBANN/ScaFFold.git && cd ScaFFold
git apply /path/to/final/scaffold_optimized.patch     # verified: applies cleanly + compiles

# 2. install (edit WS path inside the scripts first)
bash final/scripts/install_app.sh          # ROCm torch + mpi4py
bash final/scripts/install_dftracer.sh     # dftracer HIP + pydftracer[dynamo]

# 3. generate data for the target scale (scale 7 -> np8192 required)
flux proxy <JOBID> flux run -N1 -n1 -c96 -g1 bash final/scripts/run_genfractals.sh

# 4. run 32-rank scale-7 with tracing + profiler
flux proxy <JOBID> bash final/scripts/run_benchmark_s7.sh
```

## Recommended production config (this study's best)

`dataloader_num_workers: 24`, `prefetch_factor: 4` (default in patch),
`checkpoint_interval: 1` + `async_save: true`, Lustre `lfs setstripe -c 16 -S 4M`
on the dataset dir **before** the dataset is built. Do **not** enable `DFT_PACKED`.

## Non-bottlenecks confirmed (don't spend effort here)

- **Communication** — DDP all-reduce ≈ 17 ms, fully hidden behind compute.
- **Checkpoint I/O** — small U-Net state_dict; async vs sync within noise at this scale.
