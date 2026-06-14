from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_dfanalyzer_module():
    repo_root = Path(__file__).resolve().parents[1]
    service_path = (
        repo_root
        / "dftracer-agents"
        / "mcp-tools"
        / "tools"
        / "dftracer"
        / "dfanalyzer_service.py"
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

        @classmethod
        def get_service(cls, name):
            return cls._services.get(name)

    factory_mod.MCPService = MCPService
    factory_mod.MCPServiceFactory = MCPServiceFactory

    sys.modules["dftracer_agents"] = pkg_root
    sys.modules["dftracer_agents.mcp_tools"] = mcp_pkg
    sys.modules["dftracer_agents.mcp_tools.tools"] = tools_pkg
    sys.modules["dftracer_agents.mcp_service_factory"] = factory_mod

    mod_name = "dftracer_agents.mcp_tools.tools.dfanalyzer_service"
    if mod_name in sys.modules:
        del sys.modules[mod_name]

    spec = importlib.util.spec_from_file_location(mod_name, service_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    sys.modules[mod_name] = module

    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def dfanalyzer_module():
    return _load_dfanalyzer_module()


@pytest.fixture()
def dfanalyzer_service(dfanalyzer_module):
    return dfanalyzer_module.DFAnalyzerService()


def _tool_map(service):
    tools = {}
    for t in asyncio.run(service.analyzer_subservice.list_tools()):
        tools[t.name] = t
    return tools


def test_hydra_args_default_shape(dfanalyzer_module):
    cmd = dfanalyzer_module._hydra_args(trace_path="/tmp/trace")
    assert cmd[0] == "dfanalyzer"
    assert "--trace-path" in cmd
    assert "/tmp/trace" in cmd
    assert "-ahydra.analyzer/preset=posix" in cmd
    assert "--output=console" in cmd
    assert "--cluster=local" in cmd


def test_analyze_tool_registered(dfanalyzer_service):
    tools = _tool_map(dfanalyzer_service)
    assert "analyze" in tools
    assert "list_presets" in tools


def test_analyze_tool_invokes_subprocess_success(monkeypatch, dfanalyzer_module, dfanalyzer_service):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="ANALYSIS_OK", stderr="")

    monkeypatch.setattr(dfanalyzer_module.subprocess, "run", fake_run)

    tools = _tool_map(dfanalyzer_service)
    analyze_fn = tools["analyze"].fn
    out = analyze_fn(trace_path="/tmp/trace")

    assert out == "ANALYSIS_OK"
    assert calls
    cmd, kwargs = calls[-1]
    assert cmd[0] == "dfanalyzer"
    assert "--trace-path" in cmd
    assert "/tmp/trace" in cmd
    assert kwargs.get("capture_output") is True
    assert kwargs.get("text") is True


def test_analyze_tool_invokes_subprocess_failure(monkeypatch, dfanalyzer_module, dfanalyzer_service):
    def fake_run(_cmd, **_kwargs):
        return SimpleNamespace(returncode=2, stdout="oops", stderr="boom")

    monkeypatch.setattr(dfanalyzer_module.subprocess, "run", fake_run)

    tools = _tool_map(dfanalyzer_service)
    analyze_fn = tools["analyze"].fn
    out = analyze_fn(trace_path="/tmp/trace")

    assert "dfanalyzer exited with code 2" in out
    assert "stdout:" in out
    assert "stderr:" in out


def test_service_name_property(dfanalyzer_service):
    assert dfanalyzer_service.name == "dfanalyzer"


def test_service_registered_in_factory(dfanalyzer_module):
    service = dfanalyzer_module.MCPServiceFactory.get_service("dfanalyzer")
    assert service is not None
    assert service.name == "dfanalyzer"
