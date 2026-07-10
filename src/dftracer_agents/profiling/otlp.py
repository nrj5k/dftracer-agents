"""Parse OTLP/JSON payloads into flat records.

Claude Code is the telemetry *source*: with ``CLAUDE_CODE_ENABLE_TELEMETRY=1``
it exports metrics and log-events over OTLP. We ask for the ``http/json``
protocol specifically so that the receiver is ~200 lines of stdlib instead of a
protobuf dependency and a vendored ``opentelemetry-proto`` tree.

Two payload roots matter:

* ``resourceLogs`` — the *events*. ``claude_code.api_request`` carries
  ``cost_usd``, ``duration_ms`` and all four token counts already attributed to
  an ``agent.name`` and ``query_source``; ``claude_code.tool_result`` carries
  ``tool_name``/``duration_ms``/``success``. Everything this profile needs is in
  the events, which is why they, not the metrics, drive attribution.
* ``resourceMetrics`` — the *counters* (``claude_code.token.usage``,
  ``claude_code.cost.usage``). Kept only as an independent total to cross-check
  the event-derived sums against; if the two diverge, the events dropped.

OTLP encodes every attribute as a tagged union (``{"stringValue": "x"}``), and
JSON-encodes 64-bit ints as *strings* to survive JavaScript's float53. Both
quirks are handled in :func:`_anyvalue` and :func:`_as_num` — do not "simplify"
them away.
"""
from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional

#: Events we keep. Claude Code emits ~24 event types; the rest are noise for a
#: cost/performance profile and would bloat the raw JSONL on disk.
KEPT_EVENTS = frozenset({
    "claude_code.api_request",
    "claude_code.api_error",
    "claude_code.tool_result",
    "claude_code.tool_decision",
    "claude_code.user_prompt",
    "claude_code.skill_activated",
    "claude_code.compaction",
    "claude_code.api_retries_exhausted",
})

#: Metrics we keep, for the cross-check totals only.
KEPT_METRICS = frozenset({
    "claude_code.token.usage",
    "claude_code.cost.usage",
    "claude_code.active_time.total",
    "claude_code.lines_of_code.count",
})


def _anyvalue(v: Any) -> Any:
    """Unwrap an OTLP ``AnyValue`` union into a plain Python value."""
    if not isinstance(v, dict):
        return v
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        # Protobuf JSON mapping renders int64 as a string.
        try:
            return int(v["intValue"])
        except (TypeError, ValueError):
            return v["intValue"]
    if "doubleValue" in v:
        return v["doubleValue"]
    if "boolValue" in v:
        return v["boolValue"]
    if "arrayValue" in v:
        return [_anyvalue(x) for x in v["arrayValue"].get("values", [])]
    if "kvlistValue" in v:
        return _attrs(v["kvlistValue"].get("values", []))
    return None


def _attrs(items: Optional[List[dict]]) -> Dict[str, Any]:
    """Flatten an OTLP ``KeyValue`` list into a dict."""
    out: Dict[str, Any] = {}
    for kv in items or []:
        key = kv.get("key")
        if key:
            out[key] = _anyvalue(kv.get("value"))
    return out


def _as_num(v: Any, default: float = 0.0) -> float:
    """Coerce an attribute to a float. OTLP may hand us ``"1234"`` or ``1234``."""
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return default
    return default


def _ts_ns(rec: dict) -> int:
    """Event timestamp in nanoseconds.

    ``timeUnixNano`` is the emit time and ``observedTimeUnixNano`` the receive
    time; prefer the former. Both arrive as strings.
    """
    raw = rec.get("timeUnixNano") or rec.get("observedTimeUnixNano") or 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def parse_logs(payload: dict) -> Iterator[dict]:
    """Yield one flat record per interesting log event.

    Each record is ``{"kind": "event", "name", "ts_ns", **resource_attrs,
    **event_attrs}``. Resource attributes are merged first so an event
    attribute of the same name wins.

    Args:
        payload: Decoded body of an OTLP ``ExportLogsServiceRequest``.

    Yields:
        Flat event dicts, in payload order.
    """
    for rl in payload.get("resourceLogs", []):
        res = _attrs(rl.get("resource", {}).get("attributes"))
        for sl in rl.get("scopeLogs", []):
            for rec in sl.get("logRecords", []):
                attrs = _attrs(rec.get("attributes"))
                # Claude Code sets `event.name`; the OTLP body carries it too.
                name = attrs.get("event.name") or _anyvalue(rec.get("body"))
                if name not in KEPT_EVENTS:
                    continue
                yield {"kind": "event", "name": name, "ts_ns": _ts_ns(rec),
                       **res, **attrs}


def parse_metrics(payload: dict) -> Iterator[dict]:
    """Yield one flat record per interesting metric data point.

    Sums and gauges are the only shapes Claude Code emits. A data point's value
    lives in ``asInt`` (string) or ``asDouble``.

    Args:
        payload: Decoded body of an OTLP ``ExportMetricsServiceRequest``.

    Yields:
        ``{"kind": "metric", "name", "ts_ns", "value", **resource_attrs, **dp_attrs}``.
    """
    for rm in payload.get("resourceMetrics", []):
        res = _attrs(rm.get("resource", {}).get("attributes"))
        for sm in rm.get("scopeMetrics", []):
            for m in sm.get("metrics", []):
                name = m.get("name")
                if name not in KEPT_METRICS:
                    continue
                points = (m.get("sum") or m.get("gauge") or {}).get("dataPoints", [])
                for dp in points:
                    value = (_as_num(dp["asInt"]) if "asInt" in dp
                             else _as_num(dp.get("asDouble")))
                    yield {"kind": "metric", "name": name, "ts_ns": _ts_ns(dp),
                           "value": value, **res, **_attrs(dp.get("attributes"))}
