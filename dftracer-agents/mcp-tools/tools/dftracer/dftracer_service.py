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
from ..session.workspace import _ws, _load_state, _save_state, _ok, _err, _run


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
    two :class:`fastmcp.FastMCP` sub-servers and delegates tool registration to
    specialised helper functions so that each concern (session lifecycle vs.
    pipeline execution) is independently maintainable.

    The service itself does not run a standalone HTTP server; it is mounted into
    the parent MCP gateway through :class:`~mcp_service_factory.MCPServiceFactory`.

    Attributes:
        session_subservice (FastMCP): Sub-server named ``"DFTracerSession"``.
            Hosts the session-lifecycle MCP tools registered by
            :func:`~tools.session.session_tools.register_session_tools`,
            including tools for creating, querying, and terminating dftracer
            annotation sessions.
        pipeline_subservice (FastMCP): Sub-server named ``"DFTracerPipeline"``.
            Hosts both the pipeline-management tools registered by
            :func:`~tools.session.pipeline_tools.register_pipeline_tools` and
            the run-execution tools registered by
            :func:`~tools.session.pipeline_tools.register_run_tools`.  Together
            these cover building annotation pipelines, triggering smoke-test
            runs, and inspecting run results.
        daemon_subservice (FastMCP): Sub-server named ``"DFTracerDaemon"``.
            Hosts the ``session_service_start`` and ``session_service_stop``
            tools registered by :func:`register_daemon_tools`.  These manage
            the ``dftracer_service`` background daemon independently from the
            application run.
    """

    def __init__(self) -> None:
        """Initialise the service and register all MCP tools on the sub-servers.

        Side effects:
            * Creates ``self.session_subservice`` (``FastMCP("DFTracerSession")``)
              and populates it with session-lifecycle tools via
              :func:`~tools.session.session_tools.register_session_tools`.
            * Creates ``self.pipeline_subservice`` (``FastMCP("DFTracerPipeline")``)
              and populates it with pipeline tools via
              :func:`~tools.session.pipeline_tools.register_pipeline_tools` and
              run tools via
              :func:`~tools.session.pipeline_tools.register_run_tools`.
            * Creates ``self.daemon_subservice`` (``FastMCP("DFTracerDaemon")``)
              and populates it with daemon management tools via
              :func:`register_daemon_tools`.

        After ``__init__`` returns all three sub-servers are fully configured
        and ready to be mounted by the parent gateway.
        """
        self.session_subservice = FastMCP("DFTracerSession")
        self.pipeline_subservice = FastMCP("DFTracerPipeline")
        self.daemon_subservice = FastMCP("DFTracerDaemon")

        register_session_tools(self.session_subservice)
        register_pipeline_tools(self.pipeline_subservice)
        register_run_tools(self.pipeline_subservice)
        register_daemon_tools(self.daemon_subservice)

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
