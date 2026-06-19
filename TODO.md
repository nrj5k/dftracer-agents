# MCP Tool Gaps & Fixes

## A. clang_annotate_file — Entry Point / MPI Awareness

### A1. Detect MPI entry points
- [x] Pre-scan lines for `MPI_Init`/`MPI_Comm_rank`/`MPI_Comm_size` and `MPI_Finalize`
  when `is_entry=True`; store as `_mpi_init_line` / `_mpi_finalize_line`

### A2. INIT must be emitted AFTER `MPI_Init` / `MPI_Comm_rank`
- [x] INIT/START/UPDATE now inserted at `_mpi_init_line` (after last MPI startup call)
- [x] Fallback to `body_first_line` when no MPI startup calls are found

### A3. INIT must come before START (ordering bug)
- [x] Sort key gives INIT `prio=3` (highest), START `prio=2`; in the bottom-to-top
  insertion pass, INIT is inserted last and lands above START in the file

### A4. END + FINI must be emitted BEFORE `MPI_Finalize`
- [x] END+FINI inserted at `_mpi_finalize_line - 1` (before `MPI_Finalize`)
- [x] Fallback to `close_brace_line` when no `MPI_Finalize` found

### A5. Skip END+FINI on return paths that occur before `MPI_Init`
- [x] Exit lines filtered: only those with `line > _mpi_init_line` AND
  `line < _mpi_finalize_line` receive END+FINI; pre-MPI returns are left bare

### A6. comp= auto-classification for entry point
- [x] `_make_update` gains `comp_override` parameter; main() always passes `"cpu"`
  regardless of MPI calls detected in the function body

### A7. Regular `clang_annotate_file` must skip main() when is_entry=True
- [x] N/A — the code never double-wraps; `is_main_fn` flag gates INIT/FINI addition
  while the normal START/END path only runs for non-main functions


## B. Annotation Coverage — Files Done Manually

### B1. aiori-MPIIO.c and aiori-POSIX.c were annotated with Read/Edit/Write
- [ ] Both files were instrumented manually before `clang_annotate_file` was loaded
- [ ] Re-run `clang_annotate_file` on each (check for double-wrap guard first)
  to verify tool would produce equivalent output — use this as a regression test
- [ ] Document any discrepancy as a new lesson in SKILL.md

### B2. aiori.h extern fix has no MCP equivalent
- [ ] The multiple-definition bug (GCC 10 `-fno-common`) was fixed with a raw `Edit`
- [ ] Consider adding a `clang_fix_header_tentative_defs` tool or a pre-build lint step
  that detects bare global declarations in headers and rewrites them as `extern`


## C. Trace Infrastructure Gaps

### C1. traces/ior/ subdirectory not auto-created
- [x] `Path(log_file_prefix).parent.mkdir(parents=True, exist_ok=True)` added in
  `session_run_with_dftracer` right after computing `log_file_prefix`

### C2. .pfw.gz files must be manually copied before splitting
- [x] `session_split_traces` now uses `traces_in.rglob("*.pfw.gz")` instead of
  `traces_in.glob("*.pfw.gz")`, so files in `traces/ior/` are found automatically

### C3. session_split_traces crashes with module import error
- [x] `_load_dftracer_utils_service` now wraps `spec.loader.exec_module(mod)` in
  `try/except Exception`; on failure it cleans up `sys.modules` and returns `None`,
  causing `_dftracer_utils_split` to fall through to the `dftracer_split` binary fallback


## D. Remaining Gaps (not yet in any tool)

### D1. No tool for bulk header structural fixes
- [ ] No MCP tool can detect or fix `tentative definition` issues in C headers
- [ ] Affects: any project built with GCC 10+ that has globals declared in shared headers

### D2. No MPI-aware syntax checker
- [ ] The stub-based syntax check (`gcc -include dftracer_stub.h -fsyntax-only`)
  works for simple C but cannot validate MPI ordering constraints at compile time
- [ ] A lint step that checks INIT-before-START, FINI-before-MPI_Finalize, and
  no-END-before-MPI_CHECK would catch annotation errors before a full build

### D3. No tool for PDF / report export
- [ ] Session summary currently requires manual `pandoc` invocation
- [ ] Low priority but a `session_export_report` tool would close the loop

### D4. comp= classification has no feedback loop
- [ ] When the tool auto-classifies comp= incorrectly (e.g. "comm" for main),
  there is no way to provide a per-function override without editing the file manually
- [ ] Consider: `clang_annotate_file(..., comp_overrides={"main": "cpu"})` parameter


## E. Priority Order

| ID  | Fix | Effort | Impact |
|-----|-----|--------|--------|
| A3  | INIT before START ordering | Low — sort fix | Critical: broken traces |
| A2  | INIT after MPI_Init | Medium — line scan | Critical: broken rank metadata |
| A4  | END/FINI before MPI_Finalize | Medium — line scan | Critical: data loss on flush |
| A5  | Skip END/FINI on pre-MPI returns | Medium — line filter | High: crash / undefined behavior |
| A6  | comp="cpu" for entry point | Low — special-case | Medium: wrong trace categorization |
| A7  | Skip main() in regular pass | Low — exclusion list | High: double-wrap |
| A1  | MPI entry point detection | Medium — heuristic | Enables A2–A5 |
| C1  | Auto mkdir for traces dir | Low — one liner | High: silent failure |
| C2  | Trace path mismatch | Low — param or path fix | High: split never works |
| C3  | session_split_traces crash | Medium — env/fallback | High: no compaction |
| B1  | Regression test via re-annotation | Low | Medium: tool validation |
| B2  | Header tentative-def lint | High | Low: rare but nasty |
| D1–D4 | Misc | Various | Low–Medium |
