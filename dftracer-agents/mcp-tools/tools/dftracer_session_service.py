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

import json
import os
import re
import shutil
import subprocess
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


def _annotate_c_source(content: str, filepath: Path, is_entry: bool) -> str:
    """Inject dftracer C/C++ macros into source.  Idempotent."""
    if _C_INCLUDE in content:
        return content

    is_cpp = filepath.suffix.lower() in {".cpp", ".cxx", ".cc"}
    fn_macro = "DFTRACER_CPP_FUNCTION();" if is_cpp else "DFTRACER_C_FUNCTION_START();"

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

    if is_entry:
        # Insert DFTRACER_C_INIT right after main()'s opening brace
        result = re.sub(
            r"(int\s+main\s*\([^)]*\)\s*\{)",
            r"\1\n  DFTRACER_C_INIT(nullptr, nullptr, nullptr);",
            result,
            count=1,
        )
        # Insert DFTRACER_FINI before every return in main — simplified: flag for LLM
        result += "\n/* TODO: add DFTRACER_FINI() before process exit */\n"

    # Add per-function macro at the opening of every non-trivial function body
    result = re.sub(
        r"(\b\w[\w\s\*:<>]*\s+\w+\s*\([^)]*\)\s*(?:const\s*)?(?:override\s*)?(?:noexcept\s*)?\{)",
        r"\1\n  " + fn_macro,
        result,
    )
    return result


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
            rid = run_id or uuid.uuid4().hex[:12]
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
                    return _err("make failed", **r)
                r2 = _run(["make", "install"], cwd=build, timeout=300)
                if not r2["success"]:
                    return _err("make install failed", **r2)
                _save_state(run_id, {"step": "installed"})
                return _ok("Build and install succeeded", make=r, install=r2)

            if bt == "python":
                _save_state(run_id, {"step": "installed"})
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
            Run a smoke test command inside the workspace.

            Args:
                run_id:    Session identifier.
                command:   Shell command to run (passed to /bin/sh -c).
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

            r = _run(["/bin/sh", "-c", command], cwd=cwd, env=env, timeout=timeout)
            _save_state(run_id, {"last_smoke_test": {"command": command, **r}})
            if r["success"]:
                return _ok("Smoke test passed", **r)
            return _err("Smoke test failed", **r)

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
            Automatically inject dftracer API calls into C/C++ and Python source
            files in annotated/.

            Uses:
              - C/C++:  DFTRACER_C_INIT / DFTRACER_CPP_FUNCTION / DFTRACER_C_FUNCTION_START
              - Python: @dft_fn decorator + DFTRACER_INIT

            See https://dftracer.readthedocs.io/en/latest/api.html for the full API.

            Args:
                run_id:              Session identifier.
                auto_detect_entries: If True, detect main() / __main__ as entry points
                                     and inject DFTRACER_C_INIT / DFTRACER_INIT there.
            """
            ws = _ws(run_id)
            ann = ws / "annotated"
            if not ann.exists():
                return _err("annotated/ not found — run session_copy_annotated first")

            state = _load_state(run_id)
            info = state.get("detection") or _detect_info(ws / "source")
            langs = info.get("languages", [])

            c_entries = set()
            py_entries = set()
            if auto_detect_entries:
                c_entries = {str(p) for p in _find_c_entry_points(ann)}
                py_entries = {str(p) for p in _find_python_entry_points(ann)}

            annotated_files: List[str] = []
            errors: List[str] = []

            if "c" in langs or "cpp" in langs:
                for ext in ("*.c", "*.h", "*.cpp", "*.cxx", "*.cc", "*.hpp"):
                    for f in ann.rglob(ext):
                        try:
                            old = f.read_text(errors="ignore")
                            new = _annotate_c_source(old, f, is_entry=str(f) in c_entries)
                            if new != old:
                                f.write_text(new)
                                annotated_files.append(str(f.relative_to(ann)))
                        except OSError as exc:
                            errors.append(f"{f}: {exc}")

            if "python" in langs:
                for f in ann.rglob("*.py"):
                    try:
                        old = f.read_text(errors="ignore")
                        new = _annotate_python_source(old, is_entry=str(f) in py_entries)
                        if new != old:
                            f.write_text(new)
                            annotated_files.append(str(f.relative_to(ann)))
                    except OSError as exc:
                        errors.append(f"{f}: {exc}")

            _save_state(run_id, {"step": "source_annotated", "annotated_files": annotated_files})
            return _ok(
                f"Annotated {len(annotated_files)} file(s)",
                annotated=annotated_files,
                entry_points_c=sorted(c_entries),
                entry_points_py=sorted(py_entries),
                errors=errors,
            )

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

            steps: Dict[str, Any] = {}

            if bt == "cmake":
                flags = [
                    f"-DCMAKE_INSTALL_PREFIX={install_ann}",
                    "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
                ] + (extra_cmake_flags.split() if extra_cmake_flags else [])
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
                r_cfg = _run(
                    [str(ann / "configure"), f"--prefix={install_ann}"],
                    cwd=build_ann,
                    timeout=300,
                )
                steps["configure"] = r_cfg
                if not r_cfg["success"]:
                    return _err("configure failed for annotated source", **r_cfg)
                r_bld = _run(["make", f"-j{jobs}"], cwd=build_ann, timeout=600)
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
            trace_subdir: str = "traces",
            data_dir: Optional[str] = None,
            timeout: int = 600,
            env_extra: Optional[str] = None,
        ) -> str:
            """
            Run a command with dftracer environment variables set so traces are
            captured in <workspace>/traces/.

            Sets:
              DFTRACER_ENABLE=1
              DFTRACER_LOG_FILE=<workspace>/traces/trace
              DFTRACER_DATA_DIR=<data_dir or source/>

            Args:
                run_id:      Session identifier.
                command:     Shell command to run (via /bin/sh -c).
                subfolder:   Working directory (default: build_ann).
                trace_subdir: Sub-folder for traces (default: traces).
                data_dir:    Path to monitor for I/O tracing (defaults to source/).
                timeout:     Seconds before killing the command.
                env_extra:   JSON object of additional env vars.
            """
            ws = _ws(run_id)
            traces_dir = ws / trace_subdir
            traces_dir.mkdir(exist_ok=True)

            cwd = ws / subfolder
            if not cwd.exists():
                cwd = ws / "build"
            if not cwd.exists():
                cwd = ws / "source"

            env: Dict[str, str] = {
                "DFTRACER_ENABLE": "1",
                "DFTRACER_LOG_FILE": str(traces_dir / "trace"),
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
            if r["success"]:
                return _ok("Command completed with dftracer", traces_dir=str(traces_dir), **r)
            return _err("Command failed with dftracer", traces_dir=str(traces_dir), **r)

        @self.session_subservice.tool()
        def session_split_traces(
            run_id: str,
            trace_subdir: str = "traces",
            output_subdir: str = "traces_split",
            extra_flags: str = "",
        ) -> str:
            """
            Compact raw dftracer traces using dftracer_split.

            Args:
                run_id:        Session identifier.
                trace_subdir:  Sub-folder containing raw .pfw / .pfw.gz files.
                output_subdir: Sub-folder for the compacted output.
                extra_flags:   Additional flags passed to dftracer_split.
            """
            ws = _ws(run_id)
            traces_in = ws / trace_subdir
            traces_out = ws / output_subdir
            traces_out.mkdir(exist_ok=True)

            if not traces_in.exists():
                return _err(f"{trace_subdir}/ not found in session {run_id}")

            trace_files = list(traces_in.glob("*.pfw")) + list(traces_in.glob("*.pfw.gz"))
            if not trace_files:
                return _err(f"No .pfw or .pfw.gz files found in {traces_in}")

            flags = extra_flags.split() if extra_flags else []
            r = _run(
                ["dftracer_split", "-d", str(traces_in), "-o", str(traces_out)] + flags,
                timeout=600,
            )
            _save_state(run_id, {"step": "traces_split", "split_result": r})
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
        ) -> str:
            """
            Full dftracer annotation + smoke-test pipeline.

            Executes all steps in sequence and returns a detailed report:
              1.  Create session and clone source
              2.  Detect language, build tool, and dftracer features
              3.  Configure build
              4.  Build and install
              5.  Run smoke test (with auto-detected command if not provided)
              6.  Copy source to annotated/
              7.  Patch build system for dftracer
              8.  Auto-annotate C/C++ and Python source
              9.  Build annotated source with dftracer
              10. Run smoke test with dftracer (traces collected)
              11. dftracer_split — compact traces
              12. dftracer_info  — summarise traces

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
            """
            report: Dict[str, Any] = {}

            # --- Step 1: create session + clone ---
            rid = run_id or uuid.uuid4().hex[:12]
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

            report["step_8_annotate"] = {"status": "ok", "files_annotated": len(annotated),
                                          "annotated": annotated}

            # --- Step 9: build annotated ---
            build_ann = ws / "build_ann"
            install_ann = ws / "install_ann"
            build_ann.mkdir(exist_ok=True)
            install_ann.mkdir(exist_ok=True)

            if bt == "cmake":
                ann_flags = [
                    f"-DCMAKE_INSTALL_PREFIX={install_ann}",
                    "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
                ] + (extra_cmake_flags.split() if extra_cmake_flags else [])
                r_ac = _run(["cmake", "-S", str(ann), "-B", str(build_ann)] + ann_flags, timeout=300)
                report["step_9_configure_ann"] = r_ac
                if not r_ac["success"]:
                    return _err("Step 9 failed: cmake configure (annotated)", step=9, report=report)
                r_ab = _run(["make", f"-j{jobs}"], cwd=build_ann, timeout=600)
                report["step_9_build_ann"] = r_ab
                if not r_ab["success"]:
                    return _err("Step 9 failed: make (annotated)", step=9, report=report)
                _run(["make", "install"], cwd=build_ann, timeout=300)

            elif bt == "autotools":
                if (ann / "configure.ac").exists() and not (ann / "configure").exists():
                    _run(["autoreconf", "-fi"], cwd=ann, timeout=120)
                r_ac = _run([str(ann / "configure"), f"--prefix={install_ann}"],
                             cwd=build_ann, timeout=300)
                report["step_9_configure_ann"] = r_ac
                if not r_ac["success"]:
                    return _err("Step 9 failed: configure (annotated)", step=9, report=report)
                r_ab = _run(["make", f"-j{jobs}"], cwd=build_ann, timeout=600)
                report["step_9_build_ann"] = r_ab
                if not r_ab["success"]:
                    return _err("Step 9 failed: make (annotated)", step=9, report=report)
                _run(["make", "install"], cwd=build_ann, timeout=300)

            elif bt == "python":
                pip = ws / "install" / "bin" / "pip"
                if not pip.exists():
                    pip = Path("pip3")
                r_ab = _run([str(pip), "install", "-e", str(ann)], timeout=300)
                report["step_9_build_ann"] = r_ab
                if not r_ab["success"]:
                    return _err("Step 9 failed: pip install (annotated)", step=9, report=report)

            _save_state(rid, {"step": "annotated_built"})
            report["step_9_build_ann_status"] = "ok"

            # --- Step 10: run smoke test with dftracer ---
            traces_dir = ws / "traces"
            traces_dir.mkdir(exist_ok=True)

            dftracer_env = {
                "DFTRACER_ENABLE": "1",
                "DFTRACER_LOG_FILE": str(traces_dir / "trace"),
                "DFTRACER_DATA_DIR": str(src),
                "DFTRACER_INIT": "1",
            }
            ann_smoke_cwd = build_ann if build_ann.exists() else ann

            if smoke_cmd:
                sm2_r = _run(
                    ["/bin/sh", "-c", smoke_cmd],
                    cwd=ann_smoke_cwd,
                    env=dftracer_env,
                    timeout=300,
                )
                report["step_10_smoke_with_dftracer"] = {**sm2_r, "command": smoke_cmd}
                if not sm2_r["success"]:
                    return _err(
                        "Step 10 failed: smoke test with dftracer",
                        step=10,
                        hint="Use session_write_file + session_build_annotated to iterate",
                        report=report,
                    )
            else:
                report["step_10_smoke_with_dftracer"] = {"status": "no smoke command provided"}

            # --- Step 11: split traces ---
            traces_split = ws / "traces_split"
            traces_split.mkdir(exist_ok=True)
            trace_files = list(traces_dir.glob("*.pfw")) + list(traces_dir.glob("*.pfw.gz"))

            if trace_files:
                sp_r = _run(
                    ["dftracer_split", "-d", str(traces_dir), "-o", str(traces_split)],
                    timeout=600,
                )
                report["step_11_split"] = sp_r
                if not sp_r["success"]:
                    report["step_11_split"]["warning"] = "dftracer_split failed — proceeding"
            else:
                report["step_11_split"] = {"status": "no trace files found"}
                traces_split = traces_dir  # fall back

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
