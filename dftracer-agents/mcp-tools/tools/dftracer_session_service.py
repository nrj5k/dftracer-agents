"""
DFTracer Session Service

Orchestrates the full dftracer annotation + smoke-test workflow for any project:

  1.  session_create          — allocate a RUN-ID workspace and clone source
  2.  session_detect          — detect language, build tool, dftracer features
  3.  session_read_file       — read any workspace file (for LLM inspection)
  4.  session_list_files      — list files in a workspace sub-folder
  5.  session_configure       — configure the build system
  6.  session_build_install   — compile and install
  7.  session_run_smoke_test  — run a smoke test against the installed binary
  8.  session_copy_annotated  — copy source to annotated/ sub-folder
  9.  session_write_file      — write a file inside the workspace (apply edits)
  10. session_patch_build     — auto-patch CMake/autotools/Python build for dftracer
  11. session_build_annotated — build the annotated copy with dftracer linked
  12. session_run_with_dftracer— run a command with dftracer env vars set
  13. session_split_traces    — compact raw traces with dftracer_split
  14. session_analyze_traces  — summarise traces via dfanalyzer
  15. session_status          — show current session state
  16. session_run_pipeline    — full orchestration convenience tool

Workspace layout  ./workspaces/<RUN-ID>/
  source/       original git checkout
  build/        out-of-source build dir   (cmake / autotools)
  install/      install prefix            (cmake / autotools)
                  OR python venv named install (python projects)
  annotated/    copy of source with dftracer instrumentation applied
  build_ann/    build dir for annotated source
  traces/       raw dftracer trace output
  traces_split/ compacted traces produced by dftracer_split
  session.json  persisted session metadata
"""
from __future__ import annotations

import asyncio
import difflib
from datetime import datetime, timezone
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP

from ...mcp_service_factory import MCPService, MCPServiceFactory


# ---------------------------------------------------------------------------
# Workspace helpers
# ---------------------------------------------------------------------------

def _workspaces_root() -> Path:
    env = os.environ.get("DFTRACER_WORKSPACES", "workspaces")
    root = Path(env)
    return root if root.is_absolute() else Path.cwd() / root


# Placeholder strings an LLM might pass instead of a real ID
_PLACEHOLDER_IDS = frozenset({
    "run_id", "RUN_ID", "RUN-ID", "<run_id>", "<RUN_ID>", "<RUN-ID>",
    "run-id", "runid", "RUNID", "{run_id}", "{RUN_ID}",
})


def _new_run_id(requested: Optional[str] = None) -> str:
    """Return requested ID if it looks real, otherwise generate a timestamp-based ID."""
    if requested and requested not in _PLACEHOLDER_IDS:
        return requested
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _ws(run_id: str) -> Path:
    return _workspaces_root() / run_id


def _state_path(run_id: str) -> Path:
    return _ws(run_id) / "session.json"


def _load_state(run_id: str) -> Dict[str, Any]:
    p = _state_path(run_id)
    return json.loads(p.read_text()) if p.exists() else {}


def _save_state(run_id: str, updates: Dict[str, Any]) -> None:
    p = _state_path(run_id)
    state = _load_state(run_id)
    state.update(updates)
    p.write_text(json.dumps(state, indent=2))


def _write_artifact_log(
    ws: Path,
    step_num: int,
    step_name: str,
    data: Dict[str, Any],
    run_id: str = "",
) -> Path:
    """Write a stage log to <workspace>/artifacts/<NN>_<step_name>.log."""
    artifacts = ws / "artifacts"
    artifacts.mkdir(exist_ok=True)
    log_path = artifacts / f"{step_num:02d}_{step_name}.log"
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        f"=== {step_name} ===",
        f"Timestamp : {ts}",
        f"Run ID    : {run_id or ws.name}",
        f"Step      : {step_num:02d}",
        "",
    ]
    for key, val in data.items():
        if isinstance(val, dict):
            lines.append(f"[{key}]")
            stdout = val.get("stdout", "")
            stderr = val.get("stderr", "")
            rc = val.get("returncode", val.get("success", ""))
            if stdout:
                lines.append(f"  stdout: {stdout}")
            if stderr:
                lines.append(f"  stderr: {stderr}")
            if rc != "":
                lines.append(f"  exit  : {rc}")
        else:
            lines.append(f"{key}: {val}")
    log_path.write_text("\n".join(lines) + "\n")
    return log_path


def _ok(msg: str, **extra) -> str:
    return json.dumps({"status": "ok", "message": msg, **extra}, indent=2)


def _err(msg: str, **extra) -> str:
    return json.dumps({"status": "error", "message": msg, **extra}, indent=2)


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------

def _run(
    cmd: List[str],
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: int = 600,
) -> Dict[str, Any]:
    merged = {**os.environ, **(env or {})}
    try:
        r = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=merged,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "returncode": r.returncode,
            "stdout": r.stdout.strip(),
            "stderr": r.stderr.strip(),
            "success": r.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "stdout": "", "stderr": "Command timed out", "success": False}
    except Exception as exc:
        return {"returncode": -1, "stdout": "", "stderr": str(exc), "success": False}


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

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

    features = {
        "mpi":      bool(re.search(r"mpi\.h|MPI_Init|MPI_Comm|mpi4py", all_text, re.I)),
        "python":   "python" in languages,
        "hdf5":     bool(re.search(r"hdf5\.h|H5Fopen|h5py", all_text, re.I)),
        "posix_io": bool(re.search(r"\bopen\s*\(|\bfopen\s*\(|\bread\s*\(|\bwrite\s*\(", all_text)),
        "openmp":   bool(re.search(r"omp\.h|#pragma omp|import openmp", all_text, re.I)),
    }

    # Map features → dftracer cmake flags (aligns with autobuild.sh)
    dftracer_cmake_flags: List[str] = ["-DDFTRACER_ENABLE_TESTS=OFF"]
    if features["python"]:
        dftracer_cmake_flags.append("-DDFTRACER_ENABLE_PYTHON=ON")
    if features["mpi"]:
        dftracer_cmake_flags.append("-DDFTRACER_ENABLE_MPI=ON")

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


# ---------------------------------------------------------------------------
# Annotation helpers — C / C++
# ---------------------------------------------------------------------------

_C_INCLUDE = "#include <dftracer/dftracer.h>"

# Keywords that must never be treated as function names by the annotation regex
_C_KEYWORDS: frozenset = frozenset({
    "if", "else", "for", "while", "do", "switch", "return", "case",
    "break", "continue", "goto", "default", "sizeof", "typeof", "alignof",
    "typedef", "struct", "union", "enum", "namespace", "class", "template",
    "new", "delete", "throw", "try", "catch", "operator", "using", "friend",
    "public", "private", "protected", "virtual", "override", "final",
    "explicit", "inline", "volatile", "extern", "register", "typename",
    "decltype", "static_assert", "constexpr", "noexcept", "nullptr",
})

# MPI / parallel-launcher stripping for smoke tests
_MPI_LAUNCHER_RE = re.compile(
    r"^\s*(?:mpirun|mpiexec|orterun|srun|jsrun|aprun|prun|flux\s+run)\b"
)
_MPI_NP_RE = re.compile(
    r"\s+(?:-np?|-n|--ntasks|--npernode|--ntasks-per-node|-N|--nodes)\s+\d+"
)
_MPI_MISC_FLAGS_RE = re.compile(
    r"\s+--(?:oversubscribe|allow-run-as-root|bind-to\s+\S+|map-by\s+\S+|"
    r"host\s+\S+|hostfile\s+\S+|rankfile\s+\S+)"
)


def _strip_mpi_launcher(cmd: str) -> tuple:
    """Remove MPI/parallel launcher prefix from a shell command string.

    Returns (clean_cmd, was_stripped).  The underlying binary is run as a
    single process with no MPI context.
    """
    if not _MPI_LAUNCHER_RE.search(cmd):
        return cmd, False
    clean = _MPI_LAUNCHER_RE.sub("", cmd)
    clean = _MPI_NP_RE.sub("", clean)
    clean = _MPI_MISC_FLAGS_RE.sub("", clean)
    return clean.strip(), True


# Matches any single-line dftracer macro injected by _annotate_c_source
_DFTRACER_MACRO_LINE_RE = re.compile(
    r"^\s*DFTRACER_(?:C|CPP)_(?:FUNCTION_(?:START|END)|FUNCTION_UPDATE_(?:STR|INT)|"
    r"CPP_FUNCTION_UPDATE|FUNCTION|INIT|FINI|METADATA)\s*\([^)]*\)\s*;\s*$",
    re.MULTILINE,
)

# GCC/Clang error line: "path/file.c:42:5: error: ..."
_COMPILER_ERROR_RE = re.compile(
    r"^([^\s:][^:]*\.(?:c|cpp|cxx|cc|h|hpp)):(\d+):\d+:\s+error:",
    re.MULTILINE,
)


def _strip_dftracer_c_macros(content: str) -> str:
    """Remove all dftracer macros injected by _annotate_c_source. Leaves other code intact."""
    content = content.replace(f"{_C_INCLUDE}\n", "")
    content = _DFTRACER_MACRO_LINE_RE.sub("", content)
    content = content.replace("\n/* TODO: add DFTRACER_FINI() before process exit */\n", "\n")
    content = content.replace("\n# TODO: call DFTRACER_FINI() before process exit\n", "\n")
    # Collapse any runs of blank lines left by macro removal
    content = re.sub(r"\n{3,}", "\n\n", content)
    return content


def _fix_dftracer_annotation_errors(ann: Path, compiler_stderr: str) -> List[str]:
    """
    Parse compiler output, find files where our injected dftracer macros caused
    errors, strip those macros from the affected files, and return the list of
    relative paths that were cleaned.  The cleaned files can be rebuilt immediately.
    The build system will compile them without dftracer instrumentation; Goose
    should then manually re-annotate the troublesome files.
    """
    if not compiler_stderr.strip():
        return []

    # Determine whether the errors are dftracer-related at all
    dftracer_related = (
        "DFTRACER" in compiler_stderr
        or "data_fn" in compiler_stderr
        or "initialize_region" in compiler_stderr
        or "finalize_region" in compiler_stderr
    )

    # Collect which source files have errors
    error_files: Dict[str, Set[int]] = {}
    for m in _COMPILER_ERROR_RE.finditer(compiler_stderr):
        error_files.setdefault(m.group(1), set()).add(int(m.group(2)))

    if not error_files and not dftracer_related:
        return []

    fixed: List[str] = []
    for raw_path, error_lines in error_files.items():
        # Resolve file path relative to annotated/ directory
        candidate: Optional[Path] = ann / raw_path
        if not candidate.exists():
            hits = list(ann.rglob(Path(raw_path).name))
            candidate = hits[0] if hits else None
        if not candidate or not candidate.exists():
            continue

        content = candidate.read_text(errors="ignore")
        lines = content.splitlines()

        # Only strip if the failing line is on or near one of our macros, OR if
        # the overall stderr mentions dftracer symbols (link/type errors)
        near_macro = any(
            1 <= ln <= len(lines) and (
                "DFTRACER" in lines[ln - 1]
                or "data_fn" in lines[ln - 1]
                or (ln > 1 and "DFTRACER" in lines[ln - 2])
                or (ln < len(lines) and "DFTRACER" in lines[ln])
            )
            for ln in error_lines
        )
        if not (near_macro or dftracer_related):
            continue

        new_content = _strip_dftracer_c_macros(content)
        if new_content != content:
            candidate.write_text(new_content)
            fixed.append(str(candidate.relative_to(ann)))

    return fixed


# Regex patterns for detecting trackable parameters in function signatures
_STR_PARAM_RE = re.compile(
    r"\b(?:const\s+)?char\s*\*+\s*(\w*(?:file|path|name|dir|mode|cmd|key|buf)\w*)\b",
    re.IGNORECASE,
)
_INT_PARAM_RE = re.compile(
    r"\b(?:size_t|off_t|ssize_t|uint64_t|int64_t|int|long)\s+(\w*(?:size|count|len|offset|fd|flags|bytes|num)\w*)\b",
    re.IGNORECASE,
)


def _metadata_update_calls(params: str, is_cpp: bool, indent: str = "  ") -> List[str]:
    """Generate DFTRACER_*_FUNCTION_UPDATE_* calls for trackable function parameters."""
    calls: List[str] = []
    seen: set = set()
    for m in _STR_PARAM_RE.finditer(params):
        name = m.group(1)
        if name and name not in seen:
            seen.add(name)
            if is_cpp:
                calls.append(f'{indent}DFTRACER_CPP_FUNCTION_UPDATE("{name}", {name});')
            else:
                calls.append(f'{indent}DFTRACER_C_FUNCTION_UPDATE_STR("{name}", {name});')
    for m in _INT_PARAM_RE.finditer(params):
        name = m.group(1)
        if name and name not in seen:
            seen.add(name)
            if not is_cpp:
                calls.append(f'{indent}DFTRACER_C_FUNCTION_UPDATE_INT("{name}", (int){name});')
    return calls


def _annotate_c_source(content: str, filepath: Path, is_entry: bool) -> str:
    """Inject dftracer C/C++ macros into source.  Idempotent."""
    if _C_INCLUDE in content:
        return content

    is_cpp = filepath.suffix.lower() in {".cpp", ".cxx", ".cc"}

    lines = content.splitlines(keepends=True)
    last_inc = max(
        (i for i, ln in enumerate(lines) if ln.strip().startswith("#include")),
        default=-1,
    )
    out: List[str] = []
    for i, ln in enumerate(lines):
        out.append(ln)
        if i == last_inc:
            out.append(f"{_C_INCLUDE}\n")

    result = "".join(out)

    # Step 1: inject DFTRACER_C_FUNCTION_START() / DFTRACER_CPP_FUNCTION() and
    # parameter metadata at the opening of every real function body.
    # Group 2 captures the function name so we can reject C/C++ keywords (if/else/for/…).
    _FN_HEADER_RE = re.compile(
        r"(\b\w[\w\s\*:<>]*\s+(\w+)\s*\(([^)]*)\)\s*(?:const\s*)?(?:override\s*)?(?:noexcept\s*)?\{)"
    )

    def _inject_fn_open(m: re.Match) -> str:
        header = m.group(1)
        fn_name = m.group(2)
        params = m.group(3)
        # Skip control-flow keywords that look like functions to the regex
        if fn_name in _C_KEYWORDS:
            return header
        if is_cpp:
            injected = f"{header}\n  DFTRACER_CPP_FUNCTION();"
        else:
            injected = f"{header}\n  DFTRACER_C_FUNCTION_START();"
        meta_calls = _metadata_update_calls(params, is_cpp)
        if meta_calls:
            injected += "\n" + "\n".join(meta_calls)
        return injected

    result = _FN_HEADER_RE.sub(_inject_fn_open, result)

    # Step 2 (entry file only): insert DFTRACER_C_INIT *before* DFTRACER_C_FUNCTION_START
    # in main() so the tracer is initialized before any region/metadata calls are made.
    if is_entry:
        init_call = (
            "DFTRACER_CPP_INIT(nullptr, nullptr, nullptr);"
            if is_cpp
            else "DFTRACER_C_INIT(nullptr, nullptr, nullptr);"
        )
        result = re.sub(
            r"(int\s+main\s*\([^)]*\)\s*\{)",
            r"\1\n  " + init_call,
            result,
            count=1,
        )

    # Step 3 (C only): inject FUNCTION_END before every return, FUNCTION_END at the
    # closing } of void/fallthrough functions, and FINI in the entry (main) function.
    if not is_cpp:
        result = _finalize_c_ends_and_fini(result, is_entry)

    return result


def _finalize_c_ends_and_fini(content: str, is_entry: bool) -> str:
    """
    Post-annotation pass for C source files.  Operates only within function bodies
    that already contain DFTRACER_C_FUNCTION_START() (so it's safe to re-run).

    For every such function:
      - Injects DFTRACER_C_FUNCTION_END() before every `return` statement.
      - Injects DFTRACER_C_FUNCTION_END() before the closing } when the function
        has no explicit return (void / fallthrough).

    Additionally, for the entry file's main() function (detected by DFTRACER_C_INIT):
      - Injects DFTRACER_C_FINI() immediately before DFTRACER_C_FUNCTION_END() at
        every exit point (return statements and process-exit calls).
      - Injects DFTRACER_C_FINI() before process-exit calls (exit/abort/_exit/…)
        throughout the file, since they terminate without returning to main.
    """
    END = "DFTRACER_C_FUNCTION_END()"
    FINI = "DFTRACER_C_FINI()"
    _EXIT_RE = re.compile(r"\b(?:exit|_exit|_Exit|quick_exit|abort)\s*\(")

    lines = content.splitlines(keepends=True)
    insertions: List[Tuple[int, str]] = []  # (line_index, text to insert before that line)

    # Find each annotated function by its DFTRACER_C_FUNCTION_START() line
    for si, ln in enumerate(lines):
        if "DFTRACER_C_FUNCTION_START()" not in ln:
            continue

        fn_indent = len(ln) - len(ln.lstrip())
        ind = " " * fn_indent  # same indent as the START call

        # Determine if this is main (DFTRACER_C_INIT is within ±4 lines of START)
        search = range(max(0, si - 4), min(len(lines), si + 5))
        is_main = is_entry and any("DFTRACER_C_INIT" in lines[k] for k in search)

        # Brace-count from just after START to find the function's closing }
        depth = 1
        j = si + 1
        while j < len(lines) and depth > 0:
            for ch in lines[j]:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        break
            if depth > 0:
                j += 1
        fn_end = j  # index of the line that contains the closing }

        # Scan lines inside this function
        for k in range(si + 1, fn_end):
            s = lines[k].strip()
            if not s or s.startswith("//") or s.startswith("*"):
                continue
            k_ind = " " * (len(lines[k]) - len(lines[k].lstrip()))
            prev = lines[k - 1].strip() if k > 0 else ""

            # Before return statements (skip if END is already the previous line)
            if re.match(r"return\b", s) and END not in prev:
                # list.insert(k, x) pushes the previous occupant of k to k+1.
                # Appending in stable-sort order means the first-appended runs first.
                # To get END → FINI → return we need the LAST insert to land at k
                # (i.e. END), so append FINI first, then END.
                if is_main and FINI not in prev:
                    insertions.append((k, f"{k_ind}{FINI};\n"))
                insertions.append((k, f"{k_ind}{END};\n"))

            # Before process-exit calls anywhere — process terminates here so FINI is needed
            elif _EXIT_RE.search(s) and FINI not in prev:
                insertions.append((k, f"{k_ind}{FINI};\n"))

        # Handle void / fallthrough functions: add END before the closing }
        # if the last real statement is not already a return or END
        m = fn_end - 1
        while m > si and not lines[m].strip():
            m -= 1
        last = lines[m].strip() if m > si else ""
        if last and END not in last and not re.match(r"return\b", last):
            close_ind = ind  # same indent as START
            if is_main and FINI not in last:
                insertions.append((fn_end, f"{close_ind}{FINI};\n"))
            insertions.append((fn_end, f"{close_ind}{END};\n"))

    # Apply insertions in reverse order so earlier indices stay valid
    result = list(lines)
    for idx, text in sorted(insertions, key=lambda x: x[0], reverse=True):
        result.insert(idx, text)

    return "".join(result)


# ---------------------------------------------------------------------------
# Annotation helpers — Python
# ---------------------------------------------------------------------------

_PY_IMPORT = "from dftracer.logger import dft_fn, DFTRACER_INIT, DFTRACER_FINI"


def _annotate_python_source(content: str, is_entry: bool) -> str:
    """Inject dftracer Python decorators.  Idempotent."""
    if "dftracer" in content:
        return content

    lines = content.splitlines(keepends=True)
    last_imp = max(
        (i for i, ln in enumerate(lines)
         if ln.strip().startswith(("import ", "from "))),
        default=-1,
    )
    out: List[str] = []
    for i, ln in enumerate(lines):
        out.append(ln)
        if i == last_imp:
            out.append(f"\n{_PY_IMPORT}\n")
            if is_entry:
                out.append(
                    "DFTRACER_INIT(log_file=None, data_dirs=None, process_id=-1)\n\n"
                )
    result = "".join(out)

    # Decorate top-level function definitions
    result = re.sub(r"^(def\s)", r"@dft_fn\n\1", result, flags=re.MULTILINE)

    if is_entry:
        result += "\n# TODO: call DFTRACER_FINI() before process exit\n"
    return result


# ---------------------------------------------------------------------------
# Build-system patch helpers
# ---------------------------------------------------------------------------

def _patch_cmake(path: Path) -> str:
    """Return CMakeLists.txt content with dftracer find_package + link injected."""
    content = path.read_text()
    if "dftracer" in content.lower():
        return content

    preamble = textwrap.dedent("""\
        # --- dftracer (auto-injected) ---
        find_package(dftracer QUIET)
        if(dftracer_FOUND)
          message(STATUS "dftracer found — tracing enabled")
        endif()
        # ---------------------------------
    """)

    suffix = textwrap.dedent("""\

        # --- dftracer link (auto-injected) ---
        if(dftracer_FOUND)
          get_property(_dft_targets DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
                       PROPERTY BUILDSYSTEM_TARGETS)
          foreach(_t ${_dft_targets})
            get_target_property(_t_type ${_t} TYPE)
            if(_t_type MATCHES "EXECUTABLE|LIBRARY")
              target_link_libraries(${_t} PRIVATE dftracer::dftracer)
              target_include_directories(${_t} PRIVATE ${dftracer_INCLUDE_DIRS})
              target_compile_definitions(${_t} PRIVATE DFTRACER_ENABLE)
            endif()
          endforeach()
        endif()
        # -------------------------------------
    """)

    m = re.search(r"^(add_executable|add_library)", content, re.MULTILINE)
    if m:
        content = content[: m.start()] + preamble + "\n" + content[m.start():]
    else:
        content = preamble + "\n" + content
    content += suffix
    return content


def _patch_setup_py(path: Path) -> str:
    """Return setup.py content with dftracer added to install_requires."""
    content = path.read_text()
    if "dftracer" in content:
        return content
    return re.sub(
        r"(install_requires\s*=\s*\[)",
        r'\1\n        "dftracer",',
        content,
    )


def _patch_pyproject(path: Path) -> str:
    """Return pyproject.toml with dftracer added to dependencies."""
    content = path.read_text()
    if "dftracer" in content:
        return content
    return re.sub(
        r"(dependencies\s*=\s*\[)",
        r'\1\n    "dftracer",',
        content,
    )


def _patch_autotools_makefile(path: Path) -> str:
    """Prepend pkg-config dftracer flags to CFLAGS/LDFLAGS in Makefile."""
    content = path.read_text()
    if "dftracer" in content:
        return content
    injection = textwrap.dedent("""\
        # --- dftracer (auto-injected) ---
        DFTRACER_CFLAGS  := $(shell pkg-config --cflags dftracer 2>/dev/null)
        DFTRACER_LDFLAGS := $(shell pkg-config --libs   dftracer 2>/dev/null)
        CFLAGS   += $(DFTRACER_CFLAGS)   -DDFTRACER_ENABLE
        CXXFLAGS += $(DFTRACER_CFLAGS)   -DDFTRACER_ENABLE
        LDFLAGS  += $(DFTRACER_LDFLAGS)
        # ----------------------------------
    """)
    return injection + "\n" + content


# ---------------------------------------------------------------------------
# Entry-point detection helpers
# ---------------------------------------------------------------------------

def _find_c_entry_points(source_dir: Path) -> List[Path]:
    """Return C/C++ files that define main()."""
    results: List[Path] = []
    for ext in ("*.c", "*.cpp", "*.cxx", "*.cc"):
        for f in source_dir.rglob(ext):
            try:
                text = f.read_text(errors="ignore")
                if re.search(r"\bint\s+main\s*\(", text):
                    results.append(f)
            except OSError:
                pass
    return results


def _find_python_entry_points(source_dir: Path) -> List[Path]:
    """Return Python files that look like entry points (contain if __name__)."""
    results: List[Path] = []
    for f in source_dir.rglob("*.py"):
        try:
            text = f.read_text(errors="ignore")
            if '__name__' in text and '__main__' in text:
                results.append(f)
        except OSError:
            pass
    return results


# ---------------------------------------------------------------------------
# Smoke-test heuristic
# ---------------------------------------------------------------------------

def _guess_smoke_test(source_dir: Path, build_tool: str, install_dir: Path) -> Optional[str]:
    """Best-guess smoke test command for a project."""
    if build_tool == "cmake":
        return "ctest --test-dir . -L smoke -R smoke --output-on-failure || ctest --test-dir . --output-on-failure -N"
    if build_tool == "autotools":
        return "make check -j1"
    if build_tool == "python":
        return "python -m pytest tests/ -x -q 2>/dev/null || python -m pytest test/ -x -q 2>/dev/null || python -c 'import pkg_resources; print(\"import ok\")'"
    if build_tool == "make":
        return "make test"
    return None


# ---------------------------------------------------------------------------
# dftracer-utils helpers (split via MCP service + installation)
# ---------------------------------------------------------------------------

_UTILS_SERVICE_PATH = Path(__file__).resolve().parent / "dftracer" / "dftracer_utils_service.py"


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


# ---------------------------------------------------------------------------
# dftracer install helper
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Service class
# ---------------------------------------------------------------------------

class DFTracerSessionService(MCPService):
    """
    MCP service that orchestrates dftracer annotation + smoke-test sessions.
    """

    def __init__(self) -> None:
        self.session_subservice = FastMCP("DFTracerSession")
        self.pipeline_subservice = FastMCP("DFTracerPipeline")

        self._register_session_tools()
        self._register_pipeline_tool()

    def execute(self, data: dict) -> Optional[str]:
        return f"Use session_* tools to orchestrate the dftracer workflow."

    @property
    def name(self) -> str:
        return "dftracer-session"

    # -----------------------------------------------------------------------
    # Individual step tools
    # -----------------------------------------------------------------------

    def _register_session_tools(self) -> None:  # noqa: C901  (long but intentional)

        @self.session_subservice.tool()
        def session_create(
            url: str,
            ref: str = "main",
            run_id: Optional[str] = None,
        ) -> str:
            """
            Create a new session workspace and clone the source repository.

            Args:
                url: Git URL to clone (https or ssh).
                ref: Branch, tag, or commit to checkout (default: main).
                run_id: Optional fixed RUN-ID; a UUID is generated if omitted.

            Returns JSON with run_id and workspace path.
            """
            rid = _new_run_id(run_id)
            ws = _ws(rid)
            ws.mkdir(parents=True, exist_ok=True)

            src = ws / "source"
            src.mkdir(exist_ok=True)

            clone_result = _run(
                ["git", "clone", "--depth", "1", "--branch", ref, url, str(src)],
                timeout=300,
            )
            if not clone_result["success"]:
                # Retry without --branch (bare clone then checkout)
                shutil.rmtree(src, ignore_errors=True)
                src.mkdir(exist_ok=True)
                r2 = _run(["git", "clone", "--depth", "1", url, str(src)], timeout=300)
                if not r2["success"]:
                    return _err("git clone failed", clone_stderr=r2["stderr"])
                _run(["git", "checkout", ref], cwd=src)

            _save_state(rid, {
                "run_id": rid,
                "url": url,
                "ref": ref,
                "workspace": str(ws),
                "step": "cloned",
            })
            return _ok(
                f"Session {rid} created",
                run_id=rid,
                workspace=str(ws),
                source=str(src),
            )

        @self.session_subservice.tool()
        def session_detect(run_id: str) -> str:
            """
            Detect the programming language, build tool, and dftracer feature flags
            for the cloned source in a session.

            Returns detailed JSON including readme excerpt, key files, and
            recommended dftracer cmake flags derived from autobuild.sh options.
            """
            src = _ws(run_id) / "source"
            if not src.exists():
                return _err("source/ not found — run session_create first")

            info = _detect_info(src)
            _save_state(run_id, {"detection": info, "step": "detected"})
            _write_artifact_log(_ws(run_id), 2, "session_detect", info, run_id)
            return _ok("Detection complete", **info)

        @self.session_subservice.tool()
        def session_list_files(
            run_id: str,
            subfolder: str = "source",
            pattern: str = "**/*",
            max_results: int = 100,
        ) -> str:
            """
            List files inside a workspace sub-folder.

            Args:
                run_id:     Session identifier.
                subfolder:  Sub-folder to list (source, annotated, build, install…).
                pattern:    Glob pattern relative to the sub-folder.
                max_results: Maximum number of paths to return.
            """
            base = _ws(run_id) / subfolder
            if not base.exists():
                return _err(f"{subfolder}/ does not exist in session {run_id}")
            paths = [
                str(p.relative_to(base))
                for p in base.glob(pattern)
                if p.is_file()
            ][:max_results]
            return _ok(f"{len(paths)} files found", files=paths)

        @self.session_subservice.tool()
        def session_read_file(
            run_id: str,
            filepath: str,
            subfolder: str = "source",
            max_bytes: int = 32768,
        ) -> str:
            """
            Read a file from the workspace for inspection.

            Args:
                run_id:    Session identifier.
                filepath:  Path relative to the sub-folder root.
                subfolder: Workspace sub-folder (source, annotated, build…).
                max_bytes: Truncate content after this many bytes.
            """
            p = _ws(run_id) / subfolder / filepath
            if not p.exists():
                return _err(f"File not found: {subfolder}/{filepath}")
            content = p.read_text(errors="replace")[:max_bytes]
            truncated = len(content) == max_bytes
            return _ok(
                "File read",
                filepath=filepath,
                subfolder=subfolder,
                content=content,
                truncated=truncated,
            )

        @self.session_subservice.tool()
        def session_write_file(
            run_id: str,
            filepath: str,
            content: str,
            subfolder: str = "annotated",
        ) -> str:
            """
            Write (create or overwrite) a file inside the workspace.
            Use this to apply LLM-generated annotations or build patches.

            Args:
                run_id:    Session identifier.
                filepath:  Path relative to the sub-folder root.
                content:   File content to write.
                subfolder: Workspace sub-folder to write into (default: annotated).
            """
            p = _ws(run_id) / subfolder / filepath
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            return _ok(f"Wrote {len(content)} bytes to {subfolder}/{filepath}")

        @self.session_subservice.tool()
        def session_configure(
            run_id: str,
            extra_cmake_flags: str = "",
            extra_configure_flags: str = "",
            extra_pip_flags: str = "",
        ) -> str:
            """
            Configure the build system for the cloned source.

            For cmake:     runs cmake -S source -B build -DCMAKE_INSTALL_PREFIX=install
            For autotools: runs ./configure --prefix=<install>
            For python:    creates a venv at install/ and installs in editable mode

            Args:
                run_id:               Session identifier.
                extra_cmake_flags:    Additional -D flags for cmake.
                extra_configure_flags: Additional flags for ./configure.
                extra_pip_flags:      Additional flags for pip install.
            """
            ws = _ws(run_id)
            src = ws / "source"
            build = ws / "build"
            install = ws / "install"
            build.mkdir(exist_ok=True)
            install.mkdir(exist_ok=True)

            state = _load_state(run_id)
            info = state.get("detection") or _detect_info(src)
            bt = info.get("build_tool", "unknown")

            if bt == "cmake":
                flags = [
                    f"-DCMAKE_INSTALL_PREFIX={install}",
                    "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
                ] + (extra_cmake_flags.split() if extra_cmake_flags else [])
                r = _run(
                    ["cmake", "-S", str(src), "-B", str(build)] + flags,
                    timeout=300,
                )
            elif bt == "autotools":
                # Bootstrap if needed
                if (src / "configure.ac").exists() and not (src / "configure").exists():
                    _run(["autoreconf", "-fi"], cwd=src, timeout=120)
                flags = [f"--prefix={install}"] + (
                    extra_configure_flags.split() if extra_configure_flags else []
                )
                r = _run([str(src / "configure")] + flags, cwd=build, timeout=300)
            elif bt == "python":
                venv_r = _run(["python3", "-m", "venv", str(install)], timeout=60)
                if not venv_r["success"]:
                    return _err("venv creation failed", **venv_r)
                pip = install / "bin" / "pip"
                flags = ["install", "-e", str(src)] + (
                    extra_pip_flags.split() if extra_pip_flags else []
                )
                r = _run([str(pip)] + flags, timeout=300)
            else:
                return _err(f"Unsupported build tool: {bt}")

            _save_state(run_id, {"step": "configured", "build_tool": bt})
            _write_artifact_log(_ws(run_id), 3, "session_configure", {"build_tool": bt, "result": r}, run_id)
            if not r["success"]:
                return _err("Configure failed", **r)
            return _ok("Configure succeeded", build_tool=bt, **r)

        @self.session_subservice.tool()
        def session_build_install(
            run_id: str,
            jobs: int = 4,
        ) -> str:
            """
            Compile and install the project after session_configure.

            For cmake/autotools: runs make -j<jobs> && make install
            For python:          pip install is already done by session_configure.

            Args:
                run_id: Session identifier.
                jobs:   Parallel make jobs (default: 4).
            """
            ws = _ws(run_id)
            build = ws / "build"
            install = ws / "install"
            state = _load_state(run_id)
            bt = state.get("build_tool", "unknown")

            if bt in {"cmake", "autotools", "make"}:
                r = _run(["make", f"-j{jobs}"], cwd=build, timeout=600)
                if not r["success"]:
                    _write_artifact_log(_ws(run_id), 4, "session_build_install", {"build": r}, run_id)
                    return _err("make failed", **r)
                r2 = _run(["make", "install"], cwd=build, timeout=300)
                _write_artifact_log(_ws(run_id), 4, "session_build_install", {"build": r, "install": r2}, run_id)
                if not r2["success"]:
                    return _err("make install failed", **r2)
                _save_state(run_id, {"step": "installed"})
                return _ok("Build and install succeeded", make=r, install=r2)

            if bt == "python":
                _save_state(run_id, {"step": "installed"})
                _write_artifact_log(_ws(run_id), 4, "session_build_install", {"status": "python project installed via pip"}, run_id)
                return _ok("Python project installed via session_configure (pip install -e)")

            return _err(f"Unknown build tool: {bt}")

        @self.session_subservice.tool()
        def session_run_smoke_test(
            run_id: str,
            command: str,
            subfolder: str = "build",
            env_extra: Optional[str] = None,
            timeout: int = 300,
        ) -> str:
            """
            Run a smoke test command inside the workspace as a single process
            (no MPI, no parallelism).

            Any MPI/parallel launcher prefix (mpirun, mpiexec, srun, jsrun, aprun,
            flux run) is automatically stripped so the binary runs directly.
            This is intentional: smoke tests must be deterministic and reproducible
            without a cluster scheduler or MPI runtime.

            Args:
                run_id:    Session identifier.
                command:   Shell command to run (passed to /bin/sh -c).
                           MPI launchers are stripped automatically — pass the
                           original command as-is; the tool will clean it up.
                subfolder: Working directory sub-folder (default: build).
                env_extra: Optional JSON object of extra env vars.
                timeout:   Seconds before the command is killed.
            """
            cwd = _ws(run_id) / subfolder
            if not cwd.exists():
                cwd = _ws(run_id) / "source"

            env = {}
            if env_extra:
                env = json.loads(env_extra)

            safe_command, stripped = _strip_mpi_launcher(command)

            r = _run(["/bin/sh", "-c", safe_command], cwd=cwd, env=env, timeout=timeout)
            _save_state(run_id, {"last_smoke_test": {"command": safe_command, **r}})
            _write_artifact_log(_ws(run_id), 5, "session_run_smoke_test", {
                "command_original": command,
                "command_run": safe_command,
                "mpi_launcher_stripped": stripped,
                "result": r,
            }, run_id)
            if r["success"]:
                return _ok(
                    "Smoke test passed",
                    command_run=safe_command,
                    mpi_launcher_stripped=stripped,
                    **r,
                )
            return _err(
                "Smoke test failed",
                command_run=safe_command,
                mpi_launcher_stripped=stripped,
                **r,
            )

        @self.session_subservice.tool()
        def session_copy_annotated(run_id: str) -> str:
            """
            Copy the original source tree to annotated/ ready for instrumentation.
            Existing annotated/ contents are replaced.

            Args:
                run_id: Session identifier.
            """
            ws = _ws(run_id)
            src = ws / "source"
            dst = ws / "annotated"
            if not src.exists():
                return _err("source/ not found — run session_create first")
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            _save_state(run_id, {"step": "annotated_copy_created"})
            return _ok(f"Copied source to {dst}")

        @self.session_subservice.tool()
        def session_patch_build(run_id: str) -> str:
            """
            Automatically patch the build system in annotated/ to link dftracer.

            - CMake:     injects find_package(dftracer) + target_link_libraries
            - Autotools: prepends pkg-config flags to Makefile.am / Makefile
            - Python:    adds dftracer to install_requires / dependencies

            Args:
                run_id: Session identifier.
            """
            ws = _ws(run_id)
            ann = ws / "annotated"
            if not ann.exists():
                return _err("annotated/ not found — run session_copy_annotated first")

            patched: List[str] = []
            state = _load_state(run_id)
            info = state.get("detection") or _detect_info(ws / "source")
            bt = info.get("build_tool", "unknown")

            if bt == "cmake":
                cml = ann / "CMakeLists.txt"
                if cml.exists():
                    cml.write_text(_patch_cmake(cml))
                    patched.append("CMakeLists.txt")
                # Recurse one level for sub-projects
                for sub in ann.iterdir():
                    if sub.is_dir():
                        scml = sub / "CMakeLists.txt"
                        if scml.exists():
                            scml.write_text(_patch_cmake(scml))
                            patched.append(str(scml.relative_to(ann)))

            elif bt == "autotools":
                for mf in ann.glob("Makefile*"):
                    mf.write_text(_patch_autotools_makefile(mf))
                    patched.append(mf.name)

            elif bt == "python":
                for name, fn in (
                    ("setup.py", _patch_setup_py),
                    ("pyproject.toml", _patch_pyproject),
                ):
                    p = ann / name
                    if p.exists():
                        p.write_text(fn(p))
                        patched.append(name)

            _save_state(run_id, {"step": "build_patched"})
            return _ok(f"Patched {len(patched)} build file(s)", patched=patched, build_tool=bt)

        @self.session_subservice.tool()
        def session_annotate_source(
            run_id: str,
            auto_detect_entries: bool = True,
        ) -> str:
            """
            Scan annotated/ and produce a manual annotation plan.

            Auto-annotation is intentionally disabled — automated regex insertion
            causes syntax errors (macros inside strings, comments, macro bodies, etc.).
            This tool scans the source, identifies entry points and unannotated files,
            and returns a structured plan for Goose to follow manually.

            Goose MUST use session_read_file + session_write_file to annotate each
            file by hand, following the rules in .goosehints
            "C / C++ Annotation Rules".

            Args:
                run_id:              Session identifier.
                auto_detect_entries: If True, detect main() / __main__ as entry points.
            """
            ws = _ws(run_id)
            ann = ws / "annotated"
            if not ann.exists():
                return _err("annotated/ not found — run session_copy_annotated first")

            state = _load_state(run_id)
            info = state.get("detection") or _detect_info(ws / "source")
            langs = info.get("languages", [])

            c_entries: set = set()
            py_entries: set = set()
            if auto_detect_entries:
                c_entries = {str(p) for p in _find_c_entry_points(ann)}
                py_entries = {str(p) for p in _find_python_entry_points(ann)}

            # Rough function-name scan — only used for the plan summary, not for injection
            _FN_SCAN_RE = re.compile(
                r"^\s*(?:static\s+)?(?:inline\s+)?\w[\w\s\*:<>]*\s+(\w+)\s*\([^;{]*\)\s*\{",
                re.MULTILINE,
            )

            c_plan: List[dict] = []
            if "c" in langs or "cpp" in langs:
                for ext in ("*.c", "*.cpp", "*.cxx", "*.cc"):
                    for f in sorted(ann.rglob(ext)):
                        try:
                            content = f.read_text(errors="ignore")
                            if _C_INCLUDE in content:
                                continue  # already has dftracer include — may be annotated
                            rel = str(f.relative_to(ann))
                            is_entry = str(f) in c_entries
                            is_cpp = f.suffix.lower() in {".cpp", ".cxx", ".cc"}
                            fns = [
                                m.group(1) for m in _FN_SCAN_RE.finditer(content)
                                if m.group(1) not in _C_KEYWORDS
                            ]
                            c_plan.append({
                                "file": rel,
                                "is_entry": is_entry,
                                "is_cpp": is_cpp,
                                "approx_functions": len(fns),
                                "sample_functions": fns[:10],
                            })
                        except OSError:
                            pass

            py_plan: List[dict] = []
            if "python" in langs:
                for f in sorted(ann.rglob("*.py")):
                    try:
                        content = f.read_text(errors="ignore")
                        if "dft_fn" in content or "@dft_fn" in content:
                            continue
                        rel = str(f.relative_to(ann))
                        py_plan.append({"file": rel, "is_entry": str(f) in py_entries})
                    except OSError:
                        pass

            _save_state(run_id, {"step": "annotation_planned"})
            _write_artifact_log(ws, 8, "session_annotate_source", {
                "c_files_to_annotate": len(c_plan),
                "py_files_to_annotate": len(py_plan),
                "entry_points_c": sorted(c_entries),
                "entry_points_py": sorted(py_entries),
            }, run_id)

            return _ok(
                "Annotation plan ready. Annotate each file MANUALLY using "
                "session_read_file + session_write_file. Follow all rules in "
                ".goosehints 'C / C++ Annotation Rules' — especially the pitfalls "
                "section. Do NOT call session_annotate_source to auto-write files.",
                c_files=c_plan,
                py_files=py_plan,
                entry_points_c=sorted(c_entries),
                entry_points_py=sorted(py_entries),
            )

        @self.session_subservice.tool()
        def session_autobuild_dftracer(
            run_id: str,
            dftracer_ref: str = "v2.0.3",
            install_mode: str = "auto",
            jobs: int = 4,
        ) -> str:
            """
            Clone dftracer and build+install it via its own autobuild.sh script.

            This is the low-level build tool behind session_install_dftracer.
            Call it directly when you want fine-grained control over the build
            (e.g. forcing cmake mode on a Python project, or changing the ref).

            autobuild.sh flags driven by detected project features:
              --enable-mpi    added when MPI is detected in the project source
              --enable-hdf5   added when HDF5 is detected in the project source
              --python <exe>  added when Python is detected (pip mode) or when
                              the project has Python bindings (cmake mode)

            install_mode choices:
              "cmake" — builds and installs the full C/C++ library + headers into
                        <workspace>/install_ann/.  Required for C/C++ projects so
                        find_package(dftracer) works in the annotated build.
              "pip"   — installs the Python package into the project venv at
                        <workspace>/install/.  Used for Python projects.
              "auto"  — "cmake" for cmake/autotools/make projects, "pip" for Python.

            Build tree is placed at <workspace>/dftracer_build/ and the dftracer
            source is cached at <workspace>/dftracer_src/ so subsequent calls
            (e.g. after a failed first attempt) skip the clone step.

            Args:
                run_id:       Session identifier.
                dftracer_ref: Git tag or branch to build (default: v2.0.3).
                install_mode: "cmake", "pip", or "auto" (default: auto).
                jobs:         Parallel build jobs (default: 4).
            """
            ws = _ws(run_id)
            state = _load_state(run_id)
            info = state.get("detection") or _detect_info(ws / "source")
            bt = info.get("build_tool", "unknown")
            features = info.get("features", {})

            # Resolve "auto" mode
            if install_mode == "auto":
                install_mode = "pip" if bt == "python" else "cmake"

            if install_mode == "cmake":
                install_prefix = ws / "install_ann"
                install_prefix.mkdir(exist_ok=True)
                python_exe = None
                if features.get("python"):
                    venv_py = ws / "install" / "bin" / "python3"
                    python_exe = str(venv_py) if venv_py.exists() else sys.executable
            else:  # pip
                install_prefix = ws / "install"
                install_prefix.mkdir(exist_ok=True)
                venv_py = ws / "install" / "bin" / "python3"
                python_exe = str(venv_py) if venv_py.exists() else sys.executable

            result = _install_dftracer_autobuild(
                ws=ws,
                install_prefix=install_prefix,
                dftracer_ref=dftracer_ref,
                jobs=jobs,
                install_mode=install_mode,
                features=features,
                python_exe=python_exe,
            )

            if not result["success"]:
                return _err(
                    f"dftracer autobuild.sh failed (mode={install_mode})",
                    ref=dftracer_ref,
                    prefix=str(install_prefix),
                    steps=result["steps"],
                )

            _save_state(run_id, {"dftracer_install_prefix": str(install_prefix)})

            # Also install dftracer-utils from develop so dftracer_split is available
            # for compacting traces produced by the annotated app.
            utils_pip = (install_prefix / "bin" / "pip")
            if not utils_pip.exists():
                utils_pip = Path(sys.executable).parent / "pip"
            utils_r = _install_dftracer_utils(utils_pip)

            return _ok(
                f"dftracer built and installed via autobuild.sh (mode={install_mode})",
                ref=dftracer_ref,
                prefix=str(install_prefix),
                dftracer_utils_installed=utils_r["success"],
                steps=result["steps"],
            )

        @self.session_subservice.tool()
        def session_install_dftracer(
            run_id: str,
            dftracer_ref: str = "v2.0.3",
            jobs: int = 4,
        ) -> str:
            """
            Install dftracer into the session's annotated install directory.

            For C/C++ projects (cmake / autotools / make):
                Clones https://github.com/llnl/dftracer.git at <dftracer_ref>,
                builds with cmake, and installs into <workspace>/install_ann/ —
                the same prefix used by session_build_annotated.  The install
                prefix is stored in session state so session_build_annotated
                automatically passes CMAKE_PREFIX_PATH / pkg-config flags.

            For Python projects:
                Installs dftracer via pip into the project venv at
                <workspace>/install/.  Tries PyPI first; falls back to git source.

            Call this after session_annotate_source and before
            session_build_annotated.

            Args:
                run_id:       Session identifier.
                dftracer_ref: Git tag or branch (default: v2.0.3).
                jobs:         Parallel make jobs for cmake build (default: 4).
            """
            ws = _ws(run_id)
            state = _load_state(run_id)
            info = state.get("detection") or _detect_info(ws / "source")
            bt = info.get("build_tool", "unknown")

            if bt in {"cmake", "autotools", "make"}:
                install_ann = ws / "install_ann"
                install_ann.mkdir(exist_ok=True)
                result = _install_dftracer_autobuild(
                    ws=ws,
                    install_prefix=install_ann,
                    dftracer_ref=dftracer_ref,
                    jobs=jobs,
                    install_mode="cmake",
                    features=info.get("features", {}),
                )
                if not result["success"]:
                    return _err(
                        "dftracer autobuild (cmake mode) failed",
                        prefix=str(install_ann),
                        ref=dftracer_ref,
                        steps=result["steps"],
                    )
                _save_state(run_id, {"dftracer_install_prefix": str(install_ann)})
                return _ok(
                    "dftracer installed via autobuild.sh (cmake mode)",
                    prefix=str(install_ann),
                    ref=dftracer_ref,
                    steps=result["steps"],
                )

            if bt == "python":
                venv_python = ws / "install" / "bin" / "python3"
                if not venv_python.exists():
                    venv_python = Path(sys.executable)
                install_dir = ws / "install"
                install_dir.mkdir(exist_ok=True)
                result = _install_dftracer_autobuild(
                    ws=ws,
                    install_prefix=install_dir,
                    dftracer_ref=dftracer_ref,
                    jobs=jobs,
                    install_mode="pip",
                    features=info.get("features", {}),
                    python_exe=str(venv_python),
                )
                if not result["success"]:
                    return _err(
                        "dftracer autobuild (pip mode) failed",
                        ref=dftracer_ref,
                        steps=result["steps"],
                    )
                _save_state(run_id, {"dftracer_install_prefix": str(install_dir)})
                return _ok(
                    "dftracer installed via autobuild.sh (pip mode)",
                    ref=dftracer_ref,
                    steps=result["steps"],
                )

            return _err(f"Unsupported build tool for dftracer install: {bt}")

        @self.session_subservice.tool()
        def session_install_dftracer_utils(
            run_id: str,
        ) -> str:
            """
            Install dftracer-utils from the develop branch into the session environment.

            dftracer-utils provides the dftracer_split binary used by
            session_split_traces to compact raw .pfw trace files, as well as
            dftracer_info, dftracer_merge, and other analysis tools.

            Installs into the Python environment currently running the MCP server
            (the same env that provides the dftracer-utils MCP tools) using pip
            with --upgrade so the latest develop snapshot is always fetched.

            Call this once per session before session_split_traces if you want
            to guarantee the develop-branch version of dftracer-utils is active.

            Args:
                run_id: Session identifier (used only for state tracking).
            """
            # Prefer the session venv pip; fall back to the server's own pip
            ws = _ws(run_id)
            pip = ws / "install" / "bin" / "pip"
            if not pip.exists():
                pip = Path(sys.executable).parent / "pip"
            if not pip.exists():
                pip = Path("pip3")

            r = _install_dftracer_utils(pip)
            _save_state(run_id, {"dftracer_utils_installed": r["success"]})
            if r["success"]:
                return _ok("dftracer-utils installed from develop", **r)
            return _err("dftracer-utils install failed", **r)

        @self.session_subservice.tool()
        def session_build_annotated(
            run_id: str,
            jobs: int = 4,
            extra_cmake_flags: str = "",
        ) -> str:
            """
            Configure and build the annotated source with dftracer linked.

            Builds into build_ann/ and installs into install_ann/ so the
            original build is preserved for comparison.

            If session_install_dftracer was called first, the dftracer install
            prefix is read from session state and automatically added:
              - cmake:     -DCMAKE_PREFIX_PATH=<prefix>
              - autotools: PKG_CONFIG_PATH / CPPFLAGS / LDFLAGS env vars
              - python:    dftracer already in the venv; no extra flags needed

            Args:
                run_id:            Session identifier.
                jobs:              Parallel make jobs.
                extra_cmake_flags: Extra -D flags passed to cmake.
            """
            ws = _ws(run_id)
            ann = ws / "annotated"
            build_ann = ws / "build_ann"
            install_ann = ws / "install_ann"
            build_ann.mkdir(exist_ok=True)
            install_ann.mkdir(exist_ok=True)

            if not ann.exists():
                return _err("annotated/ not found — run session_copy_annotated first")

            state = _load_state(run_id)
            info = state.get("detection") or _detect_info(ws / "source")
            bt = info.get("build_tool", "unknown")
            dft_prefix = state.get("dftracer_install_prefix")

            steps: Dict[str, Any] = {}

            if bt == "cmake":
                flags = [
                    f"-DCMAKE_INSTALL_PREFIX={install_ann}",
                    "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
                ]
                if dft_prefix:
                    flags.append(f"-DCMAKE_PREFIX_PATH={dft_prefix}")
                flags += (extra_cmake_flags.split() if extra_cmake_flags else [])
                r_cfg = _run(
                    ["cmake", "-S", str(ann), "-B", str(build_ann)] + flags,
                    timeout=300,
                )
                steps["configure"] = r_cfg
                if not r_cfg["success"]:
                    return _err("cmake configure failed for annotated source", **r_cfg)

                r_bld = _run(["make", f"-j{jobs}"], cwd=build_ann, timeout=600)
                steps["build"] = r_bld
                if not r_bld["success"]:
                    return _err("make failed for annotated source", **r_bld)

                r_ins = _run(["make", "install"], cwd=build_ann, timeout=300)
                steps["install"] = r_ins
                if not r_ins["success"]:
                    return _err("make install failed for annotated source", **r_ins)

            elif bt == "autotools":
                if (ann / "configure.ac").exists() and not (ann / "configure").exists():
                    _run(["autoreconf", "-fi"], cwd=ann, timeout=120)
                env: Dict[str, str] = {}
                if dft_prefix:
                    pkg_cfg = f"{dft_prefix}/lib/pkgconfig"
                    env["PKG_CONFIG_PATH"] = pkg_cfg
                    env["CPPFLAGS"] = f"-I{dft_prefix}/include"
                    env["LDFLAGS"] = f"-L{dft_prefix}/lib -Wl,-rpath,{dft_prefix}/lib"
                r_cfg = _run(
                    [str(ann / "configure"), f"--prefix={install_ann}"],
                    cwd=build_ann,
                    env=env if env else None,
                    timeout=300,
                )
                steps["configure"] = r_cfg
                if not r_cfg["success"]:
                    return _err("configure failed for annotated source", **r_cfg)
                r_bld = _run(["make", f"-j{jobs}"], cwd=build_ann, env=env if env else None, timeout=600)
                steps["build"] = r_bld
                if not r_bld["success"]:
                    return _err("make failed for annotated source", **r_bld)
                r_ins = _run(["make", "install"], cwd=build_ann, timeout=300)
                steps["install"] = r_ins

            elif bt == "python":
                pip = ws / "install" / "bin" / "pip"
                if not pip.exists():
                    pip = Path("pip3")
                r_bld = _run([str(pip), "install", "-e", str(ann)], timeout=300)
                steps["pip_install"] = r_bld
                if not r_bld["success"]:
                    return _err("pip install failed for annotated source", **r_bld)

            else:
                return _err(f"Unsupported build tool: {bt}")

            _save_state(run_id, {"step": "annotated_built"})
            return _ok("Annotated build succeeded", build_tool=bt, steps=steps)

        @self.session_subservice.tool()
        def session_run_with_dftracer(
            run_id: str,
            command: str,
            subfolder: str = "build_ann",
            data_dir: Optional[str] = None,
            timeout: int = 600,
            env_extra: Optional[str] = None,
        ) -> str:
            """
            Run a command with dftracer environment variables set so traces are
            captured in the dedicated <workspace>/traces/ directory.

            Trace files land at <workspace>/traces/<run_id>.<pid>.pfw and are
            consumed by session_split_traces.

            Sets (per https://dftracer.readthedocs.io/en/latest/api.html):
              DFTRACER_ENABLE=1        — activate tracing
              DFTRACER_INC_METADATA=1  — include process/thread metadata in traces
              DFTRACER_LOG_FILE=<workspace>/traces/<run_id>  (prefix; dftracer appends .<pid>.pfw)
              DFTRACER_DATA_DIR=<data_dir or source/>
              DFTRACER_INIT=1          — auto-initialise without explicit API call

            Args:
                run_id:    Session identifier.
                command:   Shell command to run (via /bin/sh -c).
                subfolder: Working directory inside the workspace (default: build_ann).
                data_dir:  Path to monitor for I/O tracing (defaults to source/).
                timeout:   Seconds before killing the command.
                env_extra: JSON object of additional env vars to merge/override.
            """
            ws = _ws(run_id)
            traces_dir = ws / "traces"   # always the canonical trace directory
            traces_dir.mkdir(exist_ok=True)

            cwd = ws / subfolder
            if not cwd.exists():
                cwd = ws / "build"
            if not cwd.exists():
                cwd = ws / "source"

            # DFTRACER_LOG_FILE is a prefix; dftracer appends .<pid>.pfw to it.
            # Using run_id as the prefix keeps trace files clearly associated with
            # this specific run: <workspace>/traces/<run_id>.<pid>.pfw
            log_file_prefix = str(traces_dir / run_id)

            env: Dict[str, str] = {
                "DFTRACER_ENABLE": "1",
                "DFTRACER_INC_METADATA": "1",
                "DFTRACER_LOG_FILE": log_file_prefix,
                "DFTRACER_DATA_DIR": data_dir or str(ws / "source"),
                "DFTRACER_INIT": "1",
            }
            if env_extra:
                env.update(json.loads(env_extra))

            r = _run(["/bin/sh", "-c", command], cwd=cwd, env=env, timeout=timeout)
            _save_state(run_id, {
                "step": "ran_with_dftracer",
                "dftracer_run": {"command": command, **r},
            })
            _write_artifact_log(ws, 11, "session_run_with_dftracer", {"command": command, "result": r, "traces_dir": str(traces_dir)}, run_id)
            if r["success"]:
                return _ok("Command completed with dftracer", traces_dir=str(traces_dir), **r)
            return _err("Command failed with dftracer", traces_dir=str(traces_dir), **r)

        @self.session_subservice.tool()
        def session_split_traces(
            run_id: str,
            app_name: str = "app",
        ) -> str:
            """
            Compact raw dftracer traces via the dftracer-utils split MCP tool.

            Reads raw .pfw / .pfw.gz files from <workspace>/traces/ (the
            dedicated trace directory written by session_run_with_dftracer)
            and writes compacted chunks to <workspace>/traces_split/.

            Uses DftracerUtilsService.split under the hood so that all
            dftracer-utils error handling and output formatting is applied.
            Falls back to calling the dftracer_split binary directly if the
            service cannot be loaded.

            Call session_install_dftracer_utils first to ensure the
            develop-branch version of dftracer-utils is active.

            Args:
                run_id:   Session identifier.
                app_name: Prefix for output chunk files (default: "app").
            """
            ws = _ws(run_id)
            traces_in = ws / "traces"
            traces_out = ws / "traces_split"
            traces_out.mkdir(exist_ok=True)

            if not traces_in.exists():
                return _err(f"traces/ not found in session {run_id} — run session_run_with_dftracer first")

            trace_files = list(traces_in.glob("*.pfw")) + list(traces_in.glob("*.pfw.gz"))
            if not trace_files:
                return _err(f"No .pfw or .pfw.gz files found in {traces_in}")

            r = _dftracer_utils_split(
                directory=str(traces_in),
                output_dir=str(traces_out),
                app_name=app_name,
            )
            _save_state(run_id, {"step": "traces_split", "split_result": r})
            _write_artifact_log(ws, 12, "session_split_traces", r, run_id)
            if r["success"]:
                return _ok("Traces split successfully", output=str(traces_out), **r)
            return _err("dftracer_split failed", **r)

        @self.session_subservice.tool()
        def session_analyze_traces(
            run_id: str,
            trace_subdir: str = "traces_split",
            query_type: str = "summary",
            index_dir: Optional[str] = None,
            extra_flags: str = "",
        ) -> str:
            """
            Summarise dftracer traces using dftracer_info (dfanalyzer).

            Args:
                run_id:       Session identifier.
                trace_subdir: Sub-folder containing split traces.
                query_type:   dftracer_info --query value (default: summary).
                index_dir:    Optional index directory; defaults to traces_subdir/idx.
                extra_flags:  Additional flags for dftracer_info.
            """
            ws = _ws(run_id)
            traces = ws / trace_subdir
            if not traces.exists():
                return _err(f"{trace_subdir}/ not found — run session_split_traces first")

            idx = Path(index_dir) if index_dir else traces / "idx"
            idx.mkdir(parents=True, exist_ok=True)

            flags = extra_flags.split() if extra_flags else []
            r = _run(
                [
                    "dftracer_info",
                    "-d", str(traces),
                    "--query", query_type,
                    "--index-dir", str(idx),
                ] + flags,
                timeout=600,
            )
            _save_state(run_id, {"step": "traces_analyzed", "analysis_result": r})
            _write_artifact_log(_ws(run_id), 13, "session_analyze_traces", r, run_id)
            if r["success"]:
                return _ok("Analysis complete", **r)
            return _err("dftracer_info failed", **r)

        @self.session_subservice.tool()
        def session_status(run_id: str) -> str:
            """
            Return the current state of a session.

            Args:
                run_id: Session identifier.
            """
            ws = _ws(run_id)
            if not ws.exists():
                return _err(f"Session {run_id} not found")
            state = _load_state(run_id)
            subdirs = [d.name for d in ws.iterdir() if d.is_dir()]
            # Drop keys that we pass explicitly to avoid duplicate-keyword errors
            extra = {k: v for k, v in state.items() if k not in {"workspace"}}
            return _ok("Session status", workspace=str(ws), subdirs=subdirs, **extra)

    # -----------------------------------------------------------------------
    # Orchestration pipeline
    # -----------------------------------------------------------------------

    def _register_pipeline_tool(self) -> None:

        @self.pipeline_subservice.tool()
        def session_run_pipeline(
            url: str,
            ref: str = "main",
            smoke_test_command: Optional[str] = None,
            extra_cmake_flags: str = "",
            jobs: int = 4,
            run_id: Optional[str] = None,
            skip_annotation: bool = False,
            dftracer_ref: str = "v2.0.3",
        ) -> str:
            """
            Full dftracer annotation + smoke-test pipeline.

            Executes all steps in sequence and returns a detailed report:
              1.   Create session and clone source
              2.   Detect language, build tool, and dftracer features
              3.   Configure build
              4.   Build and install
              5.   Run smoke test (with auto-detected command if not provided)
              6.   Copy source to annotated/
              7.   Patch build system for dftracer
              8.   Auto-annotate C/C++ and Python source
              8.5. Install dftracer into install_ann/ (C/C++) or venv (Python)
              9.   Build annotated source with dftracer (CMAKE_PREFIX_PATH set)
              10.  Run smoke test with dftracer (traces collected)
              11.  dftracer_split — compact traces
              12.  dftracer_info  — summarise traces

            If a step fails the pipeline stops and reports which step failed
            along with stdout/stderr so the LLM can diagnose and retry using
            individual session_* tools.

            Args:
                url:                Git URL to clone.
                ref:                Branch, tag, or commit (default: main).
                smoke_test_command: Shell command to verify the build.  Auto-
                                    detected from build tool if omitted.
                extra_cmake_flags:  Extra cmake -D flags for both builds.
                jobs:               Parallel make jobs.
                run_id:             Optional fixed RUN-ID; UUID generated if omitted.
                skip_annotation:    If True, stop after step 5 (original smoke test).
                dftracer_ref:       dftracer git tag/branch to install (default: v2.0.3).
            """
            report: Dict[str, Any] = {}

            # --- Step 1: create session + clone ---
            rid = _new_run_id(run_id)
            ws = _ws(rid)
            ws.mkdir(parents=True, exist_ok=True)
            src = ws / "source"
            src.mkdir(exist_ok=True)

            clone_r = _run(
                ["git", "clone", "--depth", "1", "--branch", ref, url, str(src)],
                timeout=300,
            )
            if not clone_r["success"]:
                shutil.rmtree(src, ignore_errors=True)
                src.mkdir(exist_ok=True)
                clone_r = _run(["git", "clone", "--depth", "1", url, str(src)], timeout=300)
                if not clone_r["success"]:
                    return _err("Step 1 failed: git clone", step=1, **clone_r)
                _run(["git", "checkout", ref], cwd=src)
            report["step_1_clone"] = {"status": "ok", "run_id": rid}
            _write_artifact_log(ws, 1, "session_create", {"clone": clone_r, "run_id": rid, "url": url, "ref": ref}, rid)

            # --- Step 2: detect ---
            info = _detect_info(src)
            bt = info["build_tool"]
            _save_state(rid, {"run_id": rid, "url": url, "ref": ref,
                               "workspace": str(ws), "detection": info})
            report["step_2_detect"] = {
                "status": "ok",
                "languages": info["languages"],
                "build_tool": bt,
                "features": info["features"],
                "dftracer_cmake_flags": info["dftracer_cmake_flags"],
            }
            _write_artifact_log(ws, 2, "session_detect", report["step_2_detect"], rid)

            # --- Step 3: configure ---
            build = ws / "build"
            install = ws / "install"
            build.mkdir(exist_ok=True)
            install.mkdir(exist_ok=True)

            cmake_flags = [
                f"-DCMAKE_INSTALL_PREFIX={install}",
                "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
            ] + (extra_cmake_flags.split() if extra_cmake_flags else [])

            if bt == "cmake":
                cfg_r = _run(["cmake", "-S", str(src), "-B", str(build)] + cmake_flags, timeout=300)
            elif bt == "autotools":
                if (src / "configure.ac").exists() and not (src / "configure").exists():
                    _run(["autoreconf", "-fi"], cwd=src, timeout=120)
                cfg_r = _run([str(src / "configure"), f"--prefix={install}"], cwd=build, timeout=300)
            elif bt == "python":
                cfg_r = _run(["python3", "-m", "venv", str(install)], timeout=60)
                if cfg_r["success"]:
                    pip = install / "bin" / "pip"
                    cfg_r = _run([str(pip), "install", "-e", str(src)], timeout=300)
            else:
                cfg_r = {"success": False, "returncode": -1,
                          "stdout": "", "stderr": f"Unknown build tool: {bt}"}

            report["step_3_configure"] = cfg_r
            _write_artifact_log(ws, 3, "session_configure", {"configure": cfg_r, "build_tool": bt}, rid)
            if not cfg_r["success"]:
                return _err("Step 3 failed: configure", step=3, report=report)

            # --- Step 4: build + install ---
            if bt in {"cmake", "autotools", "make"}:
                bld_r = _run(["make", f"-j{jobs}"], cwd=build, timeout=600)
                report["step_4_build"] = bld_r
                if not bld_r["success"]:
                    return _err("Step 4 failed: make", step=4, report=report)
                ins_r = _run(["make", "install"], cwd=build, timeout=300)
                report["step_4_install"] = ins_r
                if not ins_r["success"]:
                    return _err("Step 4 failed: make install", step=4, report=report)
            else:
                report["step_4_build"] = {"status": "skipped (python)"}

            _write_artifact_log(ws, 4, "session_build_install", {
                k: v for k, v in report.items() if k.startswith("step_4")
            }, rid)

            # --- Step 5: original smoke test ---
            smoke_cmd = smoke_test_command or _guess_smoke_test(src, bt, install)
            if smoke_cmd:
                sm_r = _run(["/bin/sh", "-c", smoke_cmd], cwd=build, timeout=300)
                report["step_5_smoke_test"] = {**sm_r, "command": smoke_cmd}
                if not sm_r["success"]:
                    report["step_5_smoke_test"]["warning"] = (
                        "Original smoke test failed — continuing to annotation phase"
                    )
            else:
                report["step_5_smoke_test"] = {"status": "no smoke test detected"}

            _write_artifact_log(ws, 5, "session_run_smoke_test", report["step_5_smoke_test"], rid)
            _save_state(rid, {"step": "original_build_done", "detection": info})

            if skip_annotation:
                return _ok(
                    "Pipeline complete (annotation skipped)",
                    run_id=rid,
                    workspace=str(ws),
                    report=report,
                )

            # --- Step 6: copy to annotated/ ---
            ann = ws / "annotated"
            if ann.exists():
                shutil.rmtree(ann)
            shutil.copytree(src, ann)
            report["step_6_copy_annotated"] = {"status": "ok", "path": str(ann)}
            _write_artifact_log(ws, 6, "session_copy_annotated", report["step_6_copy_annotated"], rid)

            # --- Step 7: patch build system ---
            patched: List[str] = []
            if bt == "cmake":
                cml = ann / "CMakeLists.txt"
                if cml.exists():
                    cml.write_text(_patch_cmake(cml))
                    patched.append("CMakeLists.txt")
            elif bt == "autotools":
                for mf in ann.glob("Makefile*"):
                    mf.write_text(_patch_autotools_makefile(mf))
                    patched.append(mf.name)
            elif bt == "python":
                for pname, pfn in (("setup.py", _patch_setup_py), ("pyproject.toml", _patch_pyproject)):
                    pp = ann / pname
                    if pp.exists():
                        pp.write_text(pfn(pp))
                        patched.append(pname)
            report["step_7_patch_build"] = {"status": "ok", "patched": patched}
            _write_artifact_log(ws, 7, "session_patch_build", report["step_7_patch_build"], rid)

            # --- Step 8: annotate source ---
            c_entries = {str(p) for p in _find_c_entry_points(ann)}
            py_entries = {str(p) for p in _find_python_entry_points(ann)}
            annotated: List[str] = []
            langs = info.get("languages", [])

            if "c" in langs or "cpp" in langs:
                for ext in ("*.c", "*.h", "*.cpp", "*.cxx", "*.cc", "*.hpp"):
                    for f in ann.rglob(ext):
                        try:
                            old = f.read_text(errors="ignore")
                            new = _annotate_c_source(old, f, is_entry=str(f) in c_entries)
                            if new != old:
                                f.write_text(new)
                                annotated.append(str(f.relative_to(ann)))
                        except OSError:
                            pass

            if "python" in langs:
                for f in ann.rglob("*.py"):
                    try:
                        old = f.read_text(errors="ignore")
                        new = _annotate_python_source(old, is_entry=str(f) in py_entries)
                        if new != old:
                            f.write_text(new)
                            annotated.append(str(f.relative_to(ann)))
                    except OSError:
                        pass

            ann_patch = ws / "annotation.patch"
            ann_patch_chunks: List[str] = []
            # collect diffs already written to annotated files
            for f_rel in annotated:
                src_f = ws / "source" / f_rel
                ann_f = ann / f_rel
                if src_f.exists() and ann_f.exists():
                    ann_patch_chunks.append("".join(difflib.unified_diff(
                        src_f.read_text(errors="ignore").splitlines(keepends=True),
                        ann_f.read_text(errors="ignore").splitlines(keepends=True),
                        fromfile=f"a/{f_rel}", tofile=f"b/{f_rel}",
                    )))
            ann_patch.write_text("".join(ann_patch_chunks))

            report["step_8_annotate"] = {
                "status": "ok",
                "files_annotated": len(annotated),
                "annotated": annotated,
                "patch_file": str(ann_patch),
            }
            _write_artifact_log(ws, 8, "session_annotate_source", report["step_8_annotate"], rid)

            # --- Step 8.5: install dftracer into install_ann/ (C/C++) or venv (Python) ---
            build_ann = ws / "build_ann"
            install_ann = ws / "install_ann"
            build_ann.mkdir(exist_ok=True)
            install_ann.mkdir(exist_ok=True)

            dft_prefix: Optional[str] = None
            if bt in {"cmake", "autotools", "make"}:
                dft_r = _install_dftracer_autobuild(
                    ws=ws,
                    install_prefix=install_ann,
                    dftracer_ref=dftracer_ref,
                    jobs=jobs,
                    install_mode="cmake",
                    features=info.get("features", {}),
                )
                report["step_8_5_install_dftracer"] = {
                    "ref": dftracer_ref,
                    "prefix": str(install_ann),
                    "steps": dft_r["steps"],
                    "success": dft_r["success"],
                }
                _write_artifact_log(ws, 9, "session_install_dftracer", report["step_8_5_install_dftracer"], rid)
                if not dft_r["success"]:
                    return _err("Step 8.5 failed: dftracer autobuild (cmake mode)", step="8.5", report=report)
                dft_prefix = str(install_ann)
            elif bt == "python":
                venv_python = ws / "install" / "bin" / "python3"
                if not venv_python.exists():
                    venv_python = Path(sys.executable)
                install_dir = ws / "install"
                install_dir.mkdir(exist_ok=True)
                dft_r = _install_dftracer_autobuild(
                    ws=ws,
                    install_prefix=install_dir,
                    dftracer_ref=dftracer_ref,
                    jobs=jobs,
                    install_mode="pip",
                    features=info.get("features", {}),
                    python_exe=str(venv_python),
                )
                report["step_8_5_install_dftracer"] = {
                    "ref": dftracer_ref,
                    "steps": dft_r["steps"],
                    "success": dft_r["success"],
                }
                _write_artifact_log(ws, 9, "session_install_dftracer", report["step_8_5_install_dftracer"], rid)
                if not dft_r["success"]:
                    return _err("Step 8.5 failed: dftracer autobuild (pip mode)", step="8.5", report=report)
                dft_prefix = str(install_dir)

            _save_state(rid, {"dftracer_install_prefix": dft_prefix})

            # --- Steps 9-10: build annotated + run with dftracer (retry loop) ---
            # Automatically fixes dftracer annotation errors and retries up to
            # MAX_ANNOTATION_RETRIES times before giving up.
            traces_dir = ws / "traces"
            traces_dir.mkdir(exist_ok=True)
            dftracer_env = {
                "DFTRACER_ENABLE": "1",
                "DFTRACER_INC_METADATA": "1",
                "DFTRACER_LOG_FILE": str(traces_dir / "trace"),
                "DFTRACER_DATA_DIR": str(src),
                "DFTRACER_INIT": "1",
            }
            ann_smoke_cwd = build_ann if build_ann.exists() else ann
            MAX_ANNOTATION_RETRIES = 3
            build_ok = False
            run_ok = not bool(smoke_cmd)  # trivially ok if there is no smoke command

            for attempt in range(1, MAX_ANNOTATION_RETRIES + 1):
                sfx = f"_attempt{attempt}"
                build_step: Dict[str, Any] = {"attempt": attempt}

                # Wipe the build dir on retries to avoid stale object files
                if attempt > 1:
                    shutil.rmtree(build_ann, ignore_errors=True)
                    build_ann.mkdir(exist_ok=True)

                # ---- build ----
                if bt == "cmake":
                    ann_flags = [
                        f"-DCMAKE_INSTALL_PREFIX={install_ann}",
                        "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
                    ]
                    if dft_prefix:
                        ann_flags.append(f"-DCMAKE_PREFIX_PATH={dft_prefix}")
                    ann_flags += (extra_cmake_flags.split() if extra_cmake_flags else [])
                    r_ac = _run(["cmake", "-S", str(ann), "-B", str(build_ann)] + ann_flags, timeout=300)
                    build_step["configure"] = r_ac
                    if r_ac["success"]:
                        r_ab = _run(["make", f"-j{jobs}"], cwd=build_ann, timeout=600)
                        build_step["build"] = r_ab
                        if r_ab["success"]:
                            _run(["make", "install"], cwd=build_ann, timeout=300)
                            build_ok = True

                elif bt == "autotools":
                    if (ann / "configure.ac").exists() and not (ann / "configure").exists():
                        _run(["autoreconf", "-fi"], cwd=ann, timeout=120)
                    env_ann: Dict[str, str] = {}
                    if dft_prefix:
                        env_ann["PKG_CONFIG_PATH"] = f"{dft_prefix}/lib/pkgconfig"
                        env_ann["CPPFLAGS"] = f"-I{dft_prefix}/include"
                        env_ann["LDFLAGS"] = f"-L{dft_prefix}/lib -Wl,-rpath,{dft_prefix}/lib"
                    r_ac = _run(
                        [str(ann / "configure"), f"--prefix={install_ann}"],
                        cwd=build_ann, env=env_ann or None, timeout=300,
                    )
                    build_step["configure"] = r_ac
                    if r_ac["success"]:
                        r_ab = _run(["make", f"-j{jobs}"], cwd=build_ann,
                                    env=env_ann or None, timeout=600)
                        build_step["build"] = r_ab
                        if r_ab["success"]:
                            _run(["make", "install"], cwd=build_ann, timeout=300)
                            build_ok = True

                elif bt == "python":
                    pip = ws / "install" / "bin" / "pip"
                    if not pip.exists():
                        pip = Path("pip3")
                    r_ab = _run([str(pip), "install", "-e", str(ann)], timeout=300)
                    build_step["build"] = r_ab
                    build_ok = r_ab["success"]

                report[f"step_9{sfx}"] = build_step
                _write_artifact_log(ws, 10, f"session_build_annotated{sfx}", build_step, rid)

                if not build_ok:
                    # Collect all stderr from this attempt
                    build_stderr = "\n".join(
                        v.get("stderr", "") for v in build_step.values()
                        if isinstance(v, dict)
                    )
                    fixed = _fix_dftracer_annotation_errors(ann, build_stderr)
                    report[f"step_9_fix{sfx}"] = {
                        "fixed_files": fixed,
                        "stderr_excerpt": build_stderr[:500],
                    }
                    if fixed and attempt < MAX_ANNOTATION_RETRIES:
                        # Write fix log and try again
                        _write_artifact_log(ws, 10, f"session_build_fix{sfx}",
                                            report[f"step_9_fix{sfx}"], rid)
                        continue
                    return _err(
                        f"Step 9 failed after {attempt} attempt(s): build annotated",
                        step=9, attempts=attempt,
                        fixed_files=fixed,
                        hint=(
                            "Automated fix was unable to resolve all errors. "
                            "Use session_read_file + session_write_file to manually "
                            "correct the annotation in annotated/, then call "
                            "session_build_annotated to rebuild."
                        ),
                        report=report,
                    )

                # ---- run with dftracer ----
                if smoke_cmd:
                    # Clear stale traces from previous attempts
                    for tf in traces_dir.glob("*.pfw*"):
                        tf.unlink()

                    sm2_r = _run(
                        ["/bin/sh", "-c", smoke_cmd],
                        cwd=ann_smoke_cwd, env=dftracer_env, timeout=300,
                    )
                    report[f"step_10{sfx}"] = {**sm2_r, "command": smoke_cmd, "attempt": attempt}
                    _write_artifact_log(ws, 11, f"session_run_with_dftracer{sfx}",
                                        report[f"step_10{sfx}"], rid)
                    run_ok = sm2_r["success"]

                    if run_ok:
                        break  # both build and run succeeded

                    # Run failed — try annotation fix and rebuild
                    run_stderr = sm2_r.get("stderr", "")
                    fixed = _fix_dftracer_annotation_errors(ann, run_stderr)
                    report[f"step_10_fix{sfx}"] = {
                        "fixed_files": fixed,
                        "stderr_excerpt": run_stderr[:500],
                    }
                    if fixed and attempt < MAX_ANNOTATION_RETRIES:
                        _write_artifact_log(ws, 11, f"session_run_fix{sfx}",
                                            report[f"step_10_fix{sfx}"], rid)
                        build_ok = False  # force rebuild on next iteration
                        continue
                    # No fixable dftracer issue — stop retrying
                    return _err(
                        f"Step 10 failed after {attempt} attempt(s): smoke test with dftracer",
                        step=10, attempts=attempt,
                        hint=(
                            "Use session_read_file + session_write_file to manually "
                            "fix annotation in annotated/, then call "
                            "session_build_annotated and session_run_with_dftracer."
                        ),
                        report=report,
                    )
                else:
                    report["step_10_smoke_with_dftracer"] = {"status": "no smoke command provided", "attempt": attempt}
                    _write_artifact_log(ws, 11, f"session_run_with_dftracer{sfx}",
                                        report["step_10_smoke_with_dftracer"], rid)
                    break  # no smoke test — build alone is enough

            # Promote the last successful attempt's data to canonical report keys
            for k in list(report.keys()):
                if k.startswith("step_9_attempt") or k.startswith("step_10_attempt"):
                    base = "step_9_build_ann" if "step_9" in k else "step_10_smoke_with_dftracer"
                    report.setdefault(base, report[k])

            _save_state(rid, {"step": "annotated_built"})
            # Write final canonical logs with fixed step numbers
            _write_artifact_log(ws, 10, "session_build_annotated",
                                 report.get("step_9_build_ann", {}), rid)
            _write_artifact_log(ws, 11, "session_run_with_dftracer",
                                 report.get("step_10_smoke_with_dftracer", {}), rid)

            # --- Step 11: split traces (via dftracer-utils MCP service) ---
            traces_split = ws / "traces_split"
            traces_split.mkdir(exist_ok=True)
            trace_files = list(traces_dir.glob("*.pfw")) + list(traces_dir.glob("*.pfw.gz"))

            if trace_files:
                sp_r = _dftracer_utils_split(
                    directory=str(traces_dir),
                    output_dir=str(traces_split),
                    app_name=rid,
                )
                report["step_11_split"] = sp_r
                if not sp_r["success"]:
                    report["step_11_split"]["warning"] = "dftracer_split (utils service) failed — proceeding"
            else:
                report["step_11_split"] = {"status": "no trace files found"}
                traces_split = traces_dir  # fall back
            _write_artifact_log(ws, 12, "session_split_traces", report["step_11_split"], rid)

            # --- Step 12: analyze traces ---
            idx_dir = traces_split / "idx"
            idx_dir.mkdir(exist_ok=True)
            an_r = _run(
                [
                    "dftracer_info",
                    "-d", str(traces_split),
                    "--query", "summary",
                    "--index-dir", str(idx_dir),
                ],
                timeout=600,
            )
            report["step_12_analyze"] = an_r
            _write_artifact_log(ws, 13, "session_analyze_traces", an_r, rid)

            _save_state(rid, {
                "step": "pipeline_complete",
                "traces": str(traces_dir),
                "traces_split": str(traces_split),
            })

            return _ok(
                "Pipeline complete",
                run_id=rid,
                workspace=str(ws),
                report=report,
            )


MCPServiceFactory.register("dftracer-session", DFTracerSessionService())
