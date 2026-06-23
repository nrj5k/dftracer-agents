#!/usr/bin/env python3
"""
Wrapper that creates a session workspace, starts MLflow tracking,
and runs a goose session inside that workspace directory.

Usage:
    python scripts/run_session.py <app-name-or-url> [goose args...]

    # Run with a specific profile
    python scripts/run_session.py ior --profile dftracer

    # Disable MLflow UI server
    python scripts/run_session.py ior --no-mlflow-ui

    # Custom workspaces root
    python scripts/run_session.py ior --workspaces-root /scratch/sessions

The script:
  1. Creates <workspaces_root>/<app_name>/<timestamp>/ as the session run dir.
  2. Starts an MLflow run with tracking URI at <workspace>/logs/mlruns.
  3. Optionally launches the MLflow UI server on --mlflow-port (default 5000).
  4. Sets DFTRACER_WORKSPACES=<workspaces_root> so the MCP server uses the
     same root, then runs: goose session [goose_args].
  5. Ends the MLflow run (FINISHED / FAILED) when goose exits.
  6. Writes a session summary to <workspace>/logs/session_summary.md.

Goose (and the dftracer MCP tools) will read DFTRACER_WORKSPACES and create
pipeline artifacts inside the same workspaces root so everything is co-located
under the session workspace tree.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Helpers (mirror workspace._derive_app_name so IDs stay consistent)
# ---------------------------------------------------------------------------

def _derive_app_name(app: str) -> str:
    name = app.rstrip("/")
    if name.endswith(".git"):
        name = name[:-4]
    name = Path(name.split(":")[-1]).name
    name = name.split(".")[0] if "." in name else name
    name = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return name or "app"


def _write_summary(workspace: Path, run_id: str, app: str, exit_code: int,
                   mlflow_run_id: Optional[str], tracking_uri: str,
                   mlflow_port: int, duration_s: float) -> Path:
    logs_dir = workspace / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    status = "FINISHED" if exit_code == 0 else "FAILED"
    lines = [
        f"# Session Summary — {run_id}",
        f"",
        f"**Generated:** {now}  ",
        f"**App:** {app}  ",
        f"**Status:** {status}  ",
        f"**Exit code:** {exit_code}  ",
        f"**Duration:** {duration_s:.1f}s  ",
        f"**Workspace:** `{workspace}`  ",
    ]
    if mlflow_run_id:
        lines += [
            f"",
            f"## MLflow",
            f"- Run ID: `{mlflow_run_id}`",
            f"- Tracking URI: `{tracking_uri}`",
            f"- To relaunch UI: `mlflow ui --backend-store-uri {tracking_uri} --port {mlflow_port}`",
        ]

    # Collect pipeline step artifacts if any exist
    artifacts_dir = workspace / "artifacts"
    if artifacts_dir.exists():
        logs = sorted(artifacts_dir.glob("*.log"))
        if logs:
            lines += ["", "## Pipeline Steps"]
            for log in logs:
                lines.append(f"- `{log.name}`")

    report = "\n".join(lines) + "\n"
    path = logs_dir / "session_summary.md"
    path.write_text(report)
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a goose session with MLflow tracking",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("app", help="Application name or git URL")
    parser.add_argument(
        "--workspaces-root",
        default=os.environ.get("DFTRACER_WORKSPACES", "workspaces"),
        help="Root directory for workspace storage (default: workspaces/)",
    )
    parser.add_argument(
        "--mlflow-port", type=int, default=5000,
        help="Port for the MLflow UI server (default: 5000)",
    )
    parser.add_argument(
        "--no-mlflow-ui", action="store_true",
        help="Skip launching the MLflow UI server",
    )
    parser.add_argument(
        "goose_args", nargs=argparse.REMAINDER,
        help="Additional arguments forwarded to 'goose session'",
    )
    args = parser.parse_args()

    # ------------------------------------------------------------------
    # 1. Create workspace directory
    # ------------------------------------------------------------------
    app_name = _derive_app_name(args.app)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    workspaces_root = Path(args.workspaces_root).resolve()
    workspace = workspaces_root / app_name / timestamp
    workspace.mkdir(parents=True, exist_ok=True)

    logs_dir = workspace / "logs"
    logs_dir.mkdir(exist_ok=True)
    mlflow_db = logs_dir / "mlflow.db"
    tracking_uri = f"sqlite:///{mlflow_db}"

    run_id = f"{app_name}/{timestamp}"

    # Write session.json so MCP tools can load this session
    session_data: dict[str, Any] = {
        "run_id": run_id,
        "app_name": app_name,
        "app": args.app,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "workspace": str(workspace),
        "step": "created",
    }
    (workspace / "session.json").write_text(json.dumps(session_data, indent=2))

    # Write .current_run pointer (mirrors _create_run in workspace.py)
    pointer = workspaces_root / app_name / ".current_run"
    pointer.write_text(run_id)

    print(f"Workspace : {workspace}")
    print(f"Run ID    : {run_id}")

    # ------------------------------------------------------------------
    # 2. Start MLflow tracking
    # ------------------------------------------------------------------
    mlflow_available = False
    mlflow_run_id: Optional[str] = None
    ui_proc: Optional[subprocess.Popen] = None  # type: ignore[type-arg]
    active_run: Any = None

    try:
        import mlflow  # type: ignore[import]
        mlflow.set_tracking_uri(tracking_uri)
        mlflow.set_experiment(app_name)
        active_run = mlflow.start_run(run_name=f"{app_name}-{timestamp}")
        mlflow_run_id = active_run.info.run_id
        mlflow.log_params({
            "app": args.app[:500],
            "app_name": app_name,
            "run_id": run_id,
            "workspace": str(workspace)[:500],
        })
        mlflow_available = True
        print(f"MLflow    : {tracking_uri}")

        if not args.no_mlflow_ui:
            ui_proc = subprocess.Popen(
                [
                    "mlflow", "ui",
                    "--backend-store-uri", tracking_uri,
                    "--port", str(args.mlflow_port),
                    "--host", "0.0.0.0",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(1.5)
            print(f"MLflow UI : http://localhost:{args.mlflow_port}")

    except ImportError:
        print("Warning: mlflow not installed — tracking disabled")

    # ------------------------------------------------------------------
    # 3. Run goose session with DFTRACER_WORKSPACES set
    # ------------------------------------------------------------------
    env = os.environ.copy()
    env["DFTRACER_WORKSPACES"] = str(workspaces_root)

    goose_cmd = ["goose", "session"] + (args.goose_args or [])
    print(f"Command   : {' '.join(goose_cmd)}")
    print(f"Working dir: {workspace}")
    print()

    exit_code = 0
    status = "FINISHED"
    start_time = time.monotonic()

    try:
        result = subprocess.run(goose_cmd, env=env, cwd=str(workspace))
        exit_code = result.returncode
        status = "FINISHED" if exit_code == 0 else "FAILED"
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        exit_code = 130
        status = "FAILED"
    except FileNotFoundError:
        print("Error: 'goose' not found on PATH. Install goose and try again.")
        exit_code = 1
        status = "FAILED"

    duration_s = time.monotonic() - start_time

    # ------------------------------------------------------------------
    # 4. End MLflow run and write summary
    # ------------------------------------------------------------------
    if mlflow_available and active_run is not None:
        try:
            import mlflow  # type: ignore[import]
            mlflow.log_params({"exit_code": str(exit_code), "duration_s": f"{duration_s:.1f}"})
            mlflow.end_run(status=status)
        except Exception:
            pass

    summary_path = _write_summary(
        workspace=workspace,
        run_id=run_id,
        app=args.app,
        exit_code=exit_code,
        mlflow_run_id=mlflow_run_id,
        tracking_uri=tracking_uri,
        mlflow_port=args.mlflow_port,
        duration_s=duration_s,
    )

    print(f"\nSession {status.lower()} (exit {exit_code}, {duration_s:.1f}s)")
    print(f"Summary   : {summary_path}")
    if ui_proc is not None:
        print(f"MLflow UI still running (PID {ui_proc.pid}).")
        print(f"To stop:   kill {ui_proc.pid}")
        print(f"To relaunch: mlflow ui --backend-store-uri {tracking_uri} --port {args.mlflow_port}")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
