# DFTracer Session — ScaFFold, told as a conversation

**Run ID:** `scaffold/20260705_175606`  ·  **Date:** 2026-07-05
**App:** [LBANN/ScaFFold](https://github.com/LBANN/ScaFFold.git) @ main — 3D U-Net, PyTorch DDP
**Platform:** Tuolumne (AMD MI300A APU, Cray PE, ROCm 7.1.1, Flux)

This report retells the whole session as the exchange between the **user** and the
**agent**, with what was done, the measured impact, and pointers to the concrete
artifacts so every step is traceable. Workspace root (`<WS>`):
`$PROJECT_ROOT/workspaces/scaffold/20260705_175606/`.
App data lives on Lustre: `/p/lustre5/$USER/workspaces/scaffold/`.

---

## Turn 1 — "Run the annotate and optimize pipeline for ScaFFold"

**Agent did:**
- Detected the system (Tuolumne, ROCm) and recognized ScaFFold as a **PyTorch/ROCm**
  DL benchmark → used the `dftracer-ml-annotate` skill.
- Installed the app into a session venv: `torch 2.12.0+rocm7.1` (5.8 GB), patched
  `mpi4py` for Cray MPICH; installed `dftracer 2.0.3.dev50` with **HIP tracing** +
  `pydftracer[dynamo]`.
- Annotated **29 Python files** with dftracer AI/ML regions; applied the known
  ScaFFold pitfall fixes (numba `@njit` un-annotated, 10 `@staticmethod` orders,
  `__len__` de-annotated, hot-loop region helpers stripped).
- Smoke test: **4-rank DDP, 2 epochs — PASSED** (FOM 0.687); 36 traces, 128 K events;
  all AI/ML categories present (`pipeline/compute/data/dataloader/checkpoint`).
- First optimization (smoke scale): num_workers 4→16.

**Impact:** annotation validated end-to-end on GPU; baseline DataLoader bottleneck
identified (`fetch:compute` 3.37×); num_workers change cut POSIX I/O −40% at smoke
scale (full throughput gain needs production scale).

**Artifacts:** `<WS>/annotated/` · `<WS>/install/` · `<WS>/traces/`, `traces_split/`
· `analysis_cat.txt` · lessons in
[`.agents/skills/dftracer-ml-annotation-lessons/SKILL.md`](../../.agents/skills/dftracer-ml-annotation-lessons/SKILL.md)

---

## Turn 2 — "Do an 8-node scale run. Enable the PyTorch profiler. Look for compute & communication bottlenecks. For I/O, raise PFS bandwidth with better layout, and make sure I/O and compute overlap."

**Agent did:**
- **Injected the PyTorch Profiler** into the trainer batch loop (`profile()`
  start/step/stop + `dftracer.python.torch.trace_handler`, gated by
  `DFT_TORCH_PROFILER=1`) → new trace category **`PP`**.
- Regenerated **scale-7** fractals (`point_num = vol_size³/256 = 8192`; np1024 was
  insufficient), set **wide Lustre striping** (the app default was a single OST!),
  confirmed the app already uses the optimized dataset format + `non_blocking=True`.
- Ran **32-rank (8×4 GPU) DDP** at scale 7 with the profiler; analyzed
  compute/comm/I-O.
- Applied an I/O-overlap iteration: **prefetch_factor 2→4 + num_workers 16→24 +
  Lustre stripe 8→16**, re-ran, compared.

**Impact (32-rank scale-7):**

| Dimension | Finding |
|---|---|
| I/O / DataLoader | dominant bottleneck; overlap iteration moved **`fetch:compute` 4.70→3.39 (−28%)**, POSIX −17% |
| Communication | `distributed` all-reduce ≈ **17 ms — negligible**, fully hidden; not worth optimizing |
| Compute | `PP` captured 2.38 M kernel events; `losses` (CE class-weighting) the top non-conv hotspot |

**Artifacts:** `<WS>/traces_s7*`, `traces_s7opt*` · `analysis_s7_cat.txt`,
`analysis_s7opt_cat.txt` · run wrappers `<WS>/tmp/run_benchmark_s7*.sh` ·
data `/p/lustre5/$USER/workspaces/scaffold/s7/`, `s7opt/`.

---

## Turn 3 — "Also generate more data for a deeper training pipeline (ask me first)."

**Agent did:** offered it as an opt-in step; on approval regenerated to **145
instances/class** on wide-striped Lustre and ran **10 epochs, 32 ranks**.

**Impact:** richer sustained profile — **13.06 M events, 11.8 M `PP` kernel events**;
DataLoader fetch (2.68 M ms) ≈ GPU kernel time (2.81 M ms) → I/O stays ~1:1 with
compute and remains the dominant lever; comm still negligible (23.9 s / 10 epochs).

**Artifacts:** `<WS>/traces_s7big*` · `analysis_s7big_cat.txt` ·
`/p/lustre5/$USER/workspaces/scaffold/s7big/`.

---

## Turn 4 — "Optimize the compute hotspot: losses / cross-entropy class-weighting (~150 s), via caching/precompute."

**Agent did:**
- Isolated the hotspot with `dftracer_stats --group-by name` →
  **`_compute_ce_class_weights`**: a once-per-run setup that re-reads ~17 sampled
  masks (25 MB each ≈ 1.25 GB PFS I/O) to produce a tiny deterministic vector.
  Mis-attributed as "compute" but really I/O-bound startup.
- Added a **disk cache** ([`losses.py`](../annotated/ScaFFold/utils/losses.py)) keyed on
  `sha1(dataset_dir | n_train | n_categories | sample_fraction | num_classes)`,
  stored beside the dataset; hit → all ranks load the vector and uniformly skip the
  mask I/O + internal all-reduce; miss → rank 0 writes atomically. Wired at the
  [trainer](../annotated/ScaFFold/utils/trainer.py).
- Verified with a clean A/B (run A writes cache, run B hits it).

**Impact:**

| `_compute_ce_class_weights` | cold (miss) | warm (hit) |
|---|---|---|
| per-call | 1.18 s | **31 ms** |

**≈38× faster (−97%)** on that startup step; ~1.25 GB mask I/O eliminated per run,
compounding across every restart/sweep. Per-batch `F.cross_entropy` left unchanged
(genuine compute).

**Artifacts:** `losses.py`, `trainer.py` diffs · `<WS>/traces_cacheA*`,
`traces_cacheB*` · cache file
`…/s7big/datasets/…/ce_class_weights_60b026c8c1bc7efe.pt`.

---

## Turn 5 — "Yes, do the remaining I/O levers: sharded archives + max PFS bandwidth, cut metadata/open overhead."

**Agent did (three things):**

1. **Removed the per-sample `glob()`** in `BasicDataset.__getitem__` — it did two
   Lustre directory scans per sample just to resolve a filename. `__init__` now
   builds `id→path` dicts once (single `listdir`/dir); `__getitem__` does O(1)
   lookups. *Verified live* (no `.glob(` in the read path).
2. **Wide striping** (`lfs setstripe`). **Finding:** on `/p/lustre5`, `-c -1` falls
   back to the filesystem **default PFL** (1→2→4→8→16→-1), not a flat all-OST stripe;
   use explicit `-c N` for deterministic layout.
3. **Implemented + rigorously tested packed sharded archives** — consolidated the
   168 × 25 MB volumes into one stacked memmap per split
   (`packed/images_training.npy` (N,3,128³) etc.), gated by `DFT_PACKED=1` with a
   `pack_split()` helper and transparent per-file fallback. Ran a **clean
   same-dataset A/B** (packed off vs on, no rebuild, identical config).

**Impact:**
- Glob-removal: correct O(N)→O(1) scalability fix, but **not cleanly isolable at this
  micro-scale** — per-run rebuild I/O dominates POSIX and `opendir` traffic is
  runtime/HIP-dominated, not the dataset glob.
- **Packed archives — NEGATIVE result (not adopted):** wall time **32.9 s → 40.9 s
  (+24 %)**, per-item `__getitem__` 48 ms → 167 ms; POSIX `open`/`opendir` counts
  **identical** in both arms. Root cause: (a) the opens it targeted are
  framework/runtime-dominated, not dataset opens (premise false); (b) DDP-shuffled
  random access into a 4 GB memmap faults 24 MB/access with no locality, slower than
  sequential per-file `np.load`. Kept behind `DFT_PACKED` (default off).

**Artifacts:** [`data_loading.py`](../annotated/ScaFFold/utils/data_loading.py) ·
`<WS>/tmp/pack_dataset.py`, `run_ab_off.sh`, `run_ab_on.sh` ·
`<WS>/traces_aboff*`, `traces_abon*` · packed archives under
`…/s7big/datasets/…/packed/`.

---

## Turn 6 — "While you wait, rewrite the report as this conversation."

**Agent did:** this document.

---

## Turn 7 — "Finish all remaining points."

Closed out the three open levers, plus fixed a latent bug found along the way.

**A — Cache other dataset statistics (like the CE cache).**
Investigated; the only other per-run statistic (unique `mask_values`) is **already
persisted** at build time (`train_unique_mask_vals`/`val_unique_mask_vals` pickles).
Nothing else recomputes I/O-heavy stats → no new cache needed. *Verified.*

**Bug fix (found en route):** the `pack_split` staticmethod had been inserted
*inside* `__init__`, orphaning the `mask_values` load — a latent crash that only
fires on the checkpoint-save path (which reads `train_set.mask_values`), which is
why earlier checkpoint-disabled runs never hit it. Moved the load back into
`__init__` and `pack_split` to class scope.

**B — Enable + verify async/overlapped checkpoint.**
The app already has async checkpointing (`ThreadPoolExecutor` offload), but
`async_save` wasn't even read by `Config` and `checkpoint_interval` defaulted to
`-1`. Added `async_save` to the config, enabled `checkpoint_interval=1`, ran with
async and with sync.
- ✅ Works: "Async checkpoint offloaded to background thread"; `checkpoint_best/last.pth`
  written each epoch.
- **Finding:** async 32.92 s vs **sync 32.40 s** vs no-checkpoint 32.85 s — all within
  run-to-run noise. Checkpoint I/O is **not a bottleneck** for this small U-Net
  (small state_dict, wide-striped PFS). The async path matters for large models,
  not here — same class of finding as communication.

**C — Controlled A/B to quantify the glob removal.**
Gated the old per-item `glob()` behind `DFT_USE_GLOB=1` and ran it against the dict
path on the same prebuilt dataset (only that code path differs).

| Metric | glob path | dict path | Δ |
|---|---|---|---|
| **`opendir` syscalls** | 21,168 | **15,408** | **−27%** |
| wall time (4 epochs) | 32.44 s | 32.85 s | noise |

The glob→dict change is a **real, now-isolated** metadata reduction (−27% `opendir`);
it's wall-time-neutral at this scale because per-sample time is dominated by the
25 MB volume read + GPU compute, not `opendir`. It pays off on metadata-constrained
filesystems / larger directories. (The earlier "not isolable" was because the packed
A/B compared two *dict* paths — no glob to remove.)

**Artifacts:** `annotated/ScaFFold/utils/{config_utils,data_loading}.py` ·
`<WS>/traces_ckpt*`, `traces_ckptsync*`, `traces_globon*` ·
`<WS>/tmp/run_{ckpt,ckptsync,globon,globoff}.sh` · checkpoints under
`…/s7big/runs/benchmark_*/checkpoints/`.

---

## Net impact scoreboard

| Change | Layer | Status | Measured impact |
|---|---|---|---|
| dftracer AI/ML annotation (29 files) | instrumentation | ✅ shipped | full pipeline/compute/data/dataloader/comm/checkpoint + `PP` visibility |
| PyTorch Profiler injection | instrumentation | ✅ shipped | 11.8 M kernel events (`PP`) for compute attribution |
| num_workers 4→16→24, prefetch 2→4 | L1 app | ✅ shipped | `fetch:compute` 4.70→3.39 (−28%), POSIX −17% |
| Lustre wide striping (c8/c16) | L3 fs | ✅ shipped | app default was 1 OST → wide; PFL caveat documented |
| **CE class-weight disk cache** | L1 app | ✅ shipped | **1.18 s → 31 ms (−97%)**, −1.25 GB I/O/run |
| Per-sample glob removal | L1 app | ✅ shipped | O(N)→O(1) metadata; scales with dir size |
| Packed stacked-memmap archives | L1/L3 layout | ⛔ tested, rejected | **+24% slower** under random access — negative result |
| Per-sample glob removal (isolated A/B) | L1 app | ✅ shipped | **−27% `opendir`** (21,168→15,408); wall-time-neutral at this scale |
| Async/overlapped checkpoint | L1 app | ✅ enabled+verified | works; **not a bottleneck** here (small model) — noise vs sync |
| Other dataset-stat caching | L1 app | ✅ already cached | `mask_values` persisted at build time — no new work |
| Communication tuning | L2 | ⏸ not needed | all-reduce negligible (17 ms) |

**Headline:** ScaFFold fully instrumented and profiled at 32-rank scale-7; the
durable wins are the **CE class-weight cache (−97% on that step)**, the
**DataLoader-overlap tuning (−28% fetch:compute)**, **wide striping**, and the
**glob removal**. Two findings that save future effort: **communication is not a
bottleneck** for this workload, and **archive consolidation regresses** it.

---

## Where everything lives

| Artifact | Path |
|---|---|
| Annotated + optimized source | `<WS>/annotated/ScaFFold/` |
| Session venv (torch + dftracer) | `<WS>/install/` |
| Code changes | `annotated/ScaFFold/utils/{losses,trainer,data_loading}.py` |
| Traces (per run) | `<WS>/traces{,_s7,_s7opt,_s7big,_s7max,_aboff,_abon,_cacheA,_cacheB}/` |
| Split traces + analyses | `<WS>/traces_*_split/`, `<WS>/analysis_*_cat.txt` |
| Run wrappers | `<WS>/tmp/run_*.sh`, `pack_dataset.py` |
| App data (fractals/datasets, striped) | `/p/lustre5/$USER/workspaces/scaffold/{fractals,s7,s7opt,s7big,s7max}/` |
| Packed archives | `…/s7big/datasets/…/packed/` |
| **Final reproducible artifacts** | [`<WS>/final/`](.) — [`scaffold_optimized.patch`](scaffold_optimized.patch) (29 files, applies from repo root, verified apply+compile) + [`scripts/`](scripts/) + [`configs/`](configs/) + [`README`](README.md) |
| Institutional lessons (persisted) | [`.agents/skills/dftracer-ml-annotation-lessons/SKILL.md`](../../.agents/skills/dftracer-ml-annotation-lessons/SKILL.md) |
| This report | `<WS>/session_report.md` |

## Open levers — all closed
- ✅ Other dataset-statistic caching → `mask_values` already persisted at build time.
- ✅ Async/overlapped checkpoint → enabled + verified; not a bottleneck for this model size.
- ✅ Glob-removal quantified via controlled A/B → −27% `opendir`, wall-time-neutral here.

Genuinely remaining (would need larger models/scale to matter): async checkpoint
for large state_dicts; glob/metadata gains on metadata-constrained filesystems.
