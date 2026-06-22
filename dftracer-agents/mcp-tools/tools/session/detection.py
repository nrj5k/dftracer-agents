"""
Source tree detection helpers — language, build tool, features, and dftracer pip env vars.

This module implements heuristic analysis of an application's source tree so
that downstream pipeline stages (build, install, run) can make informed
decisions without requiring the user to supply build flags manually.  The
analysis operates entirely at the filesystem and text level — no build system
is invoked during detection.

Primary entry points:

- :func:`_detect_system_hdf5` — probes the host system for an existing HDF5
  installation using ``pkg-config``, compiler wrappers, and header path scans.
- :func:`_detect_system_hwloc` — probes the host system for hwloc dev libs.
- :func:`_detect_info` — scans the source tree and returns a comprehensive
  dict covering detected languages, build tool, optional features, the complete
  ``dftracer_pip_env`` dict ready to pass to ``pip install``, and the first few
  kilobytes of the project README.

``dftracer_pip_env`` maps directly to the environment variables read by
dftracer's ``setup.py``.  All options supported by setup.py are covered:

  DFTRACER_ENABLE_MPI              ON if MPI detected in source
  DFTRACER_ENABLE_HDF5             ON if HDF5 detected in source or system
  HDF5_ROOT / HDF5_DIR             set when system HDF5 prefix is known
  DFTRACER_ENABLE_HIP_TRACING      ON if HIP GPU code detected in source
  DFTRACER_DISABLE_HWLOC           OFF if hwloc dev libs found; absent otherwise
  DFTRACER_BUILD_TYPE              RelWithDebInfo (always)
  DFTRACER_ENABLE_TESTS            OFF (always)
  DFTRACER_ENABLE_DLIO_BENCHMARK_TESTS  OFF (always)
  DFTRACER_ENABLE_PAPER_TESTS      OFF (always)
  JOBS / CMAKE_BUILD_PARALLEL_LEVEL    set at install time from jobs param

Detection is intentionally conservative: a feature is reported as present only
when a concrete indicator is found (a header include pattern, an API call
pattern, or a pkg-config entry).

Runtime constraints: :func:`_detect_info` reads up to 5 MB of combined source
text to bound execution time on large repositories.  External tool invocations
each carry a 10-second timeout so the detection phase cannot block the pipeline.
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

#: dftracer-compatible HDF5 (major, minor) series.
#: Any patch within these series is accepted; the specific recommended release
#: for each series is listed in :data:`_HDF5_RECOMMENDED_VERSIONS`.
_HDF5_COMPATIBLE_SERIES: set = {(1, 8), (1, 10), (1, 12), (1, 14)}

#: Recommended specific version string for each compatible HDF5 series.
#: Use these when building HDF5 from source; prefer the highest series (1.14.x).
_HDF5_RECOMMENDED_VERSIONS: Dict[tuple, str] = {
    (1, 8):  "1.8.23",
    (1, 10): "1.10.5",
    (1, 12): "1.12.3",
    (1, 14): "1.14.5",
}

#: Preferred default when no compatible system HDF5 is found.
_HDF5_DEFAULT_VERSION = "1.14.5"


def _hdf5_version_compatible(version: Optional[str]) -> bool:
    """Return True if *version* belongs to a dftracer-compatible HDF5 series.

    Accepts dotted strings such as ``"1.14.3"`` or ``"1.12.2"``.  Returns
    ``False`` when *version* is ``None`` or cannot be parsed.
    """
    if not version:
        return False
    parts = version.split(".")
    try:
        series = (int(parts[0]), int(parts[1]))
    except (IndexError, ValueError):
        return False
    return series in _HDF5_COMPATIBLE_SERIES


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
            - ``compatible`` (bool): ``True`` if the detected version belongs
              to a dftracer-compatible HDF5 series (1.8.x, 1.10.x, 1.12.x,
              or 1.14.x).  Always ``False`` when ``version`` is ``None``.
            - ``recommended`` (str or None): The preferred specific release for
              the detected series (e.g. ``"1.14.5"``), or ``None`` when the
              version could not be determined.
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
                compat = _hdf5_version_compatible(version)
                series = tuple(int(x) for x in version.split(".")[:2]) if version else None
                return {
                    "found": True, "version": version,
                    "prefix": prefix,
                    "cmake_hint": f"-DHDF5_ROOT={prefix}" if prefix else None,
                    "source": f"pkg-config:{pkg}",
                    "compatible": compat,
                    "recommended": _HDF5_RECOMMENDED_VERSIONS.get(series) if series else None,
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
                    compat = _hdf5_version_compatible(version)
                    series = tuple(int(x) for x in version.split(".")[:2]) if version else None
                    return {
                        "found": True, "version": version,
                        "prefix": prefix,
                        "cmake_hint": f"-DHDF5_ROOT={prefix}" if prefix else None,
                        "source": wrapper,
                        "compatible": compat,
                        "recommended": _HDF5_RECOMMENDED_VERSIONS.get(series) if series else None,
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
                version = m.group(1)
                compat = _hdf5_version_compatible(version)
                series = tuple(int(x) for x in version.split(".")[:2])
                return {
                    "found": True, "version": version,
                    "prefix": None, "cmake_hint": None,
                    "source": "h5dump",
                    "compatible": compat,
                    "recommended": _HDF5_RECOMMENDED_VERSIONS.get(series),
                }
        except subprocess.TimeoutExpired:
            pass

    # 4. Header scan — version unknown; mark as not yet verified compatible
    for inc in _HDF5_HEADER_SEARCH:
        h = Path(inc) / "hdf5.h"
        if h.exists():
            prefix = str(Path(inc).parent) if Path(inc).name == "include" else None
            return {
                "found": True, "version": None,
                "prefix": prefix,
                "cmake_hint": f"-DHDF5_ROOT={prefix}" if prefix else None,
                "source": f"header:{h}",
                "compatible": False,
                "recommended": _HDF5_DEFAULT_VERSION,
            }

    return {"found": False, "version": None, "prefix": None,
            "cmake_hint": None, "source": None,
            "compatible": False, "recommended": _HDF5_DEFAULT_VERSION}


# ---------------------------------------------------------------------------
# MPI compatibility constants (derived from dftracer brahma/mpi.cpp)
# BRAHMA_MPI_VERSION encoding: MAJOR * 100000 + MINOR * 100 + PATCH
# ---------------------------------------------------------------------------

#: Compatible MPI version ranges per implementation.
#: Each entry is a list of ``(min_inclusive, max_exclusive)`` BRAHMA_MPI_VERSION ints.
_MPI_COMPATIBLE_RANGES: Dict[str, list] = {
    "openmpi":   [(400106, 400200), (500006, 500100)],
    "mpich":     [(300403, 300500), (400203, 400300)],
    "craympich": [(800108, 800200), (900001, 900200)],
}

#: Human-readable description of compatible ranges for each implementation.
_MPI_COMPATIBLE_DISPLAY: Dict[str, str] = {
    "openmpi":   "4.1.6 – 4.1.x  or  5.0.6 – 5.0.x",
    "mpich":     "3.4.3 – 3.4.x  or  4.2.3 – 4.2.x",
    "craympich": "8.1.8 – 8.1.x  or  9.0.1 – 9.1.x",
}

#: GitHub issue URL for requesting new MPI version support.
_DFTRACER_ISSUES_URL = "https://github.com/llnl/dftracer/issues"


def _mpi_to_brahma_int(major: int, minor: int, patch: int) -> int:
    """Convert a (major, minor, patch) MPI version tuple to a BRAHMA_MPI_VERSION int."""
    return major * 100000 + minor * 100 + patch


def _mpi_version_compatible(impl: str, version_str: str) -> bool:
    """Return True if *version_str* of *impl* falls within a dftracer-compatible range.

    Args:
        impl:        Normalised implementation key: ``"openmpi"``, ``"mpich"``,
                     or ``"craympich"``.  Unknown impls always return ``False``.
        version_str: Dotted version string, e.g. ``"4.1.6"`` or ``"5.0.7"``.

    Returns:
        True when the version is within at least one compatible range; False otherwise.
    """
    ranges = _MPI_COMPATIBLE_RANGES.get(impl)
    if not ranges or not version_str:
        return False
    parts = version_str.split(".")
    try:
        vint = _mpi_to_brahma_int(int(parts[0]), int(parts[1]), int(parts[2]))
    except (IndexError, ValueError):
        return False
    return any(lo <= vint < hi for lo, hi in ranges)


def _detect_system_mpi() -> Dict[str, Any]:
    """Detect the system MPI implementation, version, and dftracer compatibility.

    Probes in order:
    1. ``mpiexec --version`` / ``mpirun --version`` — available on most MPI installs.
    2. ``ompi_info --version`` — OpenMPI-specific fallback.
    3. ``mpichversion`` — MPICH-specific fallback.
    4. ``mpicc --show`` / ``--showme`` — last resort compiler-wrapper probe.

    Returns:
        Dict[str, Any] with keys:

        - ``found`` (bool): True if any MPI was detected.
        - ``impl`` (str or None): Normalised implementation key —
          ``"openmpi"``, ``"mpich"``, ``"craympich"``, or ``"unknown"``.
        - ``impl_display`` (str or None): Human-readable implementation name.
        - ``version`` (str or None): Dotted version string, e.g. ``"4.1.6"``.
        - ``compatible`` (bool): True if the version is dftracer-compatible.
        - ``compatible_versions`` (str or None): Human-readable range string
          for the detected impl, e.g. ``"4.1.6 – 4.1.x  or  5.0.6 – 5.0.x"``.
        - ``source`` (str or None): Which probe succeeded.
    """
    impl: Optional[str] = None
    version_str: Optional[str] = None
    source: Optional[str] = None

    # 1. mpiexec / mpirun --version
    for cmd in ("mpiexec", "mpirun"):
        if not shutil.which(cmd):
            continue
        try:
            r = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=10)
            output = r.stdout + r.stderr
            m = re.search(r"Open MPI[^0-9]*(\d+\.\d+\.\d+)", output, re.I)
            if m:
                impl, version_str, source = "openmpi", m.group(1), cmd
                break
            m = re.search(r"Cray MPICH[^0-9]*(\d+\.\d+\.\d+)", output, re.I)
            if m:
                impl, version_str, source = "craympich", m.group(1), cmd
                break
            m = re.search(r"MPICH[^0-9]*(\d+\.\d+\.\d+)", output, re.I)
            if m:
                impl, version_str, source = "mpich", m.group(1), cmd
                break
            m = re.search(r"Intel.{0,20}MPI[^0-9]*(\d+\.\d+\.\d+)", output, re.I)
            if m:
                impl, version_str, source = "intelmpi", m.group(1), cmd
                break
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    # 2. ompi_info for OpenMPI
    if not impl and shutil.which("ompi_info"):
        try:
            r = subprocess.run(["ompi_info", "--version"], capture_output=True, text=True, timeout=10)
            m = re.search(r"Open MPI[^0-9]*(\d+\.\d+\.\d+)", r.stdout + r.stderr, re.I)
            if m:
                impl, version_str, source = "openmpi", m.group(1), "ompi_info"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    # 3. mpichversion
    if not impl and shutil.which("mpichversion"):
        try:
            r = subprocess.run(["mpichversion"], capture_output=True, text=True, timeout=10)
            m = re.search(r"MPICH\s+Version:\s+(\d+\.\d+\.\d+)", r.stdout + r.stderr, re.I)
            if m:
                impl, version_str, source = "mpich", m.group(1), "mpichversion"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    # 4. mpicc compiler wrapper
    if not impl and shutil.which("mpicc"):
        for flag in ("--showme:version", "--version", "-v"):
            try:
                r = subprocess.run(["mpicc", flag], capture_output=True, text=True, timeout=10)
                output = r.stdout + r.stderr
                m = re.search(r"Open MPI[^0-9]*(\d+\.\d+\.\d+)", output, re.I)
                if m:
                    impl, version_str, source = "openmpi", m.group(1), f"mpicc {flag}"
                    break
                m = re.search(r"MPICH[^0-9]*(\d+\.\d+\.\d+)", output, re.I)
                if m:
                    impl, version_str, source = "mpich", m.group(1), f"mpicc {flag}"
                    break
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

    if not impl:
        return {"found": False, "impl": None, "impl_display": None, "version": None,
                "compatible": False, "compatible_versions": None, "source": None}

    _DISPLAY = {"openmpi": "Open MPI", "mpich": "MPICH", "craympich": "Cray MPICH",
                "intelmpi": "Intel MPI", "unknown": "Unknown MPI"}
    compat = _mpi_version_compatible(impl, version_str or "")

    return {
        "found": True,
        "impl":             impl,
        "impl_display":     _DISPLAY.get(impl, impl),
        "version":          version_str,
        "compatible":       compat,
        "compatible_versions": _MPI_COMPATIBLE_DISPLAY.get(impl),
        "source":           source,
    }


#: Header search paths for hwloc (hardware locality library).
_HWLOC_HEADER_SEARCH = [
    "/usr/include/hwloc.h",
    "/usr/local/include/hwloc.h",
    "/opt/hwloc/include/hwloc.h",
]


def _detect_system_hwloc() -> bool:
    """Return True if hwloc development libraries are installed on the system.

    Tries, in order:

    1. ``pkg-config --exists hwloc`` — most reliable on Debian/Ubuntu systems.
    2. Header scan across :data:`_HWLOC_HEADER_SEARCH`.

    Both probes carry a 10-second timeout.  Returns ``False`` if hwloc is not
    found rather than raising.
    """
    try:
        r = subprocess.run(
            ["pkg-config", "--exists", "hwloc"],
            capture_output=True, timeout=10,
        )
        if r.returncode == 0:
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return any(Path(p).exists() for p in _HWLOC_HEADER_SEARCH)


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
      consistent with dftracer's cmake build flags.
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
    hwloc_found = _detect_system_hwloc()

    features = {
        "mpi":      bool(re.search(r"mpi\.h|MPI_Init|MPI_Comm|mpi4py", all_text, re.I)),
        "python":   "python" in languages,
        "hdf5":     hdf5_in_source or hdf5_system["found"],
        "hdf5_in_source": hdf5_in_source,
        "hdf5_system":    hdf5_system,
        "hip":      bool(re.search(
            r"hip/hip_runtime\.h|hipMalloc|hipMemcpy|hipLaunchKernelGGL|#include\s+[<\"]hip/",
            all_text, re.I,
        )),
        "hwloc":    hwloc_found,
        "posix_io": bool(re.search(r"\bopen\s*\(|\bfopen\s*\(|\bread\s*\(|\bwrite\s*\(", all_text)),
        "openmp":   bool(re.search(r"omp\.h|#pragma omp|import openmp", all_text, re.I)),
    }

    # Complete pip env dict covering every setup.py-supported env var.
    # JOBS / CMAKE_BUILD_PARALLEL_LEVEL are set at install time (jobs param).
    dftracer_pip_env: Dict[str, str] = {
        "DFTRACER_BUILD_TYPE":                   "RelWithDebInfo",
        "DFTRACER_ENABLE_TESTS":                 "OFF",
        "DFTRACER_ENABLE_DLIO_BENCHMARK_TESTS":  "OFF",
        "DFTRACER_ENABLE_PAPER_TESTS":           "OFF",
    }
    if features["mpi"]:
        dftracer_pip_env["DFTRACER_ENABLE_MPI"] = "ON"
    if features["hdf5"]:
        dftracer_pip_env["DFTRACER_ENABLE_HDF5"] = "ON"
        hdf5_prefix = hdf5_system.get("prefix") or ""
        if hdf5_prefix:
            dftracer_pip_env["HDF5_ROOT"] = hdf5_prefix
            dftracer_pip_env["HDF5_DIR"] = hdf5_prefix
    if features["hip"]:
        dftracer_pip_env["DFTRACER_ENABLE_HIP_TRACING"] = "ON"
    if hwloc_found:
        dftracer_pip_env["DFTRACER_DISABLE_HWLOC"] = "OFF"

    # Map features → dftracer cmake flags (legacy; pip install is preferred)
    dftracer_cmake_flags: List[str] = ["-DDFTRACER_ENABLE_TESTS=OFF"]
    if features["python"]:
        dftracer_cmake_flags.append("-DDFTRACER_ENABLE_PYTHON=ON")
    if features["mpi"]:
        dftracer_cmake_flags.append("-DDFTRACER_ENABLE_MPI=ON")
    if features["hdf5"]:
        dftracer_cmake_flags.append("-DDFTRACER_ENABLE_HDF5=ON")
        if hdf5_system.get("cmake_hint"):
            dftracer_cmake_flags.append(hdf5_system["cmake_hint"])
    if features["hip"]:
        dftracer_cmake_flags.append("-DDFTRACER_ENABLE_HIP_TRACING=ON")

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
        "dftracer_pip_env": dftracer_pip_env,
        "dftracer_cmake_flags": dftracer_cmake_flags,
        "key_files": key_files,
        "readme_excerpt": readme_content,
    }
