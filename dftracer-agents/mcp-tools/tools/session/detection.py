"""
Source tree detection helpers — language, build tool, features.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional


# Common locations where HDF5 installs its headers and libraries.
_HDF5_HEADER_SEARCH = [
    "/usr/include/hdf5",
    "/usr/include/hdf5/serial",
    "/usr/local/include",
    "/usr/local/hdf5/include",
    "/opt/hdf5/include",
]

_HDF5_LIB_SEARCH = [
    "/usr/lib",
    "/usr/lib/x86_64-linux-gnu/hdf5/serial",
    "/usr/lib/x86_64-linux-gnu",
    "/usr/local/lib",
    "/usr/local/hdf5/lib",
    "/opt/hdf5/lib",
]


def _detect_system_hdf5() -> Dict[str, Any]:
    """Probe the system for an HDF5 installation.

    Tries (in order):
      1. ``pkg-config --libs hdf5``
      2. ``h5cc -showconfig`` / ``h5pcc -showconfig``
      3. ``h5dump --version``
      4. Header scan in common prefix paths

    Returns a dict with keys:
      found (bool), version (str|None), prefix (str|None), cmake_hint (str|None)
    """
    # 1. pkg-config
    for pkg in ("hdf5", "hdf5-serial"):
        try:
            r = subprocess.run(
                ["pkg-config", "--modversion", pkg],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                version = r.stdout.strip()
                prefix_r = subprocess.run(
                    ["pkg-config", "--variable=prefix", pkg],
                    capture_output=True, text=True, timeout=10,
                )
                prefix = prefix_r.stdout.strip() if prefix_r.returncode == 0 else None
                return {
                    "found": True, "version": version,
                    "prefix": prefix,
                    "cmake_hint": f"-DHDF5_ROOT={prefix}" if prefix else None,
                    "source": f"pkg-config:{pkg}",
                }
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    # 2. h5cc / h5pcc wrapper
    for wrapper in ("h5cc", "h5pcc"):
        if shutil.which(wrapper):
            try:
                r = subprocess.run(
                    [wrapper, "-showconfig"],
                    capture_output=True, text=True, timeout=10,
                )
                if r.returncode == 0:
                    m = re.search(r"HDF5 Version:\s+(\S+)", r.stdout)
                    version = m.group(1) if m else None
                    mp = re.search(r"Installation point:\s+(\S+)", r.stdout)
                    prefix = mp.group(1) if mp else None
                    return {
                        "found": True, "version": version,
                        "prefix": prefix,
                        "cmake_hint": f"-DHDF5_ROOT={prefix}" if prefix else None,
                        "source": wrapper,
                    }
            except subprocess.TimeoutExpired:
                pass

    # 3. h5dump version string
    if shutil.which("h5dump"):
        try:
            r = subprocess.run(
                ["h5dump", "--version"],
                capture_output=True, text=True, timeout=10,
            )
            m = re.search(r"(\d+\.\d+\.\d+)", r.stdout + r.stderr)
            if m:
                return {
                    "found": True, "version": m.group(1),
                    "prefix": None, "cmake_hint": None,
                    "source": "h5dump",
                }
        except subprocess.TimeoutExpired:
            pass

    # 4. Header scan
    for inc in _HDF5_HEADER_SEARCH:
        h = Path(inc) / "hdf5.h"
        if h.exists():
            # Walk up to find prefix (inc/../ → root)
            prefix = str(Path(inc).parent) if Path(inc).name == "include" else None
            return {
                "found": True, "version": None,
                "prefix": prefix,
                "cmake_hint": f"-DHDF5_ROOT={prefix}" if prefix else None,
                "source": f"header:{h}",
            }

    return {"found": False, "version": None, "prefix": None,
            "cmake_hint": None, "source": None}


def _detect_info(source_dir: Path) -> Dict[str, Any]:
    """Scan source tree; return language, build-tool, features, and dftracer hints."""
    files = [f for f in source_dir.rglob("*") if f.is_file()]
    names = {f.name for f in files}
    suffixes = {f.suffix.lower() for f in files}

    languages: List[str] = []
    if suffixes & {".c", ".h"}:
        languages.append("c")
    if suffixes & {".cpp", ".cxx", ".cc", ".hpp", ".hxx"}:
        languages.append("cpp")
    if ".py" in suffixes:
        languages.append("python")
    if suffixes & {".f90", ".f95", ".f03", ".f", ".for", ".f77"}:
        languages.append("fortran")

    if "CMakeLists.txt" in names:
        build_tool = "cmake"
    elif "configure.ac" in names or "configure.in" in names:
        build_tool = "autotools"
    elif "meson.build" in names:
        build_tool = "meson"
    elif names & {"setup.py", "pyproject.toml", "setup.cfg"}:
        build_tool = "python"
    elif names & {"Makefile", "makefile", "GNUmakefile"}:
        build_tool = "make"
    else:
        build_tool = "unknown"

    # Autotools is a C/C++ build system; .py files are build scripts, not project sources.
    # Python projects (setup.py/pyproject.toml) can contain C/C++ extensions — keep all.
    if build_tool == "autotools":
        languages = [l for l in languages if l != "python"]

    # Source scan for feature detection (cap at 5 MB to avoid huge repos)
    all_text = ""
    scannable = {".c", ".h", ".cpp", ".cxx", ".cc", ".hpp", ".py", ".f90", ".f"}
    for f in files:
        if f.suffix.lower() in scannable:
            try:
                all_text += f.read_text(errors="ignore")
                if len(all_text) > 5_000_000:
                    break
            except OSError:
                pass

    hdf5_in_source = bool(re.search(r"hdf5\.h|H5Fopen|H5Fcreate|H5Dread|H5Dwrite|h5py", all_text, re.I))
    hdf5_system = _detect_system_hdf5()

    features = {
        "mpi":      bool(re.search(r"mpi\.h|MPI_Init|MPI_Comm|mpi4py", all_text, re.I)),
        "python":   "python" in languages,
        "hdf5":     hdf5_in_source or hdf5_system["found"],
        "hdf5_in_source": hdf5_in_source,
        "hdf5_system":    hdf5_system,
        "posix_io": bool(re.search(r"\bopen\s*\(|\bfopen\s*\(|\bread\s*\(|\bwrite\s*\(", all_text)),
        "openmp":   bool(re.search(r"omp\.h|#pragma omp|import openmp", all_text, re.I)),
    }

    # Map features → dftracer cmake flags (aligns with autobuild.sh)
    dftracer_cmake_flags: List[str] = ["-DDFTRACER_ENABLE_TESTS=OFF"]
    if features["python"]:
        dftracer_cmake_flags.append("-DDFTRACER_ENABLE_PYTHON=ON")
    if features["mpi"]:
        dftracer_cmake_flags.append("-DDFTRACER_ENABLE_MPI=ON")
    if features["hdf5"]:
        dftracer_cmake_flags.append("-DDFTRACER_ENABLE_HDF5=ON")
        if hdf5_system.get("cmake_hint"):
            dftracer_cmake_flags.append(hdf5_system["cmake_hint"])

    key_files = sorted(n for n in names if n in {
        "CMakeLists.txt", "configure.ac", "setup.py", "pyproject.toml",
        "Makefile", "README.md", "README.rst", "README.txt", "INSTALL.md",
        "INSTALL", "meson.build",
    })

    # Look for readme
    readme_content = None
    for rname in ["README.md", "README.rst", "README.txt", "README", "INSTALL.md"]:
        rp = source_dir / rname
        if rp.exists():
            readme_content = rp.read_text(errors="ignore")[:6000]
            break

    return {
        "languages": languages,
        "build_tool": build_tool,
        "features": features,
        "dftracer_cmake_flags": dftracer_cmake_flags,
        "key_files": key_files,
        "readme_excerpt": readme_content,
    }
