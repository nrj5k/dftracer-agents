"""
DFTracer Session Service — core service class.

Orchestrates the full dftracer annotation + smoke-test workflow.
Tools are registered from the session/ package.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from ...mcp_service_factory import MCPService, MCPServiceFactory
from ..session.session_tools import register_session_tools
from ..session.pipeline_tools import register_pipeline_tools, register_run_tools


class DFTracerSessionService(MCPService):
    """MCP service that orchestrates dftracer annotation + smoke-test sessions."""

    def __init__(self) -> None:
        self.session_subservice = FastMCP("DFTracerSession")
        self.pipeline_subservice = FastMCP("DFTracerPipeline")

        register_session_tools(self.session_subservice)
        register_pipeline_tools(self.pipeline_subservice)
        register_run_tools(self.pipeline_subservice)

    def execute(self, data: dict) -> Optional[str]:
        return "Use session_* tools to orchestrate the dftracer workflow."

    @property
    def name(self) -> str:
        return "dftracer-session"


MCPServiceFactory.register("dftracer-session", DFTracerSessionService())
