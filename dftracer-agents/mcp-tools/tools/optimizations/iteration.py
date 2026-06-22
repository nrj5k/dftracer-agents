"""Optimization iteration tools — session_optimization_iteration and session_generate_optimization_proposals."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP

from ..session.workspace import (
    _ws, _load_state, _save_state, _write_artifact_log, _ok, _err, _run, _workspaces_root,
)
from ..session.install import _dftracer_utils_comparator
from ..session.session_tools import (
    _session_build_annotated_impl,
    _session_run_with_dftracer_impl,
    _session_split_traces_impl,
    _session_collect_system_info_impl,
)
from .diagnose import _session_diagnose_bottlenecks_impl
from .strategies import (
    _fetch_arxiv_papers,
    _BUILTIN_REFS,
    _L1_STRATEGIES,
    _L2_STRATEGIES,
    _L3_STRATEGIES,
    _gen_level_proposals,
    _METRIC_SYNONYM_PAIRS,
    _GENERAL_FALLBACK_QUERIES,
    _build_sys_context,
    _bottleneck_search_queries,
)


def register_iteration_tools(mcp: FastMCP) -> None:
    """Register session_optimization_iteration and session_generate_optimization_proposals onto *mcp*."""

    @mcp.tool()
    def session_optimization_iteration(
        run_id: str,
        command: str,
        app_name: str = "app",
        data_dir: str = "all",
        timeout: int = 600,
        env_extra: Optional[str] = None,
        optimization_applied: str = "",
        rebuild: bool = True,
        max_search_attempts: int = 10,
        papers_per_query: int = 3,
    ) -> str:
        """Run one iteration of the build → profile → diagnose → search optimization loop.

        Each call executes the following pipeline in sequence:

        1. **Build** (optional) — rebuild the annotated binary so any source
           changes applied between calls take effect.  Skip with
           ``rebuild=False`` to re-profile without rebuilding.
        2. **Profile** — run *command* with dftracer tracing enabled.
        3. **Split** — compact raw ``.pfw`` traces.
        4. **Diagnose** — run DFAnalyzer + DFDiagnoser and score bottlenecks.
        5. **System context** — collect or re-read ``system_config.json`` so
           hardware details (CPU arch, filesystem type, network) are available
           to refine search queries.
        6. **Literature search** — for each of the top-5 highest-severity
           bottlenecks, search arXiv with up to *max_search_attempts*
           progressively fuzzier queries that combine the bottleneck behaviour
           **and** the system hardware context:

           * Attempts 1-2: most specific phrase + system context.
           * Attempts 3-8: synonym phrase pairs with/without system context.
           * Attempts 9-10: broadest fallback ("I/O optimization {sys_context}",
             then generic "parallel I/O performance optimization HPC").

           A bottleneck is marked **unsolved** if no papers are found after all
           *max_search_attempts* queries.  The tool reports what it could not
           find so the agent knows where to ask the user for guidance.

        7. **Compare** — diff the bottleneck severity table against the previous
           iteration (stored in ``session.json`` under ``optimization_history``).

        **Typical agent loop**::

            # Baseline — first iteration
            r0 = session_optimization_iteration(
                run_id, command, optimization_applied="baseline")
            # Agent reads r0.literature and r0.unsolved, applies a source change.
            r1 = session_optimization_iteration(
                run_id, command, optimization_applied="increased write buffer to 4 MiB")
            # r1.delta shows which bottlenecks improved / regressed.
            # Repeat until r1.bottlenecks is empty or delta shows no improvement.

        Args:
            run_id:               Session identifier.
            command:              Benchmark command passed to
                                  ``session_run_with_dftracer``.
            app_name:             Trace split file prefix.
            data_dir:             ``DFTRACER_DATA_DIR`` value (default ``"all"``).
            timeout:              Seconds allowed for profiled run and diagnosis.
            env_extra:            Extra env vars as JSON (same format as
                                  ``session_run_with_dftracer``).
            optimization_applied: Human-readable label for this iteration's change.
                                  Use ``"baseline"`` on the first call.
            rebuild:              If ``True``, rebuild before profiling.
            max_search_attempts:  Maximum arXiv queries per bottleneck (1-10,
                                  default 10).  Stops early when papers are found.
            papers_per_query:     Papers to fetch per query attempt (1-5, default 3).

        Returns:
            JSON string with keys:

            * ``status``          — ``"ok"`` or ``"error"``.
            * ``iteration``       — zero-based iteration index.
            * ``optimization``    — echoed *optimization_applied* label.
            * ``build``           — build step summary (or ``null``).
            * ``profile``         — profiling step summary.
            * ``diagnosis``       — DFDiagnoser summary (severity counts).
            * ``system_context``  — hardware context string used for searches.
            * ``top5_bottlenecks``— top-5 bottlenecks by severity.
            * ``literature``      — list of per-bottleneck search results:
              ``{"bottleneck": …, "queries_tried": N, "papers": [...]}``.
            * ``unsolved``        — bottlenecks for which no papers were found
              after all *max_search_attempts* attempts, with the queries tried.
            * ``delta``           — severity delta vs previous iteration:
              ``{"improved": [...], "regressed": [...], "resolved": [...],
              "new": [...], "unchanged": [...]}``.
            * ``recommendation``  — plain-text guidance for the next step.
        """
        import json as _json

        state = _load_state(run_id)
        history: list = state.get("optimization_history", [])
        iteration = len(history)
        _SEV = {"trivial": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

        # ── Step 1: (re)build annotated binary ──────────────────────────────
        build_summary = None
        if rebuild:
            raw = _session_build_annotated_impl(run_id=run_id)
            build_result = _json.loads(raw)
            if build_result.get("status") != "ok":
                return _json.dumps({
                    "status": "error",
                    "message": f"Build failed at iteration {iteration}: "
                               + build_result.get("message", ""),
                    "build": build_result,
                    "iteration": iteration,
                })
            build_summary = {
                "returncode": build_result.get("returncode", 0),
                "duration_s": build_result.get("duration_s"),
            }

        # ── Per-iteration artifact directory ─────────────────────────────────
        ws = _ws(run_id)
        iter_dir = ws / f"opt{iteration}"
        iter_dir.mkdir(exist_ok=True)
        iter_traces_dir  = iter_dir / "traces"
        iter_split_dir   = iter_dir / "traces_split"
        iter_traces_dir.mkdir(exist_ok=True)
        iter_split_dir.mkdir(exist_ok=True)

        # Capture a diff of annotated source vs the previous iteration
        import subprocess as _sp
        prev_patch = iter_dir / "changes.patch"
        if iteration == 0:
            prev_patch.write_text("# baseline — no prior iteration\n")
        else:
            _diff = _sp.run(
                ["git", "diff", "--no-index",
                 str(ws / f"opt{iteration - 1}" / "annotated_snapshot"),
                 str(ws / "annotated")],
                capture_output=True, text=True,
            )
            prev_patch.write_text(_diff.stdout or "# no diff\n")
        # Snapshot the current annotated tree
        import shutil as _sh
        ann_snapshot = iter_dir / "annotated_snapshot"
        if ann_snapshot.exists():
            _sh.rmtree(ann_snapshot)
        _sh.copytree(str(ws / "annotated"), str(ann_snapshot))

        # ── Step 2: profile run ──────────────────────────────────────────────
        raw = _session_run_with_dftracer_impl(
            run_id=run_id,
            command=command,
            subfolder="build_ann",
            data_dir=data_dir,
            timeout=timeout,
            env_extra=env_extra,
        )
        profile_result = _json.loads(raw)
        if profile_result.get("status") != "ok":
            return _json.dumps({
                "status": "error",
                "message": f"Profile run failed at iteration {iteration}: "
                           + profile_result.get("message", ""),
                "profile": profile_result,
                "iteration": iteration,
            })
        profile_summary = {
            "returncode": profile_result.get("returncode"),
            "elapsed_s":  profile_result.get("elapsed_s"),
            "trace_files": profile_result.get("trace_files", []),
        }

        # Copy raw traces to per-iteration directory
        import glob as _glob
        for _pfw in _glob.glob(str(ws / "traces" / "*.pfw.gz")):
            _sh.copy2(_pfw, str(iter_traces_dir))
        # Also handle run_id-subdirectory layout (traces/<prefix>/*.pfw.gz)
        _run_prefix = run_id.split("/")[0]
        for _pfw in _glob.glob(str(ws / "traces" / _run_prefix / "*.pfw.gz")):
            _sh.copy2(_pfw, str(iter_traces_dir))

        # ── Step 3: split traces into per-iteration split dir ────────────────
        _session_split_traces_impl(run_id=run_id, app_name=app_name)
        # Copy split output to per-iteration directory
        for _chunk in _glob.glob(str(ws / "traces_split" / "*.pfw.gz")):
            _sh.copy2(_chunk, str(iter_split_dir))

        # ── Step 3b: compare against previous iteration traces ───────────────
        comparison: Dict[str, Any] = {}
        if iteration > 0:
            prev_split = ws / f"opt{iteration - 1}" / "traces_split"
            if prev_split.exists() and any(prev_split.glob("*.pfw.gz")):
                try:
                    cmp_raw = _dftracer_utils_comparator(
                        baseline=str(prev_split),
                        variant=str(iter_split_dir),
                        query='cat == "POSIX" OR cat == "STDIO" OR cat == "C_APP"',
                        group_by_dims="cat,name",
                        output_format="json",
                        threshold_pct=5.0,
                    )
                    cmp_result = (
                        _json.loads(cmp_raw["stdout"])
                        if cmp_raw["success"] and cmp_raw["stdout"].strip()
                        else {"error": cmp_raw["stderr"][:500] or "empty output",
                              "returncode": cmp_raw["returncode"]}
                    )
                    comparison = {
                        "baseline_iter": iteration - 1,
                        "variant_iter":  iteration,
                        "baseline_dir":  str(prev_split),
                        "variant_dir":   str(iter_split_dir),
                        "result":        cmp_result,
                    }
                    # Persist comparison to per-iteration dir
                    (iter_dir / "comparison.json").write_text(
                        _json.dumps(comparison, indent=2)
                    )
                except Exception as _cmp_err:
                    comparison = {"error": str(_cmp_err)}

        # ── Step 4: diagnose bottlenecks ────────────────────────────────────
        # Clear stale checkpoint so each iteration scores only its own traces
        _ckpt = ws / "dfanalyzer_checkpoint"
        if _ckpt.exists():
            import shutil as _sh2
            _sh2.rmtree(str(_ckpt))
        _ckpt.mkdir(exist_ok=True)
        raw = _session_diagnose_bottlenecks_impl(run_id=run_id, timeout=timeout)
        diag_result = _json.loads(raw)
        current_bottlenecks: list = diag_result.get("bottlenecks", [])
        diag_summary = {
            "severity_counts": diag_result.get("severity_counts", {}),
            "bottleneck_count": len(current_bottlenecks),
        }

        # ── Step 5: system context ───────────────────────────────────────────
        sys_cfg_path = ws / "system_config.json"
        if sys_cfg_path.exists():
            try:
                sys_info = _json.loads(sys_cfg_path.read_text())
            except Exception:
                sys_info = {}
        else:
            raw_sys = _session_collect_system_info_impl(run_id=run_id)
            sys_info = _json.loads(raw_sys)
        sys_context = _build_sys_context(sys_info)

        # ── Step 6: literature search — top 5 bottlenecks ───────────────────
        top5 = sorted(
            current_bottlenecks,
            key=lambda b: _SEV.get(b.get("severity", "trivial"), 0),
            reverse=True,
        )[:5]

        max_attempts = max(1, min(10, max_search_attempts))
        n_papers     = max(1, min(5, papers_per_query))
        seen_titles: set = set()
        literature: List[Dict[str, Any]] = []
        unsolved:   List[Dict[str, Any]] = []

        for bn in top5:
            metric      = bn.get("metric", "")
            description = bn.get("description", "")
            severity    = bn.get("severity", "")

            queries = _bottleneck_search_queries(
                metric=metric,
                description=description,
                sys_context=sys_context,
                max_queries=max_attempts,
            )

            found_papers: List[Dict[str, Any]] = []
            queries_tried: List[str] = []

            for q in queries:
                queries_tried.append(q)
                papers = _fetch_arxiv_papers(q, n=n_papers)
                for p in papers:
                    title_key = p["title"].lower()[:80]
                    if title_key not in seen_titles:
                        seen_titles.add(title_key)
                        found_papers.append({**p, "query": q, "bottleneck": metric})
                if found_papers:
                    break  # stop as soon as at least one paper is found

            entry = {
                "bottleneck":    metric,
                "severity":      severity,
                "description":   description,
                "sys_context":   sys_context,
                "queries_tried": len(queries_tried),
                "last_query":    queries_tried[-1] if queries_tried else "",
                "papers":        found_papers,
            }
            if found_papers:
                literature.append(entry)
            else:
                unsolved.append({
                    "bottleneck":    metric,
                    "severity":      severity,
                    "description":   description,
                    "queries_tried": queries_tried,
                    "message":       (
                        f"No papers found for '{metric}' ({severity}) after "
                        f"{len(queries_tried)} search attempt(s) including "
                        f"system-context '{sys_context}'. "
                        "Manual expert guidance recommended."
                    ),
                })

        # ── Step 7: compare with previous iteration ──────────────────────────
        prev_bottlenecks: list = []
        if history:
            prev_bottlenecks = history[-1].get("bottlenecks", [])

        def _bn_key(b: dict) -> str:
            return f"{b.get('metric', '')}:{b.get('view', '')}"

        prev_keys = {_bn_key(b): b for b in prev_bottlenecks}
        curr_keys = {_bn_key(b): b for b in current_bottlenecks}

        improved   = []
        regressed  = []
        unchanged  = []
        new_issues = []

        for key, cb in curr_keys.items():
            if key not in prev_keys:
                new_issues.append(cb)
            else:
                pb = prev_keys[key]
                cs = _SEV.get(cb.get("severity", "trivial"), 0)
                ps = _SEV.get(pb.get("severity", "trivial"), 0)
                if cs < ps:
                    improved.append({"metric": key, "from": pb["severity"], "to": cb["severity"]})
                elif cs > ps:
                    regressed.append({"metric": key, "from": pb["severity"], "to": cb["severity"]})
                else:
                    unchanged.append(key)

        resolved = [k for k in prev_keys if k not in curr_keys]

        delta = {
            "improved":  improved,
            "regressed": regressed,
            "resolved":  resolved,
            "new":       new_issues,
            "unchanged": unchanged,
        }

        # ── Build recommendation ─────────────────────────────────────────────
        # ── Build per-bottleneck citations list ──────────────────────────────
        # Each entry: {metric, severity, papers: [{title, url, authors, published}]}
        citations: List[Dict[str, Any]] = []
        for lit_entry in literature:
            cite_papers = [
                {
                    "title":     p.get("title", ""),
                    "url":       p.get("url", ""),
                    "authors":   p.get("authors", []),
                    "published": p.get("published", ""),
                    "abstract":  p.get("abstract", "")[:300] + "…" if p.get("abstract") else "",
                    "query":     p.get("query", ""),
                }
                for p in lit_entry.get("papers", [])
            ]
            citations.append({
                "metric":   lit_entry["bottleneck"],
                "severity": lit_entry["severity"],
                "papers":   cite_papers,
            })

        def _cite_str(lit_entry: dict) -> str:
            """One-line citation: 'Title (Authors, Year) <url>'"""
            papers = lit_entry.get("papers", [])
            if not papers:
                return "(no papers found)"
            p = papers[0]
            year  = (p.get("published") or "")[:4]
            auth  = p.get("authors", [])
            first = auth[0].split()[-1] if auth else "Unknown"
            et_al = " et al." if len(auth) > 1 else ""
            return (
                f'"{p["title"][:70]}..." '
                f"- {first}{et_al}, {year} "
                f"<{p.get('url', '')}>"
            )

        n_papers = sum(len(e["papers"]) for e in literature)
        lit_summary = (
            f"{n_papers} paper(s) found across {len(literature)} bottleneck(s)"
        ) if literature else "no papers found"

        if not current_bottlenecks:
            recommendation = "No active bottlenecks — optimization complete."
        elif unsolved:
            names = ", ".join(u["bottleneck"] for u in unsolved)
            recommendation = (
                f"Literature search exhausted for: {names}. "
                "These require manual expert analysis — see 'unsolved' for the "
                "full list of attempted queries. "
            )
            if literature:
                top_lit = literature[0]
                recommendation += (
                    f"For remaining solvable bottlenecks, start with "
                    f"'{top_lit['bottleneck']}': {_cite_str(top_lit)}"
                )
        elif regressed:
            cite_lines = "\n".join(f"  • [{c['metric']}] {_cite_str(c)}" for c in citations[:3])
            recommendation = (
                f"{len(regressed)} metric(s) regressed — revert last change or "
                "investigate interaction effects.\n"
                f"Evidence ({lit_summary}):\n{cite_lines}"
            )
        elif improved or resolved:
            top = top5[0] if top5 else {}
            cite_lines = "\n".join(f"  • [{c['metric']}] {_cite_str(c)}" for c in citations[:3])
            recommendation = (
                f"Progress: {len(improved)} improved, {len(resolved)} resolved. "
                f"Top remaining: {top.get('metric','')} ({top.get('severity','')}).\n"
                f"Evidence ({lit_summary}):\n{cite_lines}\n"
                "Apply the suggested optimization and iterate."
            )
        else:
            top = top5[0] if top5 else {}
            cite_lines = "\n".join(f"  • [{c['metric']}] {_cite_str(c)}" for c in citations[:3])
            recommendation = (
                f"No change yet. Top bottleneck: {top.get('metric','')} "
                f"({top.get('severity','')}).\n"
                f"Evidence ({lit_summary}):\n{cite_lines}\n"
                "Apply the technique from the first cited paper and re-run."
            )

        # ── Persist iteration to history ─────────────────────────────────────
        entry = {
            "iteration":       iteration,
            "optimization":    optimization_applied,
            "build":           build_summary,
            "profile":         profile_summary,
            "diagnosis":       diag_summary,
            "system_context":  sys_context,
            "bottlenecks":     current_bottlenecks,
            "top5":            top5,
            "literature":      literature,
            "unsolved":        unsolved,
            "delta":           delta,
            "comparison":      comparison,
        }
        history.append(entry)
        _save_state(run_id, {"optimization_history": history, "step": "optimization_loop"})

        # Save full literature results to workspace for reference
        lit_file = ws / f"optimization_literature_iter{iteration}.json"
        iter_summary = {
            "iteration":    iteration,
            "optimization": optimization_applied,
            "sys_context":  sys_context,
            "literature":   literature,
            "citations":    citations,
            "unsolved":     unsolved,
            "bottlenecks":  current_bottlenecks,
            "delta":        delta,
            "comparison":   comparison,
        }
        lit_file.write_text(_json.dumps(iter_summary, indent=2))
        # Mirror into per-iteration directory for comparison
        (iter_dir / "summary.json").write_text(_json.dumps(iter_summary, indent=2))
        if sys_cfg_path.exists():
            _sh.copy2(str(sys_cfg_path), str(iter_dir / "system_config.json"))

        return _ok(
            f"Iteration {iteration} complete — {len(current_bottlenecks)} active "
            f"bottleneck(s), {len(literature)} solved by literature, "
            f"{len(unsolved)} unsolved. " + recommendation,
            iteration=iteration,
            optimization=optimization_applied,
            build=build_summary,
            profile=profile_summary,
            diagnosis=diag_summary,
            system_context=sys_context,
            top5_bottlenecks=top5,
            literature=literature,
            unsolved=unsolved,
            delta=delta,
            literature_file=str(lit_file),
            iter_dir=str(iter_dir),
            citations=citations,
            recommendation=recommendation,
        )

    @mcp.tool()
    def session_generate_optimization_proposals(
        run_id: str,
        iteration: int = -1,
        levels: str = "123",
        metric: str = "time",
        max_proposals_per_level: int = 3,
    ) -> str:
        """Generate concrete, citation-backed optimization proposals from bottleneck diagnosis.

        Reads the bottleneck list and searched literature from a completed
        ``session_optimization_iteration`` call, maps each bottleneck to a set of
        concrete code / config / system changes using the three-level strategy
        tables (L1 application, L2 software/middleware, L3 system/filesystem),
        and attaches a verifiable citation (URL) to every proposal.

        Citation priority per proposal:
        1. Papers found by the arXiv / Semantic Scholar search in the latest iteration.
        2. Built-in reference: WisIO (Yildirim et al., ICS 2025) or
           Drishti (Bez et al., PDSW 2022) — whichever is most relevant.

        A proposal is OMITTED if neither a searched paper nor a built-in reference
        can be matched to the bottleneck + strategy combination.

        Args:
            run_id:                  Session identifier.
            iteration:               Which optimization iteration to read (-1 = latest).
            levels:                  Which levels to generate proposals for.
                                     Any combination of "1", "2", "3" (default "123").
            metric:                  Optimization objective used to rank proposals
                                     (time | bandwidth | iops | metadata_ops).
            max_proposals_per_level: Cap on proposals returned per level (default 3).

        Returns:
            JSON with keys:
            * ``status``     — ``"ok"`` or ``"error"``.
            * ``proposals``  — list of proposal dicts, each containing:
              ``id``, ``level``, ``title``, ``bottleneck``, ``severity``,
              ``change``, ``expected_delta``, ``risk``, ``citation``
              (sub-keys: authors, title, venue, year, url, finding),
              and for L2: ``delivery``, ``env_key``;
              for L3: ``privilege``, ``rollback``, ``side_effect``.
            * ``unsupported`` — bottlenecks for which no strategy was found.
            * ``citation_sources`` — breakdown of how many proposals used searched
              papers vs built-in references.
        """
        import json as _json

        state = _load_state(run_id)
        history: list = state.get("optimization_history", [])
        if not history:
            return _err("No optimization iterations found — run session_optimization_iteration first.")

        idx = iteration if iteration >= 0 else len(history) - 1
        if idx >= len(history):
            return _err(f"Iteration {iteration} not found (history has {len(history)} entries).")

        iter_entry  = history[idx]
        bottlenecks = iter_entry.get("bottlenecks", [])
        literature  = iter_entry.get("literature", [])

        # Build metric → papers lookup from the iteration's existing literature search
        searched: Dict[str, List[Dict[str, Any]]] = {}
        for lit_entry in literature:
            m = lit_entry.get("bottleneck", "")
            searched[m] = lit_entry.get("papers", [])

        want_l1 = "1" in levels
        want_l2 = "2" in levels
        want_l3 = "3" in levels

        all_proposals: List[Dict[str, Any]] = []
        all_unsupported: List[str] = []
        total_cs = 0
        total_cb = 0

        if want_l1:
            p1, cs1, cb1 = _gen_level_proposals(
                bottlenecks, _L1_STRATEGIES, "L1", searched,
                max_per_level=max_proposals_per_level,
            )
            all_proposals.extend(p1)
            total_cs += cs1
            total_cb += cb1

        if want_l2:
            p2, cs2, cb2 = _gen_level_proposals(
                bottlenecks, _L2_STRATEGIES, "L2", searched,
                max_per_level=max_proposals_per_level,
                extra_fields=["delivery", "env_key"],
            )
            all_proposals.extend(p2)
            total_cs += cs2
            total_cb += cb2

        if want_l3:
            p3, cs3, cb3 = _gen_level_proposals(
                bottlenecks, _L3_STRATEGIES, "L3", searched,
                max_per_level=max_proposals_per_level,
                extra_fields=["privilege", "rollback", "side_effect"],
            )
            all_proposals.extend(p3)
            total_cs += cs3
            total_cb += cb3

        # Collect bottlenecks not covered by any level
        _SEV = {"trivial": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
        covered = {p["bottleneck"] for p in all_proposals}
        for bn in bottlenecks:
            met = bn.get("metric", "")
            if _SEV.get(bn.get("severity", "trivial"), 0) >= 2 and met not in covered:
                all_unsupported.append(met)

        total = len(all_proposals)
        return _ok(
            f"{total} citation-backed proposal(s) across levels '{levels}' "
            f"for run {run_id} iteration {idx}. "
            f"Citations: {total_cs} from arXiv search, {total_cb} from built-in references. "
            f"Unsupported bottlenecks: {all_unsupported or 'none'}.",
            proposals=all_proposals,
            unsupported=all_unsupported,
            citation_sources={
                "searched_papers": total_cs,
                "builtin_references": total_cb,
            },
            iteration=idx,
        )
