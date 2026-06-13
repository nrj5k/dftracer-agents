#!/usr/bin/env python3
import asyncio, sys, types
from pathlib import Path
from fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parents[1]

def _load():
    sp = REPO_ROOT / "dftracer-agents" / "mcp-tools" / "tools" / "dfanalyzer_service.py"
    pkg = types.ModuleType("dftracer_agents")
    pkg.__path__ = [str(REPO_ROOT / "dftracer-agents")]
    mcp = types.ModuleType("dftracer_agents.mcp_tools")
    mcp.__path__ = [str(REPO_ROOT / "dftracer-agents" / "mcp-tools")]
    tools = types.ModuleType("dftracer_agents.mcp_tools.tools")
    tools.__path__ = [str(sp.parent)]
    fmod = types.ModuleType("dftracer_agents.mcp_service_factory")
    class MCPService: pass
    class MCPServiceFactory:
        _s = {}
        @classmethod
        def register(cls, n, s): cls._s[n] = s
        @classmethod
        def get_service(cls, n): return cls._s.get(n)
    fmod.MCPService = MCPService
    fmod.MCPServiceFactory = MCPServiceFactory
    sys.modules["dftracer_agents"] = pkg
    sys.modules["dftracer_agents.mcp_tools"] = mcp
    sys.modules["dftracer_agents.mcp_tools.tools"] = tools
    sys.modules["dftracer_agents.mcp_service_factory"] = fmod
    mn = "dftracer_agents.mcp_tools.tools.dfanalyzer_service"
    import importlib.util
    spec = importlib.util.spec_from_file_location(mn, sp)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mn] = mod
    spec.loader.exec_module(mod)
    return mod

def main():
    mod = _load()
    svc = mod.DFAnalyzerService()
    server = FastMCP("DFAnalyzerREPLServer")
    for t in asyncio.run(svc.analyzer_subservice.list_tools()):
        server.add_tool(t)
    asyncio.run(server.run_stdio_async(show_banner=False))

if __name__ == "__main__":
    main()
