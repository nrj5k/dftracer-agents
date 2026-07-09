"""Console-script entry point for the ``dftracer_agents_stack`` launcher.

The launcher itself is a bash script shipped as package data beside this module.
Bash, because its whole job is supervising three daemons — pid files, ports,
signals, ``tail -f`` — and that is what bash is for. This module exists only to
make it *installable*: ``pip install`` puts a ``dftracer_agents_stack`` command
on ``PATH``, and packaging a shell script as a console script requires a Python
shim to exec it.

The shim resolves three things bash cannot reliably work out for itself once the
package is installed into a wheel rather than run from the repo:

``DFTRACER_STACK_BIN``
    The directory holding ``mlflow``, ``dftracer-mcp-server`` and
    ``dftracer-profile-collector``. This is the *environment* running this shim —
    a venv or a conda env — not whatever ``python3`` happens to be first on
    ``PATH`` inside the script.

    It is derived from ``sys.prefix``, never from ``Path(sys.executable).resolve()``:
    a venv's ``bin/python`` is a symlink to the base interpreter, so resolving it
    walks straight out of the venv and returns the base environment's bin dir,
    where none of these console scripts exist.

``DFTRACER_STACK_ROOT``
    The project root, i.e. the parent of ``workspaces/``. From an installed
    wheel the script's own location is ``site-packages/dftracer_agents/`` and
    tells us nothing, so we search upward from the working directory instead.

``DFTRACER_STACK_CMD``
    How the user spelled the command, so printed help is copy-pasteable.

``os.execve`` rather than ``subprocess``: the launcher's exit status, its
signal handling and its ``tail -f`` should all belong to the user's shell
directly, with no Python process loitering in between.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

#: Markers that identify a project root — the directory that owns ``workspaces/``.
_ROOT_MARKERS = ("workspaces", ".git", "pyproject.toml")


def _project_root() -> Path:
    """Find the project root by walking up from the working directory.

    Prefers a directory that already has ``workspaces/``; a repo checkout with
    only ``.git``/``pyproject.toml`` is the next-best answer (the stack will
    create ``workspaces/`` there). Falls back to the cwd, which is what a user
    running the stack in a scratch directory means.
    """
    cwd = Path.cwd().resolve()
    for parent in (cwd, *cwd.parents):
        if (parent / "workspaces").is_dir():
            return parent
    for parent in (cwd, *cwd.parents):
        if any((parent / m).exists() for m in _ROOT_MARKERS):
            return parent
    return cwd


def _script() -> Path:
    return Path(__file__).resolve().with_name("dftracer_agents_stack")


def _env_bin() -> Path:
    """The active environment's ``bin/`` directory.

    ``sys.prefix`` is the venv or conda root under both layouts. Falls back to the
    interpreter's own (UNRESOLVED) directory, since resolving the symlink would
    leave the environment.
    """
    candidate = Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin")
    if candidate.is_dir():
        return candidate
    return Path(sys.executable).parent


def main() -> None:
    script = _script()
    if not script.exists():
        sys.exit(f"dftracer_agents_stack: launcher missing at {script}")

    bash = shutil.which("bash") or "/bin/bash"
    env = os.environ.copy()
    env.setdefault("DFTRACER_STACK_BIN", str(_env_bin()))
    env.setdefault("DFTRACER_STACK_ROOT", str(_project_root()))
    env.setdefault("DFTRACER_STACK_CMD", Path(sys.argv[0]).name or "dftracer_agents_stack")

    # Package data is not guaranteed executable after install; invoke the
    # interpreter explicitly rather than relying on the mode bit.
    os.execve(bash, [bash, str(script), *sys.argv[1:]], env)


if __name__ == "__main__":
    main()
