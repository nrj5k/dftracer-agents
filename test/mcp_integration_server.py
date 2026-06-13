#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

from fastmcp import FastMCP


def _load_service_module():
    repo_root = Path(__file__).resolve().parents[1]
    service_path = (
        repo_root
        / "dftracer-agents"
        / "mcp-tools"
        / "tools"
        / "dftracer_utils_service.py"
    )

    pkg_root = types.ModuleType("dftracer_agents")
    pkg_root.__path__ = [str(repo_root / "dftracer-agents")]

    mcp_pkg = types.ModuleType("dftracer_agents.mcp_tools")
    mcp_pkg.__path__ = [str(repo_root / "dftracer-agents" / "mcp-tools")]

    tools_pkg = types.ModuleType("dftracer_agents.mcp_tools.tools")
    tools_pkg.__path__ = [str(service_path.parent)]

    factory_mod = types.ModuleType("dftracer_agents.mcp_service_factory")

    class MCPService:
        pass

    class MCPServiceFactory:
        _services = {}

        @classmethod
        def register(cls, name, service):
            cls._services[name] = service

    factory_mod.MCPService = MCPService
    factory_mod.MCPServiceFactory = MCPServiceFactory

    sys.modules["dftracer_agents"] = pkg_root
    sys.modules["dftracer_agents.mcp_tools"] = mcp_pkg
    sys.modules["dftracer_agents.mcp_tools.tools"] = tools_pkg
    sys.modules["dftracer_agents.mcp_service_factory"] = factory_mod

    mod_name = "dftracer_agents.mcp_tools.tools.dftracer_utils_service"
    module = types.ModuleType(mod_name)
    sys.modules[mod_name] = module

    source = service_path.read_text(encoding="utf-8")
    source = source.replace("self._register_index_tools()", "pass")
    source = source.replace("self._register_comparator_tools()", "pass")
    code = compile(source, str(service_path), "exec")
    exec(code, module.__dict__)
    return module


def build_server() -> FastMCP:
    module = _load_service_module()
    service = module.DftracerUtilsService()

    combined = FastMCP("DFTracerUtilsIntegrationServer")
    for attr_name in [
        "core_subservice",
        "analysis_subservice",
        "query_subservice",
        "utility_subservice",
        "dlio_subservice",
        "synthetic_subservice",
        "mpi_subservice",
    ]:
        sub = getattr(service, attr_name, None)
        if sub is None:
            continue
        for tool in asyncio.run(sub.list_tools()):
            combined.add_tool(tool)

    return combined


def main() -> None:
    server = build_server()
    asyncio.run(server.run_stdio_async(show_banner=False))


if __name__ == "__main__":
    main()
