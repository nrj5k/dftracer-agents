"""
dftracer install helpers — autobuild.sh wrapper and dftracer-utils installer.

This module handles the two external installation concerns of the dftracer
session pipeline:

1. **dftracer itself** (:func:`_install_dftracer_autobuild`) — clones the
   dftracer repository at a requested Git ref and delegates the entire build
   and install process to the project's own ``autobuild.sh`` script.  Using
   the upstream script ensures that all dependency detection, CMake flag
   handling, and install-tree layout are exactly as the dftracer project
   intends, rather than being re-implemented here.

2. **dftracer-utils** (:func:`_install_dftracer_utils`,
   :func:`_dftracer_utils_split`) — installs the ``dftracer-utils`` Python
   package (post-processing tools) from the upstream ``develop`` branch and
   provides a helper that compacts raw trace files via the ``split`` MCP tool.

The split helper (:func:`_dftracer_utils_split`) uses dynamic module loading
(:func:`_load_dftracer_utils_service`) to call the split tool's Python
function directly in-process, avoiding a network round-trip to an MCP server.
It falls back transparently to the ``dftracer_split`` CLI binary when the
service module cannot be loaded.

Runtime constraints:
- Git clone operations time out after 600 seconds.
- The full dftracer build (``autobuild.sh``) may take up to 1 800 seconds on
  slow hardware; plan accordingly when setting pipeline stage timeouts.
- The cloned dftracer source is cached at ``<workspace>/dftracer_src/`` so
  that retries after a build failure do not re-download the repository.
"""
from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from .workspace import _run


#: Absolute path to the ``dftracer_utils_service.py`` module located in the
#: sibling ``dftracer/`` package directory.  Resolved at import time so that
#: :func:`_load_dftracer_utils_service` can use it without re-computing the
#: path on every call.
_UTILS_SERVICE_PATH = Path(__file__).resolve().parent.parent / "dftracer" / "dftracer_utils_service.py"


def _load_dftracer_utils_service():
    """Return the ``dftracer_utils_service`` module, loading it dynamically on first call.

    The module is registered in ``sys.modules`` under the key
    ``"dftracer_agents.mcp_tools.tools.dftracer_utils_service"`` after the
    first successful load, so subsequent calls return the cached module object
    without re-executing the module code.

    Using dynamic loading (rather than a normal import) avoids making
    ``dftracer_utils_service`` a hard dependency of this package: if the file
    does not exist — for example in a minimal install that omits the dftracer
    subpackage — the function returns ``None`` and callers fall back gracefully.

    Returns:
        module or None: The loaded ``dftracer_utils_service`` module, or
            ``None`` if :data:`_UTILS_SERVICE_PATH` does not exist on disk.
    """
    mod_name = "dftracer_agents.mcp_tools.tools.dftracer_utils_service"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    if not _UTILS_SERVICE_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location(mod_name, _UTILS_SERVICE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _dftracer_utils_split(
    directory: str,
    output_dir: str,
    app_name: str = "app",
) -> Dict[str, Any]:
    """Compact raw dftracer trace files using the ``split`` tool.

    Attempts to invoke the split operation in-process via the
    ``DftracerUtilsService`` MCP service (loaded dynamically by
    :func:`_load_dftracer_utils_service`).  This avoids the overhead of an
    out-of-process call when the service module is available.

    The in-process path introspects the tool object returned by
    ``service.core_subservice.list_tools()`` for a callable attribute
    (trying ``fn``, ``function``, ``callable``, ``handler``, ``_fn`` in
    order) and invokes it with keyword arguments.  Both synchronous and
    ``async`` tool functions are supported.

    If the service module cannot be loaded or any step of the in-process path
    raises an exception, the function falls back to invoking the
    ``dftracer_split`` CLI binary directly via :func:`~workspace._run`.

    Args:
        directory: Path to the directory containing raw ``.pfw`` trace files
            produced by a dftracer-instrumented application run.
        output_dir: Destination directory for the compacted output files.
            Created by the split tool if it does not exist.
        app_name: Application name tag embedded in the output file names.
            Defaults to ``"app"``.

    Returns:
        Dict[str, Any]: A normalised result dict with keys:

            - ``success`` (bool): ``True`` on success.
            - ``returncode`` (int): Exit code (``0`` on success, non-zero or
              ``-1`` on failure).
            - ``stdout`` (str): Captured output or tool result string.
            - ``stderr`` (str): Error output or exception message.
    """
    mod = _load_dftracer_utils_service()
    if mod is not None:
        try:
            service = mod.DftracerUtilsService()
            tools = asyncio.run(service.core_subservice.list_tools())
            split_tool = next((t for t in tools if t.name == "split"), None)
            if split_tool is not None:
                fn = None
                for attr in ("fn", "function", "callable", "handler", "_fn"):
                    val = getattr(split_tool, attr, None)
                    if callable(val):
                        fn = val
                        break
                if fn is not None:
                    try:
                        kwargs = {"directory": directory, "output_dir": output_dir,
                                  "app_name": app_name}
                        result = (asyncio.run(fn(**kwargs))
                                  if asyncio.iscoroutinefunction(fn) else fn(**kwargs))
                        return {"success": True, "returncode": 0,
                                "stdout": str(result), "stderr": ""}
                    except subprocess.CalledProcessError as exc:
                        return {"success": False, "returncode": exc.returncode,
                                "stdout": "", "stderr": getattr(exc, "stderr", str(exc))}
        except Exception:
            pass  # fall through to binary fallback

    # Fallback: call binary directly
    return _run(
        ["dftracer_split", "-n", app_name, "-d", directory, "-o", output_dir],
        timeout=600,
    )


def _install_dftracer_utils(pip: Path) -> Dict[str, Any]:
    """Install the ``dftracer-utils`` package from its upstream ``develop`` branch.

    Runs ``pip install --upgrade git+https://…/dftracer-utils.git@develop``
    using the pip executable at *pip*, which may belong to a virtual
    environment or a specific Python interpreter selected by the pipeline.

    Args:
        pip: Absolute path to the ``pip`` (or ``pip3``) executable to use for
            installation.  Must be writable by the current process; if a
            virtual environment is in use the venv's pip should be supplied.

    Returns:
        Dict[str, Any]: A normalised result dict as returned by
            :func:`~workspace._run`.  Check the ``"success"`` key to determine
            whether installation succeeded.
    """
    return _run(
        [str(pip), "install", "--upgrade",
         "git+https://github.com/llnl/dftracer-utils.git@develop"],
        timeout=600,
    )


def _install_dftracer_autobuild(
    ws: Path,
    install_prefix: Path,
    dftracer_ref: str = "v2.0.3",
    jobs: int = 4,
    install_mode: str = "cmake",
    features: Optional[Dict[str, Any]] = None,
    python_exe: Optional[str] = None,
) -> Dict[str, Any]:
    """Clone dftracer and build and install it via the project's own ``autobuild.sh``.

    Delegates the entire build to dftracer's upstream ``autobuild.sh`` so that
    dependency detection, CMake configuration, compiler selection, and the
    install-tree layout all follow the project's own conventions rather than
    being duplicated here.

    The dftracer source is cloned into ``<ws>/dftracer_src/`` on the first
    call.  Subsequent calls (e.g. after a build failure) reuse the existing
    clone, so the ``--depth=1`` git clone is performed at most once per
    workspace.  The CMake build directory is placed at ``<ws>/dftracer_build/``
    via the ``BUILD_DIR`` environment variable consumed by ``autobuild.sh``.

    Feature flags detected by :func:`~detection._detect_info` (``mpi``,
    ``hdf5``) are forwarded to ``autobuild.sh`` as ``--enable-mpi`` and
    ``--enable-hdf5`` switches so that the installed dftracer library matches
    the needs of the application being instrumented.

    Note:
        The build step may take up to 30 minutes on slow hardware.  The
        function blocks until ``autobuild.sh`` exits or the 1 800-second
        timeout is reached.  Ensure the calling MCP tool's timeout is set
        accordingly.

    Args:
        ws: Workspace root directory (absolute ``Path``).  Source and build
            trees are created as sub-directories of this path.
        install_prefix: Installation prefix passed as ``--install-prefix`` to
            ``autobuild.sh``.  dftracer headers, libraries, and CMake config
            files are placed under this directory.
        dftracer_ref: Git tag or branch name to clone.  Defaults to
            ``"v2.0.3"``.  Must be a valid ref on the
            ``https://github.com/llnl/dftracer.git`` remote.
        jobs: Number of parallel build jobs forwarded as ``--jobs`` to
            ``autobuild.sh``.  Defaults to ``4``.
        install_mode: Controls what ``autobuild.sh`` installs.  Use
            ``"cmake"`` for C/C++ projects (installs headers, libraries, and
            CMake package config files); use ``"pip"`` for pure-Python projects
            (installs the dftracer Python package into the target interpreter).
        features: Detected project feature dict as returned by
            :func:`~detection._detect_info`.  Relevant keys: ``"mpi"`` (bool)
            and ``"hdf5"`` (bool).  When ``None`` no feature flags are passed.
        python_exe: Absolute path to the Python interpreter to use for Python
            bindings and pip-mode installation.  Forwarded as ``--python`` to
            ``autobuild.sh``.  When ``None`` Python support is not explicitly
            requested (though ``autobuild.sh`` may still auto-detect it).

    Returns:
        Dict[str, Any]: A dict with the following keys:

            - ``success`` (bool): ``True`` if all steps completed without
              error.
            - ``steps`` (Dict[str, Any]): Per-step result dicts.  Keys:
              ``"clone"`` (git clone result or reuse notice) and
              ``"autobuild"`` (``autobuild.sh`` execution result).
            - ``prefix`` (str): String form of *install_prefix* (present only
              on success or when ``autobuild.sh`` was reached regardless of
              its exit code).
            - ``error`` (str): Human-readable error message (present only when
              a pre-build check fails, e.g. ``autobuild.sh`` is missing from
              the cloned source).
    """
    features = features or {}
    src = ws / "dftracer_src"
    bld = ws / "dftracer_build"
    bld.mkdir(exist_ok=True)
    steps: Dict[str, Any] = {}

    # Clone once; reuse on subsequent calls (e.g. retry after a failure)
    if not src.exists():
        r = _run(
            ["git", "clone", "--depth=1", "--branch", dftracer_ref,
             "https://github.com/llnl/dftracer.git", str(src)],
            timeout=600,
        )
        steps["clone"] = r
        if not r["success"]:
            return {"success": False, "steps": steps}
    else:
        steps["clone"] = {"status": "reused", "path": str(src)}

    autobuild = src / "autobuild.sh"
    if not autobuild.exists():
        return {
            "success": False,
            "steps": steps,
            "error": "autobuild.sh not found in cloned dftracer source",
        }

    cmd = [
        "/bin/bash", str(autobuild),
        "--install-prefix", str(install_prefix),
        "--install-mode", install_mode,
        "--build-type", "RelWithDebInfo",
        "--jobs", str(jobs),
        "--quiet",
        "--skip-smoke-test",
    ]

    # Feature flags — mirror what autobuild.sh exposes
    if python_exe:
        cmd += ["--python", python_exe]
    if features.get("mpi"):
        cmd += ["--enable-mpi"]
    if features.get("hdf5"):
        cmd += ["--enable-hdf5"]

    # BUILD_DIR via env so autobuild.sh places its build tree inside the workspace
    r = _run(cmd, cwd=src, env={"BUILD_DIR": str(bld)}, timeout=1800)
    steps["autobuild"] = r
    return {"success": r["success"], "steps": steps, "prefix": str(install_prefix)}
