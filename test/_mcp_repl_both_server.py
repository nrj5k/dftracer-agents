#!/usr/bin/env python3
import asyncio, sys, types, importlib.util
from pathlib import Path
from fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parents[1]
UTILS_SERVER = str(Path(__file__).resolve().parent / "mcp_integration_server.py")

def _load_utils():
    import runpy
    # reuse the build_server from mcp_integration_server
    import importlib.util as ilu
    spec = ilu.spec_from_file_location("_utils_srv", UTILS_SERVER)
    mod = ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.build_server()

def _load_analyzer():
    sp = REPO_ROOT / "dftracer-agents" / "mcp-tools" / "tools" / "dftracer" / "dfanalyzer_service.py"
    for k, v in {
        "dftracer_agents": None,
        "dftracer_agents.mcp_tools": None,
        "dftracer_agents.mcp_tools.tools": None,
        "dftracer_agents.mcp_service_factory": None,
    }.items():
        sys.modules.pop(k, None)
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
    sys.modules.pop(mn, None)
    spec = importlib.util.spec_from_file_location(mn, sp)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mn] = mod
    spec.loader.exec_module(mod)
    svc = mod.DFAnalyzerService()
    return asyncio.run(svc.analyzer_subservice.list_tools())

def main():
    combined = FastMCP("DFTracerAllServicesREPLServer")
    utils_srv = _load_utils()
    for t in asyncio.run(utils_srv.list_tools()):
        combined.add_tool(t)
    for t in _load_analyzer():
        combined.add_tool(t)
    asyncio.run(combined.run_stdio_async(show_banner=False))

if __name__ == "__main__":
    main()
