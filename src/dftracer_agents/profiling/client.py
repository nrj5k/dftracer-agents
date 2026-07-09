"""Thin HTTP client for the profile collector's control API.

Used by the MCP tools, which run inside the MCP server process and therefore
cannot touch the collector's in-memory :class:`~.aggregate.Profile` directly.

Every call is short-timeout and failure-tolerant: if the collector is not
running, the pipeline must carry on unprofiled rather than stall. A missing
collector is reported, never raised.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

#: Where the collector listens. Matches `dftracer-stack.sh` and the OTLP default.
DEFAULT_ENDPOINT = "http://127.0.0.1:4318"

#: The collector answers from memory; anything slower than this is a hung process.
TIMEOUT_S = 5.0


def endpoint() -> str:
    return os.environ.get("DFTRACER_PROFILE_ENDPOINT", DEFAULT_ENDPOINT).rstrip("/")


def _request(path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{endpoint()}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"{}")
        except Exception:
            body = {}
        return {"error": body.get("error", f"HTTP {exc.code}"), "status_code": exc.code}
    except urllib.error.URLError as exc:
        return {"error": f"collector unreachable at {endpoint()}: {exc.reason}",
                "unreachable": True}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def bind(run_id: str, tags: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    return _request("/control/bind", {"run_id": run_id, "tags": tags or {}})


def step_begin(step: str, agent: str = "", notes: str = "") -> Dict[str, Any]:
    return _request("/control/step_begin", {"step": step, "agent": agent, "notes": notes})


def step_end(status: str = "ok", step: str = "", error: str = "") -> Dict[str, Any]:
    return _request("/control/step_end", {"status": status, "step": step, "error": error})


def flush() -> Dict[str, Any]:
    return _request("/control/flush", {})


def status() -> Dict[str, Any]:
    return _request("/control/status")


def summary() -> Dict[str, Any]:
    return _request("/control/summary")


def stop() -> Dict[str, Any]:
    return _request("/control/stop", {})
