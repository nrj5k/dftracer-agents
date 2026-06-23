"""
Utilities for locating and installing the dftracer agent skills.

Skills are bundled inside the package at ``dftracer_agents/.agents/skills/``.
After ``pip install dftracer-agents`` they live in site-packages; in an
editable / dev install they are in the project source tree.

CLI usage (after pip install)::

    dftracer-install-skills                # copies skills to ./.agents/skills/
    dftracer-install-skills /path/to/proj  # copies to /path/to/proj/.agents/skills/

Programmatic usage::

    from dftracer_agents.skills import bundled_skills_dir, install_skills
    path = bundled_skills_dir()            # Path to the packaged skills
    install_skills()                       # copy to CWD
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path


def bundled_skills_dir() -> Path:
    """Return the Path to the skills bundled inside this package.

    Works for both regular and editable installs.  Raises ``FileNotFoundError``
    if the skills directory is missing from the installation.
    """
    pkg_dir = Path(__file__).resolve().parent
    skills = pkg_dir / ".agents" / "skills"
    if not skills.is_dir():
        raise FileNotFoundError(
            f"Bundled skills directory not found at {skills}. "
            "Re-install the package to restore it."
        )
    return skills


def install_skills(target_dir: str | Path | None = None, overwrite: bool = False) -> Path:
    """Copy the bundled skills into *target_dir*/.agents/skills/.

    Args:
        target_dir: Destination directory (defaults to the current working
            directory).  The subtree ``<target_dir>/.agents/skills/`` is
            created if it does not exist.
        overwrite: If True, replace any existing skill directories that share
            a name with a bundled skill.  If False (default), existing
            skill directories are left untouched and only new ones are copied.

    Returns:
        The Path to the installed ``<target_dir>/.agents/skills/`` directory.
    """
    dest_root = (Path(target_dir) if target_dir else Path.cwd()) / ".agents" / "skills"
    dest_root.mkdir(parents=True, exist_ok=True)

    src_skills = bundled_skills_dir()
    copied = []
    skipped = []

    for skill_dir in sorted(src_skills.iterdir()):
        if not skill_dir.is_dir():
            continue
        dest = dest_root / skill_dir.name
        if dest.exists() and not overwrite:
            skipped.append(skill_dir.name)
            continue
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(skill_dir, dest)
        copied.append(skill_dir.name)

    if copied:
        print(f"Installed {len(copied)} skill(s) to {dest_root}:")
        for name in copied:
            print(f"  + {name}")
    if skipped:
        print(f"Skipped {len(skipped)} already-present skill(s) (use --overwrite to replace):")
        for name in skipped:
            print(f"  ~ {name}")
    if not copied and not skipped:
        print("No skills found in the package.")

    return dest_root


def main() -> None:
    """Entry point for the ``dftracer-install-skills`` CLI command."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="dftracer-install-skills",
        description=(
            "Copy the dftracer agent skills from the installed package into "
            "a project directory so the AI agent harness can find them."
        ),
    )
    parser.add_argument(
        "target_dir",
        nargs="?",
        default=None,
        help="Directory to install skills into (default: current working directory).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        default=False,
        help="Replace existing skill directories with the packaged versions.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        default=False,
        help="Print the path to the bundled skills and exit.",
    )
    args = parser.parse_args()

    if args.list:
        try:
            print(bundled_skills_dir())
        except FileNotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    try:
        install_skills(target_dir=args.target_dir, overwrite=args.overwrite)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
