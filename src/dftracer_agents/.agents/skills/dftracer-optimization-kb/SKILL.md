---
name: dftracer-optimization-kb
description: >
  Cross-session, citation-backed knowledge base of every MEASURED dftracer
  optimization, partitioned into system-centric (L3), software-centric (L2),
  and workload-centric (L1) findings. Load this FIRST in any optimization
  loop to avoid re-deriving what is already known.
---

# dftracer optimization knowledge base

**Step 1 of every optimization loop is recall, not proposal.** Call
`opt_kb_lookup(system=..., workload=..., software=..., scope=...)` before
generating any proposal, and cite prior results in the proposal table.

Scopes and what they transfer to:

| Scope | Level | Transfers to | File |
| --- | --- | --- | --- |
| system | L3 | any workload **on that system** | [system.md](system.md) |
| software | L2 | any workload **linking that software**, any system | [software.md](software.md) |
| workload | L1 | that application, **any system** | [workload.md](workload.md) |

Recorded: 2 system, 1 software, 7 workload entries.

## Rules

1. Record only **measured** results — `metric`, `before`, `after` are required.
   Record failures too: knowing a lever did nothing is a result.
2. Every entry carries a **citation**: paper (preferred) > official docs > web.
   `session:<run_id>` marks a result measured in-house, never external evidence.
3. Apply optimizations **one at a time** and measure each, or the attribution
   is worthless.

## DL run length: ASK for a time budget, then FIX the epoch count

Never guess a run length, and never let variants run for different amounts of work.

**Default budget for this project: 10 minutes of training per run** (confirm with the user each session).

1. **Ask the user for the time budget** (e.g. "10 minutes of training per run"). Do not assume.
2. **Calibrate:** run a short probe, measure seconds/epoch on the BASELINE config.
   `epochs = floor(budget_seconds / seconds_per_epoch)`.
3. **Fix that epoch count for every variant** (baseline and all optimizations). Comparisons
   must hold work constant; a variant that runs fewer epochs is "winning" by doing less.
4. Also fix `problem_scale`, dataset size, and `checkpoint_interval` across variants unless the
   knob under test IS one of them — and if it is, say so, because it changes total work.
5. Take at least one replicate of the baseline and of the best variant, and report deltas against
   that noise band. At a few seconds of training, checkpoint/collective effects are unmeasurable.

Watch for early-exit knobs (e.g. `target_dice`) that can end a run before the fixed epoch count
and silently break the equal-work assumption.

### "Do-less" levers are not speedups
Raising `checkpoint_interval`, cutting epochs, or shrinking the dataset reduce work. Any wall-clock
gain must be checked against total bytes / data volume before it is credited as a speedup.

## Always replicate the BASELINE, not just the winner

A single baseline run can silently set the entire effect size. On a 32-rank PyTorch workload the
baseline's per-epoch cost varied 26.8% across two runs (0.4927 vs 0.6249 s/epoch) while the
optimized variant varied 0.6% — so the "same" optimization measured anywhere from -15.3% to
-33.7% depending on which baseline it was compared against.

Rules:
- Replicate the baseline at least once. Report the improvement as a range against its noise band.
- A high-variance baseline and a low-variance variant is itself evidence: the variant has removed
  a dependence on a contended shared resource (here, filesystem I/O via dataloader prefetch).
- Normalize to per-epoch (or per-unit-work) before comparing runs of different lengths.

## Optimization axes for deep-learning workloads (sweep in this order)

1. **Overlap compute and I/O.** `dataloader_num_workers>0`, `persistent_workers=True`,
   `prefetch_factor`, async checkpointing. Cheapest, usually the biggest win.
   (Mohan et al., *Analyzing and Mitigating Data Stalls in DNN Training*, VLDB 2021,
   https://arxiv.org/abs/2007.06775)
2. **Pinned memory + CPU core affinity — as ONE change.** `pin_memory=True` only pays off when
   each rank is bound to all cores of its GPU's die. Pinned to a single core, the copy thread
   contends with dataloader workers and the benefit inverts. On an APU (e.g. AMD MI300A) CPU and
   GPU share the die and HBM, so affinity determines memory locality, not just scheduling.
   (PyTorch memory-pinning docs, https://docs.pytorch.org/docs/stable/data.html#memory-pinning)
3. **File layout: minimize the NUMBER of reads and metadata calls.** Per-sample small files cause
   an open/stat/close storm on the metadata server. Shard into few large files with an index.
   (Devarajan et al., *DLIO: A Data-Centric Benchmark for Scientific Deep Learning Applications*,
   CCGrid 2021, https://ieeexplore.ieee.org/document/9499416)
4. **System utilization.** PFS bandwidth (striping; Data-on-MDT for small files) and memory
   bandwidth. Establish whether you are bandwidth- or compute-bound before tuning kernels.
   (Williams et al., *Roofline: An Insightful Visual Performance Model*, CACM 2009,
   https://doi.org/10.1145/1498765.1498785)
5. **Compute last.** Mixed precision, kernel/library tuning, then algorithmic change.

**Async checkpointing** is only a win when checkpoint write time is a real fraction of epoch
time — verify first. (Mohan et al., *CheckFreq*, USENIX FAST 2021,
https://www.usenix.org/conference/fast21/presentation/mohan; Eisenman et al., *Check-N-Run*,
USENIX NSDI 2022, https://www.usenix.org/conference/nsdi22/presentation/eisenman)

**Guard rail.** A wall-clock gain from writing fewer checkpoints, reading less data, or running
fewer epochs is *doing less*, not going faster. Check event and byte counts before crediting it.

## Run the CONTROL concurrently, or your numbers are contention

On a shared cluster, comparing a variant run now against a baseline run an hour ago measures the
cluster's mood, not your change. Observed on a 32-rank PyTorch workload in one session:

| trial | measured train_s | truth |
| --- | --- | --- |
| `q1_hardlink` (vs an older baseline) | 703.5 | looked like a **+26% regression** |
| `q1_rep` (vs a CONCURRENT control) | 393.7 | actually a **-2.5% win** |

The same artifact made an affinity change look like +22.5%. Nothing about the code changed.

**Rule:** launch the variant and its control at the same time, on separate allocations of the
same size, with identical epoch counts. Report the paired delta. If you only have one allocation,
interleave A/B/A and quote the spread. Never quote a delta computed against a run from a
different hour.

Corollary: a baseline whose per-epoch cost varies 27% run-to-run (see [[workload-scaffold]]) can
manufacture or erase any effect smaller than that.

## Measure WALL CLOCK, not the app's own metric

An application's `total_train_time`/FOM often excludes exactly the phase you are optimizing.
Here it excluded checkpointing: 400.5 s "train" vs 1052.9 s wall, with the missing ~652 s being
the checkpoint stall. Every percentage computed on the app metric was blind to the target.
Always cross-check with `time` on the whole run.
