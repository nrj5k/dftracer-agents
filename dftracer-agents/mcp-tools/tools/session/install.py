"""
dftracer install helpers — autobuild.sh wrapper and dftracer-utils installer.
"""
from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from .workspace import _run


_UTILS_SERVICE_PATH = Path(__file__).resolve().parent.parent / "dftracer" / "dftracer_utils_service.py"


def _load_dftracer_utils_service():
    """Return the dftracer_utils_service module, loading it on first call."""
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
    """Compact trace files by calling the dftracer-utils split MCP tool.

    Loads DftracerUtilsService from the sibling dftracer/ package and calls
    the split tool's underlying Python function directly (no network hop).
    Falls back to the dftracer_split binary if the service cannot be loaded.
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
    """Install dftracer-utils from the develop branch into the given pip environment."""
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
    """Clone dftracer and build+install it via autobuild.sh.

    Uses the project's own autobuild.sh so that all dependency handling,
    build flags, and install steps are exactly as dftracer intends.

    Args:
        ws:             Workspace root (dftracer_src/ cached here).
        install_prefix: Where dftracer lands (-install-prefix).
        dftracer_ref:   Git tag or branch to clone.
        jobs:           Parallel build jobs (--jobs).
        install_mode:   "cmake" for C/C++ projects (installs headers + lib);
                        "pip"   for Python projects (installs Python package).
        features:       Detected project features dict (keys: mpi, hdf5, python, …).
        python_exe:     Path to Python binary; passed as --python to enable Python
                        bindings and select the target interpreter for pip mode.
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
