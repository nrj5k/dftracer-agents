"""DFTracer Session Service — core orchestration service for the dftracer MCP integration.

This module defines :class:`DFTracerSessionService`, which acts as the top-level
MCP service responsible for coordinating the full dftracer annotation and
smoke-test workflow.  It does **not** expose tools directly; instead it composes
two :class:`fastmcp.FastMCP` sub-servers whose tools are registered by helper
modules inside the ``session`` package:

* ``session_subservice`` — exposes *session-lifecycle* tools (start, stop,
  status …) registered by :func:`~tools.session.session_tools.register_session_tools`.
* ``pipeline_subservice`` — exposes *pipeline* and *run* tools (build, execute,
  inspect …) registered by
  :func:`~tools.session.pipeline_tools.register_pipeline_tools` and
  :func:`~tools.session.pipeline_tools.register_run_tools`.

External dependency:
    ``dftracer`` — the I/O tracing library whose annotation helpers are driven
    through the session tools registered on the sub-servers.

Typical usage::

    # The module-level side-effect at the bottom registers the singleton
    # automatically with MCPServiceFactory when the module is imported.
    from tools.dftracer import dftracer_service  # noqa: F401 — registers on import
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from ...mcp_service_factory import MCPService, MCPServiceFactory
from ..session.session_tools import register_session_tools
from ..session.pipeline_tools import register_pipeline_tools, register_run_tools


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

        After ``__init__`` returns both sub-servers are fully configured and
        ready to be mounted by the parent gateway.
        """
        self.session_subservice = FastMCP("DFTracerSession")
        self.pipeline_subservice = FastMCP("DFTracerPipeline")

        register_session_tools(self.session_subservice)
        register_pipeline_tools(self.pipeline_subservice)
        register_run_tools(self.pipeline_subservice)

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
