"""
Optional MLflow experiment tracking for the dftracer pipeline.

Gracefully no-ops when mlflow is not installed.

Lifecycle across two pipeline calls:

  Call 1 — session_run_pipeline (steps 1-8, then PAUSE):
      tracker = MLflowTracker(app_name, rid, ws / "logs" / "mlruns")
      ui_url  = tracker.start()          # starts server + creates run
      ...log each step...
      run_id  = tracker.end_run_only()   # ends run but leaves server up
      _save_state(rid, {"mlflow": tracker.state_dict()})

  Call 2 — session_run_pipeline(annotation_confirmed=True):
      tracker = MLflowTracker.from_session_state(state)
      ui_url  = tracker.resume()         # reconnects to existing run; no new server
      ...log each step...
      tracker.end_run_only()             # end run, leave server for user to browse

  User calls session_end(run_id):
      tracker = MLflowTracker.from_session_state(state)
      tracker.stop()                     # kills server process
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

_DEFAULT_PORT = 5000


class MLflowTracker:
    """
    Thin wrapper around mlflow for per-pipeline-run tracking.
    All methods are silent no-ops if mlflow is not installed.
    """

    def __init__(
        self,
        app_name: str,
        run_name: str,
        logs_dir: Path,
        port: int = _DEFAULT_PORT,
        _server_pid: Optional[int] = None,
        _run_id: Optional[str] = None,
    ) -> None:
        self._app_name = app_name
        self._run_name = run_name
        self._logs_dir = Path(logs_dir)
        self._port = port
        self._server_pid = _server_pid      # PID of background mlflow ui process
        self._saved_run_id = _run_id        # MLflow run_id to resume
        self._active_run: Any = None        # mlflow.ActiveRun
        self._mlflow: Any = None

    # ── construction helpers ──────────────────────────────────────────────────

    @classmethod
    def from_session_state(cls, state: Dict[str, Any]) -> "MLflowTracker":
        """Reconstruct tracker from dict saved in session.json."""
        ml = state.get("mlflow_state", {})
        return cls(
            app_name=ml.get("app_name", "dftracer"),
            run_name=ml.get("run_name", ""),
            logs_dir=Path(ml.get("logs_dir", "/tmp/mlruns")),
            port=int(ml.get("port", _DEFAULT_PORT)),
            _server_pid=ml.get("server_pid"),
            _run_id=ml.get("run_id"),
        )

    def state_dict(self) -> Dict[str, Any]:
        """Return dict suitable for saving into session.json."""
        return {
            "app_name": self._app_name,
            "run_name": self._run_name,
            "logs_dir": str(self._logs_dir),
            "port": self._port,
            "server_pid": self._server_pid,
            "run_id": self._saved_run_id or (
                self._active_run.info.run_id if self._active_run else None
            ),
        }

    # ── lifecycle ─────────────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        return self._active_run is not None and self._mlflow is not None

    @property
    def active_run_id(self) -> Optional[str]:
        if self._active_run:
            return self._active_run.info.run_id
        return self._saved_run_id

    @property
    def ui_url(self) -> Optional[str]:
        if self._server_pid:
            return f"http://localhost:{self._port}"
        return None

    def _import_mlflow(self) -> bool:
        if self._mlflow is not None:
            return True
        try:
            import mlflow  # type: ignore[import]
            self._mlflow = mlflow
            return True
        except ImportError:
            return False

    def start(self) -> Optional[str]:
        """
        Start MLflow UI server and open a new tracking run.
        Returns the UI URL, or None if mlflow is not available.
        """
        if not self._import_mlflow():
            return None

        self._logs_dir.mkdir(parents=True, exist_ok=True)
        tracking_uri = self._logs_dir.as_uri()

        # Start background UI server.
        proc = subprocess.Popen(
            [
                "mlflow", "ui",
                "--backend-store-uri", tracking_uri,
                "--port", str(self._port),
                "--host", "0.0.0.0",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._server_pid = proc.pid
        time.sleep(1.5)  # let server bind the port

        self._mlflow.set_tracking_uri(tracking_uri)
        self._mlflow.set_experiment(self._app_name)
        self._active_run = self._mlflow.start_run(run_name=self._run_name)
        self._saved_run_id = self._active_run.info.run_id

        return f"http://localhost:{self._port}"

    def resume(self) -> Optional[str]:
        """
        Reconnect to an existing run (server is already running).
        Call this in the annotation_confirmed=True code path.
        Returns the UI URL, or None if mlflow is not available.
        """
        if not self._import_mlflow():
            return None
        if not self._saved_run_id:
            return None

        tracking_uri = self._logs_dir.as_uri()
        self._mlflow.set_tracking_uri(tracking_uri)
        self._mlflow.set_experiment(self._app_name)
        self._active_run = self._mlflow.start_run(run_id=self._saved_run_id)

        return f"http://localhost:{self._port}" if self._server_pid else None

    def end_run_only(self, status: str = "RUNNING") -> Optional[str]:
        """
        End the active MLflow run (e.g. at PAUSE) but leave the UI server running
        so the user can browse results.  Returns the run_id for later resuming.
        """
        run_id = self.active_run_id
        if self._active_run is not None and self._mlflow is not None:
            try:
                self._mlflow.end_run(status=status)
            except Exception:
                pass
            self._active_run = None
        return run_id

    def stop(self, status: str = "FINISHED") -> None:
        """End the active run AND kill the UI server process."""
        self.end_run_only(status=status)

        if self._server_pid:
            try:
                os.kill(self._server_pid, signal.SIGTERM)
                # Give it a moment to exit cleanly.
                for _ in range(10):
                    time.sleep(0.3)
                    try:
                        os.kill(self._server_pid, 0)  # check if still alive
                    except ProcessLookupError:
                        break
                else:
                    os.kill(self._server_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # already dead
            except Exception:
                pass
            self._server_pid = None

    # ── logging helpers ───────────────────────────────────────────────────────

    def log_params(self, **kwargs: Any) -> None:
        if not self.active:
            return
        try:
            self._mlflow.log_params({k: str(v)[:500] for k, v in kwargs.items() if v is not None})
        except Exception:
            pass

    def set_tags(self, **kwargs: Any) -> None:
        if not self.active:
            return
        try:
            self._mlflow.set_tags({k: str(v)[:500] for k, v in kwargs.items() if v is not None})
        except Exception:
            pass

    def log_step(
        self,
        step: int,
        name: str,
        success: bool,
        duration: float = 0.0,
        **extra_metrics: Any,
    ) -> None:
        if not self.active:
            return
        try:
            self._mlflow.log_metric(
                f"step_{step:02d}_{name}_ok", 1 if success else 0, step=step
            )
            if duration > 0:
                self._mlflow.log_metric(
                    f"step_{step:02d}_{name}_sec", round(duration, 2), step=step
                )
            for k, v in extra_metrics.items():
                if isinstance(v, (int, float)):
                    self._mlflow.log_metric(f"step_{step:02d}_{k}", v, step=step)
        except Exception:
            pass

    def log_artifact_file(self, path: Path) -> None:
        if not self.active or not path.exists():
            return
        try:
            self._mlflow.log_artifact(str(path))
        except Exception:
            pass
