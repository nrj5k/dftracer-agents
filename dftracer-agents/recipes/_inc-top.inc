══════════════════════════════════════════════════════
STEP 0 — LOAD LESSONS LEARNED (do this first)
══════════════════════════════════════════════════════

Read past annotation lessons before touching any code:
```bash
cat /workspaces/dftracer-agents/.agents/skills/dftracer-annotation-lessons/SKILL.md \
  2>/dev/null || echo "(no lessons file yet)"
```
Apply every applicable lesson to this file before writing anything.

══════════════════════════════════════════════════════
GENERAL RULES (apply to every language)
══════════════════════════════════════════════════════

ALWAYS ANNOTATE (mandatory — never apply Rule 0 skip):
  • Lifecycle:   *_init, *_final, *_initialize, *_finalize
  • Sync/flush:  *_fsync, *_flush, *_sync
  • File ops:    *_delete, *_rename, *_stat, *_mknod, *_getfilesize
  • Vendor FS:   gpfs_*, beegfs_*, lustre_*, hdfs_*, ceph_*, daos_*

ANNOTATE if the function:
  • Does file or network I/O (open/read/write/close/stat/mmap/send/recv)
  • Moves or transforms significant data (memcpy, checksums, compression)
  • Allocates or frees large buffers
  • Is a driver, benchmark, or orchestration function
  • Contains a data loop across blocks or ranks
  • Takes measurable wall time

SKIP (Rule 0) ONLY if ALL of these are true:
  • Pure getter/setter ≤ 5 lines, returns a single field
  • No I/O, no data movement, no syscall
  → Justify every skip by function name in the report.

COMP= TYPES:
  "io"   → file I/O, backend lifecycle, vendor FS helpers
  "comm" → MPI wrappers, network I/O, distributed FS (S3/HDFS/RADOS/DFS)
  "mem"  → memcpy, large buffer alloc/free, mmap region setup
  "cpu"  → checksums, compression, encryption, hashing

RULE A — Annotate at every abstraction layer (wrapper AND callee).
RULE B — Simplest functions first (single-exit → nested → macros).
RULE D — Default is to annotate; skip requires Rule 0 justification.

══════════════════════════════════════════════════════
STEP 1 — READ THE FILE
══════════════════════════════════════════════════════

session_read_file(run_id="{{ run_id }}", filepath="{{ filepath }}",
  subfolder="annotated")

Record the original line count — the written file must have MORE lines.

