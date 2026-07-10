"""Attribute Claude Code telemetry to dftracer pipeline steps and attempts.

The pipeline is a sequence of agent-driven steps (``STEP 3: dftracer-annotator``),
and a step may be *attempted* more than once — an annotator that fails lint, a
build that fails and is retried after a fix. Claude Code's telemetry knows
nothing about either notion; it knows ``agent.name`` and wall-clock time. This
module supplies the missing link.

The unit of attribution is therefore the **attempt**, a half-open time interval
``[start_ns, end_ns)``. A step owns an ordered list of attempts. Every telemetry
event is folded into whichever attempt's interval contains its timestamp, and
into that attempt's step.

Timestamp attribution, not "whatever step is open right now", is the whole point.
Claude Code buffers log events and flushes them every ``OTEL_LOGS_EXPORT_INTERVAL``
milliseconds (default 5000), so an ``api_request`` from step 3 routinely lands on
the wire after step 4 has begun. Bucketing on arrival would silently move that
cost one step to the right.

Two clocks per step, and they differ for a reason:

* ``exec_s`` — the sum of the attempts' durations. The time actually spent
  working on the step. This is what a retry inflates.
* ``wall_s`` — first attempt's start to last attempt's end. Includes the gaps
  where the pipeline was off doing something else between retries.

Events whose timestamp falls in no attempt — the planning turns before the first
step, or anything after the last step closed — are attributed to the synthetic
``main`` step, so totals always reconcile to the whole session.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .otlp import _as_num

#: Bucket for telemetry that belongs to no explicit pipeline step (the main
#: thread's own planning and routing turns). Never dropped: a pipeline's cost is
#: the sum of its steps PLUS the orchestration that dispatched them.
MAIN_STEP = "main"

#: Terminal attempt statuses that count as success. Anything else is a failure.
_OK_STATUS = frozenset({"ok", "success", "succeeded", "completed", "done"})

#: Statuses meaning "this attempt ended so a retry can start", not a real failure.
_SUPERSEDED = "superseded"


def _now_ns() -> int:
    return time.time_ns()


def is_ok(status: str) -> bool:
    return status.strip().lower() in _OK_STATUS


@dataclass
class Attempt:
    """One try at a step. The interval that owns a slice of telemetry."""

    number: int
    start_ns: int = field(default_factory=_now_ns)
    end_ns: Optional[int] = None
    status: str = "running"
    error: str = ""

    def contains(self, ts_ns: int) -> bool:
        if ts_ns < self.start_ns:
            return False
        return self.end_ns is None or ts_ns < self.end_ns

    @property
    def duration_s(self) -> float:
        return ((self.end_ns or _now_ns()) - self.start_ns) / 1e9

    def as_dict(self) -> Dict[str, Any]:
        return {"number": self.number, "status": self.status,
                "duration_s": round(self.duration_s, 3), "error": self.error,
                "start_ns": self.start_ns, "end_ns": self.end_ns}


@dataclass
class Agg:
    """Rolling totals for one step. Every field is additive across events."""

    tokens_input: int = 0
    tokens_output: int = 0
    tokens_cache_read: int = 0
    tokens_cache_creation: int = 0
    cost_usd: float = 0.0
    api_calls: int = 0
    api_errors: int = 0
    api_duration_ms: float = 0.0
    tool_calls: int = 0
    tool_failures: int = 0
    tool_duration_ms: float = 0.0
    mcp_tool_calls: int = 0
    compactions: int = 0
    prompts: int = 0
    #: tool name -> {"calls": int, "ms": float, "failures": int}
    tools: Dict[str, Dict[str, float]] = field(default_factory=dict)
    #: model id -> api call count. A step may span models (haiku probe, opus fix).
    models: Dict[str, int] = field(default_factory=dict)
    #: subagent name -> api call count, from `agent.name`.
    agents: Dict[str, int] = field(default_factory=dict)
    skills: Dict[str, int] = field(default_factory=dict)

    @property
    def tokens_total(self) -> int:
        """Fresh input + output + both cache paths. Cache reads bill at ~0.1x but
        they are still context the agent chose to carry, so they are surfaced."""
        return (self.tokens_input + self.tokens_output
                + self.tokens_cache_read + self.tokens_cache_creation)

    def as_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d["tokens_total"] = self.tokens_total
        return d


@dataclass
class Step:
    """A named pipeline step and every attempt made at it."""

    key: str
    index: int
    agent: str = ""
    notes: str = ""
    attempts: List[Attempt] = field(default_factory=list)
    agg: Agg = field(default_factory=Agg)

    def __post_init__(self) -> None:
        if not self.attempts:
            self.attempts.append(Attempt(number=1))

    @property
    def current(self) -> Attempt:
        return self.attempts[-1]

    @property
    def open(self) -> bool:
        return self.current.end_ns is None

    def contains(self, ts_ns: int) -> bool:
        return any(a.contains(ts_ns) for a in self.attempts)

    @property
    def status(self) -> str:
        """The step's outcome: its last attempt's status."""
        return self.current.status

    @property
    def tries(self) -> int:
        return len(self.attempts)

    @property
    def retries(self) -> int:
        """Attempts beyond the first. ``tries - 1``, floored at zero."""
        return max(self.tries - 1, 0)

    @property
    def failed_attempts(self) -> int:
        return sum(1 for a in self.attempts
                   if a.end_ns is not None and a.status != _SUPERSEDED and not is_ok(a.status))

    @property
    def successful_attempts(self) -> int:
        return sum(1 for a in self.attempts if is_ok(a.status))

    @property
    def exec_s(self) -> float:
        """Time actually spent inside attempts. Retries inflate this."""
        return sum(a.duration_s for a in self.attempts)

    @property
    def wall_s(self) -> float:
        """First start to last end, gaps between retries included."""
        last = self.attempts[-1]
        return ((last.end_ns or _now_ns()) - self.attempts[0].start_ns) / 1e9

    def as_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key, "index": self.index, "agent": self.agent,
            "notes": self.notes, "status": self.status,
            "tries": self.tries, "retries": self.retries,
            "failed_attempts": self.failed_attempts,
            "successful_attempts": self.successful_attempts,
            "exec_s": round(self.exec_s, 3), "wall_s": round(self.wall_s, 3),
            "attempts": [a.as_dict() for a in self.attempts],
            **self.agg.as_dict(),
        }


def _bump(table: Dict[str, int], key: Any) -> None:
    if key:
        table[str(key)] = table.get(str(key), 0) + 1


class Profile:
    """Thread-safe, in-memory profile of one dftracer pipeline run.

    The OTLP receiver thread calls :meth:`ingest`; the control API thread calls
    :meth:`step_begin` / :meth:`step_end` / :meth:`snapshot`. One re-entrant lock
    guards all of it — contention is irrelevant at a few hundred events a minute.
    """

    def __init__(self, run_id: str = "") -> None:
        self.run_id = run_id
        self.started_ns = _now_ns()
        self.lock = threading.RLock()
        self.steps: List[Step] = [Step(key=MAIN_STEP, index=0)]
        #: Independent totals straight off the OTLP counters, used to detect
        #: dropped events rather than to report cost.
        self.metric_totals: Dict[str, float] = {}
        self.session_ids: set = set()
        self.dirty = True

    def _by_key(self, key: str) -> Optional[Step]:
        for s in self.steps:
            if s.key == key:
                return s
        return None

    # -- lifecycle ---------------------------------------------------------

    def step_begin(self, key: str, agent: str = "", notes: str = "",
                   retry: bool = True) -> Step:
        """Open a step, or open a fresh attempt at one that already exists.

        Re-calling with a known *key* is how a retry is recorded: the existing
        step gains an attempt rather than the profile gaining a duplicate step.

        Any other step left open is closed as ``superseded`` — an agent that
        never called ``step_end`` should not swallow the next step's telemetry.

        Args:
            key: Step identity, e.g. ``"STEP 3: dftracer-annotator"``.
            agent: Subagent responsible, for the report and MLflow tags.
            notes: Free text stored as an MLflow param.
            retry: When False, a repeat *key* raises instead of adding an attempt.
                Use to catch accidental duplicate step names.

        Returns:
            The step, with a running attempt at ``step.current``.
        """
        with self.lock:
            for s in self.steps:
                if s.key != MAIN_STEP and s.key != key and s.open:
                    s.current.end_ns = _now_ns()
                    s.current.status = _SUPERSEDED

            step = self._by_key(key)
            if step is None:
                step = Step(key=key, index=len(self.steps), agent=agent, notes=notes)
                self.steps.append(step)
            else:
                if not retry:
                    raise ValueError(f"step {key!r} already exists (retry=False)")
                if step.open:                     # begin called twice, no end
                    step.current.end_ns = _now_ns()
                    step.current.status = _SUPERSEDED
                step.attempts.append(Attempt(number=step.tries + 1))
                step.agent = agent or step.agent
                step.notes = notes or step.notes
            self.dirty = True
            return step

    def step_end(self, status: str = "ok", key: str = "", error: str = "") -> Optional[Step]:
        """Close the named step's current attempt, or the most recent open one."""
        with self.lock:
            for s in reversed(self.steps):
                if s.key == MAIN_STEP or not s.open:
                    continue
                if key and s.key != key:
                    continue
                s.current.end_ns = _now_ns()
                s.current.status = status
                s.current.error = error[:2000]
                self.dirty = True
                return s
            return None

    def _step_for(self, ts_ns: int) -> Step:
        # Reverse scan: the newest matching interval wins, and the common case
        # (telemetry for the step that is running) hits on the first probe.
        for s in reversed(self.steps):
            if s.key != MAIN_STEP and s.contains(ts_ns):
                return s
        return self.steps[0]

    # -- ingestion ---------------------------------------------------------

    def ingest(self, rec: Dict[str, Any]) -> None:
        """Fold one parsed OTLP record into the right step's totals."""
        with self.lock:
            self.dirty = True
            sid = rec.get("session.id")
            if sid:
                self.session_ids.add(str(sid))

            if rec["kind"] == "metric":
                # Keyed by metric + token type so `token.usage{type=output}` and
                # `{type=input}` do not collapse into one meaningless number.
                suffix = rec.get("type")
                mkey = f"{rec['name']}.{suffix}" if suffix else rec["name"]
                self.metric_totals[mkey] = self.metric_totals.get(mkey, 0.0) + rec["value"]
                return

            agg = self._step_for(rec["ts_ns"]).agg
            name = rec["name"]

            if name == "claude_code.api_request":
                agg.api_calls += 1
                agg.cost_usd += _as_num(rec.get("cost_usd"))
                agg.api_duration_ms += _as_num(rec.get("duration_ms"))
                agg.tokens_input += int(_as_num(rec.get("input_tokens")))
                agg.tokens_output += int(_as_num(rec.get("output_tokens")))
                agg.tokens_cache_read += int(_as_num(rec.get("cache_read_tokens")))
                agg.tokens_cache_creation += int(_as_num(rec.get("cache_creation_tokens")))
                _bump(agg.models, rec.get("model"))
                _bump(agg.agents, rec.get("agent.name") or rec.get("query_source"))
                _bump(agg.skills, rec.get("skill.name"))

            elif name in ("claude_code.api_error", "claude_code.api_retries_exhausted"):
                agg.api_errors += 1
                agg.api_duration_ms += _as_num(rec.get("duration_ms"))

            elif name == "claude_code.tool_result":
                tool = str(rec.get("tool_name") or "unknown")
                ms = _as_num(rec.get("duration_ms"))
                # OTLP renders these booleans as the strings "true"/"false".
                ok = str(rec.get("success", "true")).lower() == "true"
                agg.tool_calls += 1
                agg.tool_duration_ms += ms
                if not ok:
                    agg.tool_failures += 1
                if tool.startswith("mcp__"):
                    agg.mcp_tool_calls += 1
                t = agg.tools.setdefault(tool, {"calls": 0, "ms": 0.0, "failures": 0})
                t["calls"] += 1
                t["ms"] += ms
                t["failures"] += 0 if ok else 1

            elif name == "claude_code.user_prompt":
                agg.prompts += 1
            elif name == "claude_code.compaction":
                agg.compactions += 1
            elif name == "claude_code.skill_activated":
                _bump(agg.skills, rec.get("skill.name"))

    # -- reporting ---------------------------------------------------------

    _SUM_FIELDS = ("tokens_input", "tokens_output", "tokens_cache_read",
                   "tokens_cache_creation", "cost_usd", "api_calls", "api_errors",
                   "api_duration_ms", "tool_calls", "tool_failures",
                   "tool_duration_ms", "mcp_tool_calls", "compactions", "prompts")

    def totals(self) -> Agg:
        """Sum every step, including ``main``."""
        with self.lock:
            total = Agg()
            for s in self.steps:
                a = s.agg
                for f in self._SUM_FIELDS:
                    setattr(total, f, getattr(total, f) + getattr(a, f))
                for tool, v in a.tools.items():
                    t = total.tools.setdefault(tool, {"calls": 0, "ms": 0.0, "failures": 0})
                    for k in ("calls", "ms", "failures"):
                        t[k] += v[k]
                for src, dst in ((a.models, total.models), (a.agents, total.agents),
                                 (a.skills, total.skills)):
                    for k, v in src.items():
                        dst[k] = dst.get(k, 0) + v
            return total

    def attempt_totals(self) -> Dict[str, int]:
        """Try/retry/success/failure counts across every real step."""
        with self.lock:
            real = [s for s in self.steps if s.key != MAIN_STEP]
            return {
                "steps": len(real),
                "tries": sum(s.tries for s in real),
                "retries": sum(s.retries for s in real),
                "failed_attempts": sum(s.failed_attempts for s in real),
                "successful_attempts": sum(s.successful_attempts for s in real),
                "steps_succeeded": sum(1 for s in real if is_ok(s.status)),
                "steps_failed": sum(1 for s in real
                                    if not s.open and not is_ok(s.status)
                                    and s.status != _SUPERSEDED),
                "steps_running": sum(1 for s in real if s.open),
            }

    def snapshot(self) -> Dict[str, Any]:
        """A JSON-safe view of the whole profile."""
        with self.lock:
            total = self.totals()
            event_cost = total.cost_usd
            metric_cost = self.metric_totals.get("claude_code.cost.usage", 0.0)
            real = [s for s in self.steps if s.key != MAIN_STEP]
            return {
                "run_id": self.run_id,
                "started_ns": self.started_ns,
                "wall_s": round((_now_ns() - self.started_ns) / 1e9, 3),
                "exec_s": round(sum(s.exec_s for s in real), 3),
                "session_ids": sorted(self.session_ids),
                "steps": [s.as_dict() for s in self.steps],
                "totals": total.as_dict(),
                "attempts": self.attempt_totals(),
                "metric_totals": self.metric_totals,
                # A gap means log events were dropped or are still in flight.
                # Reported rather than hidden: silent under-counting of cost is
                # the failure mode that makes a profile worse than no profile.
                "cost_reconciliation": {
                    "from_events": round(event_cost, 6),
                    "from_metrics": round(metric_cost, 6),
                    "delta": round(metric_cost - event_cost, 6),
                },
            }
