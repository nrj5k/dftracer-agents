"""Dimension-aware optimization orchestrator — session_optimization_plan and
session_optimization_apply_trial.

These tools sit on top of the existing profile/diagnose/search engine in
``iteration.py`` (left unchanged) and organize its ``bottlenecks`` output along
three explicit axes so that an agent can run isolated, single-variable trials
and greedily converge on the best configuration per bottleneck:

* **Layer**   — L1 (application code), L2 (middleware: MPI-IO/HDF5/DataLoader),
                L3 (filesystem/OS).  Derived from which ``_L{n}_STRATEGIES``
                table in :mod:`strategies` has an entry for the bottleneck's
                metric.
* **Component** — io | communication | memory | compute, via the existing
                ``_metric_category``/``_category_sort_key`` from
                :mod:`strategies` (I/O -> communication -> memory -> compute
                priority order is preserved).
* **Scale**   — "small" (initial isolated trial, small job/allocation) then
                "full" (confirmation run at full allocation) once a winner is
                selected for a given bottleneck.

No OS-level concurrency is introduced: the calling agent still runs each
trial sequentially via existing tools (``session_run_l1_iteration``,
``session_run_with_dftracer``, etc.) and reports the measured delta back via
``session_optimization_apply_trial``.  "Parallel across dimensions" is
expressed here as independent, isolated trial branches in the plan (each
touching a disjoint layer/metric combination) that the agent can execute in
any order, plus greedy best-so-far tracking per bottleneck.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP

from ..session.workspace import _load_state, _save_state, _ok, _err
from .strategies import (
    _metric_category,
    _category_sort_key,
    _CATEGORY_ORDER,
    _L1_STRATEGIES,
    _L2_STRATEGIES,
    _L3_STRATEGIES,
)

_SEV = {"trivial": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

#: Layer tag -> strategy table used to decide whether a metric is addressable
#: at that layer (a metric present as a key means at least one strategy exists).
_LAYER_STRATEGIES = {
    "L1": _L1_STRATEGIES,
    "L2": _L2_STRATEGIES,
    "L3": _L3_STRATEGIES,
}


def _addressable_layers(metric: str) -> List[str]:
    """Return the layer tags (subset of L1/L2/L3) that have a known strategy for *metric*."""
    return [layer for layer, table in _LAYER_STRATEGIES.items() if metric in table]


def _trial_id(layer: str, component: str, metric: str, scale: str) -> str:
    return f"{layer}.{component}.{metric}.{scale}"


def register_orchestrator_tools(mcp: FastMCP) -> None:
    """Register session_optimization_plan and session_optimization_apply_trial onto *mcp*."""

    @mcp.tool()
    def session_optimization_plan(run_id: str, iteration: int = -1) -> str:
        """Build a Layer x Component x Scale trial matrix from the latest diagnosed bottlenecks.

        Reads ``bottlenecks`` from ``state["optimization_history"][iteration]``
        (populated by ``session_optimization_iteration``) and classifies each
        one along two axes:

        * **Component** (I/O, communication, memory, compute) via the existing
          ``_metric_category``/``_category_sort_key`` ordering — bottlenecks
          are greedily prioritized I/O first, then communication, memory,
          compute, with severity as the tiebreaker within a category.
        * **Layer** (L1 app / L2 middleware / L3 filesystem) — a bottleneck's
          metric is "addressable" at layer L*n* if ``_L{n}_STRATEGIES``
          (from ``strategies.py``) has a proposal table entry for it.

        Every ``(layer, metric)`` combination becomes one trial entry, scaled
        ``"small"`` initially. Trials for *different* metrics, or the *same*
        metric at *different* layers, are marked ``isolated: true`` since they
        change disjoint config/source surfaces and can be run in any order.
        Trials for the *same* metric at the *same* layer (multiple candidate
        strategies) are grouped so only one should be applied at a time to
        keep impact attribution clean.

        This tool only plans — it does not run anything or apply any change.

        Args:
            run_id: Session identifier.
            iteration: Which optimization iteration's bottlenecks to plan
                against (-1 = latest).

        Returns:
            JSON with keys: ``status``, ``message``, ``iteration``, ``trials``
            (list of ``{trial_id, layer, component, metric, severity, scale,
            isolated}``), ``trial_count``.
        """
        state = _load_state(run_id)
        history = state.get("optimization_history", [])
        if not history:
            return _err("No optimization iterations — run session_optimization_iteration first.")

        idx = iteration if iteration >= 0 else len(history) - 1
        if idx < 0 or idx >= len(history):
            return _err(f"iteration {iteration} out of range (0..{len(history)-1})")
        bottlenecks = history[idx].get("bottlenecks", [])

        trials: List[Dict[str, Any]] = []
        for bn in sorted(bottlenecks, key=_category_sort_key):
            metric = bn.get("metric", "")
            severity = bn.get("severity", "trivial")
            component = _metric_category(metric)
            layers = _addressable_layers(metric)
            if not layers:
                # No known strategy table entry — still record as an L1
                # candidate so the agent can investigate manually.
                layers = ["L1"]
            for layer in layers:
                trials.append({
                    "trial_id": _trial_id(layer, component, metric, "small"),
                    "layer": layer,
                    "component": component,
                    "metric": metric,
                    "severity": severity,
                    "scale": "small",
                    "isolated": True,
                })

        state_key = f"optimization_plan_{idx}"
        _save_state(run_id, {
            state_key: {
                "iteration": idx,
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "trials": trials,
            },
        })

        return _ok(
            f"Trial plan built for iteration {idx}: {len(trials)} isolated trial(s) "
            f"across {len({t['layer'] for t in trials})} layer(s) and "
            f"{len({t['component'] for t in trials})} component(s).",
            iteration=idx,
            trials=trials,
            trial_count=len(trials),
        )

    @mcp.tool()
    def session_optimization_apply_trial(
        run_id: str,
        trial_id: str,
        iteration: int,
        result_metric_delta: float,
        notes: str = "",
    ) -> str:
        """Record the measured impact of one isolated trial and update the greedy-best config.

        The calling agent runs the actual trial itself (e.g. via
        ``session_run_l1_iteration`` for an L1 change, or
        ``session_run_with_dftracer`` with a modified env/config for L2/L3),
        measures the resulting change in the target metric, then reports it
        here as *result_metric_delta* (negative = improvement, by convention,
        for time/latency-style metrics — the sign convention is up to the
        caller as long as it is applied consistently across trials for the
        same metric).

        Appends the result to ``state["trial_results"]``, then recomputes the
        greedy-best trial *per (layer, component, metric)* group seen so far
        (most negative delta wins). When every trial belonging to a
        ``(layer, metric)`` group at ``scale="small"`` has been reported, the
        group is flagged ``ready_for_full_scale: true`` so the caller knows to
        promote the winning trial to a ``scale="full"`` confirmation run at
        full allocation.

        Args:
            run_id: Session identifier.
            trial_id: Identifier from ``session_optimization_plan``'s
                ``trials[].trial_id`` (format ``"L{n}.{component}.{metric}.{scale}"``).
            iteration: Iteration index the trial plan was generated for.
            result_metric_delta: Measured change in the target metric after
                applying this trial in isolation.
            notes: Optional free-text description of what was changed.

        Returns:
            JSON with keys: ``status``, ``message``, ``trial_id``,
            ``best_per_group`` (mapping ``"{layer}.{metric}"`` -> best trial
            record so far), ``ready_for_full_scale`` (list of group keys whose
            small-scale trials are all reported).
        """
        state = _load_state(run_id)
        plan = state.get(f"optimization_plan_{iteration}")
        if not plan:
            return _err(
                f"No trial plan found for iteration {iteration} — call "
                "session_optimization_plan first.",
            )

        known_ids = {t["trial_id"] for t in plan["trials"]}
        planned = next((t for t in plan["trials"] if t["trial_id"] == trial_id), None)
        if planned is None:
            return _err(f"Unknown trial_id {trial_id!r} for iteration {iteration}.")

        results: List[Dict[str, Any]] = state.get("trial_results", [])
        results = [r for r in results if r.get("trial_id") != trial_id]
        record = {
            "trial_id": trial_id,
            "layer": planned["layer"],
            "component": planned["component"],
            "metric": planned["metric"],
            "scale": planned["scale"],
            "delta": result_metric_delta,
            "notes": notes,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        results.append(record)
        _save_state(run_id, {"trial_results": results})

        # Greedy best-per-group (most negative delta = best improvement).
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for r in results:
            key = f"{r['layer']}.{r['metric']}"
            groups.setdefault(key, []).append(r)

        best_per_group = {
            key: min(recs, key=lambda r: r["delta"])
            for key, recs in groups.items()
        }

        # A group is ready for full-scale confirmation once every planned
        # small-scale trial_id sharing its (layer, metric) has been reported.
        ready: List[str] = []
        for key, recs in groups.items():
            layer, metric = key.split(".", 1)
            planned_ids = {
                t["trial_id"] for t in plan["trials"]
                if t["layer"] == layer and t["metric"] == metric and t["scale"] == "small"
            }
            reported_ids = {r["trial_id"] for r in recs if r["scale"] == "small"}
            if planned_ids and planned_ids <= reported_ids:
                ready.append(key)

        return _ok(
            f"Trial {trial_id} recorded (delta={result_metric_delta}).",
            trial_id=trial_id,
            best_per_group=best_per_group,
            ready_for_full_scale=ready,
        )
