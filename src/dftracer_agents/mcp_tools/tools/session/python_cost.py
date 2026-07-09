"""AI/ML-aware cost estimation for Python functions.

The C/C++ pipeline gates generic instrumentation behind
``clang_estimate_function_cost``: cheap functions are skipped so the trace is not
drowned in noise from getters and one-line helpers. Python had no equivalent —
the shared scorer in ``source_parser._compute_cost_python`` only recognises
POSIX and MPI names, so in an AI/ML workload it sees ``torch.load``,
``h5py.File``, ``DataLoader``, ``loss.backward()`` and ``dist.all_reduce`` as
ordinary calls worth 2 points each. Everything looks cheap, so either everything
gets annotated or nothing does.

This module scores Python functions with signals that actually matter for
AI/ML I/O tracing, and returns an ``annotate`` / ``skip`` recommendation.

**Scope — read this before using it.** The recommendation governs *generic*
function annotation only (``@_dlp.log`` / ``dlp.init``). AI/ML *semantic* regions
— data loading, model init, checkpoint, training step, distributed comm — are
annotated because of what they mean, not what they cost, and must be applied
regardless of score. See ``dftracer-ml-annotate`` sections 4a–4f (always) vs
4g (cost-gated).
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP

from .workspace import _ws, _ok, _err


#: Default score at or above which a generic function is worth instrumenting.
PY_ANNOTATION_SCORE_THRESHOLD = 20

# --------------------------------------------------------------------------
# Signal vocabularies. Matched against the *called name*: for ``a.b.c(x)`` we
# look at ``c`` (the attribute), for ``f(x)`` at ``f``.
# --------------------------------------------------------------------------

#: File / dataset I/O, including framework readers. These dominate ML I/O traces.
_ML_IO_NAMES: frozenset = frozenset({
    "open", "read", "write", "close", "readlines", "writelines",
    "load", "save", "savez", "savez_compressed", "loadtxt", "savetxt",
    "fromfile", "tofile", "memmap",
    "read_csv", "read_parquet", "read_json", "read_hdf", "to_csv",
    "to_parquet", "to_hdf",
    "imread", "imwrite", "imsave",
    "File", "create_dataset",              # h5py
    "dump", "dumps", "loads",              # pickle / json
    "download", "upload", "get_object", "put_object",
    "decode", "encode",                    # codec paths on the data pipeline
})

#: Checkpoint / model persistence — always worth tracing.
_ML_CKPT_NAMES: frozenset = frozenset({
    "state_dict", "load_state_dict", "save_checkpoint", "load_checkpoint",
    "from_pretrained", "save_pretrained", "save_model", "load_model",
    "torch_save", "torch_load",
})

#: Distributed communication (torch.distributed, horovod, mpi4py).
_ML_COMM_NAMES: frozenset = frozenset({
    "all_reduce", "all_gather", "all_to_all", "reduce_scatter",
    "broadcast", "barrier", "reduce", "scatter", "gather",
    "send", "recv", "isend", "irecv", "init_process_group",
    "Send", "Recv", "Bcast", "Gather", "Scatter", "Allreduce",
    "Allgather", "Alltoall", "Barrier", "Sendrecv", "Waitall",
})

#: Data pipeline: loaders, samplers, collation, prefetch.
_ML_DATA_NAMES: frozenset = frozenset({
    "DataLoader", "Dataset", "IterableDataset", "collate_fn", "default_collate",
    "prefetch", "shuffle", "batch", "map", "sampler", "next", "iter",
})

#: Host<->device transfers. Cheap individually, expensive in aggregate.
_ML_XFER_NAMES: frozenset = frozenset({
    "to", "cuda", "cpu", "numpy", "item", "detach", "clone",
    "pin_memory", "contiguous", "from_numpy", "as_tensor",
})

#: Training / autograd compute.
_ML_COMPUTE_NAMES: frozenset = frozenset({
    "backward", "step", "zero_grad", "forward", "no_grad", "autocast",
    "scale", "unscale_", "clip_grad_norm_", "synchronize",
})

#: Dunder methods that are pure protocol glue — never instrument (Rule 0).
_TRIVIAL_DUNDERS: frozenset = frozenset({
    "__repr__", "__str__", "__len__", "__eq__", "__ne__", "__hash__",
    "__format__", "__bool__", "__lt__", "__gt__", "__le__", "__ge__",
})

#: Dunder / method names that ARE the data pipeline — always instrument.
_MANDATORY_DUNDERS: frozenset = frozenset({
    "__getitem__", "__iter__", "__next__",
})

#: Name suffixes/prefixes that mark an I/O or lifecycle boundary (Rule R6).
_MANDATORY_NAME_PARTS = (
    "_load", "_save", "_read", "_write", "_fetch", "_push", "_dump",
    "load_", "save_", "read_", "write_", "checkpoint", "_init", "_finalize",
    "train_step", "training_step", "validation_step", "test_step",
)

_SKIP_DECORATORS = frozenset({"property", "cached_property", "staticmethod_noop"})


def _called_name(node: ast.Call) -> str:
    """Return the bare callee name for ``f(...)`` or ``a.b.f(...)``."""
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def _decorator_names(fn: ast.AST) -> List[str]:
    names: List[str] = []
    for d in getattr(fn, "decorator_list", []) or []:
        if isinstance(d, ast.Name):
            names.append(d.id)
        elif isinstance(d, ast.Attribute):
            names.append(d.attr)
        elif isinstance(d, ast.Call):
            names.append(_called_name(d))
    return names


def compute_python_ml_cost(fn: ast.AST) -> Dict[str, Any]:
    """Score one Python function for AI/ML instrumentation value.

    Walks the function's AST (no regex, no text scanning) and counts signals,
    then combines them with weights chosen so that a single real I/O or
    checkpoint call outweighs any amount of arithmetic.

    Weights: io x30, checkpoint x35, comm x25, data x20, xfer x12, compute x10,
    loop x8, branch x3, call x2, plus a body-size bonus capped at 20.
    """
    counts = dict(io_calls=0, ckpt_calls=0, comm_calls=0, data_calls=0,
                  xfer_calls=0, compute_calls=0, loop_count=0,
                  branch_count=0, call_count=0, node_count=0)

    for node in ast.walk(fn):
        counts["node_count"] += 1
        if isinstance(node, ast.Call):
            counts["call_count"] += 1
            name = _called_name(node)
            # Checked most-specific first: a name can appear in several sets
            # (e.g. "save"), and checkpoint/io must win over data/compute.
            if name in _ML_CKPT_NAMES:
                counts["ckpt_calls"] += 1
            elif name in _ML_IO_NAMES:
                counts["io_calls"] += 1
            elif name in _ML_COMM_NAMES:
                counts["comm_calls"] += 1
            elif name in _ML_DATA_NAMES:
                counts["data_calls"] += 1
            elif name in _ML_COMPUTE_NAMES:
                counts["compute_calls"] += 1
            elif name in _ML_XFER_NAMES:
                counts["xfer_calls"] += 1
        elif isinstance(node, (ast.For, ast.While, ast.AsyncFor)):
            counts["loop_count"] += 1
        elif isinstance(node, ast.If):
            counts["branch_count"] += 1

    counts["score"] = (
        counts["io_calls"] * 30
        + counts["ckpt_calls"] * 35
        + counts["comm_calls"] * 25
        + counts["data_calls"] * 20
        + counts["xfer_calls"] * 12
        + counts["compute_calls"] * 10
        + counts["loop_count"] * 8
        + counts["branch_count"] * 3
        + counts["call_count"] * 2
        + min(counts["node_count"] // 5, 20)
    )
    return counts


def _body_stmt_count(fn: ast.AST) -> int:
    return len(getattr(fn, "body", []) or [])


def recommend(fn: ast.AST, threshold: int = PY_ANNOTATION_SCORE_THRESHOLD) -> Dict[str, Any]:
    """Return an annotate/skip recommendation for one function.

    Override order (first match wins):

    1. ``@property`` / trivial dunder with no I/O  -> **skip** (Rule 0). A
       property that performs I/O is *not* skipped.
    2. Data-pipeline dunders (``__getitem__``/``__iter__``/``__next__``) -> **annotate**.
    3. Any checkpoint, I/O, or comm call present -> **annotate** (Rule R6/R7).
    4. Name marks an I/O or lifecycle boundary -> **annotate**.
    5. Trivial body (<=5 statements, no loop, no I/O signal) -> **skip**.
    6. Otherwise score vs *threshold*.
    """
    name = getattr(fn, "name", "")
    cost = compute_python_ml_cost(fn)
    decos = _decorator_names(fn)
    has_io_signal = bool(cost["io_calls"] or cost["ckpt_calls"] or cost["comm_calls"])

    def out(rec: str, why: str, cat: str) -> Dict[str, Any]:
        return {
            "function": name, "recommendation": rec, "reason": why,
            "category": cat, "score": cost["score"], "threshold": threshold,
            "cost_info": cost,
        }

    if not has_io_signal and (
        any(d in _SKIP_DECORATORS for d in decos) or name in _TRIVIAL_DUNDERS
    ):
        return out("skip", "Rule 0: property/trivial dunder with no I/O", "trivial")

    if name in _MANDATORY_DUNDERS:
        return out("annotate", "data-pipeline dunder (always instrument)", "data")

    if cost["ckpt_calls"]:
        return out("annotate", "checkpoint/model persistence call present", "checkpoint")
    if cost["io_calls"]:
        return out("annotate", "file/dataset I/O call present", "io")
    if cost["comm_calls"]:
        return out("annotate", "distributed communication call present", "comm")

    low = name.lower()
    if any(part in low for part in _MANDATORY_NAME_PARTS):
        return out("annotate", "name marks an I/O or lifecycle boundary", "lifecycle")

    if (_body_stmt_count(fn) <= 5 and not cost["loop_count"] and not has_io_signal):
        return out("skip", "Rule 0: trivial body, no loops, no I/O", "trivial")

    if cost["score"] >= threshold:
        cat = "compute" if cost["compute_calls"] else "generic"
        return out("annotate", f"score {cost['score']} >= threshold {threshold}", cat)

    return out("skip", f"score {cost['score']} < threshold {threshold}", "cheap")


def _iter_functions(tree: ast.AST):
    """Yield ``(node, qualname)`` for every function/method, nested and async.

    Qualnames matter: a file with several classes has several ``__init__``s, and a
    bare-name allow-list would select all of them when the estimator chose one.
    """
    def walk(node, prefix=""):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}.{child.name}" if prefix else child.name
                yield child, qual
                yield from walk(child, qual)
            elif isinstance(child, ast.ClassDef):
                qual = f"{prefix}.{child.name}" if prefix else child.name
                yield from walk(child, qual)
            else:
                yield from walk(child, prefix)
    yield from walk(tree)


def _resolve(run_id: str, filepath: str,
             annotated_dir: str = "annotated") -> Optional[Path]:
    ws = _ws(run_id)
    for sub in (annotated_dir, "source", "."):
        cand = ws / sub / filepath if sub != "." else ws / filepath
        if cand.is_file():
            return cand
    return None


def _estimate_file_impl(run_id: str, filepath: str, threshold: int,
                        annotated_dir: str = "annotated") -> str:
    p = _resolve(run_id, filepath, annotated_dir)
    if p is None:
        return _err(f"File not found in annotated/ or source/: {filepath}")
    try:
        tree = ast.parse(p.read_text(errors="ignore"))
    except SyntaxError as exc:
        return _err(f"SyntaxError parsing {filepath}: {exc}")

    results = []
    for fn, qual in _iter_functions(tree):
        r = recommend(fn, threshold)
        r["qualname"] = qual
        results.append(r)
    ann = [r["qualname"] for r in results if r["recommendation"] == "annotate"]
    skip = [r["qualname"] for r in results if r["recommendation"] == "skip"]
    return _ok(
        f"{len(ann)} to annotate, {len(skip)} to skip in {filepath}",
        filepath=filepath, threshold=threshold,
        annotate=ann, skip=skip, functions=results,
    )


def register_python_cost_tools(mcp: FastMCP) -> None:
    """Register ``python_estimate_function_cost`` and ``python_estimate_file_costs``."""

    @mcp.tool()
    def python_estimate_function_cost(
        run_id: str,
        filepath: str,
        function_name: str,
        threshold: int = PY_ANNOTATION_SCORE_THRESHOLD,
    ) -> str:
        """Estimate the instrumentation value of ONE Python function (AI/ML aware).

        Use this to decide whether a function deserves *generic* dftracer
        instrumentation (``@_dlp.log``). It is the Python counterpart of
        ``clang_estimate_function_cost``, but scored on signals that matter for
        AI/ML workloads rather than POSIX/MPI symbols.

        **Do NOT gate AI/ML semantic regions on this.** Data loading, model
        init, checkpoint, training step, and distributed comm are annotated for
        what they mean, not what they cost — always annotate those
        (``dftracer-ml-annotate`` 4a–4f). This tool governs section 4g only.

        Signals counted from the AST (no regex): framework I/O
        (``torch.load``/``save``, ``h5py.File``, ``np.load``, ``read_parquet``,
        ``pickle``…), checkpoint/persistence (``state_dict``,
        ``from_pretrained``…), distributed comm (``all_reduce``, ``barrier``…),
        data pipeline (``DataLoader``, ``collate``…), host/device transfers
        (``.to()``, ``.cuda()``, ``.numpy()``…), autograd/compute
        (``backward``, ``step``…), loops, branches, calls, body size.

        Weights: io×30, checkpoint×35, comm×25, data×20, xfer×12, compute×10,
        loop×8, branch×3, call×2, size bonus ≤20.

        Overrides, in order: ``@property``/trivial dunder without I/O → skip;
        ``__getitem__``/``__iter__``/``__next__`` → annotate; any
        checkpoint/I/O/comm call → annotate; I/O or lifecycle name → annotate;
        trivial body (≤5 statements, no loop, no I/O) → skip; else score vs
        ``threshold``.

        Args:
            run_id: Session identifier returned by ``session_create``.
            filepath: Path relative to ``annotated/`` (falls back to ``source/``).
            function_name: Exact function or method name to evaluate.
            threshold: Score at/above which a generic function is annotated.

        Returns:
            JSON with ``recommendation`` (``"annotate"``/``"skip"``), ``score``,
            ``threshold``, ``category``, ``reason``, and a ``cost_info``
            breakdown.
        """
        p = _resolve(run_id, filepath)
        if p is None:
            return _err(f"File not found in annotated/ or source/: {filepath}")
        try:
            tree = ast.parse(p.read_text(errors="ignore"))
        except SyntaxError as exc:
            return _err(f"SyntaxError parsing {filepath}: {exc}")
        target = next((f for f, q in _iter_functions(tree)
                       if f.name == function_name or q == function_name), None)
        if target is None:
            return _err(f"Function not found: {function_name}", filepath=filepath)
        return _ok(f"cost estimate for {function_name}", **recommend(target, threshold))

    @mcp.tool()
    def python_estimate_file_costs(
        run_id: str,
        filepath: str,
        threshold: int = PY_ANNOTATION_SCORE_THRESHOLD,
    ) -> str:
        """Score EVERY function in a Python file and return annotate/skip lists.

        One call per file instead of one per function — use this to drive
        selective generic annotation (``dftracer-ml-annotate`` 4g). Same scoring
        and overrides as ``python_estimate_function_cost``.

        Args:
            run_id: Session identifier returned by ``session_create``.
            filepath: Path relative to ``annotated/`` (falls back to ``source/``).
            threshold: Score at/above which a generic function is annotated.

        Returns:
            JSON with ``annotate`` (list of names), ``skip`` (list of names),
            and ``functions`` (per-function recommendation + cost breakdown).
        """
        return _estimate_file_impl(run_id, filepath, threshold)
