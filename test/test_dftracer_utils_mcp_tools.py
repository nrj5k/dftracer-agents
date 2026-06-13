from __future__ import annotations

from types import SimpleNamespace

import pytest

from conftest import get_tool_map, resolve_command_callable


EXPECTED_TOOLS = {
    "reader",
    "info",
    "merge",
    "split",
    "event_count",
    "pgzip",
    "tar",
    "stats",
    "aggregator",
    "call_tree",
    "comparator",
    "view",
    "index",
    "organize",
    "reconstruct",
    "replay",
    "server",
    "gen_dlio_config",
    "gen_fake_trace",
    "aggregator_mpi",
    "call_tree_mpi",
}


TOOL_CASES = [
    ("reader", {"file": "sample.pfw.gz"}, "dftracer_reader", True),
    ("info", {}, "dftracer_info", True),
    ("merge", {}, "dftracer_merge", True),
    ("split", {}, "dftracer_split", True),
    ("event_count", {}, "dftracer_event_count", True),
    ("pgzip", {}, "dftracer_pgzip", True),
    ("tar", {"file": "sample.tar.gz"}, "dftracer_tar", True),
    ("stats", {}, "dftracer_stats", True),
    ("aggregator", {}, "dftracer_aggregator", True),
    ("call_tree", {}, "dftracer_call_tree", True),
    ("comparator", {}, "dftracer_comparator", True),
    ("view", {}, "dftracer_view", True),
    ("index", {}, "dftracer_index", True),
    ("organize", {}, "dftracer_organize", True),
    ("reconstruct", {}, "dftracer_reconstruct", True),
    ("replay", {}, "dftracer_replay", True),
    ("server", {}, "dftracer_server", False),
    ("gen_dlio_config", {"output": "dlio.yaml"}, "dftracer_gen_dlio_config", True),
    ("gen_fake_trace", {"output_dir": "fake-traces"}, "dftracer_gen_fake_trace", True),
    ("aggregator_mpi", {}, "dftracer_aggregator_mpi", False),
    ("call_tree_mpi", {}, "dftracer_call_tree_mpi", False),
]


def test_all_expected_tools_are_registered(service_instance):
    tool_map = get_tool_map(service_instance)
    assert set(tool_map) == EXPECTED_TOOLS


@pytest.mark.parametrize(
    "tool_name,kwargs,expected_binary,expected_check",
    TOOL_CASES,
)
def test_each_tool_invokes_expected_binary(
    monkeypatch,
    service_module,
    service_instance,
    tool_name,
    kwargs,
    expected_binary,
    expected_check,
):
    calls = []

    def fake_run(cmd, **run_kwargs):
        calls.append((cmd, run_kwargs))
        return SimpleNamespace(stdout="ok")

    monkeypatch.setattr(service_module.subprocess, "run", fake_run)

    tool_map = get_tool_map(service_instance)
    fn = resolve_command_callable(tool_map[tool_name])

    result = fn(**kwargs)

    assert isinstance(result, str)
    assert calls, f"No subprocess call recorded for tool {tool_name}"

    cmd, run_kwargs = calls[-1]
    assert cmd[0] == expected_binary
    assert run_kwargs.get("capture_output") is True
    assert run_kwargs.get("text") is True
    assert run_kwargs.get("check") is expected_check


def test_watchdog_flag_builder_includes_only_provided_values(service_module):
    flags = service_module._build_watchdog_flags(
        disable_watchdog=True,
        watchdog_global_timeout=12.5,
        watchdog_task_timeout=3.0,
        watchdog_warning_threshold=0.9,
    )
    assert flags == [
        "--disable-watchdog",
        "--watchdog-global-timeout",
        "12.5",
        "--watchdog-task-timeout",
        "3.0",
        "--watchdog-warning-threshold",
        "0.9",
    ]
