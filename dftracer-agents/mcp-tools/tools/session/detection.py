"""
Source tree detection helpers — language, build tool, features, and dftracer hints.

This module implements heuristic analysis of an application's source tree so
that downstream pipeline stages (build, install, run) can make informed
decisions without requiring the user to supply build flags manually.  The
analysis operates entirely at the filesystem and text level — no build system
is invoked during detection.

The two primary entry points are:

- :func:`_detect_system_hdf5` — probes the host system for an existing HDF5
  installation using ``pkg-config``, compiler wrappers, and header path scans.
- :func:`_detect_info` — scans the source tree and returns a comprehensive
  dict covering detected languages, build tool, optional features, recommended
  dftracer CMake flags, and the first few kilobytes of the project README.

Detection is intentionally conservative: a feature is reported as present only
when a concrete indicator is found (a header include pattern, an API call
pattern, or a pkg-config entry).  This avoids generating invalid build flags
for features that happen to be mentioned in comments or documentation.

Runtime constraints: :func:`_detect_info` reads up to 5 MB of combined source
text to bound execution time on large repositories.  External tool invocations
(``pkg-config``, ``h5cc``, ``h5dump``) each carry a 10-second timeout so the
detection phase cannot block the pipeline indefinitely.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional


#: Filesystem paths searched for the ``hdf5.h`` header when no pkg-config or
#: compiler wrapper is available.  Ordered from the most common Linux
#: distribution install location to less-common prefix installs.
_HDF5_HEADER_SEARCH = [
    "/usr/include/hdf5",
    "/usr/include/hdf5/serial",
    "/usr/local/include",
    "/usr/local/hdf5/include",
    "/opt/hdf5/include",
]

#: Filesystem paths searched for HDF5 shared/static libraries.  Used
#: indirectly by :func:`_detect_system_hdf5` to infer installation prefix when
#: compiler wrappers are unavailable.
_HDF5_LIB_SEARCH = [
    "/usr/lib",
    "/usr/lib/x86_64-linux-gnu/hdf5/serial",
    "/usr/lib/x86_64-linux-gnu",
    "/usr/local/lib",
    "/usr/local/hdf5/lib",
    "/opt/hdf5/lib",
]


def _detect_system_hdf5() -> Dict[str, Any]:
    """Probe the host system for an HDF5 installation.

    Detection is attempted via four strategies in order of decreasing
    reliability:

    1. ``pkg-config --modversion hdf5`` (and ``hdf5-serial`` as a fallback
       package name) — most reliable on Debian/Ubuntu-family systems.
    2. ``h5cc -showconfig`` / ``h5pcc -showconfig`` — HDF5-bundled C/C++
       compiler wrappers that expose version and prefix information.
    3. ``h5dump --version`` — available even when only the HDF5 tools package
       is installed; yields a version string but no prefix.
    4. Header file scan across :data:`_HDF5_HEADER_SEARCH` paths — last resort
       when no HDF5 tooling is on ``PATH``; yields a prefix but no version.

    Each strategy is attempted with a 10-second timeout.  ``FileNotFoundError``
    and ``subprocess.TimeoutExpired`` are silently swallowed so that the
    function always returns a result.

    Returns:
        Dict[str, Any]: A dict with the following keys:

            - ``found`` (bool): ``True`` if any HDF5 indicator was detected.
            - ``version`` (str or None): Dotted version string such as
              ``"1.12.2"``, or ``None`` when not determinable.
            - ``prefix`` (str or None): Installation prefix directory (e.g.
              ``"/usr/local/hdf5"``), or ``None`` when not determinable.
            - ``cmake_hint`` (str or None): A ``-DHDF5_ROOT=<prefix>`` string
              suitable for passing to CMake, or ``None`` when no prefix was
              found.
            - ``source`` (str or None): Human-readable label identifying which
              detection strategy succeeded (e.g. ``"pkg-config:hdf5"``,
              ``"h5cc"``, ``"h5dump"``, or ``"header:/usr/…/hdf5.h"``).
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
    """Scan a source tree and return a comprehensive analysis dict.

    Walks *source_dir* recursively to collect file names and suffixes, then
    uses them to infer:

    - **Languages** present (C, C++, Python, Fortran) from file extensions.
    - **Build tool** (CMake, Autotools, Meson, Python packaging, or bare Make)
      from the presence of canonical build-system files.
    - **Optional features** (MPI, HDF5, POSIX I/O, OpenMP) from regex patterns
      in source text, capped at 5 MB of combined text to bound scan time.
    - **dftracer CMake flags** recommended for the detected feature set,
      consistent with the flags accepted by dftracer's own ``autobuild.sh``.
    - **Key files** (build descriptors, READMEs, INSTALL guides) present at
      the tree root.
    - **README excerpt** — the first 6 000 characters of whichever README
      variant is found first, for display to the user.

    Note:
        Autotools projects may contain Python helper scripts in their tree.
        These are filtered out of the ``languages`` list for Autotools projects
        because they are build infrastructure, not project source language.
        Python packaging projects (``setup.py``, ``pyproject.toml``) that also
        contain C/C++ extension code keep all detected languages.

    Args:
        source_dir: Absolute path to the root of the application source tree.
            The directory must already exist and be readable.

    Returns:
        Dict[str, Any]: A dict with the following keys:

            - ``languages`` (List[str]): Detected source languages, e.g.
              ``["c", "cpp", "python"]``.  Order reflects discovery order, not
              priority.
            - ``build_tool`` (str): One of ``"cmake"``, ``"autotools"``,
              ``"meson"``, ``"python"``, ``"make"``, or ``"unknown"``.
            - ``features`` (Dict[str, Any]): Feature flags and HDF5 probe
              results.  Keys: ``"mpi"`` (bool), ``"python"`` (bool),
              ``"hdf5"`` (bool), ``"hdf5_in_source"`` (bool),
              ``"hdf5_system"`` (dict from :func:`_detect_system_hdf5`),
              ``"posix_io"`` (bool), ``"openmp"`` (bool).
            - ``dftracer_cmake_flags`` (List[str]): CMake ``-D`` flags to pass
              when building dftracer for this project.  Always contains
              ``"-DDFTRACER_ENABLE_TESTS=OFF"``; additional flags are appended
              based on detected features.
            - ``key_files`` (List[str]): Sorted list of notable file names
              found at the source tree root (build descriptors and docs).
            - ``readme_excerpt`` (str or None): First 6 000 characters of the
              README, or ``None`` if no README was found.
    """
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
