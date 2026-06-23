## I/O Optimization Key Papers

When suggesting optimizations or interpreting bottleneck diagnosis results, consult:

- **WisIO** (Yildirim et al., ICS 2025) — maps dfdiagnoser metric names
  (small_io_pct, rand_pct, fetch_pressure, etc.) to three-level optimizations.
  Full mapping: `.agents/skills/dftracer-io-optimization/SKILL.md`
- **Drishti** (Bez et al., PDSW 2022) — application/library/system tier guidance
  matching the optimize.yaml sub-recipe structure (L1/L2/L3).
  Full guidance: `.goose/hints/10-io-optimization-papers.md`

## DFTracer References

Use these links when implementing DFTracer behavior:
- https://dftracer.readthedocs.io/
- https://dftracer.readthedocs.io/en/latest/api.html
- https://dftracer.readthedocs.io/projects/python/en/latest/
- https://dftracer.readthedocs.io/projects/utils/
- https://dftracer.readthedocs.io/projects/analyzer/en/latest/
- https://github.com/llnl/dftracer

## Artifact Logging

Every `session_*` tool call automatically writes a stage log to:
  `<workspace>/artifacts/<NN>_<stage_name>.log`

The step numbering is fixed:

| NN | Log file                            | Session tool                  |
|----|-------------------------------------|-------------------------------|
| 01 | `01_session_create.log`             | session_create                |
| 02 | `02_session_detect.log`             | session_detect                |
| 03 | `03_session_configure.log`          | session_configure             |
| 04 | `04_session_build_install.log`      | session_build_install         |
| 05 | `05_session_run_smoke_test.log`     | session_run_smoke_test        |
| 06 | `06_session_copy_annotated.log`     | session_copy_annotated        |
| 07 | `07_session_patch_build.log`        | session_patch_build           |
| 08 | `08_session_annotate_source.log`    | session_annotate_source       |
| 09 | `09_session_install_dftracer.log`   | session_install_dftracer      |
| 10 | `10_session_build_annotated.log`    | session_build_annotated       |
| 11 | `11_session_run_with_dftracer.log`  | session_run_with_dftracer     |
| 12 | `12_session_split_traces.log`       | session_split_traces          |
| 13 | `13_session_analyze_traces.log`     | session_analyze_traces        |

**Each log contains:** timestamp, run_id, step number, stdout, stderr, exit code, and
any stage-specific fields (commands, file counts, patch paths, etc.).

**After each step (manual or pipeline), review the corresponding log to verify:**
- Exit code is 0 (success)
- stdout contains the expected output for that stage
- No unexpected errors appear in stderr

**Annotation correctness check:** after `session_annotate_source`, inspect
`<workspace>/annotation.patch` — it is a unified diff of every file modified.
Use `patch --dry-run -p1 < annotation.patch` from source/ to confirm it applies cleanly.

**On failure:** read the log for the failing step, diagnose from stderr, then call the
individual `session_*` tool directly (not the full pipeline) to retry that step.
