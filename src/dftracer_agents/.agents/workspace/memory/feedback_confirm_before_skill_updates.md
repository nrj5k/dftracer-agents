---
name: feedback_confirm_before_skill_updates
description: "Always confirm observations/fixes with the user BEFORE writing to skills, MCP tools, agent defs, or lessons files"
metadata: 
  node_type: memory
  type: feedback
---

Before updating ANY skill, MCP tool, agent definition, or lessons file, always
check with the user that your observation/diagnosis for the fix is correct. Do
NOT auto-write self-learning entries during a run without user confirmation.

**Why:** The pipeline's self-learning policy has agents record lessons
automatically, but the user does not want incorrect diagnoses persisted into
skills/lessons/agents/tools — a wrong lesson pollutes every future run and is
hard to unwind.

**How to apply:** When dispatching pipeline step agents, instruct them to
PROPOSE lessons (symptom → root cause → fix) back to the main thread rather than
writing them to skills/agents/tools/lessons directly. The main thread surfaces
the proposed update to the user and only persists it after the user confirms the
observation is correct. This tempers Pipeline Policy items 3–5 in CLAUDE.md.
Relates to [[feedback_pipeline_selflearning]].

**Capture aggressively, route correctly (user instruction 2026-07-08):** Agents
should also record the WORKING recipe (exact commands/flags/paths/versions), not
just failures, so future sessions reuse it. Route generic cross-workload
knowledge into GENERIC skills (keep them generic); put workload/system/software
deltas into `workload-<app>` / `system-<system>` / `software-<lib>`. Prefer
generic skill = general how-to, specific skill = deltas only. This aggressive
capture is still gated by the confirmation rule above (propose, user confirms,
then persist). Baked into all 16 agent defs (`.agents/agents/*.md`
"Self-learning confirmation gate") and the `dftracer-lessons` skill.
