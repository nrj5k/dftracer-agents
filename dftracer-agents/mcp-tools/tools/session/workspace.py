"""
Workspace helpers, subprocess runner, and response utilities.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Workspace helpers
# ---------------------------------------------------------------------------

def _workspaces_root() -> Path:
    env = os.environ.get("DFTRACER_WORKSPACES", "workspaces")
    root = Path(env)
    return root if root.is_absolute() else Path.cwd() / root


# Placeholder strings an LLM might pass instead of a real ID
_PLACEHOLDER_IDS = frozenset({
    "run_id", "RUN_ID", "RUN-ID", "<run_id>", "<RUN_ID>", "<RUN-ID>",
    "run-id", "runid", "RUNID", "{run_id}", "{RUN_ID}",
})


def _new_run_id(requested: Optional[str] = None) -> str:
    """Return requested ID if it looks real, otherwise generate a timestamp-based ID."""
    if requested and requested not in _PLACEHOLDER_IDS:
        return requested
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _derive_app_name(app: str) -> str:
    """
    Derive a safe, lowercase directory name from an app argument.

    Handles:
    - Bare names:           ``ior``        → ``ior``
    - Paths:                ``/path/to/ior`` → ``ior``
    - Git HTTPS URLs:       ``https://github.com/org/ior.git`` → ``ior``
    - Git SSH URLs:         ``git@github.com:org/ior.git``     → ``ior``
    - Names with suffixes:  ``ior.exe``    → ``ior``
    """
    # Strip .git suffix common in URLs
    name = app.rstrip("/")
    if name.endswith(".git"):
        name = name[:-4]
    # Take the last path component (works for paths and URLs)
    name = Path(name.split(":")[-1]).name  # handles ssh git@host:org/repo
    # Strip file extensions
    name = name.split(".")[0] if "." in name else name
    # Lowercase and replace non-alphanumeric runs with a single underscore
    name = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return name or "app"


def _ws(run_id: str) -> Path:
    return _workspaces_root() / run_id


def _state_path(run_id: str) -> Path:
    return _ws(run_id) / "session.json"


def _load_state(run_id: str) -> Dict[str, Any]:
    p = _state_path(run_id)
    return json.loads(p.read_text()) if p.exists() else {}


def _save_state(run_id: str, updates: Dict[str, Any]) -> None:
    p = _state_path(run_id)
    state = _load_state(run_id)
    state.update(updates)
    p.write_text(json.dumps(state, indent=2))


def _write_artifact_log(
    ws: Path,
    step_num: int,
    step_name: str,
    data: Dict[str, Any],
    run_id: str = "",
) -> Path:
    """Write a stage log to <workspace>/artifacts/<NN>_<step_name>.log."""
    artifacts = ws / "artifacts"
    artifacts.mkdir(exist_ok=True)
    log_path = artifacts / f"{step_num:02d}_{step_name}.log"
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        f"=== {step_name} ===",
        f"Timestamp : {ts}",
        f"Run ID    : {run_id or ws.name}",
        f"Step      : {step_num:02d}",
        "",
    ]
    for key, val in data.items():
        if isinstance(val, dict):
            lines.append(f"[{key}]")
            stdout = val.get("stdout", "")
            stderr = val.get("stderr", "")
            rc = val.get("returncode", val.get("success", ""))
            if stdout:
                lines.append(f"  stdout: {stdout}")
            if stderr:
                lines.append(f"  stderr: {stderr}")
            if rc != "":
                lines.append(f"  exit  : {rc}")
        else:
            lines.append(f"{key}: {val}")
    log_path.write_text("\n".join(lines) + "\n")
    return log_path


def _ok(msg: str, **extra) -> str:
    return json.dumps({"status": "ok", "message": msg, **extra}, indent=2)


def _err(msg: str, **extra) -> str:
    return json.dumps({"status": "error", "message": msg, **extra}, indent=2)


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------

def _run(
    cmd: List[str],
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: int = 600,
) -> Dict[str, Any]:
    merged = {**os.environ, **(env or {})}
    try:
        r = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=merged,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "returncode": r.returncode,
            "stdout": r.stdout.strip(),
            "stderr": r.stderr.strip(),
            "success": r.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "stdout": "", "stderr": "Command timed out", "success": False}
    except Exception as exc:
        return {"returncode": -1, "stdout": "", "stderr": str(exc), "success": False}
