---
name: dftracer-profiling
description: >
  MANDATORY pipeline self-profiling via MLflow. Every dftracer agent brackets its
  work with profile_step_begin / profile_step_end so the session records what each
  step cost in wall time, tokens, dollars, and retries. The main thread binds the
  session once with profile_bind and writes the report with profile_report. Load
  this skill before executing any pipeline step.
---

# Pipeline Self-Profiling (MLflow)

The dftracer pipeline profiles *itself*. Every step's wall time, token spend,
dollar cost, and retry count is recorded to MLflow and written to
`<workspace>/performance/performance_report.md`. This is not optional telemetry —
it is how we learn which agents are expensive, which steps thrash, and where the
pipeline needs a better tool.

## Bind at session creation — not later

**`profile_bind` is called immediately after `session_create` succeeds**, by
whoever created the session. That is the first moment a session directory exists
to dump into, so it is the first moment the profile has somewhere to live.
Binding creates `<workspace>/performance/` and the MLflow parent run.

Do not defer binding until the first step. Telemetry captured *before* the bind
is retained and attributed to the session — the planning and routing cost that
led up to it is part of what the run cost — but nothing can be written to disk
until the session exists and the bind happens.

## Who calls what

| Caller | Tools | When |
| --- | --- | --- |
| Whoever calls `session_create` | `profile_bind` | Once, in the same breath as session creation |
| Main thread / orchestrator | `profile_report` | Once, after the last step ends |
| Every step agent | `profile_step_begin` / `profile_step_end` | Around that agent's own work |
| Anyone | `profile_status` | Mid-pipeline cost check (cheap, in-memory) |

If the session already existed (a resume), bind once at the top of the resumed
run before dispatching any step.

## The rule for step agents (MANDATORY)

Bracket your entire execution. Do this *after* loading your plan section, so the
step name matches the plan heading verbatim:

```
profile_step_begin(step="STEP 3: dftracer-annotator",
                   agent="dftracer-annotator",
                   notes="<smoke cmd, file count, whatever is diagnostic>")
... do the work ...
profile_step_end(step="STEP 3: dftracer-annotator", status="ok")
```

On failure, close the attempt with the reason, then reopen with the **same**
`step` string to record a retry:

```
profile_step_end(step="STEP 3: dftracer-annotator", status="lint_error",
                 error="clang_lint_annotations: 4 files missing END")
profile_step_begin(step="STEP 3: dftracer-annotator", agent="dftracer-annotator")
```

## Rules

1. **The `step` string is an identity, not a label.** Reuse it exactly across
   retries — same string means "second attempt", a different string means "new
   step". Use the plan's `## STEP N: <agent-name>` heading verbatim.
2. **Always end what you begin.** A step left open is force-closed as
   `superseded` when the next step opens, which silently attributes your cost to
   nobody. Close it yourself, with a real status.
3. **`status="ok"` only when the step actually succeeded.** `failed`, `timeout`,
   and `lint_error` all surface in the report's Rework section — that section is
   the point of the whole exercise. Do not launder a failure into an `ok`.
4. **Never call `profile_bind` from a step agent.** Binding is the orchestrator's
   job; rebinding mid-pipeline splits the MLflow parent run.
5. **Telemetry before `profile_bind` is kept** and attributed to the session — the
   planning and routing that led up to it is part of what the run cost.

## Reading the result

`profile_status()` is served from memory (no MLflow round-trip) and returns
running totals, per-step timing, and attempt counts. `profile_report()` flushes
and writes the markdown report plus `summary.json` and `steps/<n>-<step>.json`.

Call `profile_report()` a few seconds after the last step ends — events buffered
inside Claude Code (`OTEL_LOGS_EXPORT_INTERVAL`, 5 s default) may otherwise miss
the final step.

See [[dftracer-context-economy]] for the companion rule on using the knowledge
graph instead of reading files, which is the other half of keeping a run cheap.

## Permissions

Read-only with respect to source. This skill uses:

- **MCP:** `mcp__dftracer__profile_bind`, `mcp__dftracer__profile_step_begin`,
  `mcp__dftracer__profile_step_end`, `mcp__dftracer__profile_status`,
  `mcp__dftracer__profile_report`
- **Write:** only `<workspace>/performance/` (created by `profile_bind`)
