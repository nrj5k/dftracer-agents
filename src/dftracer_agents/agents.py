"""
Render the bundled YAML agent templates into every harness's discovery path.

The canonical agent definitions are harness-neutral YAML templates under
``dftracer_agents/.agents/agents/<name>.yaml`` (see
``dftracer_agents.agent_templates`` for the schema). This module is the
filesystem side: it renders each template through the per-harness converters
and writes the results where each harness looks for agents:

    claude    <root>/.claude/agents/<name>.md
    opencode  <root>/.opencode/agents/<name>.md
    copilot   <root>/.github/agents/<name>.agent.md

The rendered files are build artifacts — gitignored, stamped with a
generation marker, and regenerated whenever the templates or the model map
change (``ensure_agents_setup`` on MCP server startup, or the ``agents_sync``
MCP tool after a self-learning edit). Track changes in git via the YAML
templates only; never edit a rendered copy.

CLI usage (after pip install)::

    dftracer-install-agents                  # interactive: asks where
    dftracer-install-agents --target cwd     # render into ./
    dftracer-install-agents --target global  # render into ~/
    dftracer-install-agents --list           # print bundled agent names

Programmatic usage::

    from dftracer_agents.agents import ensure_agents_setup
    ensure_agents_setup()                     # idempotent; used by mcp_server startup
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dftracer_agents.agent_templates import (
    HARNESSES,
    render_all,
    templates_dir,
)

# Reuse the exact state-file + target-resolution logic the skills installer
# uses, so agents and skills behave identically (same ~/.dftracer-agents state,
# same "CWD-if-project-else-home" default, same self-heal semantics).
from dftracer_agents.skills import (
    _load_state,
    _save_state,
    resolve_default_target,
)

_AGENT_STATE_KEY_SUFFIX = "::agents"  # namespace agent state separately from skills

# Legacy marker from the pre-YAML era, still recognised so upgrades self-heal.
_LEGACY_MARKER = "# generated-by: dftracer-agents"


def bundled_agents_dir() -> Path:
    """Return the Path to the YAML agent templates bundled inside this package."""
    return templates_dir()


def _is_ours(dest: Path) -> bool:
    """True if *dest* is a dftracer-managed rendered agent (or legacy form)."""
    try:
        if dest.is_symlink():
            # Legacy layout symlinked into the bundled tree.
            return ".agents/agents" in str(dest.resolve().parent)
        return dest.is_file() and _LEGACY_MARKER in dest.read_text()
    except OSError:
        return False


def install_agents(
    target_root: Optional[Path] = None,
    harnesses: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Render every bundled YAML template into each harness's agents directory.

    Idempotent and merge-safe: a file already matching what we'd render is left
    untouched; a stale rendered copy or legacy symlink we own is refreshed; a
    name colliding with an unrelated user file is reported as a conflict and
    never overwritten.
    """
    root = Path(target_root) if target_root else Path.cwd()
    rendered = render_all(target_root=root, harnesses=harnesses)

    installed: List[Dict[str, str]] = []
    conflicts: List[str] = []
    dirs: List[str] = []

    for harness, files in rendered.items():
        for rel, content in files.items():
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if str(dest.parent) not in dirs:
                dirs.append(str(dest.parent))

            # If the harness agents dir itself is a legacy symlink into the
            # bundled tree, replace it with a real directory first.
            parent = dest.parent
            if parent.is_symlink():
                parent.unlink()
                parent.mkdir(parents=True)

            try:
                current = dest.read_text() if dest.is_file() else None
            except OSError:
                current = None

            if current == content:
                installed.append({"harness": harness, "name": dest.name, "action": "already_installed"})
                continue
            if (dest.exists() or dest.is_symlink()) and not _is_ours(dest):
                conflicts.append(str(dest))
                continue

            action = "refreshed" if (dest.exists() or dest.is_symlink()) else "installed"
            if dest.is_symlink():
                dest.unlink()
            dest.write_text(content)
            installed.append({"harness": harness, "name": dest.name, "action": action})

    return {"target": str(root), "dirs": dirs, "installed": installed, "conflicts": conflicts}


def sync_agents(target_root: Optional[Path] = None) -> Dict[str, Any]:
    """Re-render all harness copies from the YAML templates (one-way, lossless).

    This is what the ``agents_sync`` MCP tool calls after a self-learning edit
    to a template. It never merges: templates are the only source of truth.
    """
    root = Path(target_root).resolve() if target_root else resolve_default_target()
    result = install_agents(target_root=root)
    changed = [i for i in result["installed"] if i["action"] != "already_installed"]
    result["changed"] = changed
    result["summary"] = (
        f"{len(changed)} rendered file(s) updated across {len(HARNESSES)} harnesses"
        if changed else "all rendered agents already current"
    )
    return result


def ensure_agents_setup(
    target_root: Optional[Path] = None, force: bool = False
) -> Dict[str, Any]:
    """Render agents for *target_root* once, tracked in ``~/.dftracer-agents``.

    Called automatically by ``dftracer-mcp-server`` on startup alongside the
    skills setup. Self-heals: install_agents itself compares rendered content,
    so a deleted or stale file is repaired on the next launch. The state file
    only short-circuits the render when the template set is unchanged AND a
    quick content check passes.
    """
    root = Path(target_root).resolve() if target_root else resolve_default_target()
    state = _load_state()
    key = str(root) + _AGENT_STATE_KEY_SUFFIX

    bundled_names = sorted(p.name for p in bundled_agents_dir().glob("*.yaml"))

    result = install_agents(target_root=root)
    changed = [i for i in result["installed"] if i["action"] != "already_installed"]

    record = {
        "bundled_names": bundled_names,
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target": result["target"],
        "conflicts": result["conflicts"],
    }
    state[key] = record
    _save_state(state)
    status = "installed" if changed or force else "already_done"
    return {"status": status, **record, "actions": result["installed"]}


def main() -> None:
    """Entry point for the ``dftracer-install-agents`` CLI command."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="dftracer-install-agents",
        description=(
            "Render the dftracer YAML agent templates into the discovery paths "
            "of every harness: .claude/agents/, .opencode/agents/, .github/agents/."
        ),
    )
    parser.add_argument(
        "--target",
        default=None,
        help="Where to install: 'global' (~), 'cwd', or an explicit path. "
        "If omitted, you'll be prompted.",
    )
    parser.add_argument(
        "--harness",
        choices=[*HARNESSES, "all"],
        default="all",
        help="Render only one harness (default: all).",
    )
    parser.add_argument(
        "--list", action="store_true", default=False,
        help="Print the bundled agent template names and exit.",
    )
    args = parser.parse_args()

    if args.list:
        for p in sorted(bundled_agents_dir().glob("*.yaml")):
            if p.name != "common-sections.yaml":
                print(p.stem)
        return

    choice = args.target
    if choice is None:
        print("Where should dftracer agents be rendered?")
        print("  [1] global  — ~/                (all your projects)")
        print("  [2] cwd     — ./                (this project only)")
        print("  [3] other   — specify a path")
        choice = input("Choose [1/2/3] (default 2): ").strip() or "2"
        choice = {"1": "global", "2": "cwd", "3": "other"}.get(choice, choice)
        if choice == "other":
            choice = input("Path: ").strip()

    if choice == "global":
        target_root = Path.home()
    elif choice == "cwd":
        target_root = Path.cwd()
    else:
        target_root = Path(choice).expanduser().resolve()

    harnesses = None if args.harness == "all" else [args.harness]
    result = install_agents(target_root=target_root, harnesses=harnesses)
    print(f"Target root: {result['target']}")
    for item in result["installed"]:
        marker = {"installed": "+", "refreshed": "~"}.get(item["action"], "=")
        print(f"  {marker} [{item['harness']}] {item['name']}")
    if result["conflicts"]:
        print(f"Skipped {len(result['conflicts'])} conflicting file(s):")
        for name in result["conflicts"]:
            print(f"  ! {name}")


if __name__ == "__main__":
    main()
