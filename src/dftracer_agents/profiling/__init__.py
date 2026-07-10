"""Pipeline profiling: what each agent step cost, in time, retries, tokens and USD.

The chain is: Claude Code exports OTLP telemetry → :mod:`.collector` receives it
→ :mod:`.aggregate` attributes each event to the pipeline step whose attempt
interval contains its timestamp → :mod:`.mlflow_sink` mirrors the result into
MLflow and into the session's ``performance/`` directory.

The MCP tools in ``mcp_tools.tools.session.profiling`` drive it through
:mod:`.client`.
"""
from __future__ import annotations

from .aggregate import MAIN_STEP, Agg, Attempt, Profile, Step, is_ok
from .mlflow_sink import (MlflowSink, default_tracking_uri, write_performance_report,
                          write_step_files, write_summary_json)
from .otlp import parse_logs, parse_metrics

__all__ = [
    "MAIN_STEP", "Agg", "Attempt", "Profile", "Step", "is_ok",
    "MlflowSink", "default_tracking_uri", "write_performance_report",
    "write_step_files", "write_summary_json", "parse_logs", "parse_metrics",
]
