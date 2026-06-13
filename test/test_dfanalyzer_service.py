"""
Real integration tests for the DFAnalyzer MCP service.

Subprocess is NOT mocked.  Each test either builds arguments and checks their
shape (no I/O), or calls the actual dfanalyzer binary and verifies that the
MCP tool's behaviour matches what the binary itself produces.
"""
from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
import types
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Module loader (same synthetic package-context trick used across all tests)
# ---------------------------------------------------------------------------

def _load_dfanalyzer_module():
    repo_root = Path(__file__).resolve().parents[1]
    service_path = (
        repo_root
        / "dftracer-agents"
        / "mcp-tools"
        / "tools"
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

    for key in [
        "dftracer_agents",
        "dftracer_agents.mcp_tools",
        "dftracer_agents.mcp_tools.tools",
        "dftracer_agents.mcp_service_factory",
    ]:
        sys.modules[key] = locals().get(
            key.split(".")[-1] + ("_mod" if "factory" in key or key == "dftracer_agents" else "_pkg"),
            None,
        )
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


# ---------------------------------------------------------------------------
# Argument-builder tests — no subprocess, just verify Hydra override shape
# ---------------------------------------------------------------------------

def test_hydra_args_minimal(dfanalyzer_module):
    """_hydra_args with only trace_path produces correct Hydra overrides."""
    cmd = dfanalyzer_module._hydra_args(trace_path="/tmp/trace")
    print(f"\n  command: {' '.join(cmd)}")
    assert cmd[0] == "dfanalyzer"
    assert "trace_path=/tmp/trace" in cmd
    assert "analyzer/preset=posix" in cmd
    assert "output=console" in cmd
    assert "cluster=local" in cmd
    # Must NOT contain any argparse-style flags
    assert not any(a.startswith("--") for a in cmd), \
        f"Found argparse-style flags: {[a for a in cmd if a.startswith('--')]}"
    print(f"  all overrides are Hydra key=value style  ✓")


def test_hydra_args_full_options(dfanalyzer_module):
    """_hydra_args with every option set produces the correct override list."""
    cmd = dfanalyzer_module._hydra_args(
        trace_path="/data/trace",
        view_types=["file_name", "proc_name"],
        debug=True,
        verbose=True,
        analyzer="darshan",
        analyzer_preset="dlio",
        analyzer_checkpoint=True,
        analyzer_checkpoint_dir="/tmp/ckpt",
        analyzer_time_approximate=False,
        analyzer_time_granularity=0.5,
        analyzer_time_resolution=1e5,
        output_format="sqlite",
        output_compact=True,
        output_root_only=False,
        output_name="run1",
        output_run_db_path="/tmp/run.db",
        cluster_type="slurm",
        cluster_n_workers=4,
        cluster_memory_limit="8GB",
        cluster_processes=2,
        cluster_cores=16,
        cluster_memory="64GB",
    )
    print(f"\n  command ({len(cmd)} args):")
    for arg in cmd:
        print(f"    {arg}")

    assert "trace_path=/data/trace" in cmd
    assert "view_types=[file_name,proc_name]" in cmd
    assert "debug=true" in cmd
    assert "verbose=true" in cmd
    assert "analyzer=darshan" in cmd
    assert "analyzer/preset=dlio" in cmd
    assert "analyzer.checkpoint=true" in cmd
    assert "analyzer.checkpoint_dir=/tmp/ckpt" in cmd
    assert "analyzer.time_approximate=false" in cmd
    assert "analyzer.time_granularity=0.5" in cmd
    assert "analyzer.time_resolution=100000.0" in cmd
    assert "output=sqlite" in cmd
    assert "output.compact=true" in cmd
    assert "output.root_only=false" in cmd
    assert "output.name=run1" in cmd
    assert "output.run_db_path=/tmp/run.db" in cmd
    assert "cluster=slurm" in cmd
    assert "cluster.n_workers=4" in cmd
    assert "cluster.memory_limit=8GB" in cmd
    assert "cluster.processes=2" in cmd
    assert "cluster.cores=16" in cmd
    assert "cluster.memory=64GB" in cmd
    print("  all overrides present and correctly formatted  ✓")


# ---------------------------------------------------------------------------
# Tool registration check — no subprocess
# ---------------------------------------------------------------------------

def test_analyze_and_list_presets_are_registered(dfanalyzer_service):
    tools = _tool_map(dfanalyzer_service)
    print(f"\n  registered tools: {sorted(tools)}")
    assert "analyze" in tools
    assert "list_presets" in tools
    print("  'analyze' and 'list_presets' both registered  ✓")


def test_service_name_property(dfanalyzer_service):
    name = dfanalyzer_service.name
    print(f"\n  service.name = {name!r}")
    assert name == "dfanalyzer"
    print("  ✓")


def test_service_registered_in_factory(dfanalyzer_module):
    service = dfanalyzer_module.MCPServiceFactory.get_service("dfanalyzer")
    print(f"\n  factory['dfanalyzer'] → {service!r}")
    assert service is not None
    assert service.name == "dfanalyzer"
    print("  ✓")


# ---------------------------------------------------------------------------
# list_presets — pure Python, no subprocess
# ---------------------------------------------------------------------------

def test_list_presets_returns_option_matrix(dfanalyzer_service):
    tools = _tool_map(dfanalyzer_service)
    fn = tools["list_presets"].fn
    result = fn()
    print(f"\n  list_presets output:\n{result}")
    assert "posix" in result
    assert "dlio" in result
    assert "dftracer" in result
    assert "darshan" in result
    assert "recorder" in result
    assert "console" in result
    assert "sqlite" in result
    assert "file_name" in result
    print("  all expected option groups present  ✓")


# ---------------------------------------------------------------------------
# analyze — real subprocess (dfanalyzer binary, no mocking)
# ---------------------------------------------------------------------------

SAMPLE_DIR = str(Path(__file__).resolve().parent / "data" / "cm1_1_48_20240926")


def test_analyze_real_subprocess_error_is_surfaced(dfanalyzer_service):
    """
    dfanalyzer with a missing trace_path should fail.  The MCP tool must surface
    the error text (returncode + stdout + stderr) rather than swallowing it.

    We run dfanalyzer directly first to capture the ground-truth failure, then
    call the MCP tool and verify it produces an equivalent error message.
    """
    # Direct call — dfanalyzer exits non-zero when trace_path does not exist
    direct = subprocess.run(
        ["dfanalyzer", "trace_path=/nonexistent/path", "analyzer/preset=posix",
         "output=console", "cluster=local"],
        capture_output=True, text=True, check=False,
    )
    print(f"\n  direct dfanalyzer: rc={direct.returncode}")
    print(f"    stdout: {direct.stdout[:300]!r}")
    print(f"    stderr: {direct.stderr[:300]!r}")

    # MCP tool call (no mock)
    tools = _tool_map(dfanalyzer_service)
    analyze_fn = tools["analyze"].fn
    result = analyze_fn(trace_path="/nonexistent/path")

    print(f"\n  mcp tool result:\n{result[:600]}")

    # dfanalyzer must have failed
    assert direct.returncode != 0, "expected dfanalyzer to fail on missing path"
    # MCP tool must report the failure
    assert "dfanalyzer exited with code" in result
    assert str(direct.returncode) in result
    assert "stdout:" in result
    assert "stderr:" in result
    print(f"  error surfaced correctly (rc={direct.returncode})  ✓")


def test_analyze_output_matches_direct_subprocess(dfanalyzer_service):
    """
    When dfanalyzer succeeds (trace_path exists), the MCP tool must return
    exactly the same stdout that the binary produced directly.

    Uses the sample trace data.  If dfanalyzer itself segfaults on this
    platform, both the direct call and the MCP tool fail consistently.
    """
    direct_cmd = [
        "dfanalyzer",
        f"trace_path={SAMPLE_DIR}",
        "analyzer/preset=posix",
        "output=console",
        "cluster=local",
    ]
    direct = subprocess.run(direct_cmd, capture_output=True, text=True, check=False)
    print(f"\n  direct: {' '.join(direct_cmd)}")
    print(f"    rc={direct.returncode}")
    print(f"    stdout ({len(direct.stdout)} chars): {direct.stdout[:400]!r}")
    print(f"    stderr ({len(direct.stderr)} chars): {direct.stderr[:200]!r}")

    tools = _tool_map(dfanalyzer_service)
    analyze_fn = tools["analyze"].fn
    result = analyze_fn(trace_path=SAMPLE_DIR)
    print(f"\n  mcp result ({len(result)} chars): {result[:400]!r}")

    if direct.returncode == 0:
        # Both should agree and produce identical stdout
        assert result == direct.stdout, (
            f"MCP output differs from direct subprocess stdout.\n"
            f"direct: {direct.stdout!r}\n"
            f"mcp:    {result!r}"
        )
        print("  MCP output == direct stdout  ✓")
    else:
        # Both fail — MCP surfaces the error, direct reports non-zero rc
        assert "dfanalyzer exited with code" in result, \
            f"Expected error message, got: {result!r:.200}"
        assert str(direct.returncode) in result
        print(f"  both failed with rc={direct.returncode}  ✓")
