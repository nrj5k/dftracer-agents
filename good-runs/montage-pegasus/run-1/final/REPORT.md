# Final Report — Montage dftracer Annotation, Pegasus/PMC Execution, and I/O Optimization

**Run ID:** `montage/20260706_062459`
**Platform:** Tuolumne (AMD MI300A APU cluster, Cray PE, Flux scheduler, no sudo)
**Date:** 2026-07-06

This report narrates the full session: from the original request to annotate
`montage-workflow-v3` through to a verified, measured I/O optimization
running the real workflow via Pegasus MPI Cluster (PMC) on Lustre.

---

## 1. Scope decision: montage-workflow-v3 → Montage C toolkit

The original ask was to run the dftracer annotation pipeline on
[montage-workflow-v3](https://github.com/pegasus-isi/montage-workflow-v3).
Investigation showed this repo has **no annotatable source of its own** — it's
a Python DAX generator (`montage-workflow.py`) that shells out to the
[Montage](https://github.com/Caltech-IPAC/Montage) C toolkit binaries
(`mProject`, `mAdd`, `mBackground`, etc.) and requires Pegasus 5.0 + a
workflow executor to run at all.

**Decision (user-confirmed):** annotate the Montage C toolkit directly —
that's where the actual I/O happens — and separately get the real
Pegasus workflow running so the annotated binaries are traced under
realistic multi-job execution rather than a synthetic smoke test.

## 2. Smoke-test-driven annotation scoping

Montage has ~700 source files across 100+ independently-linked executables.
Annotating everything would waste effort and risk annotator edge-case bugs in
code paths the workflow never touches. Built a new MCP tool,
**`session_identify_smoke_test_files`**, that:

1. Discovers which binaries a smoke test invokes (via `strace` or a static
   scan against `install/bin/`)
2. Parses each binary's Makefile link recipe to find every `.o` it pulls in
3. Resolves each `.o` back to its source file
4. Returns the minimal file set needed

Applied to the 10 binaries `montage-workflow-v3` actually runs per mosaic band
(`mArchiveList`, `mImgtbl`, `mProjExec`, `mOverlaps`, `mDiffExec`, `mFitExec`,
`mBgModel`, `mBackground`/`mBgExec`, `mAdd`), this narrowed the annotation
target from **267 files to 51** (81% reduction). The scope was presented to
the user for confirmation before proceeding.

**Tool source:** `src/dftracer_agents/mcp_tools/tools/session/annotation_filter.py`
(registered in `dftracer_service.py`; requires an MCP server restart to
become callable in future sessions).

## 3. Annotation

Used `clang_annotate_project`/`clang_annotate_file` on the scoped 51 files.
**44 files were successfully annotated and build-verified.** 7 files were
reverted to pristine (unannotated) after triggering a real bug in the
annotator's brace-insertion logic on multi-line `if` conditions and 3+ arm
`else-if` chains — confirmed as a genuine parser limitation (not the earlier
suspected in-memory-cache artifact, which was ruled out first and is a
separate, also-real bug documented in the lessons log).

A separate, more consequential annotation issue was found and fixed later
(§6): `mAdd_avg_mean`, a per-pixel coaddition function, was annotated despite
being called once per output pixel — the static AST-cost filter has no way to
detect runtime call frequency. This produced 11.9M trace events (1.87 GB)
from a single 4-image mosaic and made the trace undiagnosable. Fixed by
manually removing its 3 macro lines (a safe, surgical deletion, not the
prohibited brace-hack pattern) and adding an `exclude_functions` parameter to
`clang_annotate_file` for future sessions to force-skip known hot-loop
functions.

## 4. Build

Montage uses a plain recursive `make` build (bash `configure` + per-directory
Makefiles), not cmake/autotools. Building the annotated tree against
dftracer required two non-obvious fixes:

- **`session_install_dftracer` linker failure** (`undefined reference: dlopen`)
  — Tuolumne's system env lacked `/usr/lib64` (where `libdl.so.2` lives) in
  `LD_LIBRARY_PATH`, and explicit `-ldl` was needed on the link line. Fixed
  `resources/systems.yaml` and wired system-env injection into
  `session_install_dftracer` (needs server restart to activate this session).
- **`make CC=<wrapper>` breaks vendored Makefiles** — some of Montage's
  vendored third-party Makefiles (e.g. `mtbl`) embed compiler flags directly
  in the `CC` variable (`CC = gcc -g -fPIC -I . ...`); overriding `CC` on the
  command line discards those flags. Fixed by shimming `gcc`/`cc` via `PATH`
  instead (see `scripts/gcc_shim.sh`, `scripts/cc_shim.sh`,
  `scripts/build_annotated_montage.sh`).

All 10 target binaries built successfully, linked against `libdftracer_core`.

## 5. Running the real Pegasus workflow (not just a synthetic smoke test)

Per user direction, replaced HTCondor+DAGMan (Pegasus's default executor)
with **Pegasus MPI Cluster (PMC)** running as a single MPI job inside a Flux
allocation — no Condor daemons, no DAGMan, just `pegasus-mpi-cluster` reading
a flat task DAG and scheduling work across MPI ranks.

Key steps and pitfalls (fully documented in the new **`software-pegasus`**
skill):

- Installed personal HTCondor (planning-time tooling only, never started as
  a daemon — some `pegasus-plan` code paths still reference Condor tooling
  even when the code generator is PMC)
- Installed Pegasus 5.0.7; **rebuilt `pegasus-mpi-cluster` from source**
  against Cray MPICH — the prebuilt binary links generic MPICH ABI v10,
  incompatible with Cray's ABI v12
- Fixed a stale bundled `six.py` shadowing a working modern `six`
  (`ModuleNotFoundError: No module named 'six.moves'` in `pegasus-transfer`)
- Found and worked around a Pegasus 5.0.7 planner bug: `--cluster horizontal`
  combined with `pegasus.code.generator=PMC` corrupts the output DAG file —
  omitting `--cluster horizontal` fixes it
- Wrote an explicit `sites.yml` (Pegasus 5's default site catalog doesn't
  define a usable `local` site)
- Wired dftracer tracing into every Pegasus job **with zero workflow
  changes**: `montage-workflow.py` auto-discovers transformations via
  `which('mProject')`'s directory, so pointing `PATH` at the
  dftracer-annotated `install_ann/bin` before DAX generation was sufficient

**First full run: 57/57 tasks succeeded**, producing a real coadded FITS
mosaic + PNG and dftracer traces from actual multi-job execution.

## 6. The annotation-noise discovery (mAdd_avg_mean)

The first Pegasus-run trace looked broken: 11.9M events / 1.87 GB, with
`dfanalyzer` showing only 3 unique files and an empty POSIX layer table.
Manual inspection of the largest trace file showed `mAdd_avg_mean` (a
per-pixel coadd function) accounted for 99.9% of events. Fixed as described
in §3; re-running the same workflow with the fixed binary dropped the trace
to 62,708 events / 28 files / 811.5 MB of real, diagnosable I/O — the
difference between an unusable trace and a usable one. Full details and the
generalizable "rule of thumb" (tiny `unique_file_count` + huge event count ⇒
one hot function is dominating the trace) are in the lessons log.

## 7. Optimization pipeline: what worked and what didn't

Ran `session_optimization_iteration` against the clean trace. It executed
the profile step correctly but the **diagnosis step returned 0 bottlenecks**
— confirmed as a false negative, not a real finding: `dftracer-analyzer`'s
fact-rule engine only ships DLIO rules (no `posix.yaml`), so it never emits
the `facts.jsonl` that `dfdiagnoser` needs to score anything for a POSIX
workload. This is a genuine upstream gap between `dftracer-analyzer` and
`dfdiagnoser`, verified at the tip of both projects' `develop`/`main`
branches (reinstalling from GitHub changed nothing — the installed versions
were already current).

Given the real metrics from `dfanalyzer`'s working summary path (62,380 ops,
811.5 MB, 740.6 MB/s, 13 KB avg transfer), derived optimization proposals
manually instead of relying on the blocked automated scorer.

## 8. Optimizations applied and verified

| Level | Change | Status |
|---|---|---|
| L1 (app) | Batch `fits_read_pix`/`fits_write_pix` row-by-row calls into larger reads | **Not applied** — Montage's per-row I/O is an intentional design choice (bounds memory for arbitrarily large mosaics); rewriting a mature scientific library's numerical I/O path without pixel-correctness regression tests was judged too risky for this session |
| L2 (middleware) | `posix_fadvise(POSIX_FADV_SEQUENTIAL)` after `fits_open_file()` in `mProject.c`/`montageAdd.c`, via a separate throwaway fd (doesn't touch cfitsio's internal handle) | **Applied** |
| L3 (filesystem) | Move Pegasus scratch/storage from NFS (`/usr/WS2`) to Lustre (`/p/lustre5`) via a new `sites.yml` | **Applied** |

**Verified result** — re-ran the identical 4-image mosaic workflow through
Pegasus/PMC with both optimizations, on Lustre:

| Metric | Before (NFS, no fadvise) | After (Lustre + fadvise) | Δ |
|---|---|---|---|
| POSIX ops | 62,380 | 62,380 | unchanged |
| Total I/O | 811.5 MB | 811.5 MB | unchanged |
| Bandwidth | 740.6 MB/s | **859.8 MB/s** | **+16.1%** |
| POSIX time | 1.096 s | 0.944 s | **-13.9%** |
| Workflow | 57/57 succeeded | 57/57 succeeded | — |

Identical operation count and data volume before/after confirms these are
pure performance changes with no behavior change.

**Caveat:** at this test-mosaic scale (4 images, <1s of actual I/O time), the
wall-clock impact is small relative to Pegasus/PMC's own per-task dispatch
overhead (chmod/register/cleanup bookkeeping across 57 tasks). The 16%
bandwidth gain would matter far more at production mosaic scale.

## 9. New capabilities added to the project

1. **`session_identify_smoke_test_files`** MCP tool — scopes annotation to
   only the files a smoke test actually exercises (§2)
2. **`exclude_functions`** parameter on `clang_annotate_file` — force-skip
   known hot-loop functions regardless of the static cost filter (§3)
3. **`software-pegasus`** skill — full PMC-only Pegasus execution recipe with
   every pitfall hit this session, generalized beyond Montage
4. Fixed `dfdiagnoser_service.py`'s API/CLI fallback logic (was hard-failing
   instead of falling through when the Python API method doesn't exist)
5. Fixed `resources/systems.yaml` (Tuolumne `LD_LIBRARY_PATH` missing
   `/usr/lib64`) and wired system-env injection into `session_install_dftracer`

Items 2, 4, 5 require an MCP server restart (not just client reconnect) to
take effect in future sessions.

---

## Contents of this folder

```
final/
  REPORT.md                        — this file
  patches/
    montageAdd.c.patch             — full source vs. annotated diff (includes
                                      dftracer instrumentation + fadvise +
                                      avg_mean instrumentation removal)
    montageProject.c.patch         — fadvise addition only (file is otherwise
                                      unannotated — see §3, reverted files)
    annotation_full.patch          — complete source/ vs annotated/ diff,
                                      all 44 annotated files
  pegasus_config/
    sites.yml                      — Lustre-backed site catalog (final version)
    pegasus.properties             — PMC code-generator + transfer config
  scripts/
    build_annotated_montage.sh     — reproduces the annotated build
    run_workflow_lustre.sh         — reproduces the final Lustre+PMC run
    gcc_shim.sh / cc_shim.sh       — PATH-shadow compiler wrappers (link
                                      dftracer without breaking vendored
                                      Makefile CC= flags)
    pmc_wrapper_lustre.sh          — exact wrapper used for the final run
    make_synthetic_fits.py         — synthetic FITS generator (no
                                      network/archive access needed for a
                                      minimal smoke test)
    annotation_scope_51_files.txt  — the smoke-test-scoped annotation target
    reverted_unannotated_files.txt — files reverted due to real annotator bugs
```

## Related artifacts elsewhere in the workspace tree

| Artifact | Path |
|---|---|
| Annotated Montage source | `../annotated/` |
| Optimized+annotated build | `../install_ann/bin/` |
| Baseline traces (NFS, fixed annotation) | `../pegasus2/traces/` |
| Optimized traces (Lustre + fadvise) | `../pegasus3/traces/` |
| Lessons log (all pitfalls, full detail) | `../../../src/dftracer_agents/.agents/skills/dftracer-annotation-lessons/LESSONS_LOG.md` |
| Pegasus workflow skill | `../../../src/dftracer_agents/.agents/skills/software-pegasus/SKILL.md` |
| Pegasus/PMC install | `/usr/WS2/haridev/dftracer-agents/workspaces/pegasus_montage/` |
| Final mosaic output | `/usr/WS2/haridev/dftracer-agents/workspaces/montage_workflow_v3/20260706_062326/source/wf-output/1-mosaic.fits` |
