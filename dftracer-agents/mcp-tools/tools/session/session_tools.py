"""Session step tools — registers all ``session_*`` MCP tools onto a FastMCP instance.

This module implements the granular, step-by-step tools that an agent uses to drive
the dftracer annotation pipeline one stage at a time.  Each tool corresponds to a
single, well-defined pipeline step so that the agent can inspect intermediate results,
make decisions, and recover from failures before proceeding.

**MCP tool registration pattern**
    All tools are registered inside :func:`register_session_tools`, which is called
    once at server startup with the shared ``FastMCP`` instance.  Each inner function
    is decorated with ``@mcp.tool()``, which causes FastMCP to introspect its
    signature and docstring and expose it as a callable tool over the MCP protocol.
    Inner functions are *not* importable — they exist only as registered tools.

**Tools exposed to the agent**

    Workspace / file I/O
        * ``session_create``        — clone a Git repo into a timestamped workspace
        * ``session_list_files``    — glob files inside a workspace sub-folder
        * ``session_read_file``     — read a file from any workspace sub-folder
        * ``session_write_file``    — write (create or overwrite) a file in the workspace

    Detection & configuration
        * ``session_detect``        — detect language, build tool, and dftracer features
        * ``session_configure``     — configure the *original* build system

    Build & smoke-test (original source)
        * ``session_build_install`` — compile and install the original project
        * ``session_run_smoke_test``— run a smoke test without dftracer

    Annotation workflow
        * ``session_copy_annotated``    — copy source/ → annotated/ to begin instrumentation
        * ``session_patch_build``       — patch the build system to link dftracer
        * ``session_annotation_report`` — coverage report comparing source/ vs annotated/

    dftracer install & annotated build
        * ``session_install_dftracer``       — build/install dftracer into install_ann/ or venv
        * ``session_autobuild_dftracer``     — low-level autobuild.sh wrapper with mode control
        * ``session_install_dftracer_utils`` — install dftracer-utils from the develop branch
        * ``session_build_annotated``        — build the annotated source with dftracer linked

    Trace collection & analysis
        * ``session_service_start``      — start dftracer_service background daemon (in dftracer_service.py)
        * ``session_run_with_dftracer``  — run a command with DFTRACER_* env vars set (DFTRACER_DATA_DIR=all)
        * ``session_service_stop``       — stop dftracer_service background daemon (in dftracer_service.py)
        * ``session_split_traces``       — compact raw .pfw traces via dftracer-utils split
        * ``session_analyze_traces``     — summarise traces with dftracer_info

    Session management
        * ``session_status``         — inspect the persisted state of a session

**Relationship to the broader pipeline**
    :func:`register_session_tools` provides the *individual* building blocks.
    The orchestrating pipeline (``session_run_pipeline`` in ``pipeline_tools.py``)
    calls these same underlying helpers directly rather than going through MCP, but
    an agent can also call every tool here individually for fine-grained control,
    debugging, or partial re-runs.

    Typical ordered usage::

        session_create → session_detect → session_configure → session_build_install
        → session_run_smoke_test → session_copy_annotated → session_patch_build
        (annotation via goose recipe subagents using session_read_file + session_write_file)
        → session_annotation_report  [confirm coverage]
        → session_install_dftracer → session_build_annotated
        → session_run_with_dftracer → session_split_traces → session_analyze_traces

    Persistent state for each session is stored in
    ``workspaces/<app>/<timestamp>/session.json`` and updated by ``_save_state``
    after every step.
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
    _strip_mpi_launcher,
    _generate_annotation_report,
)
from .build import (
    _patch_cmake, _patch_setup_py, _patch_pyproject,
    _patch_autotools_makefile,
)
from .install import (
    _install_dftracer_autobuild, _dftracer_utils_split, _install_dftracer_utils,
    _find_dftracer_dirs,
)


def register_session_tools(mcp: FastMCP) -> None:  # noqa: C901  (long but intentional)
    """Register all ``session_*`` MCP tools onto *mcp*.

    This function is called once at MCP server startup.  Each nested function
    decorated with ``@mcp.tool()`` becomes a separately callable tool in the
    agent's tool palette.  The nesting pattern gives every inner function access
    to *mcp* via closure without polluting the module namespace.

    Registered tools (in pipeline order):
        ``session_create``, ``session_detect``, ``session_list_files``,
        ``session_read_file``, ``session_write_file``, ``session_configure``,
        ``session_build_install``, ``session_run_smoke_test``,
        ``session_copy_annotated``, ``session_patch_build``,
        ``session_annotation_report``,
        ``session_autobuild_dftracer``, ``session_install_dftracer``,
        ``session_install_dftracer_utils``, ``session_build_annotated``,
        ``session_run_with_dftracer``, ``session_split_traces``,
        ``session_analyze_traces``, ``session_status``.

    Args:
        mcp: The shared ``FastMCP`` server instance onto which tools are
            registered via ``@mcp.tool()`` decorators.
    """

    @mcp.tool()
    def session_create(
        url: str,
        ref: str = "main",
        run_id: Optional[str] = None,
    ) -> str:
        """Clone a Git repository into a new, isolated session workspace.

        Creates a timestamped workspace directory under
        ``workspaces/<app_name>/<YYYYMMDD_HHMMSS>/`` (where *app_name* is
        derived from *url*), clones *url* at *ref* into the ``source/``
        sub-folder, and persists initial session state to ``session.json``.

        A shallow clone (``--depth 1``) is attempted first for speed.  If the
        branch/tag does not exist on the remote the tool retries with a bare
        clone followed by ``git checkout <ref>``.

        Side effects:
            * Creates ``workspaces/<app>/<ts>/source/`` on disk.
            * Writes ``workspaces/<app>/.current_run`` pointer so that
              ``pipeline_get_run_id`` can recall this session.
            * Persists ``{"url", "ref", "step": "cloned"}`` to ``session.json``.

        Args:
            url: Git URL to clone (https or ssh).
            ref: Branch, tag, or commit SHA to checkout (default: ``"main"``).
            run_id: Optional fixed RUN-ID string.  A UUID-based ID is generated
                when omitted.

        Returns:
            JSON string with keys:
                * ``status`` (``"ok"``).
                * ``message`` — human-readable confirmation.
                * ``run_id`` — the session identifier for all subsequent calls.
                * ``workspace`` — absolute path to the session root directory.
                * ``source`` — absolute path to the cloned source tree.

        Raises:
            Returns ``{"status": "error"}`` JSON when both clone attempts fail,
            with ``clone_stderr`` carrying the git error output.

        Note:
            This must be the first tool called in a new annotation session.
            All other ``session_*`` tools require a valid *run_id* produced here.
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
        """Detect the programming language, build tool, and dftracer feature flags.

        Analyses the cloned ``source/`` tree to determine how the project is
        built, which languages it uses, and which optional dftracer features
        (MPI, HDF5, Python bindings) are appropriate.  The detection results
        guide every downstream step — ``session_configure``, ``session_patch_build``,
        and ``session_install_dftracer`` all read the
        ``detection`` key written by this tool.

        Side effects:
            * Persists ``{"detection": <info>, "step": "detected"}`` to
              ``session.json`` via ``_save_state``.
            * Writes an artifact log entry at step 2
              (``<workspace>/annotation_logs/02_session_detect.json``).

        Args:
            run_id: Session identifier returned by ``session_create``.

        Returns:
            JSON string with keys:
                * ``status`` (``"ok"``).
                * ``message`` — ``"Detection complete"``.
                * ``languages`` — list of detected languages (e.g. ``["c", "cpp"]``).
                * ``build_tool`` — one of ``"cmake"``, ``"autotools"``,
                  ``"make"``, ``"python"``, or ``"unknown"``.
                * ``features`` — dict of detected optional features
                  (``{"mpi": bool, "hdf5": bool, "python": bool, ...}``).
                * ``dftracer_cmake_flags`` — recommended ``-D`` flags for cmake
                  derived from autobuild.sh option analysis.
                * Additional keys from ``_detect_info`` (readme excerpt, key files, etc.).

        Raises:
            Returns ``{"status": "error"}`` when ``source/`` does not exist
            (i.e. ``session_create`` has not been run for this *run_id*).

        Note:
            Must be called after ``session_create`` and before ``session_configure``.
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
        """List files inside a workspace sub-folder using a glob pattern.

        Useful for exploring the directory layout of ``source/``, ``annotated/``,
        ``build/``, ``install/``, or any other sub-folder that exists in the
        session workspace.

        Args:
            run_id: Session identifier returned by ``session_create``.
            subfolder: Sub-folder to list relative to the workspace root.
                Common values: ``"source"``, ``"annotated"``, ``"build"``,
                ``"install"``, ``"traces"``.  Defaults to ``"source"``.
            pattern: ``pathlib.Path.glob``-compatible pattern relative to
                *subfolder*.  Defaults to ``"**/*"`` (all files recursively).
            max_results: Maximum number of file paths to return.  Paths are
                returned in filesystem order; results are truncated after this
                count.  Defaults to ``100``.

        Returns:
            JSON string with keys:
                * ``status`` (``"ok"``).
                * ``message`` — ``"<N> files found"``.
                * ``files`` — list of paths relative to *subfolder* (strings).

        Raises:
            Returns ``{"status": "error"}`` when *subfolder* does not exist in
            the session workspace.
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
        """Read a file from the workspace for inspection or annotation.

        The agent typically reads files from ``source/`` to understand the
        original code, and from ``annotated/`` to verify or correct
        instrumentation applied by goose recipe subagents or by hand.

        Args:
            run_id: Session identifier returned by ``session_create``.
            filepath: Path to the file relative to *subfolder* (e.g.
                ``"src/main.c"`` or ``"CMakeLists.txt"``).
            subfolder: Workspace sub-folder containing the file.  Defaults to
                ``"source"``.  Use ``"annotated"`` to read the instrumented copy.
            max_bytes: Maximum number of bytes to return.  Content is truncated
                to this limit; the ``truncated`` field in the response indicates
                whether truncation occurred.  Defaults to ``32768`` (32 KiB).

        Returns:
            JSON string with keys:
                * ``status`` (``"ok"``).
                * ``message`` — ``"File read"``.
                * ``filepath`` — the *filepath* argument as passed.
                * ``subfolder`` — the *subfolder* argument as passed.
                * ``content`` — file text (UTF-8, with replacement characters for
                  undecodable bytes), truncated to *max_bytes*.
                * ``truncated`` — ``true`` if the file was larger than *max_bytes*.

        Raises:
            Returns ``{"status": "error"}`` when the file does not exist.
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
        """Write (create or overwrite) a file inside the workspace.

        The primary use-case is applying LLM-generated dftracer annotations to
        files in the ``annotated/`` sub-folder.  The agent reads a file with
        ``session_read_file``, adds ``DFTRACER_C_FUNCTION_START`` /
        ``DFTRACER_C_FUNCTION_END`` macros (or ``@dft_fn`` for Python), and
        writes the result back with this tool.

        Intermediate directories are created automatically if they do not exist.

        Side effects:
            * Creates or overwrites
              ``<workspace>/<subfolder>/<filepath>`` on disk.

        Args:
            run_id: Session identifier returned by ``session_create``.
            filepath: Destination path relative to *subfolder* (e.g.
                ``"src/main.c"``).  Parent directories are created as needed.
            content: Complete file content to write (UTF-8 string).  The existing
                file, if any, is replaced in full — partial updates are not
                supported.
            subfolder: Workspace sub-folder to write into.  Defaults to
                ``"annotated"``.  Use ``"source"`` with caution as overwriting
                the original makes it impossible to diff changes later.

        Returns:
            JSON string with keys:
                * ``status`` (``"ok"``).
                * ``message`` — ``"Wrote <N> bytes to <subfolder>/<filepath>"``.
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
        """Configure the build system for the *original* cloned source.

        Runs the appropriate configuration command based on the build tool
        detected by ``session_detect`` (or re-detected inline if the detection
        step was skipped):

        * **cmake** — ``cmake -S source -B build -DCMAKE_INSTALL_PREFIX=install
          -DCMAKE_BUILD_TYPE=RelWithDebInfo [extra_cmake_flags]``
        * **autotools** — ``autoreconf -fi`` (if ``configure`` does not exist),
          then ``./configure --prefix=<install> [extra_configure_flags]``
        * **python** — ``python3 -m venv install/`` followed by
          ``pip install -e source/ [extra_pip_flags]``

        Side effects:
            * Creates ``<workspace>/build/`` and ``<workspace>/install/``.
            * For Python projects, creates a virtualenv at ``<workspace>/install/``.
            * Persists ``{"step": "configured", "build_tool": <bt>}`` to
              ``session.json``.
            * Writes an artifact log at step 3.

        Args:
            run_id: Session identifier returned by ``session_create``.
            extra_cmake_flags: Space-separated additional ``-D`` flags appended
                to the cmake invocation (e.g. ``"-DENABLE_TESTS=OFF"``).
                Ignored for non-cmake projects.  Defaults to ``""``.
            extra_configure_flags: Space-separated flags appended to
                ``./configure``.  Ignored for non-autotools projects.
                Defaults to ``""``.
            extra_pip_flags: Space-separated flags appended to
                ``pip install -e``.  Ignored for non-Python projects.
                Defaults to ``""``.

        Returns:
            JSON string with keys:
                * ``status`` (``"ok"`` or ``"error"``).
                * ``message`` — ``"Configure succeeded"`` or error description.
                * ``build_tool`` — detected build system string.
                * ``stdout``, ``stderr``, ``returncode`` — subprocess output.

        Raises:
            Returns ``{"status": "error"}`` for unsupported build tools or when
            the configuration command exits non-zero.

        Note:
            Must be called after ``session_detect`` so that ``session.json``
            contains the ``detection`` key.  If ``session_detect`` was not called,
            detection is re-run inline.
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
            # Clean stale .deps dirs — these cause config.status to fail
            for deps_dir in src.rglob(".deps"):
                if deps_dir.is_dir():
                    shutil.rmtree(deps_dir, ignore_errors=True)
            for deps_dir in build.rglob(".deps"):
                if deps_dir.is_dir():
                    shutil.rmtree(deps_dir, ignore_errors=True)
            # Bootstrap if needed
            if (src / "configure.ac").exists() and not (src / "configure").exists():
                _run(["autoreconf", "-fi"], cwd=src, timeout=120)
            flags = [
                f"--prefix={install}",
                "--disable-dependency-tracking",  # avoids config.status .deps failures
            ] + (extra_configure_flags.split() if extra_configure_flags else [])
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
        """Compile and install the original project after ``session_configure``.

        Runs the appropriate build command based on the ``build_tool`` persisted
        by ``session_configure``:

        * **cmake / autotools / make** — ``make -j<jobs>`` followed by
          ``make install``.  Both commands run in ``<workspace>/build/``.
        * **python** — no-op; installation was already performed by
          ``session_configure`` (``pip install -e``).

        Side effects:
            * Populates ``<workspace>/install/`` with installed binaries/libraries.
            * Persists ``{"step": "installed"}`` to ``session.json``.
            * Writes an artifact log at step 4.

        Args:
            run_id: Session identifier returned by ``session_create``.
            jobs: Number of parallel ``make`` jobs.  Defaults to ``4``.

        Returns:
            JSON string with keys:
                * ``status`` (``"ok"`` or ``"error"``).
                * ``message`` — description of the outcome.
                * For cmake/autotools: ``make`` and ``install`` sub-dicts each
                  containing ``stdout``, ``stderr``, ``returncode``.

        Raises:
            Returns ``{"status": "error"}`` when ``make`` or ``make install``
            exits non-zero, or when the ``build_tool`` stored in state is
            unrecognised (``"unknown"``).

        Note:
            Must be called after ``session_configure``.
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
        """Run a smoke test command inside the workspace as a single process.

        Executes *command* without MPI or any parallel launcher to verify that
        the original build is functional before annotation begins.  Any MPI /
        parallel launcher prefix (``mpirun``, ``mpiexec``, ``srun``, ``jsrun``,
        ``aprun``, ``flux run``) is automatically stripped from *command* so
        the binary runs directly.

        This stripping is intentional: smoke tests must be deterministic and
        reproducible without a cluster scheduler or MPI runtime.  The stripped
        command and a boolean ``mpi_launcher_stripped`` flag are both included
        in the response so the agent can audit what was actually executed.

        Side effects:
            * Persists ``{"last_smoke_test": {command, ...subprocess result}}``
              to ``session.json``.
            * Writes an artifact log at step 5.

        Args:
            run_id: Session identifier returned by ``session_create``.
            command: Shell command to execute (passed to ``/bin/sh -c``).
                MPI launchers are stripped automatically — pass the original
                command unchanged; this tool will sanitise it.
            subfolder: Working-directory sub-folder relative to the workspace
                root.  Defaults to ``"build"``.  Falls back to ``"source"``
                if *subfolder* does not exist.
            env_extra: Optional JSON object string (``'{"VAR": "val"}'``) of
                additional environment variables merged into the subprocess
                environment.  Defaults to ``None`` (no extra variables).
            timeout: Seconds before the subprocess is killed.
                Defaults to ``300``.

        Returns:
            JSON string with keys:
                * ``status`` (``"ok"`` or ``"error"``).
                * ``message`` — ``"Smoke test passed"`` or ``"Smoke test failed"``.
                * ``command_run`` — the sanitised command that was actually executed.
                * ``mpi_launcher_stripped`` — ``true`` if a launcher prefix was removed.
                * ``stdout``, ``stderr``, ``returncode`` — subprocess output.

        Raises:
            Returns ``{"status": "error"}`` when the command exits non-zero.

        Note:
            Must be called after ``session_build_install``.  A failure here does
            not block the annotation phase — ``session_run_pipeline`` treats a
            failed smoke test as a warning and continues.
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
        """Copy the original source tree to ``annotated/`` ready for instrumentation.

        Performs a full recursive copy of ``<workspace>/source/`` to
        ``<workspace>/annotated/``.  If ``annotated/`` already exists (e.g.
        from a previous attempt) it is deleted first so the copy starts clean.

        The agent subsequently uses ``session_read_file`` /
        ``session_write_file`` on *subfolder* ``"annotated"`` to apply
        dftracer instrumentation macros without touching the pristine
        ``source/`` tree.

        Side effects:
            * Removes any pre-existing ``<workspace>/annotated/`` directory.
            * Creates a fresh ``<workspace>/annotated/`` that is an exact
              copy of ``<workspace>/source/``.
            * Persists ``{"step": "annotated_copy_created"}`` to
              ``session.json``.

        Args:
            run_id: Session identifier returned by ``session_create``.

        Returns:
            JSON string with keys:
                * ``status`` (``"ok"``).
                * ``message`` — ``"Copied source to <path>"``.

        Raises:
            Returns ``{"status": "error"}`` when ``source/`` does not exist.

        Note:
            Must be called after ``session_create``.  Typically called after
            ``session_run_smoke_test`` to confirm the original build is working
            before beginning instrumentation.
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
        """Patch the build system in ``annotated/`` to link dftracer.

        Modifies the build files inside ``<workspace>/annotated/`` so that the
        project is compiled and linked against the dftracer library.  The exact
        changes depend on the build tool detected by ``session_detect``:

        * **cmake** — injects ``find_package(dftracer REQUIRED)`` and
          ``target_link_libraries(... dftracer::dftracer)`` into
          ``CMakeLists.txt`` (root and one level of sub-projects).
        * **autotools** — prepends hardcoded dftracer include/lib flags to
          ``Makefile.am`` / ``Makefile.in`` / ``Makefile`` files found in
          ``annotated/``, and appends ``-ldftracer_core`` to the ``LIBS``
          assignment in ``.in`` and generated ``Makefile`` files so the library
          is linked *after* object files (required for correct symbol resolution).
        * **python** — adds ``"dftracer"`` to ``install_requires`` in
          ``setup.py`` and/or the ``dependencies`` table in
          ``pyproject.toml``.

        Side effects:
            * Overwrites one or more build files inside
              ``<workspace>/annotated/`` in place.
            * Persists ``{"step": "build_patched"}`` to ``session.json``.

        Args:
            run_id: Session identifier returned by ``session_create``.

        Returns:
            JSON string with keys:
                * ``status`` (``"ok"``).
                * ``message`` — ``"Patched <N> build file(s)"``.
                * ``patched`` — list of build file paths (relative to
                  ``annotated/``) that were modified.
                * ``build_tool`` — the detected build system string.

        Raises:
            Returns ``{"status": "error"}`` when ``annotated/`` does not exist.

        Note:
            Must be called after ``session_copy_annotated``.
        """
        ws = _ws(run_id)
        ann = ws / "annotated"
        if not ann.exists():
            return _err("annotated/ not found — run session_copy_annotated first")

        patched: List[str] = []
        state = _load_state(run_id)
        info = state.get("detection") or _detect_info(ws / "source")
        bt = info.get("build_tool", "unknown")

        # Paths saved by session_install_dftracer; rediscover if not yet set.
        pip_inc = state.get("dftracer_pip_include_dir", "")
        pip_lib = state.get("dftracer_pip_lib_dir", "")
        if not pip_inc:
            cmake_prefix = ws / "install_ann"
            dirs = _find_dftracer_dirs(cmake_prefix=cmake_prefix if cmake_prefix.exists() else None)
            if dirs:
                pip_inc = dirs.get("include_dir", "")
                pip_lib = dirs.get("lib_dir", "")

        if bt == "cmake":
            cml = ann / "CMakeLists.txt"
            if cml.exists():
                cml.write_text(_patch_cmake(cml, pip_inc, pip_lib))
                patched.append("CMakeLists.txt")
            # Recurse one level for sub-projects
            for sub in ann.iterdir():
                if sub.is_dir():
                    scml = sub / "CMakeLists.txt"
                    if scml.exists():
                        scml.write_text(_patch_cmake(scml, pip_inc, pip_lib))
                        patched.append(str(scml.relative_to(ann)))
            # Also check annotated/src/ for deeper sub-trees
            for src_sub in ann.rglob("CMakeLists.txt"):
                rel = str(src_sub.relative_to(ann))
                if rel == "CMakeLists.txt" or rel in patched:
                    continue
                src_sub.write_text(_patch_cmake(src_sub, pip_inc, pip_lib))
                patched.append(rel)

        elif bt in ("autotools", "make"):
            # Patch all Makefiles found under annotated/ (including src/)
            for mf in ann.rglob("Makefile*"):
                new_content = _patch_autotools_makefile(mf, pip_inc, pip_lib)
                if new_content != mf.read_text():
                    mf.write_text(new_content)
                    patched.append(str(mf.relative_to(ann)))

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
        return _ok(
            f"Patched {len(patched)} build file(s)",
            patched=patched,
            build_tool=bt,
            pip_include_dir=pip_inc or "(none)",
            pip_lib_dir=pip_lib or "(none)",
        )

    @mcp.tool()
    def session_annotation_report(run_id: str) -> str:
        """Show a coverage report comparing ``source/`` against ``annotated/``.

        Generates a structured report that lets the agent (and user) verify
        annotation completeness before committing to the annotated build.  The
        report is produced in four steps:

        1. Diff ``source/`` and ``annotated/`` to find files that were changed.
        2. Detect all C/C++ function definitions in each relevant source file
           using a regex scanner.
        3. Check which functions carry ``DFTRACER_C_FUNCTION_START`` (C/C++) or
           ``@dft_fn`` (Python) in the annotated copy.
        4. Cross-reference ``annotation_logs/annotation_status.md`` for any
           recorded per-function status and reason.

        The response message also instructs the agent to call
        ``session_run_pipeline`` with ``annotation_confirmed=True`` and the
        same *run_id* once the coverage is satisfactory.

        Side effects:
            None — this is a read-only inspection tool.

        Args:
            run_id: Session identifier returned by ``session_create`` or
                ``pipeline_create_run``.

        Returns:
            JSON string with keys:
                * ``status`` (``"ok"`` or ``"error"``).
                * ``message`` — summary of annotated/total functions and
                  a prompt to confirm and continue the pipeline.
                * ``summary`` — dict with aggregate metrics:
                    * ``total_files`` — number of source files examined.
                    * ``relevant_files`` — files with at least one annotation
                      change.
                    * ``total_functions`` — total function definitions found.
                    * ``annotated`` — functions with dftracer markers present.
                    * ``skipped`` — functions explicitly recorded as skipped.
                    * ``failed`` — functions where annotation was attempted but
                      produced errors.
                    * ``coverage_pct`` — ``annotated / (total - skipped) * 100``.
                * ``files`` — list of per-file dicts, each containing:
                    * ``file`` — path relative to ``annotated/``.
                    * ``total_functions``, ``annotated``, ``skipped``, ``failed``,
                      ``pending``, ``not_annotated`` — per-file counts.
                    * ``functions`` — list of per-function status dicts with
                      ``name``, ``status`` (``annotated`` | ``skipped`` |
                      ``failed`` | ``pending`` | ``not_annotated``), and
                      ``reason`` where applicable.

        Raises:
            Returns ``{"status": "error"}`` when the workspace does not exist,
            or when ``_generate_annotation_report`` encounters an internal error
            (e.g. neither ``source/`` nor ``annotated/`` is present).

        Note:
            Call after manual annotation is complete.  To continue the pipeline
            after reviewing this report, call ``session_run_pipeline`` with
            ``annotation_confirmed=True`` and ``run_id=<run_id>``.
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
        """Clone dftracer and build/install it via its own ``autobuild.sh`` script.

        This is the low-level build tool underlying ``session_install_dftracer``.
        Call it directly when fine-grained control is needed — for example, to
        force cmake mode on a Python project, to change the installed ref, or
        to diagnose a failed ``session_install_dftracer`` call.

        The dftracer source is cloned once and cached at
        ``<workspace>/dftracer_src/``; subsequent calls skip the clone step.
        Build artefacts land in ``<workspace>/dftracer_build/``.

        ``autobuild.sh`` flags are derived from the features detected in the
        project source:

        * ``--enable-mpi``     — added when MPI is detected in the project.
        * ``--enable-hdf5``    — added when HDF5 is detected in the project.
        * ``--python <exe>``   — added for Python projects (pip mode) or when
          the project has Python bindings (cmake mode).

        *install_mode* choices:

        * ``"cmake"`` — builds and installs the full C/C++ library and headers
          into ``<workspace>/install_ann/``.  Required for C/C++ projects so
          that ``find_package(dftracer)`` resolves in the annotated cmake build.
        * ``"pip"`` — installs the Python package into the project venv at
          ``<workspace>/install/``.  Used for Python-only projects.
        * ``"auto"`` — selects ``"cmake"`` for cmake/autotools/make projects
          and ``"pip"`` for Python projects.

        After a successful build, ``dftracer-utils`` is also installed from the
        ``develop`` branch so that ``dftracer_split`` is available for trace
        compaction.

        Side effects:
            * Clones dftracer source into ``<workspace>/dftracer_src/`` (first
              call only).
            * Builds dftracer in ``<workspace>/dftracer_build/``.
            * Installs dftracer headers/libs into *install_prefix*.
            * Persists ``{"dftracer_install_prefix": str(install_prefix)}`` to
              ``session.json``.
            * Installs ``dftracer-utils`` from the ``develop`` branch into the
              resolved pip environment.

        Args:
            run_id: Session identifier returned by ``session_create``.
            dftracer_ref: Git tag or branch of dftracer to build.
                Defaults to ``"v2.0.3"``.
            install_mode: One of ``"cmake"``, ``"pip"``, or ``"auto"``.
                Defaults to ``"auto"``.
            jobs: Parallel build jobs passed to ``make``.  Defaults to ``4``.

        Returns:
            JSON string with keys:
                * ``status`` (``"ok"`` or ``"error"``).
                * ``message`` — outcome description.
                * ``ref`` — the dftracer ref that was built.
                * ``prefix`` — absolute path of the install prefix used.
                * ``dftracer_utils_installed`` — ``true`` if dftracer-utils
                  was successfully installed.
                * ``steps`` — list of step dicts (name, success, stdout, stderr)
                  from autobuild.sh execution.

        Raises:
            Returns ``{"status": "error"}`` when ``autobuild.sh`` exits
            non-zero, with ``steps`` detailing which stage failed.

        Note:
            Prefer ``session_install_dftracer`` for the normal workflow.  Use
            this tool only when the default mode selection or install location
            need to be overridden.
        """
        ws = _ws(run_id)
        state = _load_state(run_id)
        info = state.get("detection") or _detect_info(ws / "source")
        bt = info.get("build_tool", "unknown")
        features = info.get("features", {})

        # "auto" picks cmake for C/C++ projects, pip for Python projects.
        if install_mode == "auto":
            install_mode = "pip" if bt == "python" else "cmake"

        install_prefix = ws / "install_ann"
        install_prefix.mkdir(exist_ok=True)
        python_exe = sys.executable

        result = _install_dftracer_autobuild(
            ws=ws,
            install_prefix=install_prefix,
            dftracer_ref=dftracer_ref,
            jobs=jobs,
            install_mode=install_mode,
            features=features,
            python_exe=python_exe,
            cmake_flags=info.get("dftracer_cmake_flags", []),
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
        """Install dftracer into the session's annotated install directory.

        Selects the correct install mode based on the build tool detected by
        ``session_detect`` and invokes ``_install_dftracer_autobuild`` which
        clones the dftracer repository and runs its ``autobuild.sh`` script:

        * **C/C++ projects (cmake / autotools / make)** — clones
          ``https://github.com/llnl/dftracer.git`` at *dftracer_ref*, builds
          with cmake, and installs into ``<workspace>/install_ann/``.  The
          install prefix is stored in session state so that
          ``session_build_annotated`` automatically passes
          ``CMAKE_PREFIX_PATH`` / ``pkg-config`` flags.

        * **Python projects** — installs dftracer via pip into the project
          venv at ``<workspace>/install/``.  Tries PyPI first; falls back to
          the git source build.

        Side effects:
            * Populates ``<workspace>/install_ann/`` (C/C++) or
              ``<workspace>/install/`` (Python) with dftracer headers, libs,
              and/or the Python package.
            * Persists ``{"dftracer_install_prefix": str(<prefix>)}`` to
              ``session.json``.

        Args:
            run_id: Session identifier returned by ``session_create``.
            dftracer_ref: Git tag or branch of dftracer to install.
                Defaults to ``"v2.0.3"``.
            jobs: Parallel make jobs for the cmake build.  Defaults to ``4``.

        Returns:
            JSON string with keys:
                * ``status`` (``"ok"`` or ``"error"``).
                * ``message`` — outcome description including install mode.
                * ``prefix`` — absolute path to the install directory (C/C++).
                * ``ref`` — the dftracer ref that was installed.
                * ``steps`` — list of step dicts from autobuild.sh.

        Raises:
            Returns ``{"status": "error"}`` when ``autobuild.sh`` fails or
            when the build tool is not one of the supported values
            (``"cmake"``, ``"autotools"``, ``"make"``, ``"python"``).

        Note:
            Must be called after annotation is complete (goose recipe subagents
            or manual ``session_read_file`` + ``session_write_file``) and before
            ``session_build_annotated``.
        """
        ws = _ws(run_id)
        state = _load_state(run_id)
        info = state.get("detection") or _detect_info(ws / "source")
        bt = info.get("build_tool", "unknown")

        # autotools/cmake/make → cmake mode (builds libdftracer_core.so + headers)
        # python → pip mode (installs Python package; C library not needed)
        install_mode = "pip" if bt == "python" else "cmake"

        install_dir = ws / "install_ann"
        install_dir.mkdir(exist_ok=True)
        result = _install_dftracer_autobuild(
            ws=ws,
            install_prefix=install_dir,
            dftracer_ref=dftracer_ref,
            jobs=jobs,
            install_mode=install_mode,
            features=info.get("features", {}),
            python_exe=sys.executable,
            cmake_flags=info.get("dftracer_cmake_flags", []),
        )
        if not result["success"]:
            return _err(
                f"dftracer autobuild ({install_mode} mode) failed",
                ref=dftracer_ref,
                steps=result["steps"],
            )

        if install_mode == "pip":
            _save_state(run_id, {"dftracer_install_prefix": str(install_dir)})
            return _ok(
                "dftracer installed via pip (python project)",
                ref=dftracer_ref,
                steps=result["steps"],
            )

        # Locate include/lib dirs — check cmake prefix first, then site-packages.
        dirs = _find_dftracer_dirs(
            python_exe=sys.executable,
            cmake_prefix=install_dir,
        ) or {}
        _save_state(run_id, {
            "dftracer_install_prefix":  str(install_dir),
            "dftracer_pip_include_dir": dirs.get("include_dir", ""),
            "dftracer_pip_lib_dir":     dirs.get("lib_dir", ""),
        })
        return _ok(
            f"dftracer installed via autobuild.sh (cmake mode, build_tool={bt})",
            ref=dftracer_ref,
            include_dir=dirs.get("include_dir", "(not found)"),
            lib_dir=dirs.get("lib_dir", "(not found)"),
            lib_name=dirs.get("lib_name", "libdftracer_core.so"),
            steps=result["steps"],
        )

    @mcp.tool()
    def session_install_dftracer_utils(
        run_id: str,
    ) -> str:
        """Install ``dftracer-utils`` from the ``develop`` branch into the session environment.

        ``dftracer-utils`` provides the ``dftracer_split`` binary consumed by
        ``session_split_traces`` to compact raw ``.pfw`` trace files, as well as
        ``dftracer_info``, ``dftracer_merge``, and other trace analysis tools.

        The package is installed using ``pip install --upgrade`` so that the
        latest snapshot from the ``develop`` branch is always fetched, regardless
        of what PyPI currently has.

        Pip resolution order:

        1. ``<workspace>/install/bin/pip`` — the session virtualenv's pip.
        2. ``<server_python_dir>/pip`` — the pip adjacent to the MCP server's
           own Python interpreter.
        3. ``pip3`` — system fallback.

        Side effects:
            * Installs ``dftracer-utils`` (develop snapshot) into the resolved
              pip environment.
            * Persists ``{"dftracer_utils_installed": bool}`` to
              ``session.json``.

        Args:
            run_id: Session identifier (used only for workspace path resolution
                and state tracking; does not affect the install target).

        Returns:
            JSON string with keys:
                * ``status`` (``"ok"`` or ``"error"``).
                * ``message`` — ``"dftracer-utils installed from develop"`` or
                  ``"dftracer-utils install failed"``.
                * Additional keys from the pip subprocess result
                  (``stdout``, ``stderr``, ``returncode``).

        Raises:
            Returns ``{"status": "error"}`` when pip exits non-zero.

        Note:
            Call once per session before ``session_split_traces`` if you want to
            guarantee the ``develop``-branch version of ``dftracer-utils`` is
            active.  The ``session_autobuild_dftracer`` tool also installs
            ``dftracer-utils`` automatically after a successful build, so this
            tool is only needed when ``session_autobuild_dftracer`` was not used.
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
        """Configure and build the annotated source with dftracer linked.

        Mirrors the configure → build → install sequence of
        ``session_configure`` + ``session_build_install`` but targets the
        ``annotated/`` source tree and uses separate output directories
        (``build_ann/`` and ``install_ann/``) to preserve the original build
        for comparison.

        If ``session_install_dftracer`` was called first, the dftracer install
        prefix recorded in session state is automatically injected:

        * **cmake** — ``-DCMAKE_PREFIX_PATH=<prefix>`` appended to cmake flags.
        * **autotools** — ``PKG_CONFIG_PATH``, ``CPPFLAGS``, and ``LDFLAGS``
          environment variables set to point at ``<prefix>/lib/pkgconfig``,
          ``<prefix>/include``, and ``<prefix>/lib`` respectively.
        * **python** — dftracer is already in the venv; no extra flags needed.

        Side effects:
            * Creates ``<workspace>/build_ann/`` and ``<workspace>/install_ann/``.
            * For cmake/autotools: runs configure, ``make -j<jobs>``, and
              ``make install`` inside ``build_ann/``.
            * For Python: runs ``pip install -e annotated/``.
            * Persists ``{"step": "annotated_built"}`` to ``session.json``.

        Args:
            run_id: Session identifier returned by ``session_create``.
            jobs: Parallel make jobs.  Defaults to ``4``.
            extra_cmake_flags: Space-separated additional ``-D`` flags appended
                to the cmake invocation.  Defaults to ``""``.

        Returns:
            JSON string with keys:
                * ``status`` (``"ok"`` or ``"error"``).
                * ``message`` — ``"Annotated build succeeded"`` or error
                  description.
                * ``build_tool`` — the detected build system string.
                * ``steps`` — dict of step results keyed by stage name
                  (``"configure"``, ``"build"``, ``"install"``, or
                  ``"pip_install"``), each a subprocess result dict.

        Raises:
            Returns ``{"status": "error"}`` when any build stage exits
            non-zero, or when ``annotated/`` does not exist.

        Note:
            Must be called after ``session_install_dftracer``.
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
            # Clean stale .deps dirs — these cause config.status to fail
            for deps_dir in ann.rglob(".deps"):
                if deps_dir.is_dir():
                    shutil.rmtree(deps_dir, ignore_errors=True)
            for deps_dir in build_ann.rglob(".deps"):
                if deps_dir.is_dir():
                    shutil.rmtree(deps_dir, ignore_errors=True)
            if (ann / "configure.ac").exists() and not (ann / "configure").exists():
                _run(["autoreconf", "-fi"], cwd=ann, timeout=120)

            env: Dict[str, str] = {}
            if dft_prefix:
                # Prefer the .pc file path saved by session_generate_dftracer_pc;
                # fall back to the cmake prefix's conventional pkgconfig dir.
                pc_path = state.get("dftracer_pkg_config_path", "")
                if not pc_path:
                    pc_path = f"{dft_prefix}/lib/pkgconfig"
                env["PKG_CONFIG_PATH"] = pc_path
                env["CPPFLAGS"] = f"-I{dft_prefix}/include"
                env["LDFLAGS"]  = f"-L{dft_prefix}/lib -Wl,-rpath,{dft_prefix}/lib"

            r_cfg = _run(
                [str(ann / "configure"), f"--prefix={install_ann}",
                 "--disable-dependency-tracking"],
                cwd=build_ann,
                env=env if env else None,
                timeout=300,
            )
            steps["configure"] = r_cfg
            if not r_cfg["success"]:
                return _err("configure failed for annotated source", **r_cfg)

            r_bld = _run(
                ["make", f"-j{jobs}"],
                cwd=build_ann, env=env if env else None, timeout=600,
            )
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
        data_dir: str = "all",
        timeout: int = 600,
        env_extra: Optional[str] = None,
    ) -> str:
        """Run a command with dftracer environment variables set to capture traces.

        Executes *command* inside the workspace with all ``DFTRACER_*`` env
        vars configured per the
        `dftracer documentation <https://dftracer.readthedocs.io/en/latest/api.html>`_.
        Trace files (``<run_id>.<pid>.pfw``) are written to
        ``<workspace>/traces/`` and consumed by ``session_split_traces``.

        The following variables are always set:

        * ``DFTRACER_ENABLE=1``         — activates tracing.
        * ``DFTRACER_INC_METADATA=1``   — records process/thread metadata.
        * ``DFTRACER_LOG_FILE=<workspace>/traces/<run_id>`` — trace file
          prefix; dftracer appends ``.<pid>.pfw``.
        * ``DFTRACER_DATA_DIR=all``     — captures I/O on *any* file path.
          Pass an explicit path via *data_dir* only to restrict monitoring
          to a subtree.

        ``DFTRACER_INIT`` is intentionally **not** set here.  Pass it via
        *env_extra* only when the annotated source has no explicit
        ``DFTRACER_C_INIT`` / ``DFTRACER_CPP_INIT`` calls.

        Additional variables can be merged/overridden via *env_extra*.

        Side effects:
            * Creates ``<workspace>/traces/`` if it does not exist.
            * Writes one or more ``<run_id>.<pid>.pfw`` trace files inside
              ``<workspace>/traces/``.
            * Persists ``{"step": "ran_with_dftracer", "dftracer_run": {...}}``
              to ``session.json``.
            * Writes an artifact log at step 11.

        Args:
            run_id: Session identifier returned by ``session_create``.
            command: Shell command to run (via ``/bin/sh -c``).  The command
                should invoke the annotated binary installed in ``install_ann/``
                or reachable via the venv in ``install/``.
            subfolder: Working-directory sub-folder relative to the workspace
                root.  Defaults to ``"build_ann"``.  Falls back to ``"build"``
                then ``"source"`` if the requested subfolder does not exist.
            data_dir: Value passed to ``DFTRACER_DATA_DIR``.  Defaults to
                ``"all"`` which captures I/O on any file path.  Pass an
                absolute directory path to restrict monitoring to a subtree.
            timeout: Seconds before the subprocess is killed.
                Defaults to ``600``.
            env_extra: Optional JSON object string (``'{"VAR": "val"}'``) of
                additional environment variables merged into the dftracer env,
                allowing overrides of any ``DFTRACER_*`` variable or addition
                of project-specific variables.  Defaults to ``None``.

        Returns:
            JSON string with keys:
                * ``status`` (``"ok"`` or ``"error"``).
                * ``message`` — ``"Command completed with dftracer"`` or
                  ``"Command failed with dftracer"``.
                * ``traces_dir`` — absolute path to the traces directory.
                * ``stdout``, ``stderr``, ``returncode`` — subprocess output.

        Raises:
            Returns ``{"status": "error"}`` when the command exits non-zero.

        Note:
            Must be called after ``session_build_annotated``.  Surround with
            ``session_service_start`` / ``session_service_stop`` to also
            capture system-level daemon traces alongside inline annotation spans.
            Follow with ``session_split_traces`` to compact the raw ``.pfw``
            files.
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
            "DFTRACER_DATA_DIR": data_dir,
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
        """Compact raw dftracer traces via the ``dftracer-utils`` split service.

        Reads raw ``.pfw`` / ``.pfw.gz`` files from ``<workspace>/traces/``
        (written by ``session_run_with_dftracer``) and writes compacted chunk
        files to ``<workspace>/traces_split/``.

        Splitting is performed by ``DftracerUtilsService.split`` so that all
        ``dftracer-utils`` error handling and output formatting is applied.  If
        the service cannot be loaded the tool falls back to calling the
        ``dftracer_split`` binary directly.

        Side effects:
            * Creates ``<workspace>/traces_split/`` if it does not exist.
            * Writes compacted trace chunks named ``<app_name>_*.pfw`` inside
              ``<workspace>/traces_split/``.
            * Persists ``{"step": "traces_split", "split_result": <r>}`` to
              ``session.json``.
            * Writes an artifact log at step 12.

        Args:
            run_id: Session identifier returned by ``session_create``.
            app_name: Prefix used for output chunk file names.
                Defaults to ``"app"``.

        Returns:
            JSON string with keys:
                * ``status`` (``"ok"`` or ``"error"``).
                * ``message`` — outcome description.
                * ``output`` — absolute path to ``traces_split/`` directory.
                * Additional keys from the split service result.

        Raises:
            Returns ``{"status": "error"}`` when:
                * ``traces/`` does not exist in the session workspace.
                * No ``.pfw`` or ``.pfw.gz`` files are found in ``traces/``.
                * The split service/binary exits non-zero.

        Note:
            Must be called after ``session_run_with_dftracer``.  Call
            ``session_install_dftracer_utils`` first to ensure the
            ``develop``-branch version of ``dftracer-utils`` is active.
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
        """Summarise dftracer traces using ``dftracer_info`` (dfanalyzer).

        Invokes ``dftracer_info`` against the compacted trace directory to
        produce a human-readable summary of I/O behaviour — function call
        counts, time spent in I/O, per-file breakdowns, and metadata such as
        process counts.  The index directory is created automatically to cache
        the parsed trace data for faster subsequent queries.

        Side effects:
            * Creates *index_dir* (or ``<traces_subdir>/idx/``) if it does not
              exist.
            * Persists ``{"step": "traces_analyzed", "analysis_result": <r>}``
              to ``session.json``.
            * Writes an artifact log at step 13.

        Args:
            run_id: Session identifier returned by ``session_create``.
            trace_subdir: Sub-folder (relative to workspace root) containing
                compacted traces to analyse.  Defaults to ``"traces_split"``.
            query_type: Value passed to ``dftracer_info --query``.  Common
                values: ``"summary"`` (default), ``"function"``, ``"file"``.
            index_dir: Absolute path to the dftracer_info index directory.
                Defaults to ``<workspace>/<trace_subdir>/idx/``.
            extra_flags: Additional space-separated flags appended to the
                ``dftracer_info`` invocation.  Defaults to ``""``.

        Returns:
            JSON string with keys:
                * ``status`` (``"ok"`` or ``"error"``).
                * ``message`` — ``"Analysis complete"`` or error description.
                * ``stdout`` — raw ``dftracer_info`` output.
                * ``stderr``, ``returncode`` — subprocess result.

        Raises:
            Returns ``{"status": "error"}`` when *trace_subdir* does not exist
            or when ``dftracer_info`` exits non-zero.

        Note:
            Must be called after ``session_split_traces``.  This is the final
            step of the annotation pipeline; its output is the primary artifact
            for evaluating dftracer coverage.
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
        """Return the current persisted state of a session.

        Reads ``<workspace>/session.json`` and lists all sub-directories that
        exist in the workspace root.  Useful for understanding how far a
        session has progressed, what build tool was detected, and which
        workspace sub-folders are present (``source``, ``annotated``,
        ``build``, ``install``, ``traces``, etc.).

        Side effects:
            None — this is a read-only inspection tool.

        Args:
            run_id: Session identifier returned by ``session_create`` or
                ``pipeline_create_run``.

        Returns:
            JSON string with keys:
                * ``status`` (``"ok"``).
                * ``message`` — ``"Session status"``.
                * ``workspace`` — absolute path to the session workspace.
                * ``subdirs`` — list of sub-directory names present in the
                  workspace root.
                * All keys stored in ``session.json`` (e.g. ``step``,
                  ``url``, ``ref``, ``build_tool``, ``detection``,
                  ``dftracer_install_prefix``, etc.).

        Raises:
            Returns ``{"status": "error"}`` when no workspace directory exists
            for *run_id*.
        """
        ws = _ws(run_id)
        if not ws.exists():
            return _err(f"Session {run_id} not found")
        state = _load_state(run_id)
        subdirs = [d.name for d in ws.iterdir() if d.is_dir()]
        # Drop keys that we pass explicitly to avoid duplicate-keyword errors
        extra = {k: v for k, v in state.items() if k not in {"workspace"}}
        return _ok("Session status", workspace=str(ws), subdirs=subdirs, **extra)

    @mcp.tool()
    def session_collect_system_info(run_id: str) -> str:
        """Collect a system configuration snapshot for the current node.

        Gathers CPU, memory, network, and filesystem information from the
        running host and saves the result to
        ``<workspace>/system_config.json``.  This snapshot is typically
        captured immediately after the dftracer trace run so that analysis
        tools can correlate I/O behaviour with the hardware environment in
        which the benchmark was executed.

        Information collected:

        **CPU**
            Architecture, model name, socket/core/thread counts, min/max
            frequency (MHz), NUMA topology, L1/L2/L3 cache sizes.
            Source: ``lscpu --json`` with ``/proc/cpuinfo`` as fallback.

        **Memory**
            Total, available, and swap capacity; buffer and cache sizes.
            Source: ``/proc/meminfo``.

        **Network**
            Per-interface name, link type, MTU, operational state, MAC
            address, and (where ``ethtool`` is available) negotiated speed
            and duplex.
            Source: ``ip -j link show`` with ``/proc/net/dev`` as fallback.

        **Filesystems**
            All mounted filesystems: device, type, total/used/available
            capacity, use percentage, and mount point.
            Source: ``df -Th``.

        **Host / OS**
            Fully-qualified hostname, kernel release, and ``/etc/os-release``
            fields (``NAME``, ``VERSION``, ``ID``, etc.).

        Missing commands (e.g. ``ethtool``, ``lscpu``) are silently skipped
        rather than failing the tool — every section degrades independently.

        Side effects:
            * Writes ``<workspace>/system_config.json``.
            * Persists ``{"step": "system_info_collected", "system_config":
              "<path>"}`` to ``session.json``.
            * Writes an artifact log at step 14.

        Args:
            run_id: Session identifier returned by ``session_create``.

        Returns:
            JSON string with keys:
                * ``status`` (``"ok"`` or ``"error"``).
                * ``message`` — ``"System configuration collected"``.
                * ``config_file`` — absolute path to ``system_config.json``.
                * ``hostname`` — fully-qualified hostname.
                * ``cpu`` — CPU info dict.
                * ``memory_total`` — human-readable total RAM string.
                * ``network_interfaces`` — count of network interfaces found.
                * ``filesystem_mounts`` — count of mounted filesystems found.

        Raises:
            Returns ``{"status": "error"}`` when the session workspace does
            not exist.
        """
        ws = _ws(run_id)
        if not ws.exists():
            return _err(f"Session {run_id} not found")

        def _human(kb: int) -> str:
            n = kb * 1024
            for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
                if n < 1024:
                    return f"{n:.1f} {unit}"
                n //= 1024
            return f"{n:.1f} PiB"

        def _cmd(args: List[str], t: int = 10) -> str:
            r = _run(args, timeout=t)
            return r["stdout"] if r["success"] else ""

        # ── CPU ──────────────────────────────────────────────────────────
        cpu: Dict[str, Any] = {}
        lscpu_raw = _cmd(["lscpu", "--json"])
        if lscpu_raw:
            try:
                fields = {
                    item["field"].rstrip(":"): item["data"]
                    for item in json.loads(lscpu_raw).get("lscpu", [])
                }
                cpu = {
                    "architecture":    fields.get("Architecture"),
                    "model_name":      fields.get("Model name"),
                    "vendor":          fields.get("Vendor ID"),
                    "sockets":         fields.get("Socket(s)"),
                    "cores_per_socket":fields.get("Core(s) per socket"),
                    "threads_per_core":fields.get("Thread(s) per core"),
                    "logical_cpus":    fields.get("CPU(s)"),
                    "min_mhz":         fields.get("CPU min MHz"),
                    "max_mhz":         fields.get("CPU max MHz"),
                    "numa_nodes":      fields.get("NUMA node(s)"),
                    "l1d_cache":       fields.get("L1d cache"),
                    "l1i_cache":       fields.get("L1i cache"),
                    "l2_cache":        fields.get("L2 cache"),
                    "l3_cache":        fields.get("L3 cache"),
                }
            except Exception:
                pass
        if not cpu:
            cpuinfo = _cmd(["cat", "/proc/cpuinfo"])
            models = [l.split(":", 1)[1].strip() for l in cpuinfo.splitlines()
                      if l.startswith("model name")]
            mhz = [l.split(":", 1)[1].strip() for l in cpuinfo.splitlines()
                   if l.startswith("cpu MHz")]
            cpu = {
                "model_name":   models[0] if models else None,
                "logical_cpus": str(len(models)),
                "mhz_samples":  mhz[:8],
            }

        # ── Memory ───────────────────────────────────────────────────────
        memory: Dict[str, Any] = {}
        for line in _cmd(["cat", "/proc/meminfo"]).splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            key = parts[0].rstrip(":")
            try:
                val_kb = int(parts[1])
            except ValueError:
                continue
            if key == "MemTotal":
                memory["total_kb"] = val_kb
                memory["total"] = _human(val_kb)
            elif key == "MemAvailable":
                memory["available_kb"] = val_kb
                memory["available"] = _human(val_kb)
            elif key == "MemFree":
                memory["free_kb"] = val_kb
            elif key == "Buffers":
                memory["buffers_kb"] = val_kb
            elif key == "Cached":
                memory["cached_kb"] = val_kb
            elif key == "SwapTotal":
                memory["swap_total_kb"] = val_kb
                memory["swap_total"] = _human(val_kb)
            elif key == "SwapFree":
                memory["swap_free_kb"] = val_kb

        # ── Network ──────────────────────────────────────────────────────
        interfaces: List[Dict[str, Any]] = []
        link_json = _cmd(["ip", "-j", "link", "show"])
        if link_json:
            try:
                for iface in json.loads(link_json):
                    entry: Dict[str, Any] = {
                        "name":  iface.get("ifname"),
                        "type":  iface.get("link_type"),
                        "flags": iface.get("flags", []),
                        "mtu":   iface.get("mtu"),
                        "state": iface.get("operstate"),
                        "mac":   iface.get("address"),
                    }
                    # Optional: ethtool for speed/duplex (may be missing or require root)
                    eth = _cmd(["ethtool", iface.get("ifname", "")], t=5)
                    for eth_line in eth.splitlines():
                        if "Speed:" in eth_line:
                            entry["speed"] = eth_line.split(":", 1)[1].strip()
                        elif "Duplex:" in eth_line:
                            entry["duplex"] = eth_line.split(":", 1)[1].strip()
                        elif "Port:" in eth_line:
                            entry["port_type"] = eth_line.split(":", 1)[1].strip()
                    interfaces.append(entry)
            except Exception:
                pass
        if not interfaces:
            for line in _cmd(["cat", "/proc/net/dev"]).splitlines()[2:]:
                if ":" in line:
                    interfaces.append({"name": line.split(":")[0].strip()})

        # ── Filesystems ───────────────────────────────────────────────────
        mounts: List[Dict[str, Any]] = []
        for line in _cmd(["df", "-Th"]).splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 7:
                mounts.append({
                    "filesystem": parts[0],
                    "type":       parts[1],
                    "size":       parts[2],
                    "used":       parts[3],
                    "avail":      parts[4],
                    "use_pct":    parts[5],
                    "mount":      parts[6],
                })

        # ── Host / OS ────────────────────────────────────────────────────
        hostname = _cmd(["hostname", "-f"]).strip() or _cmd(["hostname"]).strip()
        kernel   = _cmd(["uname", "-r"]).strip()
        os_release: Dict[str, str] = {}
        for line in _cmd(["cat", "/etc/os-release"]).splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                os_release[k] = v.strip('"')

        info: Dict[str, Any] = {
            "hostname":    hostname,
            "kernel":      kernel,
            "os_release":  os_release,
            "cpu":         cpu,
            "memory":      memory,
            "network":     {"interfaces": interfaces},
            "filesystems": {"mounts": mounts},
        }

        config_path = ws / "system_config.json"
        config_path.write_text(json.dumps(info, indent=2))

        _save_state(run_id, {
            "step": "system_info_collected",
            "system_config": str(config_path),
        })
        _write_artifact_log(ws, 14, "session_collect_system_info", {
            "hostname":    hostname,
            "cpu_model":   cpu.get("model_name"),
            "memory_total":memory.get("total"),
            "interfaces":  len(interfaces),
            "mounts":      len(mounts),
        }, run_id)

        return _ok(
            "System configuration collected",
            config_file=str(config_path),
            hostname=hostname,
            cpu=cpu,
            memory_total=memory.get("total"),
            network_interfaces=len(interfaces),
            filesystem_mounts=len(mounts),
        )

    @mcp.tool()
    def session_diagnose_bottlenecks(
        run_id: str,
        analyzer_preset: str = "posix",
        view_types: Optional[str] = "time_range",
        metric_boundaries: Optional[str] = None,
        timeout: int = 600,
    ) -> str:
        """Diagnose I/O bottlenecks by running DFAnalyzer + DFDiagnoser on session traces.

        Two-phase pipeline:

        **Phase 1 — DFAnalyzer checkpoint**
            Runs ``dfanalyzer`` with ``analyzer.checkpoint=True`` on the split
            traces in ``<workspace>/traces_split/``, writing checkpoint files
            (``_flat_view_*.parquet``, ``_raw_stats_*.json``) to
            ``<workspace>/dfanalyzer_checkpoint/``.

        **Phase 2 — DFDiagnoser**
            Loads the checkpoint and scores every metric against severity
            thresholds (trivial → critical).  Scored views are written to
            ``<workspace>/diagnosis/scored/`` and a bottleneck summary is
            saved to ``<workspace>/diagnosis.json``.

        Severity levels (DFDiagnoser convention):
            * ``trivial`` — metric below 25 % of threshold
            * ``low``     — 25–50 %
            * ``medium``  — 50–75 %
            * ``high``    — 75–90 %  ← surfaces as a bottleneck
            * ``critical``— above 90 % ← surfaces as a bottleneck

        Side effects:
            * Creates ``<workspace>/dfanalyzer_checkpoint/``.
            * Creates ``<workspace>/diagnosis/scored/``.
            * Writes ``<workspace>/diagnosis.json`` with the bottleneck summary.
            * Persists ``{"step": "bottlenecks_diagnosed", ...}`` to ``session.json``.
            * Writes an artifact log at step 15.

        Args:
            run_id: Session identifier returned by ``session_create``.
            analyzer_preset: DFAnalyzer preset to use.  ``"posix"`` covers
                POSIX file I/O; ``"dlio"`` covers deep-learning I/O workloads.
                Defaults to ``"posix"``.
            view_types: Comma-separated DFAnalyzer view type(s) to generate.
                Defaults to ``"time_range"``.  Use ``"file_name,time_range"``
                for per-file breakdowns.
            metric_boundaries: Optional JSON object string mapping metric names
                to hardware peak values for bandwidth/IOPS normalisation
                (e.g. ``'{"bw_mean": 10000000000}'``).  Passed through to
                DFDiagnoser.  Defaults to ``None``.
            timeout: Seconds before each subprocess phase is killed.
                Defaults to ``600``.

        Returns:
            JSON string with keys:
                * ``status`` (``"ok"`` or ``"error"``).
                * ``message`` — outcome description.
                * ``diagnosis_file`` — path to ``diagnosis.json``.
                * ``checkpoint_dir`` — dfanalyzer checkpoint directory.
                * ``severity_counts`` — per-severity metric observation counts.
                * ``bottlenecks`` — list of high/critical findings (up to 50),
                  each with ``metric``, ``severity``, ``scope``, ``view``,
                  ``description``, and raw ``value``.
                * ``phases`` — dict with ``dfanalyzer`` and ``dfdiagnoser``
                  subprocess result dicts for debugging.

        Raises:
            Returns ``{"status": "error"}`` when:
                * ``traces_split/`` does not exist (run ``session_split_traces``
                  first).
                * dfanalyzer fails to produce checkpoint files.
                * DFDiagnoser is not installed and no ``dfdiagnoser`` binary
                  is found in ``PATH``.
        """
        ws = _ws(run_id)
        traces_split = ws / "traces_split"
        if not traces_split.exists():
            return _err(
                "traces_split/ not found — run session_split_traces first",
                run_id=run_id,
            )

        checkpoint_dir = ws / "dfanalyzer_checkpoint"
        diagnosis_dir  = ws / "diagnosis"
        scored_dir     = diagnosis_dir / "scored"
        checkpoint_dir.mkdir(exist_ok=True)
        diagnosis_dir.mkdir(exist_ok=True)
        scored_dir.mkdir(exist_ok=True)

        phases: Dict[str, Any] = {}

        # ── Phase 1: dfanalyzer with checkpoint ──────────────────────────
        vt_list = [v.strip() for v in (view_types or "time_range").split(",") if v.strip()]
        vt_str  = "[" + ",".join(vt_list) + "]"
        dfanalyzer_cmd = [
            "dfanalyzer",
            f"trace_path={traces_split}",
            "analyzer.checkpoint=True",
            f"analyzer.checkpoint_dir={checkpoint_dir}",
            f"analyzer/preset={analyzer_preset}",
            f"view_types={vt_str}",
        ]
        ana_r = _run(dfanalyzer_cmd, timeout=timeout)
        phases["dfanalyzer"] = ana_r
        if not ana_r["success"]:
            return _err(
                f"dfanalyzer failed (exit {ana_r['returncode']})",
                phases=phases,
                hint="Ensure dfanalyzer is installed: pip install dfanalyzer-utils",
                stderr=ana_r["stderr"],
            )

        flat_views = list(checkpoint_dir.glob("_flat_view_*.parquet"))
        if not flat_views:
            return _err(
                f"dfanalyzer ran but produced no _flat_view_*.parquet in {checkpoint_dir}",
                phases=phases,
                dfanalyzer_stdout=ana_r["stdout"],
            )

        # ── Phase 2: dfdiagnoser ─────────────────────────────────────────
        # Try Python API first, fall back to CLI.
        boundaries = json.loads(metric_boundaries) if metric_boundaries else {}
        diag_r: Optional[Dict[str, Any]] = None
        try:
            from dfdiagnoser.diagnoser import Diagnoser   # type: ignore
            from dfdiagnoser.output import FileOutput     # type: ignore
            diagnoser = Diagnoser()
            result = diagnoser.diagnose_checkpoint(
                checkpoint_dir=str(checkpoint_dir),
                metric_boundaries=boundaries,
            )
            FileOutput(output_dir=str(scored_dir), output_format="json").handle_result(result)
            diag_r = {
                "returncode": 0,
                "stdout": f"Scored {len(result.scored_flat_views)} view(s) via Python API",
                "stderr": "",
                "success": True,
            }
        except ImportError:
            # CLI fallback
            cli_cmd = [
                "dfdiagnoser",
                "input=checkpoint",
                f"input.checkpoint_dir={checkpoint_dir}",
                "output=file",
                f"output.output_dir={scored_dir}",
                "output.output_format=json",
            ]
            diag_r = _run(cli_cmd, timeout=timeout)
            if not diag_r["success"] and "not found" in diag_r.get("stderr", "").lower():
                diag_r["stderr"] += " — install with: pip install dfdiagnoser"
        except Exception as exc:
            diag_r = {"returncode": -1, "stdout": "", "stderr": str(exc), "success": False}

        phases["dfdiagnoser"] = diag_r

        # ── Parse scored outputs and build bottleneck summary ─────────────
        # Import helper functions from dfdiagnoser_service if available;
        # otherwise use inline equivalents.
        severity_counts: Dict[str, int] = {
            "critical": 0, "high": 0, "medium": 0, "low": 0, "trivial": 0
        }
        score_labels = {1: "trivial", 2: "low", 3: "medium", 4: "high", 5: "critical"}
        bottlenecks: List[Dict[str, Any]] = []

        for scored_path in sorted(scored_dir.glob("*_scored.json")):
            try:
                with open(scored_path) as f:
                    rows = json.load(f)
                view_name = scored_path.stem
                for row_key, row in (rows.items() if isinstance(rows, dict) else []):
                    for col, val in row.items():
                        if not col.endswith("_score") or val is None:
                            continue
                        metric = col[:-6]  # strip "_score"
                        try:
                            score = int(val)
                        except (TypeError, ValueError):
                            continue
                        label = score_labels.get(score, "unknown")
                        if label in severity_counts:
                            severity_counts[label] += 1
                        if score >= 4:
                            bottlenecks.append({
                                "view":        view_name,
                                "scope":       str(row_key),
                                "metric":      metric,
                                "score":       score,
                                "severity":    label,
                                "value":       row.get(metric),
                            })
            except Exception:
                pass

        bottlenecks.sort(key=lambda x: x["score"], reverse=True)

        # ── Load raw stats for context ────────────────────────────────────
        raw_stats: Optional[Dict[str, Any]] = None
        for p in checkpoint_dir.glob("_raw_stats_*.json"):
            try:
                with open(p) as f:
                    raw_stats = json.load(f)
                break
            except Exception:
                pass

        # ── Persist summary ───────────────────────────────────────────────
        total_issues = sum(severity_counts.values())
        critical_high = severity_counts["critical"] + severity_counts["high"]
        summary = {
            "run_id":          run_id,
            "checkpoint_dir":  str(checkpoint_dir),
            "diagnosis_dir":   str(diagnosis_dir),
            "severity_counts": severity_counts,
            "bottlenecks":     bottlenecks[:50],
            "raw_stats":       raw_stats,
            "phases":          phases,
        }
        diagnosis_file = ws / "diagnosis.json"
        diagnosis_file.write_text(json.dumps(summary, indent=2))

        _save_state(run_id, {
            "step":             "bottlenecks_diagnosed",
            "diagnosis_file":   str(diagnosis_file),
            "checkpoint_dir":   str(checkpoint_dir),
            "severity_counts":  severity_counts,
        })
        _write_artifact_log(ws, 15, "session_diagnose_bottlenecks", {
            "total_metrics_scored": total_issues,
            "high_critical":        critical_high,
            "severity_counts":      severity_counts,
        }, run_id)

        msg = (
            f"Bottleneck diagnosis complete: {total_issues} metric observations, "
            f"{critical_high} high/critical issue(s) identified."
        )
        if not diag_r.get("success") and not bottlenecks:
            msg = f"DFDiagnoser did not run successfully: {diag_r.get('stderr', '')}"

        return _ok(
            msg,
            diagnosis_file=str(diagnosis_file),
            checkpoint_dir=str(checkpoint_dir),
            severity_counts=severity_counts,
            bottlenecks=bottlenecks[:50],
            phases=phases,
        )

    @mcp.tool()
    def session_search_optimization_papers(
        run_id: str,
        max_results_per_topic: int = 3,
        extra_query: Optional[str] = None,
    ) -> str:
        """Search arXiv for optimization papers relevant to the diagnosed bottlenecks.

        Reads ``<workspace>/diagnosis.json`` (produced by
        ``session_diagnose_bottlenecks``) and maps each high/critical bottleneck
        metric to a targeted arXiv search query.  Results are saved as
        ``<workspace>/optimization_papers.json`` and returned as a structured
        summary for the agent to interpret.

        Metric → query mapping examples:

        * ``small_io``   → "small I/O aggregation buffering optimization HPC"
        * ``rand``       → "random access sequential I/O prefetching optimization"
        * ``read_time``  → "parallel I/O read throughput optimization filesystem"
        * ``write_time`` → "parallel I/O write throughput checkpoint optimization"
        * ``metadata``   → "metadata operation overhead reduction parallel filesystem"

        Args:
            run_id: Session identifier returned by ``session_create``.
            max_results_per_topic: Papers to fetch per unique bottleneck topic
                (1-10, default 3).
            extra_query: Optional additional search terms appended to every query
                (e.g. the application name or storage system name).

        Returns:
            JSON with keys:

            * ``status``         — ``"ok"`` or ``"error"``.
            * ``topics_searched``— list of search queries issued.
            * ``papers``         — flat list of unique papers (deduplicated by title),
              each with ``title``, ``authors``, ``published``, ``abstract`` (truncated),
              ``url``, and ``topic``.
            * ``papers_file``    — path to the saved ``optimization_papers.json``.
        """
        ws = _ws(run_id)
        diagnosis_file = ws / "diagnosis.json"
        if not diagnosis_file.exists():
            return _err(
                "diagnosis.json not found — run session_diagnose_bottlenecks first",
                run_id=run_id,
            )

        try:
            diagnosis = json.loads(diagnosis_file.read_text())
        except Exception as exc:
            return _err(f"Could not read diagnosis.json: {exc}", run_id=run_id)

        bottlenecks: List[Dict[str, Any]] = diagnosis.get("bottlenecks", [])

        # Map metric name fragments to human-readable search queries
        _METRIC_QUERIES: Dict[str, str] = {
            "small_io":        "small I/O aggregation buffering optimization HPC parallel filesystem",
            "small_read":      "small read aggregation optimization parallel I/O",
            "small_write":     "small write buffering optimization parallel I/O",
            "rand":            "random I/O access pattern optimization sequential prefetching HPC",
            "seq":             "sequential I/O access pattern fragmentation optimization",
            "read_time":       "parallel I/O read throughput optimization high performance computing",
            "write_time":      "parallel I/O write throughput checkpoint optimization",
            "metadata":        "metadata operation overhead reduction parallel filesystem POSIX",
            "fetch_pressure":  "data loader prefetching pipeline deep learning I/O optimization",
            "epoch_straggler": "stragglers load imbalance distributed training I/O optimization",
            "checkpoint":      "checkpoint I/O optimization deep learning distributed training",
            "intensity":       "I/O intensity compute I/O overlap optimization",
            "imbalance":       "I/O load imbalance optimization distributed HPC",
            "bw":              "bandwidth utilization optimization parallel I/O filesystem",
        }

        # Collect unique topics from high/critical bottlenecks
        seen_topics: Dict[str, str] = {}  # query → representative metric name
        for bn in bottlenecks:
            metric = bn.get("metric", "")
            for fragment, query in _METRIC_QUERIES.items():
                if fragment in metric and query not in seen_topics:
                    seen_topics[query] = metric
                    break

        # If no bottlenecks mapped, fall back to a general I/O performance query
        if not seen_topics:
            seen_topics["I/O performance optimization parallel filesystem HPC"] = "general"

        if extra_query:
            seen_topics = {f"{q} {extra_query}": m for q, m in seen_topics.items()}

        # Search arXiv for each topic (synchronous wrapper around async HTTP)
        try:
            import httpx as _httpx  # noqa: F401 — presence check
            import xml.etree.ElementTree as _ET
            import urllib.parse

            _ARXIV = "https://export.arxiv.org/api/query"
            _NS    = {"atom": "http://www.w3.org/2005/Atom",
                      "arxiv": "http://arxiv.org/schemas/atom"}

            def _fetch_arxiv(query: str, n: int) -> List[Dict[str, Any]]:
                params = {
                    "search_query": f"all:{query}",
                    "max_results":  n,
                    "sortBy":       "relevance",
                    "sortOrder":    "descending",
                }
                qs = urllib.parse.urlencode(params)
                url = f"{_ARXIV}?{qs}"
                r = _run(["curl", "-s", "--max-time", "30", url], timeout=45)
                if not r["success"] or not r["stdout"]:
                    return []
                try:
                    root = _ET.fromstring(r["stdout"])
                    papers = []
                    for entry in root.findall("atom:entry", _NS):
                        def _t(tag):
                            el = entry.find(tag, _NS)
                            return el.text.strip() if el is not None and el.text else ""
                        arxiv_id = _t("atom:id").split("/abs/")[-1]
                        authors  = [
                            a.find("atom:name", _NS).text.strip()
                            for a in entry.findall("atom:author", _NS)
                            if a.find("atom:name", _NS) is not None
                        ]
                        papers.append({
                            "title":     _t("atom:title").replace("\n", " "),
                            "authors":   authors,
                            "published": _t("atom:published")[:10],
                            "abstract":  _t("atom:summary").replace("\n", " ")[:400],
                            "url":       f"https://arxiv.org/abs/{arxiv_id}",
                            "pdf_url":   f"https://arxiv.org/pdf/{arxiv_id}",
                        })
                    return papers
                except Exception:
                    return []

            max_results_per_topic = max(1, min(10, max_results_per_topic))
            all_papers: List[Dict[str, Any]] = []
            seen_titles: set = set()
            topics_searched: List[str] = []

            for query, metric in seen_topics.items():
                topics_searched.append(query)
                for p in _fetch_arxiv(query, max_results_per_topic):
                    title_key = p["title"].lower()[:80]
                    if title_key not in seen_titles:
                        seen_titles.add(title_key)
                        all_papers.append({**p, "topic": metric})

        except Exception as exc:
            return _err(f"Paper search failed: {exc}", run_id=run_id)

        result = {
            "run_id":          run_id,
            "topics_searched": topics_searched,
            "papers":          all_papers,
        }
        papers_file = ws / "optimization_papers.json"
        papers_file.write_text(json.dumps(result, indent=2))
        result["papers_file"] = str(papers_file)

        _write_artifact_log(ws, 16, "session_search_optimization_papers", {
            "topics":       len(topics_searched),
            "papers_found": len(all_papers),
        }, run_id)

        return _ok(
            f"Found {len(all_papers)} unique optimization papers across "
            f"{len(topics_searched)} bottleneck topic(s).",
            papers_file=str(papers_file),
            topics_searched=topics_searched,
            papers=all_papers,
        )

    @mcp.tool()
    def session_generate_dftracer_pc(run_id: str) -> str:
        """Generate a pkg-config ``.pc`` file for dftracer and save it under install_ann/.

        dftracer is a CMake-only project and does not always install a
        ``dftracer.pc`` file.  This tool locates ``libdftracer_core.so`` —
        first in ``<workspace>/install_ann/lib[64]/``, then in the pip-installed
        site-packages layout — and writes a conforming ``dftracer.pc`` to
        ``<workspace>/install_ann/lib/pkgconfig/dftracer.pc``.

        After this tool runs, ``pkg-config --cflags dftracer`` and
        ``pkg-config --libs dftracer`` work correctly when
        ``PKG_CONFIG_PATH`` includes the returned *pkg_config_path*.
        ``session_build_annotated`` sets ``PKG_CONFIG_PATH`` automatically, so
        the typical call order for autotools projects is::

            session_install_dftracer → session_generate_dftracer_pc
              → session_patch_build → session_build_annotated

        Side effects:
            * Creates ``<workspace>/install_ann/lib/pkgconfig/`` if absent.
            * Writes ``dftracer.pc`` (overwriting any existing file).
            * Persists ``{"dftracer_pc_file": …, "dftracer_pkg_config_path": …}``
              to ``session.json``.

        Args:
            run_id: Session identifier returned by ``session_create``.

        Returns:
            JSON string with keys:
                * ``status``           — ``"ok"`` or ``"error"``.
                * ``message``          — path of the written file.
                * ``pc_file``          — absolute path to the generated ``.pc``.
                * ``pkg_config_path``  — directory to add to ``PKG_CONFIG_PATH``.
                * ``lib_dir``          — directory containing ``libdftracer_core.so``.
                * ``include_dir``      — directory containing ``dftracer/dftracer.h``.
        """
        ws = _ws(run_id)
        install_ann = ws / "install_ann"

        dirs = _find_dftracer_dirs(
            python_exe=sys.executable,
            cmake_prefix=install_ann if install_ann.exists() else None,
        )
        if not dirs:
            return _err(
                "libdftracer_core.so not found in install_ann/ or site-packages. "
                "Run session_install_dftracer first."
            )

        lib_dir     = Path(dirs["lib_dir"])
        include_dir = Path(dirs["include_dir"])
        prefix      = lib_dir.parent   # <prefix>/lib → <prefix>

        # Determine version from soname if possible
        version = "2.0.3"
        for f in lib_dir.glob("libdftracer_core.so.*"):
            parts = f.name.split(".")
            # libdftracer_core.so.4.1.0 → "4.1.0"
            if len(parts) >= 4:
                version = ".".join(parts[3:])
                break

        pc_content = (
            f"prefix={prefix}\n"
            f"exec_prefix=${{prefix}}\n"
            f"libdir=${{exec_prefix}}/lib\n"
            f"includedir=${{prefix}}/include\n"
            f"\n"
            f"Name: dftracer\n"
            f"Description: DFTracer I/O tracing library\n"
            f"Version: {version}\n"
            f"Libs: -L${{libdir}} -ldftracer_core -Wl,-rpath,${{libdir}}\n"
            f"Cflags: -I${{includedir}}\n"
        )

        pc_dir  = install_ann / "lib" / "pkgconfig"
        pc_dir.mkdir(parents=True, exist_ok=True)
        pc_file = pc_dir / "dftracer.pc"
        pc_file.write_text(pc_content)

        _save_state(run_id, {
            "dftracer_pc_file":          str(pc_file),
            "dftracer_pkg_config_path":  str(pc_dir),
        })
        return _ok(
            f"Generated {pc_file}",
            pc_file=str(pc_file),
            pkg_config_path=str(pc_dir),
            lib_dir=str(lib_dir),
            include_dir=str(include_dir),
            version=version,
            hint=(
                f"Export: PKG_CONFIG_PATH={pc_dir}:$PKG_CONFIG_PATH "
                f"before calling ./configure or make"
            ),
        )
