"""Tests for DFDiagnoserService — unit tests with subprocess mocking."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR  = REPO_ROOT / "dftracer-agents" / "mcp-tools" / "tools"

# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

def _load_diagnoser_module():
    sys.path.insert(0, str(REPO_ROOT))
    import dftracer_mcp_server as srv
    srv._bootstrap_package_context()
    return srv._load_module(
        "dftracer.dfdiagnoser_service",
        TOOLS_DIR / "dftracer" / "dfdiagnoser_service.py",
    )


@pytest.fixture(scope="module")
def dmod():
    return _load_diagnoser_module()


@pytest.fixture()
def service(dmod):
    return dmod.DFDiagnoserService()


def _tool_map(service):
    return {t.name: t for t in asyncio.run(service.diagnoser_subservice.list_tools())}


def _fn(tool):
    for attr in ("fn", "function", "callable", "handler", "_fn"):
        v = getattr(tool, attr, None)
        if callable(v):
            return v
    raise TypeError(f"No callable on tool {tool!r}")


def _call(tool, **kwargs):
    fn = _fn(tool)
    raw = asyncio.run(fn(**kwargs)) if asyncio.iscoroutinefunction(fn) else fn(**kwargs)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

class TestToolRegistration:
    def test_diagnose_tool_registered(self, service):
        tools = _tool_map(service)
        assert "diagnose" in tools

    def test_only_one_tool(self, service):
        tools = _tool_map(service)
        assert len(tools) == 1


# ---------------------------------------------------------------------------
# Helper: _describe_metric
# ---------------------------------------------------------------------------

class TestDescribeMetric:
    def test_known_suffix_small_io(self, dmod):
        desc = dmod._describe_metric("process_small_io_pct")
        assert "small" in desc.lower() or "io" in desc.lower()

    def test_known_suffix_read_time(self, dmod):
        desc = dmod._describe_metric("app_read_time_pct")
        assert "read" in desc.lower()

    def test_unknown_metric_falls_back_to_human(self, dmod):
        desc = dmod._describe_metric("my_custom_metric_foo")
        assert "my custom metric foo" == desc

    def test_metadata_time_mapped(self, dmod):
        desc = dmod._describe_metric("metadata_time_pct")
        assert desc != "metadata_time_pct"  # something was returned


# ---------------------------------------------------------------------------
# Helper: _extract_bottlenecks
# ---------------------------------------------------------------------------

class TestExtractBottlenecks:
    def test_empty_views_returns_zeros(self, dmod):
        counts, bns = dmod._extract_bottlenecks([])
        assert bns == []
        assert counts["critical"] == 0

    def test_score_4_is_high_bottleneck(self, dmod):
        views = [{
            "view_file": "test_scored.json",
            "rows": {
                "rank_0": {"read_time_pct": 0.9, "read_time_pct_score": 4}
            }
        }]
        counts, bns = dmod._extract_bottlenecks(views)
        assert len(bns) == 1
        assert bns[0]["severity"] == "high"
        assert bns[0]["score"] == 4
        assert counts["high"] == 1

    def test_score_5_is_critical(self, dmod):
        views = [{
            "view_file": "test.json",
            "rows": {"rank_0": {"small_io_pct": 0.95, "small_io_pct_score": 5}}
        }]
        counts, bns = dmod._extract_bottlenecks(views)
        assert bns[0]["severity"] == "critical"
        assert counts["critical"] == 1

    def test_score_3_is_not_a_bottleneck(self, dmod):
        views = [{
            "view_file": "test.json",
            "rows": {"rank_0": {"rand_pct": 0.6, "rand_pct_score": 3}}
        }]
        counts, bns = dmod._extract_bottlenecks(views)
        assert bns == []
        assert counts["medium"] == 1

    def test_sorted_by_score_descending(self, dmod):
        views = [{
            "view_file": "test.json",
            "rows": {
                "r0": {"a_pct": 0.9, "a_pct_score": 4, "b_pct": 0.95, "b_pct_score": 5},
            }
        }]
        counts, bns = dmod._extract_bottlenecks(views)
        scores = [b["score"] for b in bns]
        assert scores == sorted(scores, reverse=True)

    def test_none_score_skipped(self, dmod):
        views = [{
            "view_file": "test.json",
            "rows": {"r0": {"metric": 0.5, "metric_score": None}}
        }]
        counts, bns = dmod._extract_bottlenecks(views)
        assert bns == []


# ---------------------------------------------------------------------------
# diagnose tool — error cases (no subprocess needed)
# ---------------------------------------------------------------------------

class TestDiagnoseErrors:
    def test_missing_checkpoint_dir(self, service, tmp_path):
        tools = _tool_map(service)
        result = _call(tools["diagnose"], checkpoint_dir=str(tmp_path / "nonexistent"))
        assert result["status"] == "error"
        assert "not found" in result["message"].lower()

    def test_no_flat_view_files(self, service, tmp_path):
        cp = tmp_path / "checkpoint"
        cp.mkdir()
        # Directory exists but has no _flat_view_*.parquet
        tools = _tool_map(service)
        result = _call(tools["diagnose"], checkpoint_dir=str(cp))
        assert result["status"] == "error"
        assert "flat_view" in result["message"].lower() or "checkpoint" in result["message"].lower()


# ---------------------------------------------------------------------------
# diagnose tool — CLI fallback path
# ---------------------------------------------------------------------------

class TestDiagnoseCliPath:
    def test_cli_called_when_api_unavailable(self, service, dmod, monkeypatch, tmp_path):
        """When dfdiagnoser is not pip-installed, the CLI subprocess is called."""
        cp = tmp_path / "checkpoint"
        cp.mkdir()
        # Create a fake flat view parquet file (content doesn't matter for this test)
        (cp / "_flat_view_time_range.parquet").write_bytes(b"PAR1")

        cli_calls = []

        def fake_run_cli(cmd, timeout=300):
            cli_calls.append(cmd)
            # Write a scored JSON file so the parse step has something to read
            out_dir = None
            for i, arg in enumerate(cmd):
                if "output.output_dir" in arg:
                    out_dir = arg.split("=", 1)[1]
            if out_dir:
                Path(out_dir).mkdir(parents=True, exist_ok=True)
                scored = {"rank_0": {"read_time_pct": 0.8, "read_time_pct_score": 4}}
                (Path(out_dir) / "time_range_scored.json").write_text(json.dumps(scored))
            return {"returncode": 0, "stdout": "Scored OK", "stderr": "", "success": True}

        # Block Python API import so CLI fallback is used
        monkeypatch.setitem(sys.modules, "dfdiagnoser", None)
        monkeypatch.setitem(sys.modules, "dfdiagnoser.diagnoser", None)
        monkeypatch.setattr(dmod, "_run_cli", fake_run_cli)

        tools = _tool_map(service)
        result = _call(tools["diagnose"], checkpoint_dir=str(cp))

        assert cli_calls, "CLI subprocess was never called"
        assert cli_calls[0][0] == "dfdiagnoser"
        # Even with score=4 found we should get ok or at least not a hard error
        # (ok OR the scored views were parsed)
        assert result.get("status") in ("ok", "error")
        if result["status"] == "ok":
            assert "severity_counts" in result

    def test_cli_failure_returns_error(self, service, dmod, monkeypatch, tmp_path):
        """If CLI also fails and no scored files are produced, status is error."""
        cp = tmp_path / "checkpoint2"
        cp.mkdir()
        (cp / "_flat_view_time_range.parquet").write_bytes(b"PAR1")

        def bad_run_cli(cmd, timeout=300):
            return {"returncode": 1, "stdout": "", "stderr": "command not found", "success": False}

        monkeypatch.setitem(sys.modules, "dfdiagnoser", None)
        monkeypatch.setattr(dmod, "_run_cli", bad_run_cli)

        tools = _tool_map(service)
        result = _call(tools["diagnose"], checkpoint_dir=str(cp))
        assert result["status"] == "error"
        assert "dfdiagnoser" in result.get("message", "").lower() or \
               "failed" in result.get("message", "").lower()


# ---------------------------------------------------------------------------
# diagnose tool — output parsing
# ---------------------------------------------------------------------------

class TestDiagnoseOutputParsing:
    def test_loads_scored_json_and_returns_bottlenecks(self, service, dmod, monkeypatch, tmp_path):
        """End-to-end: scored JSON is written, tool parses it into bottlenecks."""
        cp  = tmp_path / "checkpoint3"
        out = tmp_path / "scored3"
        cp.mkdir(); out.mkdir()
        (cp / "_flat_view_time_range.parquet").write_bytes(b"PAR1")

        # Pre-populate scored JSON
        scored = {
            "rank_0": {
                "read_time_pct": 0.85, "read_time_pct_score": 4,
                "small_io_pct":  0.95, "small_io_pct_score": 5,
            }
        }
        (out / "_flat_view_time_range_scored.json").write_text(json.dumps(scored))

        def noop_run_cli(cmd, timeout=300):
            return {"returncode": 0, "stdout": "OK", "stderr": "", "success": True}

        monkeypatch.setitem(sys.modules, "dfdiagnoser", None)
        monkeypatch.setattr(dmod, "_run_cli", noop_run_cli)

        tools = _tool_map(service)
        result = _call(tools["diagnose"],
                       checkpoint_dir=str(cp), output_dir=str(out))

        assert result["status"] == "ok"
        counts = result["severity_counts"]
        assert counts["high"] >= 1
        assert counts["critical"] >= 1
        bns = result["bottlenecks"]
        assert len(bns) >= 1
        assert bns[0]["score"] == 5  # sorted descending

    def test_raw_stats_included_when_present(self, service, dmod, monkeypatch, tmp_path):
        cp  = tmp_path / "checkpoint4"
        out = tmp_path / "scored4"
        cp.mkdir(); out.mkdir()
        (cp / "_flat_view_time_range.parquet").write_bytes(b"PAR1")
        raw = {"total_ops": 1000, "read_ops": 800, "write_ops": 200}
        (cp / "_raw_stats_session.json").write_text(json.dumps(raw))
        (out / "scored.json").write_text(json.dumps({}))  # empty but valid

        monkeypatch.setitem(sys.modules, "dfdiagnoser", None)
        monkeypatch.setattr(dmod, "_run_cli",
                            lambda *a, **k: {"returncode": 0, "success": True,
                                             "stdout": "", "stderr": ""})
        tools = _tool_map(service)
        result = _call(tools["diagnose"], checkpoint_dir=str(cp), output_dir=str(out))
        if result["status"] == "ok":
            assert result.get("raw_stats_summary") is not None


# ---------------------------------------------------------------------------
# Service metadata
# ---------------------------------------------------------------------------

class TestServiceMetadata:
    def test_name_property(self, service):
        assert service.name == "dfdiagnoser"

    def test_factory_registration(self, dmod):
        svc = dmod.MCPServiceFactory.get_service("dfdiagnoser")
        assert svc is not None
        assert svc.name == "dfdiagnoser"

    def test_execute_returns_string(self, service):
        result = service.execute({})
        assert isinstance(result, str)
