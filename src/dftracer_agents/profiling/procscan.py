"""Discover the stack's real processes, not just the ones it remembers.

Pid files describe what the launcher *believes* is running. They go wrong in ways
that all look the same from the outside — a wedged port, a `start` that refuses:

* **stale pid file** — the process died; the file outlived it.
* **orphan** — a supervisor (``dftracer-mcp-server --reload``) was killed, but the
  child that actually binds the port survived, reparented to init.
* **untracked daemon** — someone ran ``dftracer-profile-collector`` by hand, or a
  previous launcher wrote its pid files into a different ``DFTRACER_WORKSPACES``.

Only ``/proc`` knows the truth, so this module reads it and reconciles it against
``workspaces/_stack/*.pid``. It is deliberately conservative: a process is a
candidate for cleanup only when its command line identifies it as one of *our*
daemons **and** it belongs to the current user. Nothing else is ever a target.

Used by ``dftracer_agents_stack status`` and ``dftracer_agents_stack clean``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

#: Service name -> substrings that identify it in a process command line. Both
#: spellings matter: the console script (`dftracer-mcp-server`) and the module
#: form the supervisor re-execs its child with (`-m dftracer_agents.mcp_server`).
_MARKERS: Dict[str, tuple] = {
    "collector": ("dftracer-profile-collector", "dftracer_agents.profiling.collector"),
    "mcp": ("dftracer-mcp-server", "dftracer_agents.mcp_server"),
    "mlflow": ("mlflow.server", "mlflow server", "gunicorn"),
}

#: Never a daemon, always a false positive: the launcher itself, this scanner,
#: and any shell that merely mentions a daemon's name on its command line.
_NEVER = ("dftracer_agents_stack", "profiling.procscan", "procscan.py")

_PORT_RE = re.compile(r"--port[= ](\d+)")


def _cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()


def _stat_fields(pid: int) -> Optional[Dict[str, int]]:
    """ppid and pgid from ``/proc/<pid>/stat``.

    The comm field is parenthesised and may contain spaces, so split after the
    final ``)`` rather than on whitespace.
    """
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    try:
        rest = text[text.rindex(")") + 2:].split()
        return {"ppid": int(rest[1]), "pgid": int(rest[2])}
    except (ValueError, IndexError):
        return None


def _owner(pid: int) -> Optional[int]:
    try:
        return os.stat(f"/proc/{pid}").st_uid
    except OSError:
        return None


def _classify_cmd(cmd: str) -> Optional[str]:
    if not cmd or any(n in cmd for n in _NEVER):
        return None
    for svc, markers in _MARKERS.items():
        if any(m in cmd for m in markers):
            return svc
    return None


def scan(workspaces: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every live process of ours that looks like a stack daemon.

    Args:
        workspaces: When given, an ``mlflow`` process only counts as ours if its
            command line references this workspaces root. A site-wide MLflow
            server sharing the node must never be classified as our orphan.

    Returns:
        Dicts with ``pid``, ``pgid``, ``ppid``, ``service``, ``port`` and ``cmd``.
    """
    me, uid = os.getpid(), os.getuid()
    found: List[Dict[str, Any]] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == me or _owner(pid) != uid:
            continue
        cmd = _cmdline(pid)
        svc = _classify_cmd(cmd)
        if svc is None:
            continue
        # gunicorn is how mlflow serves; only ours if it points at our store.
        if svc == "mlflow" and workspaces and workspaces not in cmd:
            continue
        st = _stat_fields(pid) or {"ppid": 0, "pgid": 0}
        m = _PORT_RE.search(cmd)
        found.append({"pid": pid, "pgid": st["pgid"], "ppid": st["ppid"],
                      "service": svc, "port": int(m.group(1)) if m else None,
                      "cmd": cmd[:160]})
    return sorted(found, key=lambda p: (p["service"], p["pid"]))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reconcile(run_dir: Path, workspaces: Optional[str] = None,
              ports: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """Compare the pid files against ``/proc``.

    Args:
        run_dir: The launcher's pid/sig directory.
        workspaces: Scopes ``mlflow`` matching to our own tracking store.
        ports: The configured port per service. A daemon of one of our services
            listening on the port we would use for it is **ours**, whatever the
            pid files say — a corrupted or deleted pid file must not turn our own
            daemon into an untouchable stranger that wedges the next ``start``.

    Returns:
        ``tracked``
            A pid file whose process is alive. Healthy bookkeeping.
        ``stale_pidfiles``
            A pid file whose process is gone. Safe to delete.
        ``orphans``
            Live daemons that are ours to reap: in the process group of a
            tracked-but-dead pid (a supervisor's surviving child), or sitting on
            the port we have configured for that service.
        ``untracked``
            Everything else — a hand-started daemon, an MCP client's own stdio
            server, a colleague's process. Reported, never killed without an
            explicit ``clean --untracked``.
    """
    procs = scan(workspaces)
    ports = ports or {}
    recorded: Dict[str, int] = {}
    for pf in sorted(run_dir.glob("*.pid")) if run_dir.is_dir() else []:
        try:
            recorded[pf.stem] = int(pf.read_text().strip())
        except (OSError, ValueError):
            recorded[pf.stem] = -1

    tracked, stale = {}, {}
    for svc, pid in recorded.items():
        (tracked if pid > 0 and _alive(pid) else stale)[svc] = pid

    live_pgids = set(tracked.values())
    dead_pgids = {p for p in stale.values() if p > 0}

    orphans, untracked = [], []
    for p in procs:
        if p["pid"] in tracked.values() or p["pgid"] in live_pgids:
            continue                       # tracked leader, or its own child
        if p["pgid"] in dead_pgids:
            p["why"] = "supervisor gone; child survived"
            orphans.append(p)
        elif p["port"] is not None and ports.get(p["service"]) == p["port"]:
            p["why"] = f"holds this stack's {p['service']} port {p['port']}"
            orphans.append(p)
        else:
            untracked.append(p)

    return {"tracked": tracked, "stale_pidfiles": stale,
            "orphans": orphans, "untracked": untracked, "all": procs}


def _kill(pid: int, sig: int) -> None:
    for target in (-pid, pid):             # process group first, then the pid
        try:
            os.kill(target, sig)
            return
        except (ProcessLookupError, PermissionError):
            continue


def clean(run_dir: Path, workspaces: Optional[str] = None,
          untracked: bool = False, ports: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """Remove stale pid files and reap orphans. Optionally kill untracked daemons.

    Orphans get SIGTERM before SIGKILL: the collector's handler does a final
    flush and closes its MLflow run, and a hard kill would lose the last step's
    cost accounting.
    """
    state = reconcile(run_dir, workspaces, ports)
    victims = list(state["orphans"]) + (list(state["untracked"]) if untracked else [])

    for p in victims:
        _kill(p["pid"], signal.SIGTERM)
    if victims:
        deadline = time.time() + 8
        while time.time() < deadline and any(_alive(p["pid"]) for p in victims):
            time.sleep(0.25)
    killed = []
    for p in victims:
        if _alive(p["pid"]):
            _kill(p["pid"], signal.SIGKILL)
        killed.append({"pid": p["pid"], "service": p["service"], "port": p["port"]})

    removed = []
    for svc in state["stale_pidfiles"]:
        for suffix in (".pid", ".sig"):
            f = run_dir / f"{svc}{suffix}"
            try:
                f.unlink(missing_ok=True)
                removed.append(str(f))
            except OSError:
                pass

    return {"killed": killed, "removed_pidfiles": removed,
            "left_untracked": [] if untracked else state["untracked"]}


def main() -> None:
    p = argparse.ArgumentParser(
        prog="python -m dftracer_agents.profiling.procscan",
        description="Reconcile the stack's pid files against the real processes.")
    p.add_argument("command", choices=["status", "clean"], nargs="?", default="status")
    p.add_argument("--run-dir", required=True, help="the stack's pid/sig directory")
    p.add_argument("--workspaces", default="", help="workspaces root, to scope mlflow")
    p.add_argument("--untracked", action="store_true",
                   help="also kill daemons this stack did not start")
    p.add_argument("--port", action="append", default=[], metavar="SVC=PORT",
                   help="configured port for a service; repeatable. A daemon of "
                        "that service on that port counts as ours.")
    p.add_argument("--json", action="store_true")
    a = p.parse_args()

    ports: Dict[str, int] = {}
    for spec in a.port:
        svc, _, num = spec.partition("=")
        if num.isdigit():
            ports[svc] = int(num)

    run_dir, ws = Path(a.run_dir), (a.workspaces or None)
    result = (clean(run_dir, ws, a.untracked, ports) if a.command == "clean"
              else reconcile(run_dir, ws, ports))

    if a.json:
        print(json.dumps(result, indent=2))
        return

    if a.command == "clean":
        for k in result["killed"]:
            print(f"  killed {k['service']:<10} pid {k['pid']}"
                  + (f" (port {k['port']})" if k["port"] else ""))
        for f in result["removed_pidfiles"]:
            print(f"  removed stale {Path(f).name}")
        for u in result["left_untracked"]:
            print(f"  left untracked {u['service']} pid {u['pid']} "
                  f"(pass --untracked to kill)")
        if not (result["killed"] or result["removed_pidfiles"] or result["left_untracked"]):
            print("  nothing to clean")
        return

    # Two audiences, two sections. Stale files and orphans are OUR mess and
    # `clean` fixes them. An untracked daemon may be a legitimately running
    # server owned by someone else — an MCP client's own stdio server, a
    # colleague's process on a shared node — so it is listed, not indicted.
    broken = list(result["stale_pidfiles"].items()) + result["orphans"]
    if broken:
        print("needs cleanup:")
        for svc, pid in result["stale_pidfiles"].items():
            print(f"  stale pid file  {svc:<10} pid {pid} is gone")
        for o in result["orphans"]:
            print(f"  orphan          {o['service']:<10} pid {o['pid']} "
                  f"— {o.get('why', 'ours')}")

    if result["untracked"]:
        if broken:
            print()
        print("running, not managed by this stack (left alone unless --untracked):")
        for u in result["untracked"]:
            print(f"  untracked       {u['service']:<10} pid {u['pid']}"
                  + (f" port {u['port']}" if u["port"] else " (no port; stdio)"))


if __name__ == "__main__":
    main()
