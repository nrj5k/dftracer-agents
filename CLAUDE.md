# Project: dftracer-agents

## Hard Constraints

### File System

- **Never write files outside the project directory** (`/usr/WS2/haridev/dftracer-agents/`).
- **Temp files** go inside the active workspace session directory: `workspaces/<session>/tmp/`. Use the system scratchpad only for truly ephemeral intermediaries that don't belong to any session.
- **Never execute sudo**. This system does not permit privilege escalation.

### Bash

- Do not run commands with `sudo`, `su`, or any privilege escalation.
- Do not write to `/tmp`, `/var`, `/etc`, `/usr` (outside the project), or any path outside the project root.
- Prefer relative paths rooted at the project directory.

## Permission Tiers

| Location | Mode | Notes |
| --- | --- | --- |
| `workspaces/<session>/` | **Auto** — no confirmation | Session workspace managed by dftracer MCP; run freely |
| Project root (`/usr/WS2/haridev/dftracer-agents/`) | **Edit with confirmation** | Source code; confirm before editing |
| Outside project root | **Restricted** — deny | Never write; settings.json enforces this |

## Session Startup

At the start of every session, invoke the `system-detect` skill to load system-specific environment configuration before doing any work that touches modules, compilers, or MPI.
