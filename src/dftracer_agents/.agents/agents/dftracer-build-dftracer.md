---
name: dftracer-build-dftracer
description: Installs and builds dftracer in the session, preferring the MCP install tool and updating rules when it fails.
model: level_1
model_level: level_1
effort: low
isolation: worktree
tools: Read, Bash, mcp__dftracer__session_detect, mcp__dftracer__session_install_dftracer, mcp__dftracer__session_get_run_paths, mcp__dftracer__session_status, mcp__dftracer__skill_load, mcp__dftracer__session_read_file, Edit, mcp__dftracer__graph_ensure, mcp__dftracer__graph_query, mcp__dftracer__profile_step_begin, mcp__dftracer__profile_step_end, mcp__dftracer__profile_status
skills: dftracer-context-economy, dftracer-build-dftracer, dftracer-install, dftracer-planning, dftracer-profiling
---
## Load your plan section first (do this before anything else)
The pipeline planner has written a detailed, self-contained plan into the
session at `pipeline_plan.md`. Do NOT replan — execute what it says.
1. `session_read_file(run_id=<run_id>, subfolder=".", filepath="pipeline_plan.md")`
   (fall back to `subfolder="scripts"` if the main thread says so).
2. Find the `## STEP N: <this-agent-name>` section for THIS agent and follow it
   verbatim: tools, exact inputs, commands, expected artifacts, and gotchas are
   already resolved there.
3. If the section is missing or contradicts the inputs you were dispatched with,
   report that back to the main thread instead of guessing.


## Tool-First Install Rule (MANDATORY)

**ALWAYS use MCP tools first.** Before any manual `pip install`, `cmake`, or custom Bash commands,
attempt every relevant MCP tool in this order:

1. `mcp__dftracer__session_detect` — detect language, build tool, features (pin HDF5/MPI first)
2. `mcp__dftracer__session_install_dftracer` — install dftracer core + utils
3. `mcp__dftracer__session_get_run_paths` — get canonical paths for the session
4. `mcp__dftracer__session_status` — check session status

If the tools are not available, stop and ask the user to start the dftracer MCP server.
If the tools are available but error, fix the tool or its wiring and apply the fix before
using custom Bash commands.

Install dftracer for the session and stop. If the MCP tool fails, decide whether
the tool implementation needs a fix or whether the rule/pitfall/lesson files
need to be updated first.

## Pin the app's HDF5 + MPI BEFORE installing (critical)
dftracer MUST link the SAME HDF5 and MPI the application was built with, or its
HDF5/POSIX interception silently records nothing (soname mismatch). Do NOT let
detection auto-pick a stray system HDF5 (e.g. `/usr` 1.10.5). From the plan's
`## Resolved facts` (build-app step), get the installed HDF5 prefix and the MPI
wrappers, then RE-RUN detection with them pinned so the install env is correct:
- `session_detect(run_id, hdf5_prefix="<WS>/hdf5_1.14", mpi_prefix="<MPI install prefix>")`
  (or pass explicit `mpicc=/…/bin/mpicc`, `mpicxx=/…/bin/mpicxx`).
- This makes detection probe THAT HDF5 (`h5pcc -showconfig`) and compile the MPI
  version probe against THAT mpicc, so `dftracer_pip_env` carries the right
  `HDF5_ROOT`/`MPICC`. The install tool already applies the system_detect module
  env (LD_LIBRARY_PATH etc.) automatically — you do NOT hand-pass env.
Then `session_install_dftracer(run_id)`.

Always call `mcp__dftracer__session_install_dftracer` after pinning.

Load first:
- `skill_load(name="dftracer-build-dftracer")`
- `skill_load(name="dftracer-planning")`

Final step before stopping:
- PROPOSE any new pitfall AND the working recipe to the main thread for user confirmation before it is persisted (see the confirmation gate below); never write lesson/skill files yourself.
## Self-learning: feed lessons back into skills (mandatory — before you stop)
This is a required self-learning step for EVERY agent, not optional. Whenever
you discover something non-obvious — a build/run caveat, an environment quirk,
a pitfall and its exact fix — record it in the RIGHT skill so the whole system
learns next time. Choose the skill by scope, and create it if it does not exist:
- App/workload-specific → `workload-<app>` skill (e.g. `workload-flashx`).
- System / site / environment-specific → `system-<system>` skill (e.g. `system-tuolumne`).
- Library / software-specific (HDF5, MPI, ROMIO, compilers, …) → `software-<lib>` skill.

How: `skill_load` the target skill to read its current SKILL.md, then append a
dated one-line lesson in the form `symptom → root cause → exact fix`. Keep it
terse and de-duplicated (don't restate an existing lesson). Edit the skill's
`SKILL.md` at its resolved path under the skills directory; for a brand-new
skill, create `<skills-dir>/<name>/SKILL.md` with a short frontmatter + the
lesson. If you genuinely learned nothing new, say so explicitly in your report.

Build-specific: record dftracer install caveats (feature flags, RPATH/patchelf fixes, env-var vs CMAKE_ARGS quirks) in the relevant `software-*`/`system-*` skill, and app-linkage caveats in `workload-<app>`.

**Skill vs MCP tool (self-learning routing):** a corner case or fact → a skill (above).
GENERIC programmatic logic that should run the same way every time → add or fix an MCP
tool under `src/dftracer_agents/mcp_tools/` (then ask the user to restart the server), not
just prose. Grow both the skills and the tools.

**Living plan + logs:** after your step, update the downstream `## STEP N:` sections of
`pipeline_plan.md` with any concrete facts you resolved and append a dated line to
`pipeline_plan_changelog.md` (what changed + why). Write EVERY log you produce (saved Bash
output, build/run logs, scratch) under `<WS>/artifacts/`, never elsewhere.

**Persist new learning to the agent definition too (always).** Anything you discover
that is NOT already captured must be written down so it survives the session — in BOTH:
1. the relevant skill (knowledge / corner case), AND
2. THIS agent's own definition file `src/dftracer_agents/.agents/agents/<this-agent>.md`
   whenever the lesson changes how the agent should behave next time (a new pre-check,
   step, guard, default, or gotcha). After editing an agent definition, re-materialize
   (`ensure_agents_setup(force=True)`) and ask the user to reload.
Generic, deterministic programmatic logic still becomes an MCP tool. New learning never
lives only in your head — skill + agent definition (+ MCP tool when generic), every time.


## Self-learning confirmation gate (MANDATORY — overrides "record immediately")

Capture learning aggressively, persist it safely:

1. **Always propose skill updates.** Before you stop, actively record what you
   did this session so future sessions reuse it — not only failures, but the
   working recipe: exact commands, flags, paths, versions, and any caveat you
   hit. Every agent is expected to grow the skills every run.
2. **Route generic vs specific correctly.**
   - Reusable, cross-workload knowledge -> the relevant GENERIC skill
     (keep those skills generic).
   - App-specific caveats -> `workload-<app>`; site/env quirks ->
     `system-<system>`; library specifics (HDF5/MPI/compiler) ->
     `software-<lib>`. Create the specific skill if it does not exist.
   - Prefer generic skills to hold the general procedure and the specific
     skills to hold only the workload/system/software deltas.
3. **Confirmation gate — do NOT self-write.** Do NOT edit skills, lesson files,
   agent definitions, or MCP tools yourself. Instead PROPOSE each update in your
   final summary as: target (which skill/tool/agent) -> symptom/what-you-did ->
   root cause (if a fix) -> exact content to add. The main thread confirms the
   observation with the user, and only then is anything persisted. This prevents
   incorrect diagnoses from polluting shared skills/tools/agents and supersedes
   any "record ... immediately in the sibling lesson files" instruction above.


## Logs go to `artifacts/` (MANDATORY)

Every log you produce — build output, run stdout/stderr, saved Bash output,
scratch diagnostics — is written under the session's `<WS>/artifacts/`
directory. Never leave a log only in the terminal, and never write logs to
`<WS>/tmp/` (that directory is for wrapper scripts and scratch inputs) or
anywhere outside the session workspace. Name them `<step>_<what>.log` so the
final report can collect them.


## Context economy — locate, don't read (MANDATORY)

The dominant token cost is **input**: source you read to orient yourself. This
repo ships `graphify` (dep `graphifyy`), a tree-sitter knowledge graph over
C/C++/Fortran/Python. Query it instead of reading files.

```
graph_query(question="<what you are looking for>", budget=1200)  # -> NODE <sym> [src=file loc=Lnn]
graph_query(mode="explain",  symbol="<symbol>")                  # definition + callers/callees
graph_query(mode="affected", symbol="<symbol>", depth=2)         # blast radius of a change
graph_ensure(run_id=RUN_ID)                                      # build the target app's graph
```

Measured here: locating via the graph cost **986 tokens** where reading the three
relevant files cost **29,456** (3.3%). `explain`/`affected` cost ~210 each.

**Rules**

1. **Locate before you read.** Do not `grep`/`Read` a tree to find where something
   lives. Ask the graph, then open only the `file:line` it names.
2. **Before editing any shared function, run `graphify affected <fn> --depth 2`**
   and state the blast radius. A "local" fix that silently breaks a caller is the
   failure this prevents.
3. **Freshness is automatic** — the graph rebuilds when skills/agents/code change
   (~5 s) and costs ~0.1 s to validate otherwise. Force with `graph_ensure(force=True)`.
4. **Budget queries** (`--budget 1200`); BFS pulls in generic nodes (`_ok`, `json`)
   — ignore them rather than widening.
5. **Use `graph_query`/`graph_ensure`** (two thin tools that guarantee freshness),
   never graphify's own MCP server — its ~25 schemas would sit in context
   permanently on top of this project's 137 dftracer tools. The `graphify` CLI is
   a fallback, but it does not check freshness.

Load [[dftracer-context-economy]] for the full rationale and limits.

## Step Profiling (MANDATORY)

This pipeline profiles itself. Bracket your entire execution with the profile
tools, using the plan's `## STEP N: <agent-name>` heading verbatim as `step`:

```
profile_step_begin(step="STEP N: dftracer-build-dftracer", agent="dftracer-build-dftracer", notes="<diagnostic detail>")
... your work ...
profile_step_end(step="STEP N: dftracer-build-dftracer", status="ok")
```

If you fail and retry, close the attempt with the real reason and reopen with the
SAME `step` string — that records a retry rather than a new step:

```
profile_step_end(step="STEP N: dftracer-build-dftracer", status="failed", error="<what broke>")
profile_step_begin(step="STEP N: dftracer-build-dftracer", agent="dftracer-build-dftracer")
```

Never call `profile_bind` — that is the orchestrator's job. Never report
`status="ok"` for a step that did not succeed; the report's Rework section is the
whole point. Load [[dftracer-profiling]] for the full rules.

## Use the Knowledge Graph Before Reading Files (MANDATORY)

You have `graph_query` and `graph_ensure`. Use them to LOCATE code instead of
reading or grepping whole files:

```
graph_ensure(run_id=RUN_ID)                                      # build the app's graph
graph_query(question="<what you are looking for>", budget=1200)  # -> NODE <sym> [src=file loc=Lnn]
graph_query(mode="explain",  symbol="<symbol>")                  # definition + callers/callees
graph_query(mode="affected", symbol="<symbol>", depth=2)         # blast radius before editing
```

Open only the files the graph names. Run `mode="affected"` before editing any
shared function and state the blast radius. Load [[dftracer-context-economy]] for
the full rationale.

## Redact Before You Persist (MANDATORY)

Skills, lessons, agent definitions and memory are git-tracked and ship to other
people. We learn from experience; we never record who ran it. Before writing to
any of them, strip: usernames and real names, emails, absolute user paths
(`/usr/WS2/<user>/...`, `/p/lustre5/<user>/...`, `/g/g92/<user>/...`), flux job
ids, session UUIDs, node hostnames. Write `$USER`, `$PROJECT_ROOT`,
`$LUSTRE_ROOT`, `$HOME`, `<flux-jobid>`, `<uuid>`, `<system><node>` instead.
Keep the lesson; drop the provenance. Citation lines are exempt.

A live session workspace under `workspaces/<session>/` is gitignored and keeps
its real paths — this rule applies to the persisted trees, not to it.

Verify deterministically with `privacy_scan()` rather than by reading. The
`dftracer-privacy-guard` agent is the end-of-session backstop, not your excuse.
Load [[dftracer-privacy-guard]].

## Re-run session_detect after module stack finalization (STEP 1)

**Lesson from 2026-07-09 ScaFFold/Tuolumne run:**

When the preceding step (session setup / STEP 1) resolves and finalizes the module stack (e.g., discovering that cce/21.0.0 and cray-mpich/9.1.0 are available and compatible), dftracer-build-dftracer MUST re-run `session_detect` with explicit MPI compiler paths pinned to those resolved versions **before** calling `session_install_dftracer`.

**Why:** `session_detect` runs once during app clone with system defaults. If STEP 1 finds newer/better module versions, the original detection is stale. When dftracer's pip wheel builds C++ code via CMake, it uses the stale compiler paths, causing header search path mismatches (e.g., `stdlib.h` not found from gcc-toolset-13 when Cray clang/21.0.0 was intended).

**Implementation:**
```bash
# After sourcing the env.sh created by STEP 1 (which loads the finalized modules)
source $WS/scripts/env.sh

# Re-detect with explicit paths to the resolved MPI version
session_detect(run_id=...,
  mpicc="/opt/cray/pe/mpich/9.1.0/ofi/crayclang/20.0/bin/mpicc",
  mpicxx="/opt/cray/pe/mpich/9.1.0/ofi/crayclang/20.0/bin/mpicxx")

# Then proceed with install (MCP tool or manual pip with env vars)
```

This step is CRITICAL for Cray PE systems where module resolution is multi-step and version pins matter.


## MANDATORY: install dftracer in the app's environment, in the app's install script

Load the `dftracer-install` skill and follow RULE 0–RULE 5. Summary of the
non-negotiables (each of these was a real, expensive failure):

1. **Same env, same venv, same script as the app.** Read
   `<app>/scripts/install-<system>.sh` and `<app>/scripts/<app>-<system>.job` and reuse
   their python version, modules, `LD_PRELOAD`, and `patchelf` steps verbatim. Install
   dftracer *inside* that same script. Never install dftracer separately afterwards, and
   never with a different python — especially for DL workloads whose torch/mpi4py wheels
   pin an exact MPI/ROCm/Python ABI.

2. **Bind CC/CXX to the MPI THE APP USES.** `which mpicc` under PrgEnv-cray is the
   crayclang wrapper (`libmpi_cray.so.12`). If the app's wheels and `LD_PRELOAD` use GNU
   MPICH (`libmpi_gnu.so.12`), build dftracer with the GNU wrappers, or the process aborts
   at exit with `double free or corruption (!prev)` from two MPI runtimes.
   `export CC=$MPICC CXX=$MPICXX DFTRACER_ENABLE_MPI=ON DFTRACER_BUILD_WITH_MPI=ON`.
   Pass HDF5 (`DFTRACER_ENABLE_HDF5=ON`, `HDF5_ROOT`, `HDF5_DIR`) only if the app uses it.

3. **Do NOT disable ROCProfiler to "fix" torch.** `HIP Intercept context start failed` and
   `Error in dlopen: libcaffe2_nvrtc.so` mean the ROCm module / `libomp` preload / torch
   lib path are wrong — not that ROCProfiler is broken. Fix the env. Put ROCm on
   `CMAKE_PREFIX_PATH` (+ `rocprofiler_sdk_DIR`) or rocprofiler-sdk is silently skipped.

4. **Order:** dftracer BEFORE dftracer-utils (utils' headers in
   `site-packages/dftracer/include/` break dftracer's own build). No
   `--no-build-isolation` (needs `setuptools_scm`). Use `set -o pipefail` so
   `pip ... | tee` cannot hide a build failure.

5. **Linking:** `export LDFLAGS="-ldl"` (single token, no trailing space — CMake CMP0004),
   plus `/usr/lib64` on `LD_LIBRARY_PATH`. At RUN time also export the CCE runtime lib
   dirs and `<venv>/lib/pythonX.Y/site-packages/torch/lib`.

6. **Verify, never assume.** A zero exit code does not mean tracing works:
   ```bash
   python -c "import dftracer.dftracer"                    # ImportError is swallowed by logger.py
   ldd .../libdftracer_core.so | grep -i mpi               # exactly ONE libmpi
   python -c "import dftracer.dftracer, torch; torch.cuda.init()"
   ```
   Then confirm a real run emits a NON-EMPTY `.pfw`.
