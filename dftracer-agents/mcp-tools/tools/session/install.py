"""
dftracer install helpers — pip installer, cmake builder, and dftracer-utils installer.

This module handles the external installation concerns of the dftracer pipeline:

1. **dftracer via pip** (:func:`_install_dftracer_pip_direct`) — installs
   dftracer directly via ``pip install git+https://github.com/llnl/dftracer.git@<ref>``
   with all setup.py feature env vars (MPI, HDF5, HIP, hwloc, build type, jobs)
   derived from the detected application source and system.  This is the
   standard installation method for all project types.

2. **dftracer via cmake** (:func:`_install_dftracer_cmake`) — clones the
   dftracer repository and builds/installs it directly via cmake configure +
   build + install steps.  Available as a lower-level alternative; prefer pip.

3. **dftracer-utils** (:func:`_install_dftracer_utils`,
   :func:`_dftracer_utils_split`) — installs the ``dftracer-utils`` Python
   package (post-processing tools) from the upstream ``develop`` branch and
   provides a helper that compacts raw trace files via the ``split`` MCP tool.

The split helper (:func:`_dftracer_utils_split`) uses dynamic module loading
(:func:`_load_dftracer_utils_service`) to call the split tool's Python
function directly in-process, avoiding a network round-trip to an MCP server.
It falls back transparently to the ``dftracer_split`` CLI binary when the
service module cannot be loaded.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .workspace import _run, _write_artifact_log


_CORE_LIB_NAMES = ("libdftracer_core.so", "libdftracer_core.so.4",
                    "libdftracer_core.dylib")
_CORE_HEADER    = Path("dftracer") / "dftracer.h"


def _find_dftracer_dirs(
    python_exe: Optional[str] = None,
    cmake_prefix: Optional[Path] = None,
) -> Optional[Dict[str, str]]:
    """Locate dftracer include and lib directories, checking every known layout.

    Search order (stops at the first lib dir that contains ``libdftracer_core.so``):

    1. **cmake prefix** — ``<cmake_prefix>/lib/`` and ``<cmake_prefix>/lib64/``
       (produced by a cmake-based dftracer build).
    2. **site-packages pkg dir** — ``<pkg>/lib/`` and ``<pkg>/lib64/``
       (pip wheel layout when the core lib is bundled there).
    3. **site-packages parent** — ``<site-packages>/lib/`` and ``<site-packages>/lib64/``
       (some wheels install the shared lib one level above the package dir).

    The include dir paired with each lib candidate is resolved by looking for
    ``dftracer/dftracer.h`` relative to the same prefix (``<prefix>/include/``).

    Args:
        python_exe: Path to the Python interpreter used to locate the dftracer
            package directory.  Defaults to the current interpreter.
        cmake_prefix: Optional workspace cmake install prefix (``install_ann/``).
            When supplied it is tried first so that cmake-mode installs win.

    Returns:
        Dict with ``include_dir``, ``lib_dir``, and ``lib_name`` on success, or
        ``None`` when no dir containing ``libdftracer_core.so`` is found.
    """
    import json as _json

    def _has_core(d: Path) -> bool:
        return d.is_dir() and any((d / n).exists() for n in _CORE_LIB_NAMES)

    def _include_for(prefix: Path) -> str:
        inc = prefix / "include"
        return str(inc) if (inc / _CORE_HEADER).exists() else str(inc)

    def _lib_name(d: Path) -> str:
        for n in _CORE_LIB_NAMES:
            if (d / n).exists():
                return n
        return "libdftracer_core.so"

    candidates: list[Path] = []

    # 1. cmake prefix (install_ann) — highest priority
    if cmake_prefix:
        candidates += [cmake_prefix / "lib", cmake_prefix / "lib64"]

    # 2. site-packages package dir  (pip wheel: <pkg>/lib, <pkg>/lib64)
    py = python_exe or sys.executable
    script = (
        "import dftracer, os, json; "
        "base = os.path.dirname(os.path.abspath(dftracer.__file__)); "
        "parent = os.path.dirname(base); "
        "print(json.dumps({'pkg': base, 'parent': parent}))"
    )
    try:
        proc = subprocess.run([py, "-c", script],
                              capture_output=True, text=True, timeout=30)
        if proc.returncode == 0 and proc.stdout.strip():
            info = _json.loads(proc.stdout.strip())
            pkg    = Path(info["pkg"])
            parent = Path(info["parent"])
            candidates += [
                pkg    / "lib",  pkg    / "lib64",
                parent / "lib",  parent / "lib64",
            ]
    except Exception:
        pass

    for lib_dir in candidates:
        if _has_core(lib_dir):
            prefix = lib_dir.parent
            return {
                "include_dir": _include_for(prefix),
                "lib_dir":     str(lib_dir),
                "lib_name":    _lib_name(lib_dir),
            }

    # Nothing found — return the cmake prefix dirs anyway so callers can still
    # set RPATH; the build will fail with a clear linker error rather than a
    # silent wrong-path error.
    if cmake_prefix and (cmake_prefix / "lib").is_dir():
        return {
            "include_dir": str(cmake_prefix / "include"),
            "lib_dir":     str(cmake_prefix / "lib"),
            "lib_name":    "libdftracer_core.so",
        }
    return None


# Keep old name as alias so existing callers don't break
_find_dftracer_pip_dirs = _find_dftracer_dirs


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
    try:
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        # C3: module has relative imports that fail outside the installed package
        # (e.g. "No module named 'dftracer_agents.mcp_service_factory'").
        # Clean up the partial registration and let callers fall back to the binary.
        sys.modules.pop(mod_name, None)
        return None


def _dftracer_info_uncompressed_bytes(file_path: str) -> Optional[int]:
    """Return the uncompressed byte count for a single trace file via dftracer_info.

    Calls ``dftracer_info --files <file> --query summary`` and parses the
    ``Total Uncompressed: ... (<N> bytes)`` line from the output.

    Returns the byte count on success, or ``None`` if the call fails or the
    line cannot be parsed.
    """
    import re as _re
    r = _run(["dftracer_info", "--files", file_path, "--query", "summary"], timeout=60)
    if not r["success"]:
        return None
    m = _re.search(r"Total Uncompressed:.*?\((\d+)\s+bytes\)", r["stdout"])
    if m:
        return int(m.group(1))
    return None


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


def _dftracer_utils_comparator(
    baseline: str,
    variant: str,
    query: str = 'cat == "POSIX" OR cat == "STDIO" OR cat == "C_APP"',
    group_by_dims: str = "cat,name",
    output_format: str = "json",
    threshold_pct: float = 5.0,
) -> Dict[str, Any]:
    """Compare trace metrics between two runs via the DftracerUtilsService comparator tool.

    Invokes ``DftracerUtilsService.analysis_subservice`` comparator in-process
    (same dynamic-loading pattern as :func:`_dftracer_utils_split`).  Falls
    back to the ``dftracer_comparator`` CLI binary if the service cannot be
    loaded.

    Returns:
        Dict[str, Any]: keys ``success``, ``returncode``, ``stdout``, ``stderr``.
    """
    mod = _load_dftracer_utils_service()
    if mod is not None:
        try:
            service = mod.DftracerUtilsService()
            tools = asyncio.run(service.analysis_subservice.list_tools())
            cmp_tool = next((t for t in tools if t.name == "comparator"), None)
            if cmp_tool is not None:
                fn = None
                for attr in ("fn", "function", "callable", "handler", "_fn"):
                    val = getattr(cmp_tool, attr, None)
                    if callable(val):
                        fn = val
                        break
                if fn is not None:
                    try:
                        kwargs = {
                            "baseline": baseline,
                            "variant": variant,
                            "query": query,
                            "group_by_dims": group_by_dims,
                            "output_format": output_format,
                            "threshold_pct": threshold_pct,
                        }
                        result = (
                            asyncio.run(fn(**kwargs))
                            if asyncio.iscoroutinefunction(fn)
                            else fn(**kwargs)
                        )
                        return {"success": True, "returncode": 0,
                                "stdout": str(result), "stderr": ""}
                    except subprocess.CalledProcessError as exc:
                        return {"success": False, "returncode": exc.returncode,
                                "stdout": "", "stderr": getattr(exc, "stderr", str(exc))}
        except Exception:
            pass  # fall through to binary fallback

    # Fallback: call binary directly
    cmd = [
        "dftracer_comparator",
        "--baseline", baseline,
        "--variant", variant,
        "--query", query,
        "--format", output_format,
        "--threshold", str(threshold_pct),
    ]
    if group_by_dims:
        cmd += ["--group-by", group_by_dims]
    return _run(cmd, timeout=120)


def _ensure_session_venv(ws: Path) -> Path:
    """Create an isolated venv at ``<ws>/venv/`` and return its Python executable.

    The venv is created once and reused on subsequent calls.  It is completely
    isolated from the MCP server's own Python environment (no ``--system-site-packages``),
    so every package installed into it — dftracer, dftracer-utils, and any
    project dependencies — is confined to the workspace directory.

    Args:
        ws: Workspace root directory (absolute ``Path``).

    Returns:
        Path to the venv's Python interpreter (``<ws>/venv/bin/python``).

    Raises:
        RuntimeError: If venv creation fails (propagated from ``_run``).
    """
    venv_dir = ws / "venv"
    python = venv_dir / "bin" / "python"
    if not python.exists():
        r = _run(
            [sys.executable, "-m", "venv", "--clear", str(venv_dir)],
            timeout=120,
        )
        if not r["success"]:
            raise RuntimeError(
                f"Failed to create session venv at {venv_dir}: {r['stderr']}"
            )
        # Upgrade pip inside the fresh venv
        _run(
            [str(python), "-m", "pip", "install", "--no-cache-dir",
             "--quiet", "--upgrade", "pip"],
            timeout=120,
        )
    return python


def _install_dftracer_utils(
    pip: Path,
    ws: Optional[Path] = None,
    run_id: str = "",
) -> Dict[str, Any]:
    """Install the ``dftracer-utils`` package from its upstream ``develop`` branch.

    Runs ``pip install -v --upgrade git+https://…/dftracer-utils.git@develop``
    using the pip executable at *pip*.  When *ws* is supplied the full verbose
    output is written to ``<ws>/artifacts/06b_session_install_dftracer_utils.log``.

    Args:
        pip: Absolute path to the ``pip`` executable to use.
        ws: Workspace root for artifact logging.  Optional.
        run_id: Session run identifier for the artifact log header.

    Returns:
        Dict[str, Any]: A normalised result dict as returned by
            :func:`~workspace._run`.
    """
    r = _run(
        [str(pip), "install", "-v", "--no-cache-dir", "--upgrade",
         "git+https://github.com/llnl/dftracer-utils.git@develop"],
        timeout=600,
    )
    if ws is not None:
        _write_artifact_log(ws, 6, "session_install_dftracer_utils",
                            {"pip_cmd": str(pip), "pip_install_utils": r}, run_id)
    return r


def _install_dftracer_cmake(
    ws: Path,
    install_prefix: Path,
    dftracer_ref: str = "v2.0.3",
    jobs: int = 4,
    features: Optional[Dict[str, Any]] = None,
    extra_cmake_flags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Clone dftracer and build/install it directly via cmake.

    Runs three cmake invocations:

    1. ``cmake -S <src> -B <bld> -DCMAKE_INSTALL_PREFIX=<prefix> <flags>``
    2. ``cmake --build <bld> --parallel <jobs>``
    3. ``cmake --install <bld>``

    Feature flags from *features* are translated to cmake ``-D`` flags:

    * ``-DDFTRACER_ENABLE_MPI=ON``   when ``features["mpi"]`` is ``True``
    * ``-DDFTRACER_ENABLE_HDF5=ON``  when ``features["hdf5"]`` is ``True``
    * ``-DHDF5_ROOT=<prefix>``       when ``hdf5_system.prefix`` is known
    * ``-DDFTRACER_ENABLE_PYTHON=OFF``  always — Python bindings are not
      needed for C/C++ annotation and pybind11 adds unnecessary build overhead.

    The dftracer source is cloned once into ``<ws>/dftracer_src/``.
    Subsequent calls reuse the existing clone.  The cmake build tree lives
    at ``<ws>/dftracer_build/``.

    Args:
        ws: Workspace root directory.
        install_prefix: cmake install prefix (``-DCMAKE_INSTALL_PREFIX``).
        dftracer_ref: Git tag or branch to clone.  Defaults to ``"v2.0.3"``.
        jobs: Parallel build jobs.  Defaults to ``4``.
        features: Detected project feature dict from ``_detect_info``.
            Relevant keys: ``"mpi"`` (bool), ``"hdf5"`` (bool),
            ``"hdf5_system"`` (dict with ``"cmake_hint"``).
        extra_cmake_flags: Additional ``-D`` flags appended verbatim after the
            auto-detected flags.  Duplicates are suppressed.

    Returns:
        Dict[str, Any]: Keys ``success`` (bool), ``steps`` (per-step results),
        ``prefix`` (str form of *install_prefix*).
    """
    features = features or {}
    src = ws / "dftracer_src"
    bld = ws / "dftracer_build"
    bld.mkdir(parents=True, exist_ok=True)
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
            return {"success": False, "steps": steps, "prefix": str(install_prefix)}
    else:
        steps["clone"] = {"status": "reused", "path": str(src)}

    # Base cmake flags — Python/pybind11 disabled for C/C++ projects
    cmake_flags: List[str] = [
        f"-DCMAKE_INSTALL_PREFIX={install_prefix}",
        "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
        "-DDFTRACER_ENABLE_TESTS=OFF",
        "-DDFTRACER_ENABLE_PYTHON=OFF",
    ]

    if features.get("mpi"):
        cmake_flags.append("-DDFTRACER_ENABLE_MPI=ON")
    if features.get("hdf5"):
        cmake_flags.append("-DDFTRACER_ENABLE_HDF5=ON")
        hdf5_hint = (features.get("hdf5_system") or {}).get("cmake_hint", "")
        if hdf5_hint:
            cmake_flags.append(hdf5_hint)

    for flag in (extra_cmake_flags or []):
        if flag not in cmake_flags:
            cmake_flags.append(flag)

    # 1. cmake configure
    r_cfg = _run(
        ["cmake", "-S", str(src), "-B", str(bld)] + cmake_flags,
        timeout=300,
    )
    steps["cmake_configure"] = r_cfg
    if not r_cfg["success"]:
        return {"success": False, "steps": steps, "prefix": str(install_prefix)}

    # 2. cmake build
    r_bld = _run(
        ["cmake", "--build", str(bld), "--parallel", str(jobs)],
        timeout=1800,
    )
    steps["cmake_build"] = r_bld
    if not r_bld["success"]:
        return {"success": False, "steps": steps, "prefix": str(install_prefix)}

    # 3. cmake install
    r_inst = _run(
        ["cmake", "--install", str(bld)],
        timeout=300,
    )
    steps["cmake_install"] = r_inst
    return {
        "success": r_inst["success"],
        "steps": steps,
        "prefix": str(install_prefix),
    }


def _install_dftracer_pip_direct(
    dftracer_ref: str = "v2.0.3",
    features: Optional[Dict[str, Any]] = None,
    python_exe: Optional[str] = None,
    jobs: int = 4,
    pip_env_override: Optional[Dict[str, str]] = None,
    ws: Optional[Path] = None,
    run_id: str = "",
) -> Dict[str, Any]:
    """Install dftracer via pip with all setup.py env vars derived from detected features.

    Runs::

        pip install -v --no-cache-dir --upgrade git+https://github.com/llnl/dftracer.git@<ref>

    Environment variables are built from ``features["dftracer_pip_env"]`` (the
    complete dict produced by ``_detect_info``) and supplemented with fallback
    logic for callers that supply a raw ``features`` dict without
    ``dftracer_pip_env``.  The full set of variables passed to ``setup.py``:

    Always set:
      ``DFTRACER_BUILD_TYPE=RelWithDebInfo``
      ``DFTRACER_ENABLE_TESTS=OFF``
      ``DFTRACER_ENABLE_DLIO_BENCHMARK_TESTS=OFF``
      ``DFTRACER_ENABLE_PAPER_TESTS=OFF``
      ``JOBS=<jobs>``
      ``CMAKE_BUILD_PARALLEL_LEVEL=<jobs>``

    Set when detected in source/system:
      ``DFTRACER_ENABLE_MPI=ON``          — MPI headers/calls found in source
      ``DFTRACER_ENABLE_HDF5=ON``         — HDF5 headers/calls found in source or system
      ``HDF5_ROOT=<prefix>``              — system HDF5 prefix (pkg-config / h5cc)
      ``HDF5_DIR=<prefix>``              — same as HDF5_ROOT
      ``DFTRACER_ENABLE_HIP_TRACING=ON``  — HIP GPU headers/calls found in source
      ``DFTRACER_DISABLE_HWLOC=OFF``      — hwloc dev libs found on system

    Args:
        dftracer_ref: Git tag or branch to install.  Defaults to ``"v2.0.3"``.
        features: Detected project feature dict from ``_detect_info``.  Uses
            ``features["dftracer_pip_env"]`` when present; falls back to
            building the env from individual feature flags.
        python_exe: Python interpreter path.  Defaults to ``sys.executable``.
        jobs: Parallel build jobs passed as ``JOBS`` and
            ``CMAKE_BUILD_PARALLEL_LEVEL``.  Defaults to ``4``.
        pip_env_override: Optional dict of additional env vars that are merged
            on top of the computed env (caller-supplied overrides take priority).
        ws: Workspace root for artifact logging.  When supplied the full verbose
            pip output is written to ``<ws>/artifacts/06_session_install_dftracer.log``.
        run_id: Session run identifier for the artifact log header.

    Returns:
        Dict[str, Any]: Keys ``success`` (bool), ``steps`` (pip_install result),
        ``pip_env`` (the env dict actually used, for diagnostics).
    """
    features = features or {}
    py = python_exe or sys.executable

    # Start from the pre-built pip_env if detection produced one
    pip_env: Dict[str, str] = dict(features.get("dftracer_pip_env") or {})

    # Always-on defaults (fill gaps when dftracer_pip_env is absent or partial)
    pip_env.setdefault("DFTRACER_BUILD_TYPE", "RelWithDebInfo")
    pip_env.setdefault("DFTRACER_ENABLE_TESTS", "OFF")
    pip_env.setdefault("DFTRACER_ENABLE_DLIO_BENCHMARK_TESTS", "OFF")
    pip_env.setdefault("DFTRACER_ENABLE_PAPER_TESTS", "OFF")

    # Feature fallbacks (in case caller passed features without dftracer_pip_env)
    if features.get("mpi"):
        pip_env.setdefault("DFTRACER_ENABLE_MPI", "ON")
    if features.get("hdf5"):
        pip_env.setdefault("DFTRACER_ENABLE_HDF5", "ON")
        hdf5_prefix = (features.get("hdf5_system") or {}).get("prefix") or ""
        if hdf5_prefix:
            pip_env.setdefault("HDF5_ROOT", hdf5_prefix)
            pip_env.setdefault("HDF5_DIR", hdf5_prefix)
    if features.get("hip"):
        pip_env.setdefault("DFTRACER_ENABLE_HIP_TRACING", "ON")
    if features.get("hwloc"):
        pip_env.setdefault("DFTRACER_DISABLE_HWLOC", "OFF")

    # Build parallelism
    pip_env["JOBS"] = str(jobs)
    pip_env["CMAKE_BUILD_PARALLEL_LEVEL"] = str(jobs)

    # Caller overrides win
    if pip_env_override:
        pip_env.update(pip_env_override)

    r = _run(
        [py, "-m", "pip", "install", "-v", "--no-cache-dir", "--upgrade",
         f"git+https://github.com/llnl/dftracer.git@{dftracer_ref}"],
        env=pip_env,
        timeout=900,
    )
    if ws is not None:
        _write_artifact_log(ws, 6, "session_install_dftracer", {
            "python_exe": py,
            "dftracer_ref": dftracer_ref,
            "pip_env": str(pip_env),
            "pip_install": r,
        }, run_id)
    return {"success": r["success"], "steps": {"pip_install": r}, "pip_env": pip_env}


