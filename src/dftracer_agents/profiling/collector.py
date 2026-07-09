"""OTLP receiver + control API for dftracer pipeline profiling.

One process, two HTTP surfaces on the same port:

``/v1/logs`` and ``/v1/metrics``
    Where Claude Code exports its telemetry. Set on the *agent* side::

        export CLAUDE_CODE_ENABLE_TELEMETRY=1
        export OTEL_LOGS_EXPORTER=otlp
        export OTEL_METRICS_EXPORTER=otlp
        export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
        export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318

    ``http/json`` is required, not merely preferred: it is what makes this
    receiver stdlib-only. Under ``grpc`` or ``http/protobuf`` the bodies are
    protobuf and nothing here will parse them.

``/control/*``
    Where the MCP tools drive the profile: bind a dftracer ``run_id``, open and
    close pipeline steps and their retry attempts, read the summary, stop.

Why a separate process rather than a thread inside the MCP server: the MCP server
supports ``--reload`` and re-execs itself whenever a source file changes. An
in-process collector would lose its accumulated profile on every edit, mid-run.

Everything is best-effort by construction. A malformed export, a dead MLflow, a
full disk — none of them may take down the receiver, because the pipeline it is
watching must not fail because its observer did.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

from .aggregate import Profile
from .mlflow_sink import (MlflowSink, default_tracking_uri, write_mlflow_pointer,
                          write_performance_report, write_step_files,
                          write_summary_json)
from .otlp import parse_logs, parse_metrics

#: Default OTLP/HTTP port. Matches the OpenTelemetry convention so
#: `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318` works with no extra config.
DEFAULT_PORT = 4318

#: How often the background thread mirrors the profile to disk and MLflow.
DEFAULT_FLUSH_S = 15.0


def _workspaces_root() -> Path:
    """Same resolution rule the session tools use, so both agree on the root."""
    return Path(os.environ.get("DFTRACER_WORKSPACES", "workspaces")).resolve()


class Collector:
    """Owns the profile, the sink, the raw event log, and the flush thread."""

    def __init__(self, mlflow_uri: str, experiment: str, flush_s: float,
                 ui_url: str = "") -> None:
        self.profile = Profile()
        self.sink = MlflowSink(mlflow_uri, experiment)
        self.ui_url = ui_url
        self.flush_s = flush_s
        self.lock = threading.RLock()
        self.events_seen = 0
        self.started = time.time()
        self._stop = threading.Event()
        self._raw: Optional[Any] = None
        self._raw_path: Optional[Path] = None
        self._open_raw()

    # -- paths -------------------------------------------------------------

    @property
    def perf_dir(self) -> Path:
        """The bound session's ``performance/`` dir, or a holding pen if unbound.

        Telemetry arrives before ``profile_bind`` can possibly be called (the
        agent is already burning tokens deciding to create the session), so an
        unbound collector must still have somewhere to write.
        """
        rid = self.profile.run_id
        if rid:
            return _workspaces_root() / rid / "performance"
        return _workspaces_root() / "_profile" / "unbound"

    def _open_raw(self) -> None:
        """(Re)open the raw event JSONL under the current ``performance/otlp/``."""
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        path = self.perf_dir / "otlp" / f"events-{day}.jsonl"
        if self._raw_path == path and self._raw:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if self._raw:
                self._raw.close()
            self._raw = path.open("a", encoding="utf-8")
            self._raw_path = path
        except OSError as exc:
            print(f"[collector] raw log unavailable: {exc}", file=sys.stderr)
            self._raw = None

    # -- ingestion ---------------------------------------------------------

    def ingest(self, records) -> int:
        n = 0
        with self.lock:
            for rec in records:
                self.profile.ingest(rec)
                if self._raw:
                    try:
                        self._raw.write(json.dumps(rec, default=str) + "\n")
                    except (OSError, TypeError):
                        pass
                n += 1
            self.events_seen += n
            if self._raw:
                try:
                    self._raw.flush()
                except OSError:
                    pass
        return n

    # -- control -----------------------------------------------------------

    def bind(self, run_id: str, tags: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Attach the profile to a dftracer session.

        Telemetry already ingested is kept — it is the orchestration cost that
        led up to session creation, and it belongs to this run. Rebinding to a
        different ``run_id`` starts a fresh profile, since the old one's steps
        and cost belong to the old session.
        """
        with self.lock:
            if self.profile.run_id and self.profile.run_id != run_id:
                self.flush()
                self.sink.close(self.profile, self.perf_dir)
                self.profile = Profile(run_id)
                self.sink = MlflowSink(self.sink.tracking_uri, self.sink.experiment)
            else:
                self.profile.run_id = run_id
            self._open_raw()
            # The flusher thread has almost certainly already opened an `unbound`
            # parent run for the pre-session telemetry. Adopt it under the real
            # run_id instead of stranding it.
            self.sink.start(self.profile, tags)
            self.sink.rename(self.profile.run_id, tags)
            self.flush()
            return self.status()

    def flush(self, mlflow: bool = True) -> Dict[str, Any]:
        """Mirror the profile to the session's ``performance/`` dir, and MLflow.

        Deliberately does NOT hold :attr:`lock`. Writing to MLflow is network I/O
        over a REST API; holding the lock across it would block the OTLP receiver
        threads from ingesting, and telemetry would pile up in Claude Code's
        exporter. :class:`~.aggregate.Profile` is internally synchronised, so the
        snapshot below is already consistent.

        Args:
            mlflow: When False, write only the local ``performance/`` files. The
                control endpoints use this: a ``profile_step_begin`` must not
                block an agent for the length of an MLflow round-trip. The
                background flusher then syncs MLflow on its own schedule.
        """
        snap = self.profile.snapshot()
        perf = self.perf_dir
        out: Dict[str, Any] = {"performance_dir": str(perf)}
        try:
            write_step_files(snap, perf)
            write_summary_json(snap, perf)
            if mlflow:
                out["mlflow"] = self.sink.flush(self.profile)
                write_mlflow_pointer(perf, self.sink, self.ui_url)
                self.profile.dirty = False
            url = ""
            pointer = perf / "mlflow.json"
            if pointer.exists():
                url = json.loads(pointer.read_text()).get("ui_url", "")
            write_performance_report(snap, perf, url)
        except Exception as exc:                      # disk full, races, ...
            out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    def status(self) -> Dict[str, Any]:
        with self.lock:
            snap = self.profile.snapshot()
            return {
                "run_id": self.profile.run_id or None,
                "bound": bool(self.profile.run_id),
                "uptime_s": round(time.time() - self.started, 1),
                "events_seen": self.events_seen,
                "performance_dir": str(self.perf_dir),
                "raw_log": str(self._raw_path) if self._raw_path else None,
                "mlflow": {"enabled": self.sink.enabled,
                           "tracking_uri": self.sink.tracking_uri,
                           "experiment": self.sink.experiment,
                           "parent_run_id": self.sink.parent_run_id,
                           "error": self.sink.error or None},
                "steps": [{k: s[k] for k in ("index", "key", "agent", "status", "tries",
                                             "retries", "failed_attempts", "exec_s",
                                             "cost_usd", "tokens_total")}
                          for s in snap["steps"]],
                "totals": snap["totals"],
                "attempts": snap["attempts"],
            }

    def stop(self) -> Dict[str, Any]:
        with self.lock:
            res = self.flush()
            self.sink.close(self.profile, self.perf_dir)
            if self._raw:
                self._raw.close()
                self._raw = None
        self._stop.set()
        return res

    # -- background --------------------------------------------------------

    def run_flusher(self) -> None:
        while not self._stop.wait(self.flush_s):
            if self.profile.dirty:
                self.flush()


class Handler(BaseHTTPRequestHandler):
    """Routes OTLP exports and control calls. One instance per request."""

    collector: Collector = None            # injected by :func:`serve`
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:   # noqa: A003
        pass                                          # do not spam stderr per export

    # -- helpers -----------------------------------------------------------

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if self.headers.get("Content-Encoding", "").lower() == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw or b"{}")

    def _reply(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:                          # noqa: N802
        c = self.collector
        if self.path.rstrip("/") == "/control/status":
            return self._reply(200, c.status())
        if self.path.rstrip("/") == "/control/summary":
            return self._reply(200, c.profile.snapshot())
        if self.path.rstrip("/") in ("/health", "/"):
            return self._reply(200, {"ok": True, "events_seen": c.events_seen})
        self._reply(404, {"error": f"no route {self.path}"})

    def do_POST(self) -> None:                         # noqa: N802
        c = self.collector
        path = self.path.rstrip("/")
        try:
            body = self._body()
        except Exception as exc:
            return self._reply(400, {"error": f"bad body: {exc}"})

        try:
            if path == "/v1/logs":
                n = c.ingest(parse_logs(body))
                # OTLP requires an ExportLogsServiceResponse; {} means "all accepted".
                return self._reply(200, {"partialSuccess": {}} if n >= 0 else {})
            if path == "/v1/metrics":
                c.ingest(parse_metrics(body))
                return self._reply(200, {"partialSuccess": {}})
            if path == "/v1/traces":
                return self._reply(200, {"partialSuccess": {}})   # accepted, ignored

            if path == "/control/bind":
                rid = (body.get("run_id") or "").strip()
                if not rid:
                    return self._reply(400, {"error": "run_id required"})
                return self._reply(200, c.bind(rid, body.get("tags")))

            if path == "/control/step_begin":
                key = (body.get("step") or "").strip()
                if not key:
                    return self._reply(400, {"error": "step required"})
                step = c.profile.step_begin(key, body.get("agent", ""),
                                            body.get("notes", ""))
                c.flush(mlflow=False)
                return self._reply(200, {"step": step.key, "attempt": step.current.number,
                                         "index": step.index, "tries": step.tries})

            if path == "/control/step_end":
                step = c.profile.step_end(body.get("status", "ok"),
                                          body.get("step", ""), body.get("error", ""))
                if step is None:
                    return self._reply(409, {"error": "no open step to end"})
                c.flush(mlflow=False)
                return self._reply(200, {"step": step.key, "status": step.status,
                                         "tries": step.tries, "retries": step.retries,
                                         "exec_s": round(step.exec_s, 3)})

            if path == "/control/flush":
                return self._reply(200, c.flush())

            if path == "/control/stop":
                res = c.stop()
                self._reply(200, res)
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return

            return self._reply(404, {"error": f"no route {path}"})
        except Exception as exc:
            return self._reply(500, {"error": f"{type(exc).__name__}: {exc}"})


def serve(host: str, port: int, collector: Collector) -> ThreadingHTTPServer:
    Handler.collector = collector
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def main() -> None:
    p = argparse.ArgumentParser(
        prog="dftracer-profile-collector",
        description="Receive Claude Code OTLP telemetry, attribute it to dftracer "
                    "pipeline steps, and mirror it to MLflow and the session's "
                    "performance/ directory.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--mlflow-uri", default="",
                   help="MLflow tracking URI (default: $MLFLOW_TRACKING_URI, else "
                        "sqlite:///<workspaces>/_mlflow/mlflow.db)")
    p.add_argument("--experiment", default=os.environ.get("MLFLOW_EXPERIMENT_NAME",
                                                          "dftracer-agents"))
    p.add_argument("--mlflow-ui", default=os.environ.get("MLFLOW_UI_URL", ""),
                   help="Base URL of the MLflow UI, used to build deep links in the report")
    p.add_argument("--flush-interval", type=float, default=DEFAULT_FLUSH_S)
    p.add_argument("--run-id", default="", help="Bind to this session immediately")
    p.add_argument("--pid-file", default="")
    args = p.parse_args()

    uri = args.mlflow_uri or default_tracking_uri(_workspaces_root())
    collector = Collector(uri, args.experiment, args.flush_interval, args.mlflow_ui)
    if args.run_id:
        collector.bind(args.run_id)

    if args.pid_file:
        Path(args.pid_file).parent.mkdir(parents=True, exist_ok=True)
        Path(args.pid_file).write_text(f"{os.getpid()}\n")

    httpd = serve(args.host, args.port, collector)
    threading.Thread(target=collector.run_flusher, daemon=True).start()

    def _shutdown(*_: Any) -> None:
        # A profile that is not flushed on SIGTERM is a profile that never
        # existed; the stack script stops this process on every teardown.
        collector.stop()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    print(f"[collector] OTLP  → http://{args.host}:{args.port}/v1/logs", file=sys.stderr)
    print(f"[collector] control → http://{args.host}:{args.port}/control/status", file=sys.stderr)
    print(f"[collector] mlflow  → {uri} (enabled={collector.sink.enabled})", file=sys.stderr)
    if collector.sink.error:
        print(f"[collector] mlflow note: {collector.sink.error}", file=sys.stderr)
    try:
        httpd.serve_forever()
    finally:
        collector.stop()


if __name__ == "__main__":
    main()
