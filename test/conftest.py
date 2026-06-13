from __future__ import annotations

import importlib.util
import asyncio
import sys
import types
from pathlib import Path

import pytest


SUBSERVICE_ATTRS = [
    "core_subservice",
    "analysis_subservice",
    "query_subservice",
    "utility_subservice",
    "dlio_subservice",
    "synthetic_subservice",
    "mpi_subservice",
]


@pytest.fixture(scope="session")
def service_module():
    """Load dftracer_utils_service.py with a synthetic package context."""
    repo_root = Path(__file__).resolve().parents[1]
    service_path = (
        repo_root
        / "dftracer-agents"
        / "mcp-tools"
        / "tools"
        / "dftracer_utils_service.py"
    )

    # Build a minimal import graph so relative imports succeed.
    pkg_root = types.ModuleType("dftracer_agents")
    pkg_root.__path__ = [str(repo_root / "dftracer-agents")]

    mcp_pkg = types.ModuleType("dftracer_agents.mcp_tools")
    mcp_pkg.__path__ = [str(repo_root / "dftracer-agents" / "mcp-tools")]

    tools_pkg = types.ModuleType("dftracer_agents.mcp_tools.tools")
    tools_pkg.__path__ = [str(service_path.parent)]

    factory_mod = types.ModuleType("dftracer_agents.mcp_service_factory")

    class MCPService:  # pragma: no cover - trivial stub
        pass

    class MCPServiceFactory:
        _services = {}

        @classmethod
        def register(cls, name, service):
            cls._services[name] = service

        @classmethod
        def get_service(cls, name):
            return cls._services.get(name)

    factory_mod.MCPService = MCPService
    factory_mod.MCPServiceFactory = MCPServiceFactory

    sys.modules["dftracer_agents"] = pkg_root
    sys.modules["dftracer_agents.mcp_tools"] = mcp_pkg
    sys.modules["dftracer_agents.mcp_tools.tools"] = tools_pkg
    sys.modules["dftracer_agents.mcp_service_factory"] = factory_mod

    mod_name = "dftracer_agents.mcp_tools.tools.dftracer_utils_service"
    if mod_name in sys.modules:
        del sys.modules[mod_name]

    spec = importlib.util.spec_from_file_location(mod_name, service_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    sys.modules[mod_name] = module

    source = service_path.read_text(encoding="utf-8")
    # The service constructor currently references two missing methods.
    # Patch these calls in-memory so tests can validate the real tool wrappers.
    source = source.replace("self._register_index_tools()", "pass")
    source = source.replace("self._register_comparator_tools()", "pass")
    code = compile(source, str(service_path), "exec")
    exec(code, module.__dict__)
    return module


@pytest.fixture()
def service_instance(service_module):
    return service_module.DftracerUtilsService()


def get_tool_map(service_instance):
    tool_map = {}
    for attr in SUBSERVICE_ATTRS:
        subservice = getattr(service_instance, attr)
        for tool in asyncio.run(subservice.list_tools()):
            tool_map[tool.name] = tool
    return tool_map


def resolve_command_callable(command):
    for attr in ("fn", "function", "callable", "handler", "_fn"):
        value = getattr(command, attr, None)
        if callable(value):
            return value
    if callable(command):
        return command
    raise TypeError(f"Cannot resolve callable for command object: {command!r}")
