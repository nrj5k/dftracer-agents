"""Deterministic MCP tools for the AI/ML annotation pipeline.

``dftracer-ml-annotate`` describes the ML annotation flow as a long prose recipe:
discover files, sort them into entry / train / data / checkpoint / comm / other,
call ``python_annotate_ai_file`` with the right flags per bucket, then
cost-gate the leftovers. Executing that by hand costs one agent turn per file
and drifts between runs.

These tools do the same work deterministically:

* ``ml_categorize_files``  — bucket every Python file (skill Step 3b).
* ``ml_annotate_plan``     — buckets + per-function cost decisions, no writes.
* ``ml_annotate_project``  — execute the whole plan in a single call.

The prose recipe remains the **backup** path: use it when a tool is missing,
when a file needs judgement the classifier cannot make, or to repair a file the
tools got wrong.

Semantic regions (data, model, checkpoint, training step, comm) are annotated
because of what they mean, so they are never cost-gated. Only the leftover
"other" files go through ``python_cost`` (see ``dftracer-ml-annotate`` 4g).
"""
from __future__ import annotations

import json as _json
import re
from pathlib import Path
from typing import Any, Dict, List

from fastmcp import FastMCP

from .workspace import _ws, _ok, _err
from .python_cost import PY_ANNOTATION_SCORE_THRESHOLD, _estimate_file_impl
from .annotation_ai import _python_annotate_ai_file_impl
from .annotation_python import _python_annotate_file_impl
from .annotation_validate import _validate_impl


#: Paths never worth annotating.
_DEFAULT_EXCLUDES = (
    "test", "tests", "__pycache__", "setup.py", "conftest.py", "docs",
    ".git", "build", "dist", "site-packages", "third_party",
)

#: Bucket signatures, checked in priority order — a file lands in the FIRST
#: bucket it matches.
#:
#: Order matters and is deliberate: a *training* file is still a training file
#: even though it also saves checkpoints and builds a DataLoader, so ``train``
#: must be tested before ``ckpt``/``data``. Files land in ``ckpt`` only when
#: checkpointing is their purpose (no training loop of their own), and in
#: ``comm`` only when they do nothing more specific.
_BUCKET_PATTERNS: List[tuple] = [
    ("entry", (r'if\s+__name__\s*==\s*[\'"]__main__[\'"]', r"^\s*def\s+main\s*\(")),
    ("train", (r"\.backward\s*\(", r"optimizer\.step", r"^\s*def\s+train",
               r"^\s*def\s+fit\b", r"training_step")),
    ("data",  (r"__getitem__", r"\bDataset\b", r"\bDataLoader\b", r"collate_fn")),
    ("ckpt",  (r"save_checkpoint", r"load_checkpoint", r"state_dict",
               r"from_pretrained", r"save_pretrained")),
    ("comm",  (r"all_reduce", r"dist\.barrier", r"\bhvd\.", r"init_process_group",
               r"all_gather", r"broadcast")),
]


def _strip_noncode(source: str) -> str:
    """Return *source* with comments and string literals blanked out.

    Bucket patterns must match real code, not prose. Without this, a file whose
    only mention of ``broadcast`` is in a ``# broadcast the staging paths``
    comment is misfiled as a distributed-communication file.

    Uses ``tokenize`` so f-strings, docstrings, and nested quotes are handled
    correctly; falls back to the raw source if the file does not tokenize.
    """
    import io
    import tokenize

    lines = source.splitlines(keepends=True)
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return source

    # Blank each comment/string span in place, so line and column layout —
    # and therefore ``^``-anchored patterns — survive untouched.
    for tok in toks:
        if tok.type not in (tokenize.COMMENT, tokenize.STRING):
            continue
        (srow, scol), (erow, ecol) = tok.start, tok.end
        for row in range(srow, erow + 1):
            i = row - 1
            if i >= len(lines):
                break
            line = lines[i]
            nl = "\n" if line.endswith("\n") else ""
            body = line[:-1] if nl else line
            a = scol if row == srow else 0
            b = ecol if row == erow else len(body)
            b = min(b, len(body))
            if a < b:
                body = body[:a] + " " * (b - a) + body[b:]
            lines[i] = body + nl
    return "".join(lines)


def _iter_py_files(root: Path, excludes: tuple) -> List[Path]:
    out: List[Path] = []
    for f in sorted(root.rglob("*.py")):
        rel = f.relative_to(root)
        if any(part in excludes for part in rel.parts):
            continue
        if rel.name in excludes:
            continue
        out.append(f)
    return out


def _bucket_of(text: str) -> str:
    for name, pats in _BUCKET_PATTERNS:
        for p in pats:
            if re.search(p, text, re.M):
                return name
    return "other"


def _categorize(run_id: str, excludes: tuple,
                annotated_dir: str = "annotated") -> Dict[str, Any]:
    ws = _ws(run_id)
    root = ws / annotated_dir
    if not root.is_dir():
        root = ws / "source"
    if not root.is_dir():
        return {"error": f"no annotated/ or source/ tree for {run_id}"}

    buckets: Dict[str, List[str]] = {k: [] for k in
                                     ("entry", "train", "data", "ckpt", "comm", "other")}
    for f in _iter_py_files(root, excludes):
        try:
            text = f.read_text(errors="ignore")
        except OSError:
            continue
        # Match on code only — a `# broadcast the paths` comment must not make
        # this a distributed-communication file.
        buckets[_bucket_of(_strip_noncode(text))].append(str(f.relative_to(root)))
    return {"root": str(root), "buckets": buckets}


def _plan(run_id: str, threshold: int, excludes: tuple,
          annotated_dir: str = "annotated") -> Dict[str, Any]:
    cat = _categorize(run_id, excludes, annotated_dir)
    if "error" in cat:
        return cat
    buckets = cat["buckets"]
    plan: List[Dict[str, Any]] = []

    # 4a-4f: semantic files. The cost gate ALWAYS runs, and its allow-list is
    # passed down — but AI-API decorators (dft_ai.data.item, dft_ai.checkpoint,
    # dft_ai.pipeline.train, dft_ai.comm...) are exempt: those functions are
    # instrumented for what they MEAN, not what they cost, and the annotator keeps
    # them regardless of the allow-list. Everything else in the file must earn its
    # decorator, so a training script does not get one on every getter.
    for bucket in ("entry", "train", "data", "ckpt", "comm"):
        for fp in buckets[bucket]:
            est = _json.loads(_estimate_file_impl(run_id, fp, threshold, annotated_dir))
            allow = est.get("annotate", []) if est.get("status") == "ok" else []
            plan.append({
                "filepath": fp, "bucket": bucket, "tool": "python_annotate_ai_file",
                "is_entry": bucket == "entry", "annotate_loops": bucket in ("entry", "train", "data"),
                "cost_gated": True,
                "annotate_functions": allow,
                "skip_functions": est.get("skip", []) if est.get("status") == "ok" else [],
                "why": (f"semantic bucket ({bucket}); AI-API regions always annotated, "
                        f"{len(allow)} other function(s) scored >= {threshold}"),
            })

    # 4g: leftovers are cost-gated so the trace is not drowned in getters.
    for fp in buckets["other"]:
        est = _json.loads(_estimate_file_impl(run_id, fp, threshold, annotated_dir))
        if est.get("status") != "ok":
            plan.append({"filepath": fp, "bucket": "other", "tool": None,
                         "cost_gated": True, "skipped": True,
                         "why": est.get("message", "cost estimate failed")})
            continue
        if est["annotate"]:
            plan.append({
                "filepath": fp, "bucket": "other", "tool": "python_annotate_file",
                "is_entry": False, "cost_gated": True,
                "annotate_functions": est["annotate"], "skip_functions": est["skip"],
                "why": f"{len(est['annotate'])} function(s) scored >= {threshold}",
            })
        else:
            plan.append({"filepath": fp, "bucket": "other", "tool": None,
                         "cost_gated": True, "skipped": True,
                         "why": f"no function scored >= {threshold}"})
    cat["plan"] = plan
    cat["threshold"] = threshold
    return cat


def register_ml_pipeline_tools(mcp: FastMCP) -> None:
    """Register ``ml_categorize_files``, ``ml_annotate_plan``, ``ml_annotate_project``."""

    @mcp.tool()
    def ml_categorize_files(run_id: str, exclude: str = "",
                            annotated_dir: str = "annotated") -> str:
        """Bucket every Python file into entry/train/data/ckpt/comm/other.

        Deterministic replacement for ``dftracer-ml-annotate`` Step 3b. A file
        lands in the FIRST bucket it matches (entry > ckpt > data > comm >
        train), because a training script that also loads data is still a
        training script.

        Args:
            run_id: Session identifier returned by ``session_create``.
            exclude: Comma-separated extra path parts to skip.

        Returns:
            JSON with ``root`` and ``buckets`` (bucket name -> file list).
        """
        ex = _DEFAULT_EXCLUDES + tuple(x.strip() for x in exclude.split(",") if x.strip())
        res = _categorize(run_id, ex, annotated_dir)
        if "error" in res:
            return _err(res["error"])
        counts = {k: len(v) for k, v in res["buckets"].items()}
        return _ok(f"categorized {sum(counts.values())} python files", counts=counts, **res)

    @mcp.tool()
    def ml_annotate_plan(
        run_id: str,
        threshold: int = PY_ANNOTATION_SCORE_THRESHOLD,
        exclude: str = "",
        annotated_dir: str = "annotated",
    ) -> str:
        """Compute the full ML annotation plan WITHOUT writing anything.

        Combines categorization (4a-4f) with cost-gating of the leftover files
        (4g, via ``python_estimate_file_costs``). Review this before running
        ``ml_annotate_project``.

        Semantic buckets are marked ``cost_gated: false`` — they are always
        annotated. Only ``other`` files carry per-function annotate/skip lists.

        Args:
            run_id: Session identifier.
            threshold: Generic-function score threshold (see ``python_cost``).
            exclude: Comma-separated extra path parts to skip.

        Returns:
            JSON with ``buckets``, ``threshold``, and ``plan`` (one entry per
            file: ``tool``, ``bucket``, ``cost_gated``, ``why``, and for gated
            files ``annotate_functions`` / ``skip_functions``).
        """
        ex = _DEFAULT_EXCLUDES + tuple(x.strip() for x in exclude.split(",") if x.strip())
        res = _plan(run_id, threshold, ex, annotated_dir)
        if "error" in res:
            return _err(res["error"])
        acts = [p for p in res["plan"] if p.get("tool")]
        return _ok(f"plan: {len(acts)} file(s) to annotate, "
                   f"{len(res['plan']) - len(acts)} skipped", **res)

    @mcp.tool()
    def ml_annotate_project(
        run_id: str,
        threshold: int = PY_ANNOTATION_SCORE_THRESHOLD,
        exclude: str = "",
        logfile: str = "None",
        data_dir: str = "None",
        dry_run: bool = False,
        annotated_dir: str = "annotated",
        validate: bool = True,
    ) -> str:
        """Annotate an entire ML project in ONE call (fast path).

        Executes ``ml_annotate_plan``: semantic files go through
        ``python_annotate_ai_file`` (AI/ML region decorators, loop wrapping,
        ``initialize_log``/``finalize`` for entry points); leftover files go
        through ``python_annotate_file`` but only when the AI/ML cost estimator
        found a function worth instrumenting.

        Use the ``dftracer-ml-annotate`` prose recipe as the BACKUP path: when a
        file needs human judgement, when this tool errors, or to repair a file.

        Args:
            run_id: Session identifier.
            threshold: Generic-function score threshold.
            exclude: Comma-separated extra path parts to skip.
            logfile: ``initialize_log(logfile=...)`` for entry points.
            data_dir: ``initialize_log(data_dir=...)`` for entry points.
            dry_run: Compute the plan and report, but write nothing.

        Returns:
            JSON with ``annotated`` (per-file result summaries), ``skipped``,
            ``failed``, and the ``counts`` per bucket.
        """
        ex = _DEFAULT_EXCLUDES + tuple(x.strip() for x in exclude.split(",") if x.strip())
        res = _plan(run_id, threshold, ex, annotated_dir)
        if "error" in res:
            return _err(res["error"])
        if dry_run:
            return _ok("dry run — nothing written", **res)

        annotated: List[Dict[str, Any]] = []
        skipped: List[str] = []
        failed: List[Dict[str, str]] = []

        for step in res["plan"]:
            fp, tool = step["filepath"], step.get("tool")
            if not tool:
                skipped.append(fp)
                continue
            cat = Path(fp).stem
            try:
                if tool == "python_annotate_ai_file":
                    out = _python_annotate_ai_file_impl(
                        run_id=run_id, filepath=fp, category=cat,
                        is_entry=step["is_entry"], logfile=logfile, data_dir=data_dir,
                        annotate_loops=step["annotate_loops"],
                        annotated_dir=annotated_dir,
                        only_functions=",".join(step.get("annotate_functions", [])),
                    )
                else:
                    # Enforce the cost gate: decorate ONLY the functions the
                    # AI/ML estimator selected, not every function in the file.
                    out = _python_annotate_file_impl(
                        run_id=run_id, filepath=fp, category=cat, is_entry=False,
                        logfile=logfile, data_dir=data_dir,
                        only_functions=",".join(step.get("annotate_functions", [])),
                        annotated_dir=annotated_dir,
                    )
                parsed = _json.loads(out)
                if parsed.get("status") == "ok":
                    annotated.append({
                        "filepath": fp, "bucket": step["bucket"], "tool": tool,
                        "decorators": parsed.get("decorators", parsed.get("functions", 0)),
                        "with_regions": parsed.get("with_regions", 0),
                        "skipped_static": parsed.get("skipped_static", []) or [],
                        "message": parsed.get("message", ""),
                    })
                else:
                    failed.append({"filepath": fp, "error": parsed.get("message", "unknown")})
            except Exception as exc:  # a bad file must not abort the project
                failed.append({"filepath": fp, "error": f"{type(exc).__name__}: {exc}"})

        # Validation is part of the pipeline: an annotation run that produces
        # unparseable files or misses a checkpoint writer is a failed run, and the
        # caller must learn that here rather than at build time.
        validation = None
        if validate:
            validation = _json.loads(
                _validate_impl(run_id, "python", "", annotated_dir))

        total_regions = sum(a["with_regions"] for a in annotated)
        all_skipped_static = [s for a in annotated for s in a["skipped_static"]]

        msg = (f"annotated {len(annotated)} file(s) "
               f"({total_regions} contextual `with` region(s)), "
               f"skipped {len(skipped)}, failed {len(failed)}")
        if all_skipped_static:
            msg += (f"; {len(all_skipped_static)} static method(s) left for manual "
                    f"annotation (multi-line string bodies)")
        if validation is not None:
            msg += (f"; validation {'PASSED' if validation.get('passed') else 'FAILED'} "
                    f"({validation.get('total_findings', 0)} finding(s))")
        return _ok(
            msg,
            counts={k: len(v) for k, v in res["buckets"].items()},
            threshold=threshold, annotated=annotated, skipped=skipped, failed=failed,
            with_regions=total_regions, skipped_static=all_skipped_static,
            validation=validation,
        )
