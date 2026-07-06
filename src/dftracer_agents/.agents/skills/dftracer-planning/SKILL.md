---
name: dftracer-planning
description: Planning, progress reporting, and live progress rules for dftracer sessions — how to structure multi-step work and report expensive steps
---

## Planning and Progress

For any task that involves more than two steps or uses multiple MCP tools in sequence:

1. **Start with a plan.** Before taking action, write out the numbered steps you intend to follow.
   Example:
   ```
   Plan:
   1. Clone the repository into a new session workspace
   2. Detect the build system
   3. Configure and build (original)
   4. Run smoke test to confirm baseline
   5. Copy source and annotate with dftracer
   6. Build the annotated version
   7. Run smoke test with dftracer (traces collected)
   8. Split and analyze traces
   ```

2. **Report progress after each step.** After completing a step, state:
   - ✅ What was just done and its outcome
   - ⏳ What comes next

   Example after step 3:
   ```
   ✅ Step 3 done — cmake configured and built successfully (install_dir: /workspaces/.../install)
   ⏳ Next: Step 4 — run smoke test against the installed binary
   ```

3. **Surface blockers immediately.** If a step fails, report the error and the remaining steps before asking how to proceed.

## Live Progress for Expensive Steps

For any step expected to take more than ~10 seconds, announce it **before** calling
the tool, then show key output lines **after** it completes.

**Steps that require a before-and-after report:**

| Step | Expected duration | What to report after |
|------|-------------------|----------------------|
| `session_create` (git clone) | 10–60 s | repo size, ref cloned |
| `session_configure` (cmake/autotools) | 10–60 s | build tool, any configure warnings |
| `session_build_install` (make) | 30 s – 10 min | exit code, last 5 lines of stdout |
| `session_run_smoke_test` | 5–120 s | exit code, full stdout if short |
| `session_install_dftracer` / `session_install_dftracer_utils` | 1–10 min | pip install exit code, features_enabled list |
| `session_build_annotated` (each attempt) | 30 s – 10 min | attempt number, exit code, first compiler error if any |
| `session_run_with_dftracer` (each attempt) | 5–120 s | attempt number, exit code, trace files created |
| `session_split_traces` | 5–60 s | output directory, file count |
| `session_analyze_traces` | 10–120 s | summary output |

**Format for the before-announce:**
```
⏳ Starting <step name> — this may take <estimated time>…
```

**Format for the after-report:**
```
✅ <step name> succeeded (exit 0)
   stdout: <last 5 non-empty lines, or full output if under 10 lines>

❌ <step name> FAILED (exit <N>)
   stderr: <first compiler error or last 10 lines>
   → artifact log: <workspace>/artifacts/<NN>_<step>.log
```

For build steps, always show the **first** compiler error (the root cause), not the flood
of cascading errors. Extract it with:
```bash
grep -m1 "error:" <workspace>/artifacts/10_session_build_annotated*.log
```

## Context management for heavy stages

Annotation and optimization stages generate large tool-call histories (build logs,
per-file annotation passes, trace analysis output). Two rules keep the supervisor's
context from filling up with that raw history:

1. **Delegate heavy stages to a sub-agent when the harness supports it.** If launching
   a fresh, isolated-context worker is available, run a whole annotation pass or
   optimization iteration there rather than in the main session — let the worker read
   the deep skill content and raw tool output, and have it report back only a compact
   status summary (files annotated, pass/fail, key metrics). This is the same pattern
   used to keep supervisor context clean when exploring or designing large changes.
2. **Prefer compact status tools over re-reading raw logs.** `session_status` and
   `session_validate_structure` already return small JSON summaries rather than raw
   artifact content — query those instead of re-reading `<workspace>/artifacts/*.log`
   or re-running `skill_load` on a large skill you've already read this session. If a
   tool's JSON response already answers the question, don't re-fetch the source file
   behind it.
