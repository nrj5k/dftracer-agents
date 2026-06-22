#!/usr/bin/env python3
"""
DFTracer MCP Server — HTTP (streamable-http) entry point for Goose and other
MCP clients.

Exposes dftracer_utils and dfanalyzer tools over the Model Context Protocol
using FastMCP's streamable-HTTP transport (default) or stdio.

Usage:
    dftracer-mcp-server                          # HTTP on 0.0.0.0:5000 (default)
    dftracer-mcp-server --port 8080
    dftracer-mcp-server --transport stdio        # legacy stdio mode
    dftracer-mcp-server --service utils
    dftracer-mcp-server --service analyzer
    dftracer-mcp-server --service both

Goose config (~/.config/goose/config.yaml):
    extensions:
      dftracer:
        type: streamable_http
        uri: http://localhost:5000/mcp
        enabled: true

Claude Code config (.claude/settings.json):
    {
      "mcpServers": {
        "dftracer": { "url": "http://localhost:5000/mcp" }
      }
    }
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.util
import sys
import types
from pathlib import Path

from fastmcp import FastMCP

PKG_ROOT = Path(__file__).resolve().parent   # dftracer-agents/
REPO_ROOT = PKG_ROOT  # alias used by dev-mode path builders below


# ---------------------------------------------------------------------------
# Package context bootstrap (dev / editable-install fallback only)
# ---------------------------------------------------------------------------

def _is_package_installed() -> bool:
    """Return True if dftracer_agents is importable as an installed package."""
    try:
        importlib.import_module("dftracer_agents")
        return True
    except ImportError:
        return False


def _bootstrap_package_context() -> None:
    """Inject synthetic package stubs so relative imports work in dev mode.

    Only called when the package is NOT installed (dev / no-pip scenario).
    When installed properly (pip install / pip install -e .) this function is
    never executed — the real packages are on sys.path already.
    """
    if "dftracer_agents" in sys.modules:
        return

    tools_dir = PKG_ROOT / "mcp-tools" / "tools"

    pkg = types.ModuleType("dftracer_agents")
    pkg.__path__ = [str(PKG_ROOT)]

    mcp_pkg = types.ModuleType("dftracer_agents.mcp_tools")
    mcp_pkg.__path__ = [str(PKG_ROOT / "mcp-tools")]

    tools_pkg = types.ModuleType("dftracer_agents.mcp_tools.tools")
    tools_pkg.__path__ = [str(tools_dir)]

    dftracer_pkg = types.ModuleType("dftracer_agents.mcp_tools.tools.dftracer")
    dftracer_pkg.__path__ = [str(tools_dir / "dftracer")]

    session_pkg = types.ModuleType("dftracer_agents.mcp_tools.tools.session")
    session_pkg.__path__ = [str(tools_dir / "session")]

    papers_pkg = types.ModuleType("dftracer_agents.mcp_tools.tools.papers")
    papers_pkg.__path__ = [str(tools_dir / "papers")]

    # Real MCPServiceFactory from disk
    factory_path = PKG_ROOT / "mcp-tools" / "mcp_service_factory.py"
    factory_mod = types.ModuleType("dftracer_agents.mcp_service_factory")
    spec = importlib.util.spec_from_file_location(
        "dftracer_agents.mcp_service_factory", factory_path
    )
    factory_mod = importlib.util.module_from_spec(spec)
    sys.modules["dftracer_agents.mcp_service_factory"] = factory_mod
    # Also register under the mcp_tools sub-path so 3-dot relative imports from
    # dftracer_agents.mcp_tools.tools.dftracer.* resolve to the same object.
    sys.modules["dftracer_agents.mcp_tools.mcp_service_factory"] = factory_mod
    spec.loader.exec_module(factory_mod)

    annotations_pkg = types.ModuleType("dftracer_agents.mcp_tools.tools.annotations")
    annotations_pkg.__path__ = [str(tools_dir / "annotations")]

    optimizations_pkg = types.ModuleType("dftracer_agents.mcp_tools.tools.optimizations")
    optimizations_pkg.__path__ = [str(tools_dir / "optimizations")]

    sys.modules["dftracer_agents"] = pkg
    sys.modules["dftracer_agents.mcp_tools"] = mcp_pkg
    sys.modules["dftracer_agents.mcp_tools.tools"] = tools_pkg
    sys.modules["dftracer_agents.mcp_tools.tools.dftracer"] = dftracer_pkg
    sys.modules["dftracer_agents.mcp_tools.tools.session"] = session_pkg
    sys.modules["dftracer_agents.mcp_tools.tools.papers"] = papers_pkg
    sys.modules["dftracer_agents.mcp_tools.tools.annotations"] = annotations_pkg
    sys.modules["dftracer_agents.mcp_tools.tools.optimizations"] = optimizations_pkg


def _load_module(name: str, path: Path):
    """Load a service module.

    Strategy:
    1. If the package is installed, import by full dotted name (no path needed).
    2. Otherwise fall back to spec_from_file_location so dev-mode works without
       a full pip install.

    ``name`` must be a dotted sub-path under ``dftracer_agents.mcp_tools.tools``,
    e.g. ``"dftracer.dftracer_utils_service"`` or ``"session.workspace"``.
    """
    full_name = f"dftracer_agents.mcp_tools.tools.{name}"

    if full_name in sys.modules:
        return sys.modules[full_name]

    # Installed path — use the real package hierarchy.
    if _is_package_installed():
        return importlib.import_module(full_name)

    # Dev / editable path — load directly from disk.
    _bootstrap_package_context()
    sys.modules.pop(full_name, None)
    spec = importlib.util.spec_from_file_location(full_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Server builders
# ---------------------------------------------------------------------------

def _build_utils_server() -> FastMCP:
    path = PKG_ROOT / "mcp-tools" / "tools" / "dftracer" / "dftracer_utils_service.py"
    mod = _load_module("dftracer.dftracer_utils_service", path)
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
    path = PKG_ROOT / "mcp-tools" / "tools" / "dftracer" / "dfanalyzer_service.py"
    mod = _load_module("dftracer.dfanalyzer_service", path)
    service = mod.DFAnalyzerService()

    server = FastMCP("DFAnalyzer")
    for tool in asyncio.run(service.analyzer_subservice.list_tools()):
        server.add_tool(tool)
    return server


def _build_plot_server() -> FastMCP:
    path = PKG_ROOT / "mcp-tools" / "tools" / "dftracer" / "dftracer_plot_service.py"
    mod = _load_module("dftracer.dftracer_plot_service", path)
    service = mod.DFTracerPlotService()

    server = FastMCP("DFTracerPlot")
    for tool in asyncio.run(service.plot_subservice.list_tools()):
        server.add_tool(tool)
    return server


def _build_docs_server() -> FastMCP:
    path = PKG_ROOT / "mcp-tools" / "tools" / "dftracer" / "docs_service.py"
    mod = _load_module("dftracer.docs_service", path)
    service = mod.DFTracerDocsService()

    server = FastMCP("DFTracerDocs")
    for tool in asyncio.run(service.docs_subservice.list_tools()):
        server.add_tool(tool)
    return server


def _build_diagnoser_server() -> FastMCP:
    path = PKG_ROOT / "mcp-tools" / "tools" / "dftracer" / "dfdiagnoser_service.py"
    mod = _load_module("dftracer.dfdiagnoser_service", path)
    service = mod.DFDiagnoserService()

    server = FastMCP("DFDiagnoser")
    for tool in asyncio.run(service.diagnoser_subservice.list_tools()):
        server.add_tool(tool)
    return server


def _build_papers_server() -> FastMCP:
    path = PKG_ROOT / "mcp-tools" / "tools" / "papers" / "academic_service.py"
    mod = _load_module("papers.academic_service", path)
    service = mod.AcademicPapersService()

    server = FastMCP("AcademicPapers")
    for tool in asyncio.run(service.papers_subservice.list_tools()):
        server.add_tool(tool)
    return server


def _build_session_server() -> FastMCP:
    session_dir = PKG_ROOT / "mcp-tools" / "tools" / "session"
    annotations_dir = PKG_ROOT / "mcp-tools" / "tools" / "annotations"
    optimizations_dir = PKG_ROOT / "mcp-tools" / "tools" / "optimizations"
    # Load session submodules in dependency order so relative imports resolve.
    # In installed mode _load_module uses importlib.import_module and these are
    # already on sys.path; in dev mode it loads by file path.
    for submod in ("workspace", "detection", "annotation", "build", "install",
                   "session_tools", "annotation_clang", "annotation_python",
                   "annotation_ai", "pipeline_tools"):
        _load_module(f"session.{submod}", session_dir / f"{submod}.py")
    for submod in ("annotate_c", "annotate_cpp", "annotate_python"):
        _load_module(f"annotations.{submod}", annotations_dir / f"{submod}.py")
    _load_module("annotations", annotations_dir / "__init__.py")
    for submod in ("diagnose", "iteration", "levels", "strategies"):
        _load_module(f"optimizations.{submod}", optimizations_dir / f"{submod}.py")
    _load_module("optimizations", optimizations_dir / "__init__.py")

    path = PKG_ROOT / "mcp-tools" / "tools" / "dftracer" / "dftracer_service.py"
    mod = _load_module("dftracer.dftracer_service", path)
    service = mod.DFTracerSessionService()

    server = FastMCP("DFTracerSession")
    for sub_name in (
        "session_subservice",
        "pipeline_subservice",
        "daemon_subservice",
        "clang_subservice",
        "annotation_api_subservice",
        "annotation_subservice",
        "optimization_subservice",
    ):
        sub = getattr(service, sub_name, None)
        if sub is None:
            continue
        for tool in asyncio.run(sub.list_tools()):
            server.add_tool(tool)
    return server


def build_server(service: str) -> FastMCP:
    """Build and return the combined FastMCP server for the requested service(s)."""
    if service == "utils":
        return _build_utils_server()

    if service == "analyzer":
        combined = FastMCP("DFAnalyzer+Plot+Diagnoser")
        for srv in (_build_analyzer_server(), _build_plot_server(), _build_diagnoser_server()):
            for tool in asyncio.run(srv.list_tools()):
                combined.add_tool(tool)
        return combined

    if service == "diagnoser":
        return _build_diagnoser_server()

    if service == "papers":
        return _build_papers_server()

    if service == "session":
        return _build_session_server()

    if service == "docs":
        return _build_docs_server()

    # both — all services
    combined = FastMCP("DFTracer")
    for srv in (
        _build_utils_server(),
        _build_analyzer_server(),
        _build_plot_server(),
        _build_diagnoser_server(),
        _build_session_server(),
        _build_docs_server(),
        _build_papers_server(),
    ):
        for tool in asyncio.run(srv.list_tools()):
            combined.add_tool(tool)
    return combined


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="DFTracer MCP Server — HTTP or stdio transport for Goose and MCP clients"
    )
    parser.add_argument(
        "--service",
        choices=["utils", "analyzer", "session", "docs", "diagnoser", "papers", "both"],
        default="both",
        help="Which service(s) to expose (default: both)",
    )
    parser.add_argument(
        "--transport",
        choices=["http", "streamable-http", "sse", "stdio"],
        default="http",
        help="Transport protocol (default: http / streamable-http)",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind to for HTTP transports (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5000,
        help="Port to listen on for HTTP transports (default: 5000)",
    )
    parser.add_argument(
        "--path",
        default="/mcp",
        help="URL path for HTTP transports (default: /mcp)",
    )
    args = parser.parse_args()

    server = build_server(args.service)

    if args.transport == "stdio":
        asyncio.run(server.run_stdio_async(show_banner=False))
    else:
        transport = "streamable-http" if args.transport == "http" else args.transport
        asyncio.run(
            server.run_http_async(
                transport=transport,
                host=args.host,
                port=args.port,
                path=args.path,
                show_banner=True,
            )
        )


if __name__ == "__main__":
    main()
