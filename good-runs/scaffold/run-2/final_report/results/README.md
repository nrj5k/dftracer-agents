# results.csv — how to read it (and how not to)

Columns: `run, change, epochs, comparison, wall_s, train_s, verdict`

## Only compare within a `comparison` group

Rows are grouped by how they were measured. **Cross-group comparisons are invalid.**

* `PAIRED control` / `PAIRED variant` — launched **concurrently** on separate same-size
  allocations with identical epoch counts. These are the trustworthy deltas.
* `1200-epoch set`, `budget replicate`, `chain` — run at different times. Cluster contention swings
  this workload **2x** (two runs of the identical control config 30 min apart: `train=140.96 s` vs
  `train=293.46 s`), so any delta smaller than that across these rows is noise.

## `wall_s`, not `train_s`, is the objective

`train_s` is the app's `total_train_time`, which **excludes checkpointing**. Compare `opt1_960`:
400.5 s train vs 1052.9 s wall. The missing ~652 s is the checkpoint stall — the thing being
optimized. `train_s` is retained only because the dataloader lever moves it directly.

## Beware "do-less" rows

`opt2_long` has the lowest wall of the 1200-epoch set (843.2 s) and is still **rejected**. It writes
75% fewer checkpoints (78,192 -> 19,692 events). It is not faster; it does less work. Always check
event and byte counts before crediting a wall-clock gain.

`b_omp` has no `wall_s`: its allocation expired mid-run. `train_s` completed and is paired against
`b_ctrl`, so the training-time conclusion stands; the wall figure does not exist.
