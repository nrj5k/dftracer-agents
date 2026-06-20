## I/O Optimization Key Papers

Two authoritative references for bottleneck diagnosis and optimization strategies.
Consult these whenever suggesting optimizations in the optimize.yaml recipe pipeline.

---

### WisIO — Bottleneck Detection & Multi-Perspective Views

**Full citation:**
Yildirim, Izzet, Hariharan Devarajan, Anthony Kougkas, Xian-He Sun, and Kathryn Mohror.
"WisIO: Automated I/O Bottleneck Detection with Multi-Perspective Views for HPC Workflows."
In *Proceedings of the 39th ACM International Conference on Supercomputing*, pp. 749–763. 2025.

**When to use:** Map diagnosed metric names (small_io_pct, rand_pct, read_time_pct, etc.)
to their root cause and to the appropriate fix at each of the three optimization levels.

**Quick mapping:**
- `small_io_pct` high      → aggregate (L1 buffer, L2 collective I/O, L3 stripe size)
- `rand_pct` high          → reorder / prefetch (L1 sort, L2 POSIX_FADV, L3 readahead)
- `read_time_pct` high     → concurrency (L1 async, L2 cb_nodes, L3 setra)
- `metadata_time_pct` high → reduce ops (L1 cache, L2 HDF5 hints, L3 MDT striping)
- `fetch_pressure` high    → pipeline (L1 DataLoader workers, L2 prefetch_factor)
- `epoch_straggler` high   → balance (L1 sort by size, L2 persistent_workers)

---

### Drishti — User-Facing Optimization Journey

**Full citation:**
Bez, Jean Luca, Hammad Ather, and Suren Byna.
"Drishti: Guiding end-users in the I/O optimization journey."
In *2022 IEEE/ACM International Parallel Data Systems Workshop (PDSW)*, pp. 1–6. IEEE, 2022.

**When to use:** Structure optimization proposals for the three-level model.
Drishti organizes suggestions into application / library / system tiers —
directly matching L1 / L2 / L3 in the optimize recipe sub-recipes.

**Quick per-level guidance:**
- L1 (app):    buffer reads, batch writes, open files once, sort data access order
- L2 (sw):     ROMIO hints, HDF5 chunking/collective metadata, DataLoader config
- L3 (system): Lustre stripe count/size, blockdev readahead, vm.dirty tuning

---

### How to Use These in Recipes

In **optimize.yaml** Step 3 (literature search): always reference both papers before
searching arXiv so the agent has pre-built domain knowledge.

In **optimize-l*.yaml** Step 2 (build proposals): the "Literature:" field of each
PROPOSAL block should cite either WisIO or Drishti by name with the specific
insight used (e.g. "WisIO: small-io category → L2 collective buffer").

In **pipeline.yaml** Step 7f–7g: when interpreting diagnosis.json output, use
WisIO's category taxonomy to summarize bottleneck types in the final report.
