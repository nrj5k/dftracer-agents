"""Tiered agentic memory for the optimization loop.

This module is the first working tier of a move away from "naive RAG" (fetch
papers fresh from arXiv every iteration, discard them when the process exits)
toward **Agentic RAG**: the agent reads from a persistent store *before*
issuing a live search, applies a proposal, observes whether the bottleneck
actually improved, and writes that verdict back — so the next iteration (in
this session or a completely different one) starts from what already worked
instead of re-deriving it from scratch.

Memory tiers in the broader design (only Tier 2 is implemented here):

* **Tier 0 — Working context.**  The live ``session.json`` /
  ``optimization_history`` already maintained by :mod:`iteration`.  Scoped to
  a single run, ephemeral, no retrieval — the agent just re-reads it.
* **Tier 1 — Session/episodic memory.**  Per-run literature + proposal
  history (``optimization_literature_iter*.json``).  Already persisted, but
  not retrieval-indexed; a future tier could add embedding search over it.
* **Tier 2 — Project/semantic memory (this module).**  A cross-session,
  cross-run store keyed by ``(metric, category, sys_context)`` that records
  which citation + strategy combinations were tried and whether the
  bottleneck's severity actually improved afterward.  Lives at
  ``<workspaces_root>/_memory/optimization_memory.json`` — sibling to every
  run's workspace, not inside any single run, since it must survive across
  runs and sessions.
* **Tier 3 — Global reference corpus.**  ``strategies._BUILTIN_REFS`` — the
  hand-curated, always-available citations (WisIO, Drishti, STREAM,
  Roofline, ...).  Static; Tier 2 supersedes a Tier-3 entry once real
  evidence (an observed improvement) is recorded for a metric.

Retrieval here is deliberately dependency-free lexical scoring (token overlap
+ a learned confidence prior), not embeddings — it is a drop-in seam.
Swapping in a vector store later only requires reimplementing
``_score_record`` and ``_memory_retrieve``; the read/write/reflect contract
that the rest of the optimization loop depends on does not change.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP

from ..session.workspace import _workspaces_root, _load_state, _ok, _err
from .strategies import _metric_category


def _memory_path() -> Path:
    """Path to the cross-session Tier-2 memory store.

    Sits at ``<workspaces_root>/_memory/optimization_memory.json`` — a
    sibling of every per-run workspace directory, so it is neither owned by
    (nor cleaned up with) any single session, but still stays inside the
    project's workspaces root.
    """
    p = _workspaces_root() / "_memory"
    p.mkdir(parents=True, exist_ok=True)
    return p / "optimization_memory.json"


def _load_memory() -> List[Dict[str, Any]]:
    path = _memory_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return []


def _save_memory(records: List[Dict[str, Any]]) -> None:
    _memory_path().write_text(json.dumps(records, indent=2))


def _tokenize(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _record_key(metric: str, sys_context: str, strategy_title: str) -> str:
    """Identity key for upsert matching — same fix, same metric, same system."""
    return f"{metric.strip().lower()}::{sys_context.strip().lower()}::{strategy_title.strip().lower()}"


def _score_record(query_metric: str, query_category: str, query_sys_context: str,
                   record: Dict[str, Any]) -> float:
    """Relevance score combining lexical overlap with a learned confidence prior.

    score = 0.6 * token_overlap(query, record) + 0.4 * confidence(record)

    confidence(record) = successes / max(1, uses) — i.e. a record that has
    never been tried scores low regardless of lexical match, and a record
    that regressed every time it was applied is actively down-ranked.
    """
    query_tokens = _tokenize(f"{query_metric} {query_category} {query_sys_context}")
    rec_tokens = _tokenize(
        f"{record.get('metric','')} {record.get('category','')} "
        f"{record.get('sys_context','')} {record.get('strategy_title','')} "
        f"{record.get('citation', {}).get('title','')}"
    )
    if not query_tokens or not rec_tokens:
        overlap = 0.0
    else:
        overlap = len(query_tokens & rec_tokens) / len(query_tokens | rec_tokens)

    uses = max(1, record.get("uses", 0))
    successes = record.get("successes", 0)
    failures = record.get("failures", 0)
    confidence = (successes - failures) / uses  # ranges roughly [-1, 1]
    confidence = max(0.0, confidence)  # never let history make it worse than "no evidence"

    return 0.6 * overlap + 0.4 * confidence


def _memory_retrieve(metric: str, sys_context: str = "", category: str = "",
                      k: int = 3, min_score: float = 0.15) -> List[Dict[str, Any]]:
    """Read path: rank stored records against a query, return top *k*.

    Called by ``session_optimization_iteration`` *before* it issues a live
    arXiv query, so a bottleneck this system (or a similar one) has already
    solved does not re-pay the cost of a fresh literature search.
    """
    cat = category or _metric_category(metric)
    records = _load_memory()
    scored = [
        (r, _score_record(metric, cat, sys_context, r))
        for r in records
    ]
    scored.sort(key=lambda t: t[1], reverse=True)
    return [
        {**r, "retrieval_score": round(score, 4)}
        for r, score in scored[:k]
        if score >= min_score
    ]


def _memory_write(metric: str, sys_context: str, strategy_title: str,
                   citation: Dict[str, Any], source: str = "searched",
                   outcome: Optional[str] = None) -> Dict[str, Any]:
    """Write path: upsert a (metric, sys_context, strategy) record.

    Args:
        outcome: One of ``"improved"``, ``"resolved"`` (success),
                 ``"regressed"`` (failure), or ``None`` (record the
                 attempt without asserting an outcome yet — e.g. right
                 after a citation is chosen, before the next profile run
                 confirms whether it helped).
    """
    records = _load_memory()
    key = _record_key(metric, sys_context, strategy_title)
    now = time.time()
    cat = _metric_category(metric)

    for r in records:
        if r.get("key") == key:
            r["uses"] = r.get("uses", 0) + 1
            r["last_used"] = now
            r["citation"] = citation or r.get("citation", {})
            if outcome in ("improved", "resolved"):
                r["successes"] = r.get("successes", 0) + 1
            elif outcome == "regressed":
                r["failures"] = r.get("failures", 0) + 1
            _save_memory(records)
            return r

    record = {
        "key":             key,
        "metric":          metric,
        "category":        cat,
        "sys_context":     sys_context,
        "strategy_title":  strategy_title,
        "citation":        citation or {},
        "source":          source,
        "created":         now,
        "last_used":       now,
        "uses":            1,
        "successes":       1 if outcome in ("improved", "resolved") else 0,
        "failures":        1 if outcome == "regressed" else 0,
    }
    records.append(record)
    _save_memory(records)
    return record


def _memory_reflect(run_id: str, iteration: int = -1) -> Dict[str, Any]:
    """Agentic reflection step: turn one iteration's observed delta into
    persisted verdicts on the citations/strategies used for it.

    Reads ``optimization_history`` from the run's ``session.json`` (written
    by ``session_optimization_iteration``).  A change applied *after*
    iteration ``i-1`` (justified by iteration ``i-1``'s literature search) is
    only observable in iteration ``i``'s ``delta`` — so this reflects on
    iteration *idx*'s delta against iteration *idx-1*'s citations, and calls
    :func:`_memory_write` with the corresponding outcome.  This is what makes
    the loop "agentic" rather than one-shot: the same evidence used to
    justify a proposal is what gets scored and fed back for the *next*
    retrieval.
    """
    state = _load_state(run_id)
    history: list = state.get("optimization_history", [])
    if not history:
        return {"written": 0, "detail": "no optimization_history for this run"}

    idx = iteration if iteration >= 0 else len(history) - 1
    if idx >= len(history) or idx < 0:
        return {"written": 0, "detail": f"iteration {iteration} not found"}
    if idx == 0:
        return {"written": 0, "detail": "iteration 0 is the baseline — nothing to reflect on yet"}

    cite_idx = idx - 1
    entry = history[idx]
    cite_entry = history[cite_idx]
    sys_context = cite_entry.get("system_context", entry.get("system_context", ""))
    literature: list = cite_entry.get("literature", [])
    delta: dict = entry.get("delta", {})

    outcome_by_metric: Dict[str, str] = {}
    for item in delta.get("improved", []):
        outcome_by_metric[item.get("metric", "")] = "improved"
    for item in delta.get("resolved", []):
        # resolved entries are bare metric:view keys, not dicts
        outcome_by_metric[item if isinstance(item, str) else item.get("metric", "")] = "resolved"
    for item in delta.get("regressed", []):
        outcome_by_metric[item.get("metric", "")] = "regressed"

    written = []
    for lit_entry in literature:
        metric = lit_entry.get("bottleneck", "")
        papers = lit_entry.get("papers", [])
        if not papers:
            continue
        # Match on the bare metric name; delta keys are "metric:view".
        outcome = next(
            (v for k, v in outcome_by_metric.items() if k.split(":")[0] == metric),
            None,
        )
        top_paper = papers[0]
        citation = {
            "authors":   top_paper.get("authors", []),
            "title":     top_paper.get("title", ""),
            "venue":     f"arXiv {(top_paper.get('published') or '')[:4]}",
            "year":      (top_paper.get("published") or "")[:4],
            "url":       top_paper.get("url", ""),
        }
        record = _memory_write(
            metric=metric,
            sys_context=sys_context,
            strategy_title=top_paper.get("title", "")[:120],
            citation=citation,
            source="searched",
            outcome=outcome,
        )
        written.append({"metric": metric, "outcome": outcome, "key": record["key"]})

    return {"written": len(written), "detail": written}


def register_memory_tools(mcp: FastMCP) -> None:
    """Register the Tier-2 project-memory MCP tools."""

    @mcp.tool()
    def session_memory_retrieve(
        metric: str,
        sys_context: str = "",
        category: str = "",
        k: int = 3,
    ) -> str:
        """Read Tier-2 project memory for citations/strategies already
        validated for this (or a similar) bottleneck, before issuing a live
        arXiv/Semantic Scholar search.

        This is the "retrieve before you search" half of the agentic loop:
        call this first for every bottleneck in
        ``session_optimization_iteration``'s literature step; only fall back
        to a live query if nothing scores above the relevance threshold.

        Args:
            metric:      Bottleneck metric name (e.g. "small_io", "comm_wait").
            sys_context: Hardware/filesystem context string (from
                         ``_build_sys_context`` / the iteration tool's
                         ``system_context`` field) to prefer citations proven
                         on similar systems.
            category:    Optional explicit category override (io |
                         communication | memory | compute); inferred from
                         *metric* if omitted.
            k:           Max results to return (default 3).

        Returns:
            JSON with ``status``, ``matches`` (records with
            ``retrieval_score``, ``uses``, ``successes``, ``failures``,
            ``citation``), and ``count``.
        """
        matches = _memory_retrieve(metric, sys_context, category, k=k)
        return _ok(
            f"{len(matches)} memory match(es) for '{metric}'.",
            matches=matches,
            count=len(matches),
        )

    @mcp.tool()
    def session_memory_write(
        metric: str,
        sys_context: str,
        strategy_title: str,
        citation_json: str,
        source: str = "searched",
        outcome: Optional[str] = None,
    ) -> str:
        """Write path: record that *strategy_title* (backed by *citation_json*)
        was tried for *metric* on *sys_context*, optionally with an observed
        *outcome*.

        Args:
            metric:         Bottleneck metric name.
            sys_context:    Hardware/filesystem context string.
            strategy_title: The proposal/paper title used.
            citation_json:  JSON object with authors/title/venue/year/url.
            source:         "searched" (arXiv) or "builtin" (WisIO/Drishti/etc).
            outcome:        "improved" | "resolved" | "regressed" | omit if
                            not yet known.

        Returns:
            JSON with the upserted record.
        """
        try:
            citation = json.loads(citation_json) if citation_json else {}
        except Exception as exc:
            return _err(f"citation_json is not valid JSON: {exc}")
        record = _memory_write(metric, sys_context, strategy_title, citation, source, outcome)
        return _ok(f"Recorded memory entry for '{metric}'.", record=record)

    @mcp.tool()
    def session_memory_reflect(run_id: str, iteration: int = -1) -> str:
        """Agentic reflection: convert one iteration's observed bottleneck
        delta (improved/regressed/resolved) into persisted Tier-2 verdicts
        for the citations used in that iteration.

        Call this right after ``session_optimization_iteration`` so the next
        call to ``session_memory_retrieve`` — in this run or a future one —
        already knows whether the cited fix actually worked here.

        Args:
            run_id:    Session identifier.
            iteration: Which iteration's delta to reflect on (-1 = latest).

        Returns:
            JSON with ``written`` (count) and ``detail`` (per-metric outcome
            written).
        """
        result = _memory_reflect(run_id, iteration)
        return _ok(
            f"Reflected on iteration {iteration}: {result['written']} verdict(s) written.",
            **result,
        )

    @mcp.tool()
    def session_memory_stats() -> str:
        """Summarize the Tier-2 project memory store — counts, top strategies
        by confidence, and per-category breakdown. Useful for inspecting what
        the optimization loop has learned across all sessions."""
        records = _load_memory()
        if not records:
            return _ok("Memory store is empty.", count=0, by_category={}, top=[])

        by_category: Dict[str, int] = {}
        for r in records:
            by_category[r.get("category", "io")] = by_category.get(r.get("category", "io"), 0) + 1

        def _confidence(r: Dict[str, Any]) -> float:
            uses = max(1, r.get("uses", 0))
            return (r.get("successes", 0) - r.get("failures", 0)) / uses

        top = sorted(records, key=_confidence, reverse=True)[:10]
        top_summary = [
            {
                "metric": r.get("metric"),
                "strategy_title": r.get("strategy_title"),
                "uses": r.get("uses"),
                "successes": r.get("successes"),
                "failures": r.get("failures"),
                "confidence": round(_confidence(r), 3),
            }
            for r in top
        ]
        return _ok(
            f"{len(records)} record(s) in Tier-2 memory.",
            count=len(records),
            by_category=by_category,
            top=top_summary,
        )
