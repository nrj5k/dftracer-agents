"""DFTracer Session Service — core orchestration service for the dftracer MCP integration.

This module defines :class:`DFTracerSessionService`, which acts as the top-level
MCP service responsible for coordinating the full dftracer annotation and
smoke-test workflow.  It does **not** expose tools directly; instead it composes
three :class:`fastmcp.FastMCP` sub-servers whose tools are registered by helper
modules inside the ``session`` package:

* ``session_subservice`` — exposes *session-lifecycle* tools (start, stop,
  status …) registered by :func:`~tools.session.session_tools.register_session_tools`.
* ``pipeline_subservice`` — exposes *pipeline* and *run* tools (build, execute,
  inspect …) registered by
  :func:`~tools.session.pipeline_tools.register_pipeline_tools` and
  :func:`~tools.session.pipeline_tools.register_run_tools`.
* ``daemon_subservice`` — exposes ``session_service_start`` and
  ``session_service_stop`` tools registered by :func:`register_daemon_tools`.

External dependency:
    ``dftracer`` — the I/O tracing library whose annotation helpers are driven
    through the session tools registered on the sub-servers.

Typical usage::

    # The module-level side-effect at the bottom registers the singleton
    # automatically with MCPServiceFactory when the module is imported.
    from tools.dftracer import dftracer_service  # noqa: F401 — registers on import
"""
from __future__ import annotations

import shutil
import socket
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

from ...mcp_service_factory import MCPService, MCPServiceFactory
from ..session.session_tools import register_session_tools
from ..session.pipeline_tools import register_pipeline_tools, register_run_tools
from ..session.annotation_clang import register_clang_tools
from ..session.annotation_python import register_python_tools
from ..session.annotation_ai import register_ai_tools
from ..session.workspace import _ws, _load_state, _save_state, _ok, _err, _run


def register_annotation_api_tools(mcp: FastMCP) -> None:
    """Register dftracer annotation API reference tools on *mcp*.

    These four tools return the correct macro names, signatures, and doc URL
    for a given source language so that annotation agents always use the right
    API without having to search the documentation from scratch.
    """

    _DOCS_BASE = "https://dftracer.readthedocs.io/en/latest"
    _PYDOCS_BASE = "https://dftracer.readthedocs.io/projects/pydftracer/en/latest"

    @mcp.tool()
    def dftracer_get_init_fini(language: str) -> str:
        """Return the correct dftracer init/fini macros and API doc URL for a language.

        Provides the exact initialization and finalization macro signatures so the
        annotation agent inserts the right calls in ``main()`` (or the equivalent
        module-level setup) without guessing.

        Args:
            language: ``"c"``, ``"cpp"``, or ``"python"`` (case-insensitive).

        Returns:
            JSON string with keys:
                * ``status``   — ``"ok"`` or ``"error"``.
                * ``language`` — normalized language name.
                * ``init``     — init macro / call signature.
                * ``fini``     — fini macro / call signature.
                * ``notes``    — placement guidance.
                * ``doc_url``  — URL of the relevant API doc page.
        """
        lang = language.strip().lower()
        if lang == "c":
            return _ok(
                "C init/fini macros",
                language="c",
                init="DFTRACER_C_INIT(log_file, data_dirs, process_id)",
                fini="DFTRACER_C_FINI()",
                notes=(
                    "Insert DFTRACER_C_INIT as the FIRST statement in main() "
                    "(or after MPI_Init if MPI is used). "
                    "IMPORTANT: DFTRACER_C_INIT must come BEFORE DFTRACER_C_FUNCTION_START "
                    "when both are present in the same function. "
                    "Insert DFTRACER_C_FINI before MPI_Finalize (if present), "
                    "and DFTRACER_C_FUNCTION_END before every return/exit in main(). "
                    "Pass NULL for unused args: DFTRACER_C_INIT(NULL, NULL, NULL)."
                ),
                doc_url=f"{_DOCS_BASE}/c-api.html",
            )
        if lang in ("cpp", "c++"):
            return _ok(
                "C++ init/fini macros",
                language="cpp",
                init="DFTRACER_CPP_INIT(log_file, data_dirs, process_id)",
                fini="DFTRACER_CPP_FINI()",
                notes=(
                    "Insert DFTRACER_CPP_INIT as the FIRST statement in main() "
                    "(or after MPI_Init). "
                    "IMPORTANT: DFTRACER_CPP_INIT must come BEFORE DFTRACER_CPP_FUNCTION() "
                    "when both are present in the same function. "
                    "Insert DFTRACER_CPP_FINI before MPI_Finalize / before the final return. "
                    "Use DFTRACER_CPP_REGION_START/END to bracket main() body "
                    "instead of DFTRACER_CPP_FUNCTION(). "
                    "Pass nullptr for unused args: DFTRACER_CPP_INIT(nullptr, nullptr, nullptr)."
                ),
                doc_url=f"{_DOCS_BASE}/c-api.html",
            )
        if lang == "python":
            return _ok(
                "Python init/fini API",
                language="python",
                init=(
                    "from dftracer.python import dftracer, dft_fn as DFTracerFn\n"
                    "_dft = DFTracerFn(\"<category>\")\n"
                    "_dft_log = dftracer.initialize_log("
                    "logfile=None, data_dir=None, process_id=None)"
                ),
                fini="_dft_log.finalize()",
                notes=(
                    "dft_fn is a CLASS, not a function — instantiate it with a category string: "
                    "_dft = DFTracerFn(\"mymodule\"). "
                    "Call dftracer.initialize_log() at module top-level (entry-point files only). "
                    "Note: data_dir (singular), not data_dirs. "
                    "Call _dft_log.finalize() before every return in main(), not at module level. "
                    "Use @_dft.log / @_dft.log_init / @_dft.log_static decorators — "
                    "NOT @dft_fn.log. See python_annotate_file tool for automated insertion."
                ),
                doc_url=f"{_PYDOCS_BASE}/api.html",
            )
        return _err(
            f"Unknown language '{language}'. Supported: 'c', 'cpp', 'python'.",
            supported=["c", "cpp", "python"],
        )

    @mcp.tool()
    def dftracer_get_function_annotations(language: str) -> str:
        """Return the correct function-level annotation macros for a language.

        Provides the exact macro (or decorator) to annotate individual functions
        so that dftracer records per-function timing spans.

        Args:
            language: ``"c"``, ``"cpp"``, or ``"python"`` (case-insensitive).

        Returns:
            JSON string with keys:
                * ``status``      — ``"ok"`` or ``"error"``.
                * ``language``    — normalized language name.
                * ``start``       — the opening macro / decorator.
                * ``end``         — the closing macro, or ``null`` if RAII/decorator handles it.
                * ``placement``   — where in the function to place the macros.
                * ``doc_url``     — URL of the relevant API doc page.
        """
        lang = language.strip().lower()
        if lang == "c":
            return _ok(
                "C function annotation macros",
                language="c",
                start="DFTRACER_C_FUNCTION_START()",
                end="DFTRACER_C_FUNCTION_END()",
                placement=(
                    "START: first statement inside the opening brace of every function. "
                    "EXCEPTION: when DFTRACER_C_INIT is also present (in main()), "
                    "DFTRACER_C_INIT must come BEFORE DFTRACER_C_FUNCTION_START. "
                    "END: immediately BEFORE every return / exit() / abort() call. "
                    "For void functions with no explicit return: last statement before '}'."
                ),
                doc_url=f"{_DOCS_BASE}/c-api.html",
            )
        if lang in ("cpp", "c++"):
            return _ok(
                "C++ function annotation macros (RAII)",
                language="cpp",
                start="DFTRACER_CPP_FUNCTION()",
                end=None,
                placement=(
                    "Place DFTRACER_CPP_FUNCTION() as the FIRST statement inside '{'."
                    " EXCEPTION: when DFTRACER_CPP_INIT is also present (in main()), "
                    "DFTRACER_CPP_INIT must come BEFORE DFTRACER_CPP_FUNCTION()."
                    " RAII destructor fires automatically on scope exit — NO manual END needed."
                    " For main(): use DFTRACER_CPP_REGION_START(name) / DFTRACER_CPP_REGION_END(name)"
                    " instead of DFTRACER_CPP_FUNCTION()."
                ),
                doc_url=f"{_DOCS_BASE}/c-api.html",
            )
        if lang == "python":
            return _ok(
                "Python function annotation decorators",
                language="python",
                start="@_dft.log",
                end=None,
                variants={
                    "regular": "@_dft.log",
                    "__init__": "@_dft.log_init",
                    "staticmethod": "@_dft.log_static",
                },
                placement=(
                    "Apply the decorator immediately above every def (or above the first "
                    "existing decorator on that function). "
                    "_dft must be a DFTracerFn instance: _dft = DFTracerFn(\"category\"). "
                    "Use @_dft.log_init for __init__, @_dft.log_static for @staticmethod methods, "
                    "@_dft.log for everything else. "
                    "For @staticmethod, the dftracer decorator goes ABOVE the @staticmethod line. "
                    "No explicit END — the decorator wraps the whole function body."
                ),
                doc_url=f"{_PYDOCS_BASE}/api.html",
            )
        return _err(
            f"Unknown language '{language}'. Supported: 'c', 'cpp', 'python'.",
            supported=["c", "cpp", "python"],
        )

    @mcp.tool()
    def dftracer_get_metadata_api(language: str) -> str:
        """Return the dftracer per-process metadata macro for a language.

        Metadata macros attach key-value pairs to the current process's trace
        records so that traces can be filtered and correlated by application context.

        Args:
            language: ``"c"``, ``"cpp"``, or ``"python"`` (case-insensitive).

        Returns:
            JSON string with keys:
                * ``status``   — ``"ok"`` or ``"error"``.
                * ``language`` — normalized language name.
                * ``macro``    — the metadata macro / call signature.
                * ``notes``    — usage guidance.
                * ``doc_url``  — URL of the relevant API doc page.
        """
        lang = language.strip().lower()
        if lang == "c":
            return _ok(
                "C metadata macro",
                language="c",
                macro='DFTRACER_C_METADATA("key", "value")',
                notes=(
                    "Attach a string key-value pair to the current process trace. "
                    "Call after DFTRACER_C_INIT(). "
                    "Example: DFTRACER_C_METADATA(\"app\", \"IOR\");"
                ),
                doc_url=f"{_DOCS_BASE}/c-api.html",
            )
        if lang in ("cpp", "c++"):
            return _ok(
                "C++ metadata macro",
                language="cpp",
                macro='DFTRACER_CPP_METADATA("key", "value")',
                notes=(
                    "Attach a string key-value pair to the current process trace. "
                    "Call after DFTRACER_CPP_INIT(). "
                    "Example: DFTRACER_CPP_METADATA(\"app\", \"IOR\");"
                ),
                doc_url=f"{_DOCS_BASE}/c-api.html",
            )
        if lang == "python":
            return _ok(
                "Python metadata API",
                language="python",
                macro=None,
                notes=(
                    "The Python dftracer bindings do not expose a separate metadata API. "
                    "Use the dft_fn.log decorator attributes or pass context via function arguments."
                ),
                doc_url=f"{_PYDOCS_BASE}/api.html",
            )
        return _err(
            f"Unknown language '{language}'. Supported: 'c', 'cpp', 'python'.",
            supported=["c", "cpp", "python"],
        )

    @mcp.tool()
    def dftracer_get_function_update_api(language: str) -> str:
        """Return the dftracer per-function metadata update macros for a language.

        Function update macros attach key-value pairs to an *individual function
        span* so that each traced call carries its own context (e.g. ``comp=io``,
        file path, byte count).

        Args:
            language: ``"c"``, ``"cpp"``, or ``"python"`` (case-insensitive).

        Returns:
            JSON string with keys:
                * ``status``     — ``"ok"`` or ``"error"``.
                * ``language``   — normalized language name.
                * ``str_update`` — macro for string values.
                * ``int_update`` — macro for integer values, or ``null`` if unavailable.
                * ``notes``      — usage and placement guidance.
                * ``doc_url``    — URL of the relevant API doc page.
        """
        lang = language.strip().lower()
        if lang == "c":
            return _ok(
                "C function update macros",
                language="c",
                str_update='DFTRACER_C_FUNCTION_UPDATE_STR("key", char_ptr_value)',
                int_update='DFTRACER_C_FUNCTION_UPDATE_INT("key", (int)value)',
                notes=(
                    "Place immediately after DFTRACER_C_FUNCTION_START(). "
                    "comp= is mandatory first UPDATE: "
                    "DFTRACER_C_FUNCTION_UPDATE_STR(\"comp\", \"io\"). "
                    "String params (paths, names): use UPDATE_STR. "
                    "Numeric params (counts, sizes, handles): use UPDATE_INT with (int) cast. "
                    "Opaque handles (MPI_File, hid_t): UPDATE_INT(\"handle\", (int)fh)."
                ),
                doc_url=f"{_DOCS_BASE}/c-api.html",
            )
        if lang in ("cpp", "c++"):
            return _ok(
                "C++ function update macro",
                language="cpp",
                str_update='DFTRACER_CPP_FUNCTION_UPDATE("key", string_value)',
                int_update=None,
                notes=(
                    "Place immediately after DFTRACER_CPP_FUNCTION(). "
                    "comp= is mandatory first UPDATE: "
                    "DFTRACER_CPP_FUNCTION_UPDATE(\"comp\", \"io\"). "
                    "C++ UPDATE accepts string values only (const char *). "
                    "For numeric params: convert to string or omit — "
                    "there is NO UPDATE_INT variant in the C++ API."
                ),
                doc_url=f"{_DOCS_BASE}/c-api.html",
            )
        if lang == "python":
            return _ok(
                "Python function update API",
                language="python",
                str_update=None,
                int_update=None,
                notes=(
                    "The Python dftracer bindings do not expose per-call UPDATE macros. "
                    "Pass context via function arguments; the @dft_fn.log decorator "
                    "records function name and timing automatically."
                ),
                doc_url=f"{_PYDOCS_BASE}/api.html",
            )
        return _err(
            f"Unknown language '{language}'. Supported: 'c', 'cpp', 'python'.",
            supported=["c", "cpp", "python"],
        )


def register_daemon_tools(mcp: FastMCP) -> None:
    """Register ``session_service_start`` and ``session_service_stop`` on *mcp*.

    These tools manage the ``dftracer_service`` background daemon — an independent
    per-node process that captures system-level I/O events in parallel with the
    inline annotation spans produced by the dftracer macros.

    The daemon lifecycle is decoupled from the application run::

        session_service_start(run_id)
        session_run_with_dftracer(run_id, command, data_dir="all")
        session_service_stop(run_id)

    Args:
        mcp: The ``FastMCP`` instance onto which the two tools are registered.
    """

    @mcp.tool()
    def session_service_start(
        run_id: str,
        trace_interval_ms: int = 1000,
        libuv_threads: int = 1,
    ) -> str:
        """Start the ``dftracer_service`` background daemon for this session.

        Locates the ``dftracer_service`` binary (first in the session's
        ``install_ann/bin/``, then via ``PATH``), then starts it as an
        independent per-node daemon with the appropriate ``DFTRACER_*``
        environment variables.

        The daemon writes its own trace files under a prefix separate from the
        application traces produced by ``session_run_with_dftracer``:

        * **Service traces** → ``<workspace>/traces/service_<hostname>.*``
        * **App traces**     → ``<workspace>/traces/<run_id>.*``

        Both trace sets land in the same ``traces/`` directory and are picked up
        by ``session_split_traces``.

        The daemon state directory (``<workspace>/traces/dftracer_service/<hostname>/``)
        is persisted to ``session.json`` so that ``session_service_stop`` can
        locate it without additional arguments.

        Args:
            run_id: Session identifier returned by ``session_create``.
            trace_interval_ms: Polling interval in milliseconds passed to
                ``DFTRACER_TRACE_INTERVAL_MS``.  Defaults to ``1000``.
            libuv_threads: Number of libuv I/O threads passed to
                ``DFTRACER_LIBUV_THREADS``.  Defaults to ``1``.

        Returns:
            JSON string with keys:
                * ``status`` (``"ok"`` or ``"error"``).
                * ``message`` — outcome description including the hostname.
                * ``state_dir`` — absolute path to the daemon state directory.
                * ``log_prefix`` — ``DFTRACER_LOG_FILE`` prefix used by the daemon.
                * ``stdout``, ``stderr``, ``returncode`` — from the ``start`` invocation.

        Raises:
            Returns ``{"status": "error"}`` when:
                * The binary is not found in ``install_ann/bin/`` or ``PATH``.
                * ``dftracer_service start`` exits non-zero.
        """
        ws = _ws(run_id)
        traces_dir = ws / "traces"
        traces_dir.mkdir(exist_ok=True)

        service_bin = ws / "install_ann" / "bin" / "dftracer_service"
        if not service_bin.exists():
            found = shutil.which("dftracer_service")
            if found:
                service_bin = Path(found)
            else:
                return _err(
                    "dftracer_service binary not found — run session_install_dftracer first",
                    searched=[
                        str(ws / "install_ann" / "bin" / "dftracer_service"),
                        "PATH",
                    ],
                )

        node_name = socket.gethostname().split(".")[0]
        state_dir = traces_dir / "dftracer_service" / node_name
        state_dir.mkdir(parents=True, exist_ok=True)
        log_prefix = str(traces_dir / f"service_{node_name}")

        env = {
            "DFTRACER_ENABLE": "1",
            "DFTRACER_LOG_FILE": log_prefix,
            "DFTRACER_TRACE_INTERVAL_MS": str(trace_interval_ms),
            "DFTRACER_LIBUV_THREADS": str(libuv_threads),
        }

        r = _run([str(service_bin), "start", str(state_dir)], env=env, timeout=30)

        _save_state(run_id, {
            "dftracer_service_state_dir": str(state_dir),
            "dftracer_service_log_prefix": log_prefix,
            "dftracer_service_bin": str(service_bin),
            "dftracer_service_running": r["success"],
        })

        if r["success"]:
            return _ok(
                f"dftracer_service started on {node_name}",
                state_dir=str(state_dir),
                log_prefix=log_prefix,
                **r,
            )
        return _err(
            f"dftracer_service start failed on {node_name}",
            state_dir=str(state_dir),
            **r,
        )

    @mcp.tool()
    def session_service_stop(run_id: str) -> str:
        """Stop the ``dftracer_service`` background daemon for this session.

        Reads the daemon state directory from ``session.json`` (written by
        ``session_service_start``) and runs ``dftracer_service stop <state_dir>``.

        A non-zero exit from ``stop`` is treated as a *warning*, not an error,
        because the daemon may have already exited cleanly.  The tool always
        returns ``status: "ok"`` so the pipeline can continue regardless.

        Args:
            run_id: Session identifier returned by ``session_create``.

        Returns:
            JSON string with keys:
                * ``status`` (``"ok"`` or ``"error"``).
                * ``message`` — outcome description.
                * ``state_dir`` — the daemon state directory that was used.
                * ``stdout``, ``stderr``, ``returncode`` — from the ``stop`` invocation.

        Raises:
            Returns ``{"status": "error"}`` when:
                * ``session_service_start`` was never called (no state in ``session.json``).
                * The binary cannot be located at all (neither saved path nor ``PATH``).
        """
        ws = _ws(run_id)
        state = _load_state(run_id)

        state_dir_str = state.get("dftracer_service_state_dir")
        if not state_dir_str:
            return _err(
                "dftracer_service state not found — was session_service_start called?",
                run_id=run_id,
            )

        state_dir = Path(state_dir_str)

        bin_str = state.get("dftracer_service_bin", "")
        service_bin = Path(bin_str) if bin_str and Path(bin_str).exists() else None
        if service_bin is None:
            candidate = ws / "install_ann" / "bin" / "dftracer_service"
            if candidate.exists():
                service_bin = candidate
            else:
                found = shutil.which("dftracer_service")
                if found:
                    service_bin = Path(found)
                else:
                    return _err(
                        "dftracer_service binary not found — cannot stop daemon",
                        state_dir=state_dir_str,
                    )

        r = _run([str(service_bin), "stop", str(state_dir)], timeout=30)
        _save_state(run_id, {"dftracer_service_running": False})

        if r["success"]:
            return _ok("dftracer_service stopped", state_dir=state_dir_str, **r)
        # Non-zero stop is non-fatal; daemon may have already exited cleanly
        return _ok(
            "dftracer_service stop returned non-zero (daemon may have already exited)",
            state_dir=state_dir_str,
            **r,
        )


class DFTracerSessionService(MCPService):
    """MCP service that orchestrates dftracer annotation and smoke-test sessions.

    This service is the central entry point for the dftracer workflow.  It owns
    five :class:`fastmcp.FastMCP` sub-servers and delegates tool registration to
    specialised helper functions so that each concern is independently maintainable.

    The service itself does not run a standalone HTTP server; it is mounted into
    the parent MCP gateway through :class:`~mcp_service_factory.MCPServiceFactory`.

    Attributes:
        session_subservice (FastMCP): Sub-server named ``"DFTracerSession"``.
            Hosts session-lifecycle tools registered by
            :func:`~tools.session.session_tools.register_session_tools`.
        pipeline_subservice (FastMCP): Sub-server named ``"DFTracerPipeline"``.
            Hosts pipeline-management and run-execution tools.
        daemon_subservice (FastMCP): Sub-server named ``"DFTracerDaemon"``.
            Hosts ``session_service_start`` and ``session_service_stop``.
        clang_subservice (FastMCP): Sub-server named ``"DFTracerClang"``.
            Hosts C/C++ tools (``clang_add_braces``, ``clang_extract_functions``,
            ``clang_insert_line``, ``clang_annotate_file``,
            ``clang_write_annotated_file``, ``clang_estimate_function_cost``)
            registered by :func:`~tools.session.annotation_clang.register_clang_tools`,
            Python tools (``python_extract_functions``,
            ``python_annotate_file``, ``python_write_annotated_file``)
            registered by :func:`~tools.session.annotation_python.register_python_tools`,
            and AI/ML tools (``find_source_files``, ``python_annotate_ai_file``,
            ``python_write_ai_file``)
            registered by :func:`~tools.session.annotation_ai.register_ai_tools`.
        annotation_api_subservice (FastMCP): Sub-server named ``"DFTracerAnnotationAPI"``.
            Hosts the four language-aware annotation API reference tools
            (``dftracer_get_init_fini``, ``dftracer_get_function_annotations``,
            ``dftracer_get_metadata_api``, ``dftracer_get_function_update_api``)
            registered by :func:`register_annotation_api_tools`.
    """

    def __init__(self) -> None:
        """Initialise the service and register all MCP tools on the sub-servers."""
        self.session_subservice = FastMCP("DFTracerSession")
        self.pipeline_subservice = FastMCP("DFTracerPipeline")
        self.daemon_subservice = FastMCP("DFTracerDaemon")
        self.clang_subservice = FastMCP("DFTracerClang")
        self.annotation_api_subservice = FastMCP("DFTracerAnnotationAPI")

        register_session_tools(self.session_subservice)
        register_pipeline_tools(self.pipeline_subservice)
        register_run_tools(self.pipeline_subservice)
        register_daemon_tools(self.daemon_subservice)
        register_clang_tools(self.clang_subservice)
        register_python_tools(self.clang_subservice)
        register_ai_tools(self.clang_subservice)
        register_annotation_api_tools(self.annotation_api_subservice)

    def execute(self, data: dict) -> Optional[str]:
        """Legacy compatibility entry-point required by the :class:`MCPService` ABC.

        This method is part of the :class:`~mcp_service_factory.MCPService`
        interface but is intentionally a no-op for this service because all
        meaningful work is performed through the registered MCP tools on the
        sub-servers.  Callers should invoke the ``session_*`` family of tools
        instead.

        Args:
            data (dict): Arbitrary key/value payload forwarded from the MCP
                gateway.  Keys and their types are not validated here; the
                method ignores the payload entirely.

        Returns:
            Optional[str]: A static guidance string directing the caller to use
            the ``session_*`` MCP tools.  Never returns ``None``.
        """
        return "Use session_* tools to orchestrate the dftracer workflow."

    @property
    def name(self) -> str:
        """Unique service identifier used by :class:`MCPServiceFactory`.

        Returns:
            str: The string ``"dftracer-session"``.
        """
        return "dftracer-session"


MCPServiceFactory.register("dftracer-session", DFTracerSessionService())
