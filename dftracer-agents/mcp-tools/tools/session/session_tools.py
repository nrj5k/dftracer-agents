"""
Session step tools — registers all session_* MCP tools onto a FastMCP instance.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP

from .workspace import (
    _ws, _load_state, _save_state, _write_artifact_log,
    _ok, _err, _new_run_id, _create_run, _run, _workspaces_root,
)
from .detection import _detect_info
from .annotation import (
    _annotate_c_source, _annotate_python_source,
    _strip_dftracer_c_macros, _fix_dftracer_annotation_errors,
    _strip_mpi_launcher, _C_INCLUDE, _C_KEYWORDS,
    _generate_annotation_report,
)
from .build import (
    _patch_cmake, _patch_setup_py, _patch_pyproject,
    _patch_autotools_makefile, _find_c_entry_points,
    _find_python_entry_points, _guess_smoke_test,
)
from .install import (
    _install_dftracer_autobuild, _dftracer_utils_split, _install_dftracer_utils,
)


def register_session_tools(mcp: FastMCP) -> None:  # noqa: C901  (long but intentional)

    @mcp.tool()
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
        # _create_run derives app name from the URL, creates workspaces/<app>/<ts>/,
        # and writes .current_run so pipeline_get_run_id can recall this session.
        rid, ws = _create_run(url, run_id)

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
            "url": url,
            "ref": ref,
            "step": "cloned",
        })
        return _ok(
            f"Session {rid} created",
            run_id=rid,
            workspace=str(ws),
            source=str(src),
        )

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
    def session_annotation_report(run_id: str) -> str:
        """
        Show a coverage report of what was annotated versus what was skipped or
        missed, comparing source/ against annotated/ in the session workspace.

        The report is generated by:
          1. Diffing source/ and annotated/ to find relevant (changed) files.
          2. Detecting all C/C++ function definitions in each source file.
          3. Checking which functions carry DFTRACER_C_FUNCTION_START (or
             @dft_fn for Python) in the annotated file.
          4. Cross-referencing annotation_logs/annotation_status.md for the
             recorded status and reason for each function.

        Returns a structured JSON report with:
          - summary:  total files, total functions, annotated / skipped /
                      failed counts, and overall coverage %.
          - files[]:  per-file breakdown — file name, function counts, and
                      per-function status (annotated | skipped | failed |
                      pending | not_annotated) with reason where applicable.

        After reviewing the report, call session_run_pipeline with
        annotation_confirmed=True and the same run_id to continue the
        pipeline from the build-annotated step onward.

        Args:
            run_id: Session identifier returned by session_create or
                    pipeline_create_run.
        """
        ws = _ws(run_id)
        if not ws.exists():
            return _err(f"Workspace not found for run_id: {run_id}")

        rpt = _generate_annotation_report(ws, run_id)

        if "error" in rpt:
            return _err(rpt["error"], run_id=run_id)

        s = rpt["summary"]
        return _ok(
            f"Annotation report: {s['annotated']}/{s['total_functions'] - s['skipped']} "
            f"functions annotated ({s['coverage_pct']}% coverage). "
            f"Review the report, then call session_run_pipeline with "
            f"annotation_confirmed=True and run_id='{run_id}' to continue.",
            **rpt,
        )

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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
