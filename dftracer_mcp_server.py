#!/usr/bin/env python3
"""
DFTracer MCP Server — stdio entry point for Goose and other MCP clients.

Exposes dftracer_utils and dfanalyzer tools over the Model Context Protocol
using a stdio transport (stdin/stdout).  Goose (and any other MCP-compatible
agent) can launch this process directly.

Usage:
    dftracer-mcp-server                 # both services (default)
    dftracer-mcp-server --service utils
    dftracer-mcp-server --service analyzer
    dftracer-mcp-server --service both

Goose config (~/.config/goose/config.yaml):
    extensions:
      dftracer:
        type: stdio
        cmd: dftracer-mcp-server       # if installed via pip install -e .
        args: []
        enabled: true

    # — or — point directly at this file if not installed:
    extensions:
      dftracer:
        type: stdio
        cmd: /path/to/venv/bin/python
        args: [/path/to/dftracer-agents/dftracer_mcp_server.py]
        enabled: true
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
import types
from pathlib import Path

from fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Package context bootstrap (avoids needing a full pip install for dev use)
# ---------------------------------------------------------------------------

def _bootstrap_package_context() -> None:
    """Inject synthetic package stubs so relative imports in service files work."""
    if "dftracer_agents" in sys.modules:
        return

    tools_dir = REPO_ROOT / "dftracer-agents" / "mcp-tools" / "tools"

    pkg = types.ModuleType("dftracer_agents")
    pkg.__path__ = [str(REPO_ROOT / "dftracer-agents")]

    mcp_pkg = types.ModuleType("dftracer_agents.mcp_tools")
    mcp_pkg.__path__ = [str(REPO_ROOT / "dftracer-agents" / "mcp-tools")]

    tools_pkg = types.ModuleType("dftracer_agents.mcp_tools.tools")
    tools_pkg.__path__ = [str(tools_dir)]

    # Real MCPServiceFactory from disk
    factory_path = REPO_ROOT / "dftracer-agents" / "mcp-tools" / "mcp_service_factory.py"
    factory_mod = types.ModuleType("dftracer_agents.mcp_service_factory")
    spec = importlib.util.spec_from_file_location(
        "dftracer_agents.mcp_service_factory", factory_path
    )
    factory_mod = importlib.util.module_from_spec(spec)
    sys.modules["dftracer_agents.mcp_service_factory"] = factory_mod
    spec.loader.exec_module(factory_mod)

    sys.modules["dftracer_agents"] = pkg
    sys.modules["dftracer_agents.mcp_tools"] = mcp_pkg
    sys.modules["dftracer_agents.mcp_tools.tools"] = tools_pkg


def _load_module(name: str, path: Path):
    mod_name = f"dftracer_agents.mcp_tools.tools.{name}"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Server builders
# ---------------------------------------------------------------------------

def _build_utils_server() -> FastMCP:
    path = REPO_ROOT / "dftracer-agents" / "mcp-tools" / "tools" / "dftracer_utils_service.py"
    mod = _load_module("dftracer_utils_service", path)
    service = mod.DftracerUtilsService()

    server = FastMCP("DFTracerUtils")
    for sub_name in (
        "core_subservice",
        "analysis_subservice",
        "query_subservice",
        "utility_subservice",
        "dlio_subservice",
        "synthetic_subservice",
        "mpi_subservice",
    ):
        sub = getattr(service, sub_name, None)
        if sub is None:
            continue
        for tool in asyncio.run(sub.list_tools()):
            server.add_tool(tool)
    return server


def _build_analyzer_server() -> FastMCP:
    path = REPO_ROOT / "dftracer-agents" / "mcp-tools" / "tools" / "dfanalyzer_service.py"
    mod = _load_module("dfanalyzer_service", path)
    service = mod.DFAnalyzerService()

    server = FastMCP("DFAnalyzer")
    for tool in asyncio.run(service.analyzer_subservice.list_tools()):
        server.add_tool(tool)
    return server


def _build_plot_server() -> FastMCP:
    path = REPO_ROOT / "dftracer-agents" / "mcp-tools" / "tools" / "dftracer_plot_service.py"
    mod = _load_module("dftracer_plot_service", path)
    service = mod.DFTracerPlotService()

    server = FastMCP("DFTracerPlot")
    for tool in asyncio.run(service.plot_subservice.list_tools()):
        server.add_tool(tool)
    return server


def build_server(service: str) -> FastMCP:
    """Build and return the combined FastMCP server for the requested service(s)."""
    _bootstrap_package_context()

    if service == "utils":
        return _build_utils_server()

    if service == "analyzer":
        combined = FastMCP("DFAnalyzer+Plot")
        for srv in (_build_analyzer_server(), _build_plot_server()):
            for tool in asyncio.run(srv.list_tools()):
                combined.add_tool(tool)
        return combined

    # both — all three services
    combined = FastMCP("DFTracer")
    for srv in (_build_utils_server(), _build_analyzer_server(), _build_plot_server()):
        for tool in asyncio.run(srv.list_tools()):
            combined.add_tool(tool)
    return combined


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="DFTracer MCP Server — stdio transport for Goose and MCP clients"
    )
    parser.add_argument(
        "--service",
        choices=["utils", "analyzer", "both"],
        default="both",
        help="Which service(s) to expose (default: both)",
    )
    args = parser.parse_args()

    server = build_server(args.service)
    asyncio.run(server.run_stdio_async(show_banner=False))


if __name__ == "__main__":
    main()
