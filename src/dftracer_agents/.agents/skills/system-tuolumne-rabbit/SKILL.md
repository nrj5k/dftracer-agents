---
name: system-tuolumne-rabbit
description: Rabbit near-node-flash storage accelerators on Tuolumne — DW directives via Flux -S "#DW ...", picking SHM vs XFS vs GFS2 vs Lustre by data-sharing scope, and how to use them as an L3 filesystem optimization to accelerate data access.
---

# Rabbit Near-Node Flash Storage Accelerators (Tuolumne)

Tuolumne compute nodes have **Rabbit** node-local accelerators: NVMe flash
devices physically close to the compute nodes that can be provisioned on demand
as a fast scratch filesystem for a job. They front the slow path (Lustre over
the network) with local flash, so any workload that reads/writes the same data
repeatedly, or stages a dataset once and reuses it, can be accelerated by
placing that data on a Rabbit allocation instead of network Lustre.

Rabbit storage is requested through **DataWarp (DW) directives** passed to Flux
with `-S "#DW ..."`. The directive provisions the storage, and the mounted path
is exposed to the job through the `$DW_JOB_STRIPED` / per-allocation environment
variables (see the storage-profile docs below). This is a **system/filesystem
(L3) optimization** — no application source change, only where data lives and
how the job is launched.

Reference (DW directives + storage profiles):
https://nearnodeflash.github.io/v0.1.27/guides/storage-profiles/readme/#setting-the-default-profile

Load alongside [[system-tuolumne]] (base system config) and use with
[[dftracer-io-optimization]] / [[software-posix]] when a trace shows an I/O
bottleneck that a faster near-node scratch tier would relieve.

---

## Decision guide: pick the storage tier by sharing scope

Choose the **smallest / most local** tier that fits the data-sharing pattern.
Locality is cheapest; only step up when the data is too large or must be shared
more widely.

| Sharing scope | Data size | Tier | How |
| --- | --- | --- | --- |
| Processes on **one node**, exchanging data locally | ≤ ~20% of node memory | **Shared memory (SHM)** | Use `/dev/shm` (tmpfs) — no Rabbit/DW needed |
| Processes on **one node** | > 20% of node memory, up to **1 TB / compute node** | **XFS** on Rabbit | `#DW jobdw type=xfs ...` |
| Processes across **up to 16 nodes on one chassis** | fits one Rabbit's flash | **GFS2** (shared) on Rabbit | `--coral2-chassis=1` + `#DW jobdw type=gfs2 ... access_mode=striped` |
| Processes across **> 16 nodes** / whole job | large / cluster-wide | **Lustre** on Rabbit | `#DW jobdw type=lustre ...` |

Rules of thumb:

- **SHM first.** For processes exchanging data locally, prefer shared memory
  (`/dev/shm`) as long as the total shared through SHM stays under **~20% of the
  node's memory**. This is the fastest and needs no allocation.
- **XFS when SHM is too small.** If the locally-shared data exceeds ~20% of node
  memory, allocate **XFS** on the Rabbit to share data between processes on a
  node. You can allocate up to **1 TB per compute node**.
- **GFS2 to share across a chassis (≤16 nodes).** To share data between multiple
  compute nodes, allocate **one Rabbit node across the compute nodes** with
  `--coral2-chassis=1` (allocates a single Rabbit on up to 16 compute nodes of a
  chassis), then use **GFS2 with shared/striped access** so those nodes see one
  filesystem.
- **Lustre for >16 nodes.** To share data across **more than 16 nodes**, use
  **Lustre on Rabbits** (spans multiple Rabbit nodes).

---

## Coordinate the allocation with the user (REQUIRED)

Rabbit storage is provisioned at **allocation time** — the `#DW` directives and
`--coral2-chassis` are flags on `flux alloc`, not on `flux run`. The agent
**cannot** create or change the allocation itself. Workflow:

1. Decide the tier and flags from the trace (table above).
2. **Ask the user to run the `flux alloc` command** with the exact
   `-S "#DW jobdw ..."` (and `--coral2-chassis=1` for GFS2) flags you need, and
   to report back the JOBID. Give them the exact command to paste.
3. Once the user has allocated and shares the JOBID, use
   `flux proxy <JOBID> bash <ws>/tmp/<script>.sh` to stage data and run tests
   against the provisioned `$DW_JOB_*` mount.

Never assume an existing allocation already has the Rabbit flags — if it does
not, the DW mount will not exist and you must ask the user to re-allocate.

## Flux launch patterns

DW directives are passed to the allocation with `-S "#DW ..."`. Always wire the
run step through a wrapper script (see the flux-proxy wrapper rule in
[[system-tuolumne]] and [[flux-alloc]]); the mounted path goes into the wrapper,
not inline env.

### XFS — per-node local scratch (single node, up to 1 TB/node)

```bash
flux alloc -N 1 -q <QUEUE> -t <TIME> \
  -S "#DW jobdw type=xfs capacity=1TiB name=scratch"
# Inside the job, the provisioned mount is exposed via the DW env var
# (e.g. $DW_JOB_scratch). Point the app's data dir at it:
export DATA_DIR="$DW_JOB_scratch"
```

### GFS2 — shared across up to 16 nodes on one chassis

```bash
flux alloc -N 16 --coral2-chassis=1 -q <QUEUE> -t <TIME> \
  -S "#DW jobdw type=gfs2 capacity=4TiB name=shared access_mode=striped"
# All 16 compute nodes see one shared filesystem at $DW_JOB_shared.
export DATA_DIR="$DW_JOB_shared"
```

`--coral2-chassis=1` pins the allocation so a single Rabbit node backs all (≤16)
compute nodes in the chassis — required for GFS2 shared access.

### Lustre on Rabbits — share across >16 nodes

```bash
flux alloc -N 64 -q <QUEUE> -t <TIME> \
  -S "#DW jobdw type=lustre capacity=16TiB name=big access_mode=striped"
export DATA_DIR="$DW_JOB_big"
```

For a full alloc → proxy → run workflow and the mandatory wrapper-script
pattern, see [[flux-alloc]].

---

## Using Rabbit as an L3 optimization

Treat Rabbit as an **L3 (filesystem/system) tier** in the optimization pipeline
(see [[dftracer-io-optimization]] L3 strategies):

1. From the dftracer trace, identify the data-sharing scope and reuse pattern
   (single-node local exchange, chassis-wide shared file, cluster-wide dataset).
2. Pick the tier from the table above (SHM → XFS → GFS2 → Lustre).
3. **Stage input data onto the Rabbit mount once**, run the app against
   `$DW_JOB_*`, and (for outputs that must persist) copy results back to Lustre
   at job end — Rabbit allocations are ephemeral and torn down with the job.
4. Keep dftracer **traces** in the session workspace (`workspaces/<session>/traces/`),
   not on the Rabbit mount (see trace-placement rule in [[system-tuolumne]]).

**When NOT to use Rabbit:** if the bottleneck is metadata- or small-I/O-bound at
the application layer, fix L1/L2 first — a faster tier hides but does not remove
an app-level inefficiency. Also skip Rabbit if the dataset already fits in SHM.

---

## Permissions

This skill uses:

- **Bash (in `workspaces/<session>/...` only):** `flux` (with `-S "#DW ..."` and
  `--coral2-chassis`), `cp`/`rsync` to stage data to/from `$DW_JOB_*`, `df`, `stat`.
- **Write / Edit:** `workspaces/<session>/*` (wrapper scripts under
  `workspaces/<session>/tmp/`); traces stay under `workspaces/<session>/traces/`.

Never `sudo`; Rabbit provisioning is unprivileged via DW directives only.
