"""MCP tools for pipeline profiling — the agent-facing wrapper.

Agents do not talk to MLflow, and they do not talk OTLP. They call these tools,
which drive the ``dftracer-profile-collector`` over its control API. The
collector owns the profile; these tools own the vocabulary:

* :func:`profile_bind`      — attach the profile to a dftracer session.
* :func:`profile_step_begin`— open a step, or a retry attempt at one.
* :func:`profile_step_end`  — close it with an outcome.
* :func:`profile_status`    — cheap liveness + running totals.
* :func:`profile_report`    — flush and return the rendered report.

Every step's timing, attempt count, token usage and cost lands in the session at
``<workspace>/performance/`` — one JSON per step under ``steps/``, plus
``performance_report.md`` — and in MLflow as a nested run under the session's
parent run.

When the collector is not running, each tool returns ``status: "ok"`` with
``profiling: "disabled"``. That is deliberate. Profiling is observability, and an
agent must never abandon a pipeline step because the observer is down. Start the
whole stack with ``scripts/dftracer-stack.sh start`` to enable it.
"""
from __future__ import annotations

from fastmcp import FastMCP

from ....profiling import client
from .workspace import _err, _ok, _perf_dir, _ws


def _disabled(res: dict) -> bool:
    return bool(res.get("unreachable"))


def _off(msg: str) -> str:
    return _ok(msg, profiling="disabled",
               hint="start the collector with `scripts/dftracer-stack.sh start`")


def register_profiling_tools(mcp: FastMCP) -> None:
    """Register the ``profile_*`` tools on *mcp*."""

    @mcp.tool()
    def profile_bind(run_id: str, app: str = "", system: str = "") -> str:
        """Attach the pipeline profile to a dftracer session. Call once, right
        after ``session_create`` or ``session_status`` resolves the run.

        Telemetry captured before this call is kept and attributed to the
        session — it is the planning and routing cost that led up to it, and it
        is part of what the run cost. Binding also creates the MLflow parent run
        and the session's ``performance/`` directory.

        Args:
            run_id: The dftracer session id, e.g. ``"ior/20260708_140322"``.
            app: Application name, recorded as an MLflow tag.
            system: System name (``tuolumne``, ...), recorded as an MLflow tag.

        Returns:
            JSON with the bound ``run_id``, the ``performance_dir`` where the
            report will be written, and the MLflow parent run id.
        """
        if not _ws(run_id).exists():
            return _err(f"No session workspace for {run_id!r}", run_id=run_id)
        tags = {k: v for k, v in (("app", app), ("system", system)) if v}
        res = client.bind(run_id, tags)
        if _disabled(res):
            return _off(f"Collector not running; {run_id} will not be profiled")
        if res.get("error"):
            return _err(res["error"], run_id=run_id)
        return _ok(f"Profile bound to {run_id}", **res)

    @mcp.tool()
    def profile_step_begin(step: str, agent: str = "", notes: str = "") -> str:
        """Open a pipeline step, starting its execution clock.

        **Calling this again with the same ``step`` records a RETRY** — the step
        gains a second attempt rather than the profile gaining a duplicate step.
        That is how the report distinguishes "the annotator took 400 s" from "the
        annotator took 400 s across three tries, two of which failed lint".

        Any other step still open is closed as ``superseded``, so a step agent
        that forgets ``profile_step_end`` cannot absorb the next step's cost.

        Args:
            step: Stable step identity. Use the plan's own heading verbatim,
                e.g. ``"STEP 3: dftracer-annotator"`` — the same string on a
                retry, or the retry will be recorded as a new step.
            agent: Subagent executing the step, e.g. ``"dftracer-annotator"``.
            notes: Free text (the smoke command, the file count) stored as an
                MLflow param on the step's run.

        Returns:
            JSON with ``step``, ``index``, and ``attempt`` (1 on the first try).
        """
        if not step.strip():
            return _err("step is required")
        res = client.step_begin(step, agent, notes)
        if _disabled(res):
            return _off(f"Collector not running; step {step!r} not timed")
        if res.get("error"):
            return _err(res["error"], step=step)
        n = res.get("attempt", 1)
        label = f"attempt {n}" if n > 1 else "first attempt"
        return _ok(f"Step {step!r} started ({label})", **res)

    @mcp.tool()
    def profile_step_end(status: str = "ok", step: str = "", error: str = "") -> str:
        """Close a step's current attempt and stop its execution clock.

        Args:
            status: Outcome of this attempt. ``ok`` (also ``success``,
                ``completed``) counts as a success; anything else — ``failed``,
                ``timeout``, ``lint_error`` — counts as a failed attempt and is
                surfaced in the report's Rework section. A step that will be
                retried should be ended with the reason it failed, then reopened
                with ``profile_step_begin`` using the same ``step``.
            step: Which step to close. Defaults to the most recent open one.
            error: Short failure reason. Shown verbatim in the report.

        Returns:
            JSON with the step's final ``status``, ``tries``, ``retries`` and
            ``exec_s`` (seconds spent inside attempts).
        """
        res = client.step_end(status, step, error)
        if _disabled(res):
            return _off("Collector not running; nothing to close")
        if res.get("error"):
            return _err(res["error"], step=step, status=status)
        return _ok(f"Step {res['step']!r} ended: {res['status']} "
                   f"({res['tries']} tries, {res['exec_s']}s)", **res)

    @mcp.tool()
    def profile_status() -> str:
        """Running totals: cost, tokens, per-step timing, tries and retries.

        Cheap — served from the collector's memory, no MLflow round-trip. Use it
        mid-pipeline to see what the run has cost so far and which steps are
        burning it.

        Returns:
            JSON with ``totals``, ``attempts``, a compact ``steps`` list, and the
            MLflow parent run id.
        """
        res = client.status()
        if _disabled(res):
            return _off("Collector not running")
        if res.get("error"):
            return _err(res["error"])
        t = res.get("totals", {})
        return _ok(f"${t.get('cost_usd', 0):.4f} · {t.get('tokens_total', 0):,} tokens · "
                   f"{res.get('attempts', {}).get('steps', 0)} steps", **res)

    @mcp.tool()
    def profile_report(run_id: str = "", flush: bool = True) -> str:
        """Write the final performance report and return it.

        Flushes the profile, so the report reflects every step that has ended.
        Events still buffered inside Claude Code (up to
        ``OTEL_LOGS_EXPORT_INTERVAL``, 5 s by default) may not be included; call
        this a few seconds after the last step ends for an exact total.

        Writes ``<workspace>/performance/performance_report.md``,
        ``summary.json``, and ``steps/<n>-<step>.json``.

        Args:
            run_id: Session to report on. Defaults to the bound one; passing a
                different one only changes where this tool *looks* for the file,
                it does not rebind the collector.
            flush: Force a flush first. Set False to read the last written report
                without touching MLflow.

        Returns:
            JSON with ``report_path`` and the report's markdown in ``report``.
        """
        if flush:
            res = client.flush()
            if _disabled(res):
                return _off("Collector not running; no report written")
            if res.get("error"):
                return _err(res["error"])

        rid = run_id or (client.status().get("run_id") or "")
        if not rid:
            return _err("No run_id bound and none given; call profile_bind first")

        path = _perf_dir(rid) / "performance_report.md"
        if not path.exists():
            return _err(f"No report at {path}", run_id=rid)
        return _ok(f"Performance report for {rid}", run_id=rid,
                   report_path=str(path), report=path.read_text())
