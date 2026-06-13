"""
DFAnalyzer MCP service wrapper.

Docs:
https://dftracer.readthedocs.io/projects/analyzer/en/latest/getting-started.html
https://dftracer.readthedocs.io/projects/analyzer/en/latest/configuration.html
"""

from __future__ import annotations

import collections
import gzip
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP

from ...mcp_service_factory import MCPService, MCPServiceFactory


def _hydra_args(
    trace_path: Optional[str] = None,
    view_types: Optional[List[str]] = None,
    debug: bool = False,
    verbose: bool = False,
    analyzer: str = "dftracer",
    analyzer_preset: str = "posix",
    analyzer_checkpoint: Optional[bool] = None,
    analyzer_checkpoint_dir: Optional[str] = None,
    analyzer_time_approximate: Optional[bool] = None,
    analyzer_time_granularity: Optional[float] = None,
    analyzer_time_resolution: Optional[float] = None,
    output_format: str = "console",
    output_compact: Optional[bool] = None,
    output_root_only: Optional[bool] = None,
    output_name: Optional[str] = None,
    output_run_db_path: Optional[str] = None,
    cluster_type: str = "local",
    cluster_n_workers: Optional[int] = None,
    cluster_memory_limit: Optional[str] = None,
    cluster_processes: Optional[int] = None,
    cluster_cores: Optional[int] = None,
    cluster_memory: Optional[str] = None,
) -> List[str]:
    """Build the ``dfanalyzer`` Hydra-style override command.

    dfanalyzer uses Hydra for configuration, so all overrides are passed as
    positional ``key=value`` arguments, not ``--key value`` flags.

    Example:
        dfanalyzer analyzer/preset=dlio trace_path=/data view_types=[time_range]
    """
    cmd: List[str] = ["dfanalyzer"]

    # Core parameters
    if trace_path:
        cmd.append(f"trace_path={trace_path}")

    if view_types is not None:
        # Hydra list syntax: view_types=[file_name,proc_name]
        cmd.append(f"view_types=[{','.join(view_types)}]")

    if debug:
        cmd.append("debug=true")
    if verbose:
        cmd.append("verbose=true")

    # Analyzer selection and preset
    if analyzer != "dftracer":
        cmd.append(f"analyzer={analyzer}")
    cmd.append(f"analyzer/preset={analyzer_preset}")

    # Analyzer sub-options
    if analyzer_checkpoint is not None:
        cmd.append(f"analyzer.checkpoint={'true' if analyzer_checkpoint else 'false'}")
    if analyzer_checkpoint_dir is not None:
        cmd.append(f"analyzer.checkpoint_dir={analyzer_checkpoint_dir}")
    if analyzer_time_approximate is not None:
        cmd.append(f"analyzer.time_approximate={'true' if analyzer_time_approximate else 'false'}")
    if analyzer_time_granularity is not None:
        cmd.append(f"analyzer.time_granularity={analyzer_time_granularity}")
    if analyzer_time_resolution is not None:
        cmd.append(f"analyzer.time_resolution={analyzer_time_resolution}")

    # Output configuration
    cmd.append(f"output={output_format}")
    if output_compact is not None:
        cmd.append(f"output.compact={'true' if output_compact else 'false'}")
    if output_root_only is not None:
        cmd.append(f"output.root_only={'true' if output_root_only else 'false'}")
    if output_name and output_name.strip():
        cmd.append(f"output.name={output_name}")
    if output_run_db_path and output_run_db_path.strip():
        cmd.append(f"output.run_db_path={output_run_db_path}")

    # Cluster configuration
    cmd.append(f"cluster={cluster_type}")
    if cluster_n_workers is not None:
        cmd.append(f"cluster.n_workers={cluster_n_workers}")
    if cluster_memory_limit is not None:
        cmd.append(f"cluster.memory_limit={cluster_memory_limit}")
    if cluster_processes is not None:
        cmd.append(f"cluster.processes={cluster_processes}")
    if cluster_cores is not None:
        cmd.append(f"cluster.cores={cluster_cores}")
    if cluster_memory is not None:
        cmd.append(f"cluster.memory={cluster_memory}")

    return cmd


def _summarize_trace_python(trace_path: str, max_files: int = 50) -> str:
    """Pure-Python trace summarizer for dftracer .pfw/.pfw.gz directories.

    Reads Chrome Trace Event JSON directly — no native C++ required.
    Returns a human-readable summary of POSIX I/O activity.
    """
    p = Path(trace_path)
    if not p.exists():
        return f"Error: trace_path does not exist: {trace_path}"

    # Collect all trace files
    pfw_files = sorted(p.glob("*.pfw.gz")) + sorted(p.glob("*.pfw"))
    if not pfw_files:
        return f"No .pfw or .pfw.gz files found in {trace_path}"

    # Limit to first N files if the trace is very large
    sampled = pfw_files[:max_files]
    truncated = len(pfw_files) > max_files

    func_counts: collections.Counter = collections.Counter()
    bytes_read = 0
    bytes_written = 0
    pids: set = set()
    tids: set = set()
    ts_min = float("inf")
    ts_max = float("-inf")
    total_events = 0
    parse_errors = 0

    for fpath in sampled:
        opener = gzip.open if str(fpath).endswith(".gz") else open
        try:
            with opener(fpath, "rt") as fh:
                for line in fh:
                    line = line.strip().rstrip(",")
                    if not line or line in ("[", "]"):
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        parse_errors += 1
                        continue

                    if ev.get("ph") != "X":
                        continue

                    total_events += 1
                    name = ev.get("name", "")
                    func_counts[name] += 1
                    pids.add(ev.get("pid"))
                    tids.add(ev.get("tid"))

                    ts = ev.get("ts", 0)
                    dur = ev.get("dur", 0)
                    if ts < ts_min:
                        ts_min = ts
                    if ts + dur > ts_max:
                        ts_max = ts + dur

                    args = ev.get("args", {})
                    ret = args.get("ret", 0) or 0
                    if name in ("read", "fread", "pread", "pread64", "readv", "preadv"):
                        if ret > 0:
                            bytes_read += ret
                    elif name in ("write", "fwrite", "pwrite", "pwrite64", "writev", "pwritev"):
                        if ret > 0:
                            bytes_written += ret
        except Exception as exc:
            return f"Error reading {fpath.name}: {exc}"

    def _fmt_bytes(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} TB"

    duration_s = (ts_max - ts_min) / 1e6 if ts_min < ts_max else 0

    lines = [
        f"DFTracer Trace Summary: {trace_path}",
        "=" * 60,
        f"  Trace files : {len(pfw_files)} total"
        + (f" (summarized first {max_files})" if truncated else ""),
        f"  Total events: {total_events:,}",
        f"  Processes   : {len(pids)}",
        f"  Threads     : {len(tids)}",
        f"  Duration    : {duration_s:.3f}s",
        f"  Bytes read  : {_fmt_bytes(bytes_read)}",
        f"  Bytes written: {_fmt_bytes(bytes_written)}",
        "",
        "Top I/O operations:",
    ]
    for fn, cnt in func_counts.most_common(15):
        lines.append(f"  {fn:<20} {cnt:>8,}")
    if parse_errors:
        lines.append(f"\n  (parse errors: {parse_errors})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# AI/ML category and function-name signatures (from dftracer ai_common.py)
# https://github.com/llnl/pydftracer/blob/develop/python/dftracer/python/ai_common.py
# ---------------------------------------------------------------------------

# Trace event categories (cat field) that only appear in AI/ML workloads
_AIML_CATEGORIES: frozenset[str] = frozenset({
    "COMPUTE",
    "DATA",
    "DATALOADER",
    "COMM",
    "DEVICE",
    "CHECKPOINT",
    "PIPELINE",
})

# Function names that only appear in AI/ML workloads (from ProfileCategory enums)
_AIML_FUNCTION_NAMES: frozenset[str] = frozenset({
    # Compute
    "forward", "backward", "step",
    # Data
    "preprocess", "item",
    # DataLoader
    "fetch",
    # Pipeline
    "epoch", "train", "evaluate", "test",
    # Checkpoint
    "capture", "restart",
    # Communication (collective ops added by dftracer AI hooks)
    "send", "receive", "barrier", "bcast", "reduce",
    "all_reduce", "gather", "all_gather", "scatter",
    "reduce_scatter", "all_to_all",
    # Device
    "transfer",
})


def _detect_preset_python(
    trace_path: str,
    max_files: int = 20,
) -> dict:
    """Scan a dftracer trace directory to determine the best dfanalyzer preset.

    Strategy (pure Python, no native C++ required):
      1. Try ``dftracer_stats -d <path> --report categories --json`` for a fast
         native category scan.  Falls back to direct .pfw.gz parsing if that
         binary crashes or is unavailable.
      2. Check the resulting category names and function names against the
         known AI/ML signatures from dftracer's ai_common.py.
      3. Return a dict with:
           preset          "dlio" | "posix"
           aiml_detected   bool
           categories      {cat: count, …}
           aiml_categories [cat, …]          — categories that are AI/ML signals
           aiml_functions  [name, …]         — function names that are AI/ML signals
           source          "dftracer_stats" | "python_scan"
           reasoning       str               — human-readable explanation
    """
    p = Path(trace_path)
    if not p.exists():
        return {
            "preset": "posix",
            "aiml_detected": False,
            "categories": {},
            "aiml_categories": [],
            "aiml_functions": [],
            "source": "none",
            "reasoning": f"trace_path does not exist: {trace_path}",
        }

    # ── 1. Try dftracer_stats native scan ────────────────────────────────────
    cats_from_native: dict | None = None
    source = "python_scan"
    try:
        r = subprocess.run(
            ["dftracer_stats", "-d", trace_path,
             "--report", "categories", "--json", "--no-auto-index"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip():
            raw = json.loads(r.stdout)
            # dftracer_stats JSON shape: {"categories": {"CAT": count, …}} or {"CAT": count}
            if isinstance(raw, dict):
                inner = raw.get("categories", raw)
                if isinstance(inner, dict) and all(isinstance(v, (int, float)) for v in inner.values()):
                    cats_from_native = inner
                    source = "dftracer_stats"
    except Exception:
        pass  # binary not available or crashed — fall through to Python scan

    # ── 2. Pure-Python category + function-name scan ─────────────────────────
    if cats_from_native is None:
        pfw_files = sorted(p.glob("*.pfw.gz")) + sorted(p.glob("*.pfw"))
        sampled = pfw_files[:max_files]

        cat_counts: collections.Counter = collections.Counter()
        func_counts: collections.Counter = collections.Counter()

        for fpath in sampled:
            opener = gzip.open if str(fpath).endswith(".gz") else open
            try:
                with opener(fpath, "rt") as fh:
                    for line in fh:
                        line = line.strip().rstrip(",")
                        if not line or line in ("[", "]"):
                            continue
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if ev.get("ph") != "X":
                            continue
                        cat_counts[ev.get("cat", "")] += 1
                        func_counts[ev.get("name", "")] += 1
            except Exception:
                continue

        cats_from_native = dict(cat_counts)
        # Also check function names for AI/ML signals
        matched_funcs = sorted(_AIML_FUNCTION_NAMES & set(func_counts))
    else:
        matched_funcs = []

    # ── 3. Classify ───────────────────────────────────────────────────────────
    all_cats = set(cats_from_native)
    matched_cats = sorted(_AIML_CATEGORIES & all_cats)

    aiml_detected = bool(matched_cats or matched_funcs)
    preset = "dlio" if aiml_detected else "posix"

    # Build reasoning
    if aiml_detected:
        signals = []
        if matched_cats:
            signals.append(f"AI/ML categories present: {', '.join(matched_cats)}")
        if matched_funcs:
            signals.append(f"AI/ML function names present: {', '.join(matched_funcs)}")
        reasoning = (
            "AI/ML workload detected — recommend analyzer_preset='dlio'. "
            + "; ".join(signals) + ". "
            "The dlio preset is optimised for deep learning I/O patterns "
            "(data loading pipelines, checkpoint I/O, collective communication)."
        )
    else:
        non_aiml = sorted(all_cats - {"", "dftracer"})
        reasoning = (
            "No AI/ML signals found — recommend analyzer_preset='posix'. "
            f"Categories observed: {', '.join(non_aiml) or 'POSIX/STDIO only'}. "
            "The posix preset covers general HPC and scientific I/O workloads."
        )

    return {
        "preset": preset,
        "aiml_detected": aiml_detected,
        "categories": cats_from_native,
        "aiml_categories": matched_cats,
        "aiml_functions": matched_funcs,
        "source": source,
        "reasoning": reasoning,
    }


# ---------------------------------------------------------------------------
# Exploratory query helpers
# ---------------------------------------------------------------------------

def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _eval_filter(expr: str, ev: Dict[str, Any]) -> bool:
    """Evaluate a simple dftracer-stats-style filter expression against one event.

    Supports the same DSL as dftracer_stats --query:
      cat == "POSIX"
      dur > 1000
      name in ("read", "write")
      cat == "POSIX" and dur > 500
    The event dict is exposed as local variables (cat, name, dur, ts, pid, tid).
    Returns True when the expression matches, True if expr is empty/None.
    """
    if not expr:
        return True
    # Expose event fields as locals; use a restricted namespace
    ns: Dict[str, Any] = {
        "cat": ev.get("cat", ""),
        "name": ev.get("name", ""),
        "dur": ev.get("dur", 0),
        "ts": ev.get("ts", 0),
        "pid": ev.get("pid", 0),
        "tid": ev.get("tid", 0),
    }
    ns.update(ev.get("args", {}))
    try:
        return bool(eval(expr, {"__builtins__": {}}, ns))  # noqa: S307
    except Exception:
        return False


def _scan_events(
    trace_path: str,
    max_files: int,
    filter_expr: str,
) -> List[Dict[str, Any]]:
    """Read .pfw/.pfw.gz files and return matching complete-events ('ph'=='X')."""
    p = Path(trace_path)
    pfw_files = sorted(p.glob("*.pfw.gz")) + sorted(p.glob("*.pfw"))
    sampled = pfw_files[:max_files]
    events: List[Dict[str, Any]] = []
    for fpath in sampled:
        opener = gzip.open if str(fpath).endswith(".gz") else open
        try:
            with opener(fpath, "rt") as fh:
                for line in fh:
                    line = line.strip().rstrip(",")
                    if not line or line in ("[", "]"):
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("ph") != "X":
                        continue
                    if _eval_filter(filter_expr, ev):
                        events.append(ev)
        except Exception:
            continue
    return events


def _view_file_name(events: List[Dict[str, Any]], top_n: int) -> str:
    """Group events by file hash (fhash); show top files by operation count."""
    rows: Dict[int, Dict[str, Any]] = {}
    for ev in events:
        fhash = ev.get("args", {}).get("fhash", 0)
        if fhash not in rows:
            rows[fhash] = {"ops": 0, "dur_us": 0, "read_b": 0, "write_b": 0, "funcs": collections.Counter()}
        r = rows[fhash]
        r["ops"] += 1
        r["dur_us"] += ev.get("dur", 0)
        name = ev.get("name", "")
        r["funcs"][name] += 1
        ret = ev.get("args", {}).get("ret", 0) or 0
        if name in ("read", "fread", "pread", "pread64", "readv"):
            r["read_b"] += max(ret, 0)
        elif name in ("write", "fwrite", "pwrite", "pwrite64", "writev"):
            r["write_b"] += max(ret, 0)

    top = sorted(rows.items(), key=lambda x: -x[1]["ops"])[:top_n]
    lines = [f"{'fhash':<12} {'ops':>8} {'dur':>10} {'read':>10} {'write':>10}  top_ops"]
    lines.append("-" * 75)
    for fhash, r in top:
        top_ops = ", ".join(f"{n}×{c}" for n, c in r["funcs"].most_common(3))
        lines.append(
            f"{fhash:<12} {r['ops']:>8,} {_fmt_bytes(r['dur_us']/1e6):>10}"
            f" {_fmt_bytes(r['read_b']):>10} {_fmt_bytes(r['write_b']):>10}  {top_ops}"
        )
    lines.append(f"\n{len(rows)} unique files; showing top {min(top_n, len(rows))}")
    return "\n".join(lines)


def _view_proc_name(events: List[Dict[str, Any]], top_n: int) -> str:
    """Group events by PID; show per-process I/O summary."""
    rows: Dict[int, Dict[str, Any]] = {}
    for ev in events:
        pid = ev.get("pid", 0)
        if pid not in rows:
            rows[pid] = {"ops": 0, "dur_us": 0, "read_b": 0, "write_b": 0, "funcs": collections.Counter()}
        r = rows[pid]
        r["ops"] += 1
        r["dur_us"] += ev.get("dur", 0)
        name = ev.get("name", "")
        r["funcs"][name] += 1
        ret = ev.get("args", {}).get("ret", 0) or 0
        if name in ("read", "fread", "pread", "pread64", "readv"):
            r["read_b"] += max(ret, 0)
        elif name in ("write", "fwrite", "pwrite", "pwrite64", "writev"):
            r["write_b"] += max(ret, 0)

    top = sorted(rows.items(), key=lambda x: -x[1]["ops"])[:top_n]
    lines = [f"{'pid':<10} {'ops':>8} {'dur':>10} {'read':>10} {'write':>10}  top_ops"]
    lines.append("-" * 70)
    for pid, r in top:
        top_ops = ", ".join(f"{n}×{c}" for n, c in r["funcs"].most_common(3))
        lines.append(
            f"{pid:<10} {r['ops']:>8,} {_fmt_bytes(r['dur_us']/1e6):>10}"
            f" {_fmt_bytes(r['read_b']):>10} {_fmt_bytes(r['write_b']):>10}  {top_ops}"
        )
    lines.append(f"\n{len(rows)} processes; showing top {min(top_n, len(rows))}")
    return "\n".join(lines)


def _view_time_range(events: List[Dict[str, Any]], top_n: int) -> str:
    """Bucket events into 1-second time windows; show I/O rate over time."""
    if not events:
        return "(no events)"
    ts_vals = [ev["ts"] for ev in events if "ts" in ev]
    ts_min, ts_max = min(ts_vals), max(ts_vals)
    duration_us = max(ts_max - ts_min, 1)
    # Aim for ~top_n buckets, each at least 1 second wide
    bucket_us = max(1_000_000, duration_us // max(top_n, 1))
    n_buckets = max(1, int(duration_us / bucket_us) + 1)

    ops_per_bucket: List[int] = [0] * n_buckets
    read_per_bucket: List[int] = [0] * n_buckets
    write_per_bucket: List[int] = [0] * n_buckets

    for ev in events:
        ts = ev.get("ts", ts_min)
        b = min(int((ts - ts_min) / bucket_us), n_buckets - 1)
        ops_per_bucket[b] += 1
        name = ev.get("name", "")
        ret = ev.get("args", {}).get("ret", 0) or 0
        if name in ("read", "fread", "pread", "pread64", "readv"):
            read_per_bucket[b] += max(ret, 0)
        elif name in ("write", "fwrite", "pwrite", "pwrite64", "writev"):
            write_per_bucket[b] += max(ret, 0)

    bucket_s = bucket_us / 1e6
    lines = [f"{'time_s':>10} {'ops':>8} {'read/s':>10} {'write/s':>10}"]
    lines.append("-" * 44)
    for i in range(n_buckets):
        t_s = ts_min / 1e6 + i * bucket_s
        lines.append(
            f"{t_s:>10.1f} {ops_per_bucket[i]:>8,}"
            f" {_fmt_bytes(read_per_bucket[i]/bucket_s):>10}"
            f" {_fmt_bytes(write_per_bucket[i]/bucket_s):>10}"
        )
    lines.append(f"\n{n_buckets} buckets × {bucket_s:.1f}s")
    return "\n".join(lines)


def _view_raw(events: List[Dict[str, Any]], top_n: int) -> str:
    """Return the first top_n raw events as formatted JSON."""
    lines = []
    for ev in events[:top_n]:
        lines.append(json.dumps(ev))
    lines.append(f"\n(showing {min(top_n, len(events))} of {len(events)} matching events)")
    return "\n".join(lines)


def _query_via_stats_cli(
    trace_path: str,
    report: str,
    filter_expr: str,
    group_by: List[str],
    top_n: int,
) -> Optional[str]:
    """Try dftracer_stats CLI and return its stdout, or None if it crashes."""
    cmd = ["dftracer_stats", "-d", trace_path, "--report", report, "--json"]
    if filter_expr:
        cmd += ["--query", filter_expr]
    for g in group_by:
        cmd += ["--group-by", g]
    if top_n > 0:
        cmd += ["--top-n", str(top_n)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout
    except Exception:
        pass
    return None


def _query_via_python_api(
    trace_path: str,
    view_types: List[str],
    analyzer_preset: str,
    top_n: int,
) -> Optional[str]:
    """Try the dftracer.analyzer Python API (init_with_hydra → analyze_trace).

    Returns formatted output, or None if it crashes (native C++ SIGSEGV).
    """
    try:
        import tempfile
        from dftracer.analyzer import init_with_hydra  # type: ignore[import]

        run_dir = tempfile.mkdtemp(prefix="dftracer_query_")
        dfa = init_with_hydra(hydra_overrides=[
            "analyzer=dftracer",
            f"analyzer/preset={analyzer_preset}",
            "analyzer.checkpoint=False",
            f"trace_path={trace_path}",
            f"hydra.run.dir={run_dir}",
        ])
        result = dfa.analyze_trace(view_types=view_types)

        lines: List[str] = []
        for vt in view_types:
            try:
                df = result.get_flat_view(vt)
                lines.append(f"\n── view: {vt} ──")
                lines.append(df.head(top_n).to_string())
            except Exception as exc:
                lines.append(f"\n── view: {vt} — {exc} ──")

        if hasattr(result, "layers") and result.layers:
            lines.append(f"\nAvailable layers: {result.layers}")

        return "\n".join(lines) if lines else None
    except Exception:
        return None


def _query_trace(
    trace_path: str,
    view_type: str,
    filter_expr: str,
    analyzer_preset: str,
    top_n: int,
    max_files: int,
) -> str:
    """Run an exploratory query on a dftracer trace.

    Tries three backends in order:
      1. dftracer.analyzer Python API (init_with_hydra → get_flat_view)
      2. dftracer_stats CLI          (--report --query --json)
      3. Pure-Python groupby         (direct .pfw.gz parsing, always works)
    """
    p = Path(trace_path)
    if not p.exists():
        return f"Error: trace_path does not exist: {trace_path}"

    vt_map = {
        "file_name":  "file_name",
        "proc_name":  "proc_name",
        "time_range": "time_range",
        "raw":        "raw",
    }
    if view_type not in vt_map:
        return (
            f"Unknown view_type {view_type!r}. "
            "Choose from: file_name, proc_name, time_range, raw"
        )

    header = [
        f"Query: {trace_path}",
        f"  view_type      : {view_type}",
        f"  filter_expr    : {filter_expr or '(none)'}",
        f"  analyzer_preset: {analyzer_preset}",
        "=" * 60,
    ]

    # ── 1. Python API ─────────────────────────────────────────────────────────
    if view_type != "raw":
        api_result = _query_via_python_api(
            trace_path, [view_type], analyzer_preset, top_n
        )
        if api_result:
            return "\n".join(header) + "\nsource: dftracer.analyzer Python API\n" + api_result

    # ── 2. dftracer_stats CLI ─────────────────────────────────────────────────
    stats_report_map = {
        "file_name":  "detailed",
        "proc_name":  "pid_tids",
        "time_range": "time_range",
        "raw":        "detailed",
    }
    group_by_map = {
        "file_name":  ["name", "fhash"],
        "proc_name":  ["pid", "name"],
        "time_range": [],
        "raw":        ["name"],
    }
    cli_out = _query_via_stats_cli(
        trace_path,
        stats_report_map[view_type],
        filter_expr,
        group_by_map[view_type],
        top_n,
    )
    if cli_out:
        try:
            parsed = json.loads(cli_out)
            formatted = json.dumps(parsed, indent=2)[:8000]
        except json.JSONDecodeError:
            formatted = cli_out[:8000]
        return "\n".join(header) + "\nsource: dftracer_stats CLI\n" + formatted

    # ── 3. Pure-Python fallback ───────────────────────────────────────────────
    events = _scan_events(trace_path, max_files, filter_expr)
    if not events:
        return "\n".join(header) + "\n(no matching events found)"

    view_fn = {
        "file_name":  _view_file_name,
        "proc_name":  _view_proc_name,
        "time_range": _view_time_range,
        "raw":        _view_raw,
    }[view_type]

    body = view_fn(events, top_n)
    note = f"\nsource: python_scan ({len(events):,} events from ≤{max_files} files)"
    return "\n".join(header) + note + "\n\n" + body


class DFAnalyzerService(MCPService):
    """MCP tools wrapping the ``dfanalyzer`` executable."""

    def __init__(self) -> None:
        self.analyzer_subservice = FastMCP("DFAnalyzer")
        self._register_analyze()
        self._register_list_presets()
        self._register_summarize_trace()
        self._register_detect_preset()
        self._register_query()

    def _register_analyze(self) -> None:
        @self.analyzer_subservice.tool()
        def analyze(
            trace_path: str,
            view_types: Optional[List[str]] = None,
            debug: bool = False,
            verbose: bool = False,
            analyzer: str = "dftracer",
            analyzer_preset: str = "posix",
            analyzer_checkpoint: Optional[bool] = None,
            analyzer_checkpoint_dir: Optional[str] = None,
            analyzer_time_approximate: Optional[bool] = None,
            analyzer_time_granularity: Optional[float] = None,
            analyzer_time_resolution: Optional[float] = None,
            output_format: str = "console",
            output_compact: Optional[bool] = None,
            output_root_only: Optional[bool] = None,
            output_name: Optional[str] = None,
            output_run_db_path: Optional[str] = None,
            cluster_type: str = "local",
            cluster_n_workers: Optional[int] = None,
            cluster_memory_limit: Optional[str] = None,
            cluster_processes: Optional[int] = None,
            cluster_cores: Optional[int] = None,
            cluster_memory: Optional[str] = None,
        ) -> str:
            """Analyze an I/O trace using dfanalyzer and return a performance summary.

            Use this tool when the user wants to analyze I/O traces captured by dftracer,
            darshan, or recorder. It produces breakdowns of I/O performance by file, process,
            time range, or other dimensions. Call list_presets first if you are unsure which
            analyzer or preset to use.

            For a first look at any trace, only trace_path is required. The defaults work
            well for general POSIX I/O workloads. For deep learning workloads (e.g. DLIO),
            set analyzer_preset="dlio". For non-dftracer traces, set the analyzer accordingly.

            QUICK START EXAMPLES:
              # Minimal — just inspect a trace directory
              analyze(trace_path="/path/to/trace")

              # Deep-learning workload with DLIO preset, save results to SQLite
              analyze(
                  trace_path="/path/to/trace",
                  analyzer_preset="dlio",
                  output_format="sqlite",
                  output_run_db_path="/results/run.db",
              )

              # Darshan trace on a Slurm cluster, 2 nodes × 16 cores
              analyze(
                  trace_path="/path/to/darshan.darshan",
                  analyzer="darshan",
                  cluster_type="slurm",
                  cluster_processes=2,
                  cluster_cores=16,
                  cluster_memory="64GB",
              )

            PARAMETER GUIDE:

            --- WHAT TO ANALYZE ---
            trace_path (required):
                Absolute path to the I/O trace data. For dftracer this is a directory;
                for darshan it is a single .darshan file; for recorder it is a directory.

            analyzer (default "dftracer"):
                Which trace format to read.
                  "dftracer"  — traces produced by the dftracer library (default)
                  "darshan"   — Darshan HPC I/O characterization logs
                  "recorder"  — Recorder I/O tracing tool output

            analyzer_preset (default "posix"):
                Selects the analysis lens. Determines which I/O layers and metrics are
                extracted. Must match the workload type:
                  "posix" — general POSIX file I/O (read/write/open/close/seek/stat)
                  "dlio"  — deep-learning I/O patterns (PyTorch/TensorFlow data pipelines)
                If you are unsure, start with "posix".

            --- WHAT VIEWS TO SHOW ---
            view_types (default ["file_name", "proc_name", "time_range"]):
                Controls which breakdown dimensions appear in the output. Each entry
                produces a separate aggregation table.
                  "file_name"  — per-file I/O statistics (bandwidth, ops, size)
                  "proc_name"  — per-process I/O statistics
                  "time_range" — I/O activity bucketed over time
                Pass a subset to focus the output, e.g. ["file_name"] for file-only view.

            --- TIMING PRECISION ---
            analyzer_time_approximate (default true):
                Use fast approximate timestamps. Set false only if exact timestamps are
                needed — it significantly increases analysis time.

            analyzer_time_granularity (float, seconds):
                Width of time buckets used in time_range views.
                Defaults: dftracer=1.0, darshan=1.0, recorder=1.0.
                Decrease to see finer-grained time slices; increase to reduce noise.

            analyzer_time_resolution (float, nanoseconds):
                Minimum resolvable event duration. Events shorter than this are collapsed.
                Defaults: dftracer=1e6, darshan=1e3, recorder=1e7.
                Lower values capture brief events; higher values reduce clutter.

            --- CHECKPOINTING (for large or long-running traces) ---
            analyzer_checkpoint (default true):
                Save intermediate analysis state so a failed or interrupted run can
                resume without reprocessing from scratch. Recommended for large traces.

            analyzer_checkpoint_dir:
                Where to write checkpoint files.
                Default: <hydra_output_dir>/checkpoints.
                Override when you need checkpoints on a shared filesystem.

            --- OUTPUT FORMAT ---
            output_format (default "console"):
                Where to send the analysis results:
                  "console" — print tables to stdout (good for interactive inspection)
                  "csv"     — write CSV files per view (good for downstream processing)
                  "sqlite"  — write all views into a SQLite database (good for querying)

            output_compact (default false):
                Condense output tables to fewer rows. Useful for very wide result sets.

            output_root_only (default true):
                In MPI/multi-process runs, suppress output from non-root ranks. Leave
                true unless you specifically need per-rank output.

            output_name:
                Label attached to the output artifact (file prefix or DB table prefix).
                Useful when saving multiple analyses to the same directory or database.

            output_run_db_path:
                Path to the SQLite database file. ONLY used when output_format="sqlite".
                The file is created if it does not exist.
                Example: "/results/analysis.db"

            --- CLUSTER (for distributed / HPC analysis) ---
            cluster_type (default "local"):
                Dask cluster backend to use for parallel analysis:
                  "local" — use local CPU cores (default, works everywhere)
                  "slurm" — submit Dask workers as Slurm jobs
                  "lsf"   — submit Dask workers as LSF jobs
                  "pbs"   — submit Dask workers as PBS jobs
                Use "local" unless you are on an HPC system and the trace is too large
                for a single node.

            -- local cluster options --
            cluster_n_workers (int):
                Number of Dask worker processes. Default: number of CPU cores.
                Reduce if memory is constrained.

            cluster_memory_limit (str, e.g. "4GB"):
                Memory cap per worker process. Default: unlimited.
                Set this to avoid OOM when traces are large.

            -- HPC cluster options (slurm / lsf / pbs only) --
            cluster_processes (int, default 1):
                Number of compute nodes to request.

            cluster_cores (int, default 16):
                CPU cores per node/job.

            cluster_memory (str, e.g. "64GB"):
                Total memory per node/job to request from the scheduler.

            --- DEBUGGING ---
            debug (default false):
                Enable debug-level logging. Use when the tool returns an error and you
                need stack traces or internal state to diagnose it.

            verbose (default false):
                Print extra progress information during analysis. Useful for monitoring
                long-running analyses without full debug output.
            """
            cmd = _hydra_args(
                trace_path=trace_path,
                view_types=view_types,
                debug=debug,
                verbose=verbose,
                analyzer=analyzer,
                analyzer_preset=analyzer_preset,
                analyzer_checkpoint=analyzer_checkpoint,
                analyzer_checkpoint_dir=analyzer_checkpoint_dir,
                analyzer_time_approximate=analyzer_time_approximate,
                analyzer_time_granularity=analyzer_time_granularity,
                analyzer_time_resolution=analyzer_time_resolution,
                output_format=output_format,
                output_compact=output_compact,
                output_root_only=output_root_only,
                output_name=output_name,
                output_run_db_path=output_run_db_path,
                cluster_type=cluster_type,
                cluster_n_workers=cluster_n_workers,
                cluster_memory_limit=cluster_memory_limit,
                cluster_processes=cluster_processes,
                cluster_cores=cluster_cores,
                cluster_memory=cluster_memory,
            )
            env = os.environ.copy()
            if not env.get("USER"):
                env["USER"] = os.environ.get("LOGNAME") or "root"
            result = subprocess.run(cmd, capture_output=True, text=True, env=env)
            if result.returncode != 0:
                return (
                    f"dfanalyzer exited with code {result.returncode}\n"
                    f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
                )
            return result.stdout or "(no output)"

    def _register_list_presets(self) -> None:
        @self.analyzer_subservice.tool()
        def list_presets() -> str:
            """Return all valid values for every dfanalyzer configuration option.

            Call this tool before calling analyze when you are unsure which
            analyzer, preset, output format, cluster type, or view types to use.
            It returns the full option matrix so you can pick the right combination
            without guessing. The output is human-readable and can be shown directly
            to the user or used to answer questions like "what presets are available?"
            """
            lines = [
                "dfAnalyzer Presets and Configuration",
                "====================================================",
                "",
                "ANALYZER PRESETS (analyzer/preset=)",
                "- posix (default)",
                "- dlio",
                "",
                "ANALYZER TYPES (analyzer=)",
                "- dftracer (default)",
                "- darshan",
                "- recorder",
                "",
                "CLUSTER TYPES (cluster=)",
                "- local (default)",
                "- slurm",
                "- lsf",
                "- pbs",
                "",
                "OUTPUT FORMATS (output=)",
                "- console (default)",
                "- csv",
                "- sqlite",
                "",
                "VIEW TYPES (view_types=[...])",
                "- file_name (default)",
                "- proc_name (default)",
                "- time_range (default)",
                "",
                "TIME RESOLUTION DEFAULTS",
                "- dftracer: time_granularity=1s, time_resolution=1e6 ns",
                "- darshan:  time_granularity=1s, time_resolution=1e3 ns",
                "- recorder: time_granularity=1s, time_resolution=1e7 ns",
            ]
            return "\n".join(lines)

    def _register_summarize_trace(self) -> None:
        @self.analyzer_subservice.tool()
        def summarize_trace(trace_path: str, max_files: int = 50) -> str:
            """Summarize a dftracer I/O trace directory using pure Python (no native dependencies).

            Use this tool when analyze() fails with a native crash (segfault/SIGSEGV) or
            when you need a quick overview of a trace without running the full dfanalyzer
            pipeline.  It reads .pfw.gz / .pfw trace files directly from the directory
            and computes basic I/O statistics.

            Produces:
              - File count, process count, thread count
              - Total duration of the captured trace
              - Total bytes read and written (from ret field of read/write events)
              - Top 15 I/O operations by call count (open, read, write, close, stat, …)

            WHEN TO USE:
              - First pass / sanity check before running analyze()
              - Platform limitations prevent native dfanalyzer from running
              - You want a fast, dependency-free overview

            RECOMMENDED WORKFLOW:
              1. detect_preset(trace_path)    — auto-select posix vs dlio preset
              2. summarize_trace(trace_path)  — fast Python overview
              3. analyze(trace_path, analyzer_preset=<detected preset>)

            Args:
                trace_path: Directory containing .pfw.gz or .pfw trace files.
                max_files:  Maximum number of files to process (default 50).
                            Increase to 0 to process all files (may be slow for large traces).
            """
            n = max_files if max_files > 0 else 10_000
            return _summarize_trace_python(trace_path, max_files=n)

    def _register_detect_preset(self) -> None:  # noqa: PLR0912
        @self.analyzer_subservice.tool()
        def detect_preset(trace_path: str, max_files: int = 20) -> str:  # noqa: F841
            """Detect the best dfanalyzer preset for a trace by scanning its event categories.

            Inspects the trace to identify whether it was produced by an AI/ML workload
            (deep learning training, DLIO benchmark, PyTorch/TensorFlow data pipelines)
            or a traditional HPC/POSIX workload, then recommends the appropriate
            dfanalyzer preset.

            HOW IT WORKS:
              1. Runs ``dftracer_stats --report categories --json`` for a fast native
                 category scan (uses pre-built index if available; builds one if not).
              2. Falls back to reading .pfw.gz files directly in pure Python if the
                 native binary is unavailable or crashes.
              3. Matches the observed event categories and function names against the
                 AI/ML signatures defined in dftracer's ai_common.py:
                   - AI/ML categories: COMPUTE, DATA, DATALOADER, COMM, DEVICE,
                     CHECKPOINT, PIPELINE
                   - AI/ML function names: forward, backward, epoch, train, fetch,
                     preprocess, item, all_reduce, barrier, …
              4. Returns a structured recommendation.

            RECOMMENDED WORKFLOW — always call this before analyze():
              1. detect_preset(trace_path)                   ← run this first
              2. analyze(trace_path, analyzer_preset=<result["preset"]>)

            PRESETS:
              "posix"  General POSIX/HPC I/O (open, read, write, stat, …).
                       Best for scientific simulations, HPC benchmarks.
              "dlio"   Deep learning I/O patterns (data loaders, checkpoints,
                       collective communication).  Best for PyTorch/TensorFlow
                       training runs and DLIO benchmark traces.

            Returns a JSON-formatted report with:
              preset          — "dlio" or "posix"
              aiml_detected   — true if AI/ML signals were found
              categories      — all event categories observed and their counts
              aiml_categories — subset of categories that are AI/ML signals
              aiml_functions  — function names that are AI/ML signals
              source          — "dftracer_stats" or "python_scan"
              reasoning       — plain-English explanation of the decision

            Args:
                trace_path: Directory containing .pfw.gz or .pfw trace files.
                max_files:  Files to scan in the Python fallback path (default 20).
                            Ignored when dftracer_stats is used (it reads the index).
            """
            result = _detect_preset_python(trace_path, max_files=max_files)
            lines = [
                f"Preset Detection: {trace_path}",
                "=" * 60,
                f"  Recommended preset : {result['preset']}",
                f"  AI/ML detected     : {result['aiml_detected']}",
                f"  Detection source   : {result['source']}",
                "",
                "Event categories observed:",
            ]
            for cat, count in sorted(result["categories"].items(), key=lambda x: -x[1]):
                marker = " ← AI/ML" if cat in _AIML_CATEGORIES else ""
                lines.append(f"  {cat:<20} {count:>8,}{marker}")
            if result["aiml_categories"]:
                lines.append("")
                lines.append(f"AI/ML category signals : {', '.join(result['aiml_categories'])}")
            if result["aiml_functions"]:
                lines.append(f"AI/ML function signals : {', '.join(result['aiml_functions'])}")
            lines += [
                "",
                "Reasoning:",
                f"  {result['reasoning']}",
                "",
                f"Next step:  analyze(trace_path=\"{trace_path}\","
                f" analyzer_preset=\"{result['preset']}\")",
            ]
            return "\n".join(lines)

    def _register_query(self) -> None:  # noqa: F841
        @self.analyzer_subservice.tool()
        def query(trace_path: str,  # noqa: F841
            view_type: str = "file_name",
            filter_expr: str = "",
            analyzer_preset: str = "posix",
            top_n: int = 20,
            max_files: int = 30,
        ) -> str:
            """Run an exploratory query on a dftracer trace and return a grouped view.

            This is the interactive analysis tool — use it to drill into a trace after
            calling detect_preset() and summarize_trace().  It supports four view types
            and an optional filter expression, and tries three execution backends in
            order of capability:

              1. dftracer.analyzer Python API  — full analysis via init_with_hydra();
                 returns get_flat_view() DataFrames.  Best results, requires native C++.
              2. dftracer_stats CLI            — fast indexed query via --report/--query.
                 Requires the binary and a pre-built .dftindex.
              3. Pure-Python groupby           — reads .pfw.gz files directly.
                 Always works; no native dependencies.

            RECOMMENDED WORKFLOW:
              1. detect_preset(trace_path)                      — pick posix vs dlio
              2. summarize_trace(trace_path)                     — high-level overview
              3. query(trace_path, view_type="proc_name")        — per-process breakdown
              4. query(trace_path, view_type="file_name",
                       filter_expr='cat == "POSIX" and dur > 1000')  — hot files
              5. analyze(trace_path, analyzer_preset=<preset>)  — full pipeline

            VIEW TYPES:
              "file_name"   Per-file aggregation (ops, bytes read/written, duration).
                            Groups by file hash (fhash). Use to find hot files.
              "proc_name"   Per-process aggregation (ops, bytes, top functions).
                            Groups by PID. Use to compare MPI rank I/O behaviour.
              "time_range"  I/O activity bucketed into ~top_n time windows.
                            Shows ops/s, read/s, write/s over the trace duration.
              "raw"         Return the first top_n matching raw events as JSON.
                            Use to inspect individual event fields and args.

            FILTER EXPRESSION (filter_expr):
              A Python-style predicate evaluated against each event's fields:
                cat      — event category string  (e.g. "POSIX", "STDIO", "COMPUTE")
                name     — function name          (e.g. "read", "write", "open")
                dur      — duration in µs         (e.g. dur > 1000)
                ts       — timestamp in µs
                pid, tid — process / thread ID

              Examples:
                'cat == "POSIX"'
                'name in ("read", "write") and dur > 500'
                'cat == "COMPUTE"'                          # AI/ML workload events
                'dur > 10000'                               # slow calls only (>10ms)

              Leave empty to include all events.

            Args:
                trace_path:      Directory containing .pfw.gz or .pfw trace files.
                view_type:       Aggregation dimension: file_name | proc_name |
                                 time_range | raw  (default: file_name).
                filter_expr:     Optional event filter expression (default: all events).
                analyzer_preset: "posix" or "dlio" — passed to the Python API backend
                                 when it is used (default: posix).
                                 Run detect_preset() first to auto-select.
                top_n:           Maximum rows / events to return (default: 20).
                max_files:       Files to scan in the pure-Python fallback (default: 30).
            """
            return _query_trace(
                trace_path=trace_path,
                view_type=view_type,
                filter_expr=filter_expr,
                analyzer_preset=analyzer_preset,
                top_n=top_n,
                max_files=max_files,
            )

    def execute(self, data: dict) -> Optional[str]:
        """Compatibility entrypoint required by the MCPService abstract base."""
        cmd_string = " ".join(_hydra_args(**{k: v for k, v in data.items() if k != "command"}))
        return f"Would run: {cmd_string}"

    @property
    def name(self) -> str:
        return "dfanalyzer"


MCPServiceFactory.register("dfanalyzer", DFAnalyzerService())


def run() -> None:
    """Run the standalone DFAnalyzer MCP server."""
    service = DFAnalyzerService()
    service.analyzer_subservice.run()


if __name__ == "__main__":
    run()
