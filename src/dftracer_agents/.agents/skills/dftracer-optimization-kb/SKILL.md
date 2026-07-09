---
name: dftracer-optimization-kb
description: >
  Cross-session, citation-backed knowledge base of every MEASURED dftracer
  optimization, partitioned into system-centric (L3), software-centric (L2),
  and workload-centric (L1) findings. Load this FIRST in any optimization
  loop to avoid re-deriving what is already known.
---

# dftracer optimization knowledge base

**Step 1 of every optimization loop is recall, not proposal.** Call
`opt_kb_lookup(system=..., workload=..., software=..., scope=...)` before
generating any proposal, and cite prior results in the proposal table.

Scopes and what they transfer to:

| Scope | Level | Transfers to | File |
| --- | --- | --- | --- |
| system | L3 | any workload **on that system** | [system.md](system.md) |
| software | L2 | any workload **linking that software**, any system | [software.md](software.md) |
| workload | L1 | that application, **any system** | [workload.md](workload.md) |

Recorded: 1 system, 1 software, 2 workload entries.

## Rules

1. Record only **measured** results — `metric`, `before`, `after` are required.
   Record failures too: knowing a lever did nothing is a result.
2. Every entry carries a **citation**: paper (preferred) > official docs > web.
   `session:<run_id>` marks a result measured in-house, never external evidence.
3. Apply optimizations **one at a time** and measure each, or the attribution
   is worthless.


---

## Context economy: query the graph, don't read the tree

Before any step that would open source files, use the `graphify` knowledge graph
(project dependency `graphifyy`, CLI `graphify`):

```bash
graphify query "<target>" --budget 1200   # locate: NODE <sym> [src=file loc=Lnn]
graphify explain <symbol>                 # definition + callers/callees
graphify affected <symbol> --depth 2      # blast radius before you change it
graphify update .                         # refresh after edits (~4s, no LLM)
```

Measured on this repo: locating cost 986 tokens vs 29,456 to read the three
relevant files (3.3%). Run `affected` before editing any shared function and
state the blast radius. Use the CLI, never `graphify-mcp` — its extra tool
schemas would sit in context permanently. See [[dftracer-context-economy]].
