"""Python-AST-backed annotation tools for dftracer source code instrumentation.

This module registers three MCP tools on a FastMCP instance:

* ``python_extract_functions``  — build an authoritative function map from a
                                   Python file using the built-in ``ast`` module
* ``python_annotate_file``      — whole-file annotation: insert dftracer
                                   decorators and init/fini stubs in one in-memory
                                   pass, writing the file exactly once
* ``python_write_annotated_file`` — flush the in-memory cache to disk

Python dftracer API
-------------------
The annotation inserts the following pydftracer constructs:

  from dftracer.python import dftracer, dft_fn as DFTracerFn

  # One instance per file, category = module name
  _dft = DFTracerFn("<MODULE>")

  # Entry-point files only (file containing main() or if __name__ == "__main__")
  _dft_log = dftracer.initialize_log(logfile=None, data_dir=None, process_id=None)

  # Per-function decorators (inserted before the first decorator or 'def'):
  @_dft.log          # regular functions and methods
  @_dft.log_init     # __init__ methods

  # @staticmethod methods use a CONTEXTUAL region, never @log_static: a decorator
  # fights @staticmethod ordering, while dft_fn is a context manager.
  @staticmethod
  def f(...):
      with DFTracerFn("<MODULE>", name="f"):
          ...

  # Entry-point cleanup (inserted at end of main() or script body):
  _dft_log.finalize()

Line-shift safety
-----------------
All insertion positions are computed from the original ``ast`` parse (which
sees the unmodified file), then sorted highest-line-number first and applied
to the in-memory line list in a single pass before writing.  Each insertion
therefore uses the original (unshifted) line numbers, avoiding the classic
off-by-one errors that occur when inserting top-to-bottom.
"""
from __future__ import annotations

import ast
import re
from typing import Dict, List

from fastmcp import FastMCP

from .workspace import _ws, _ok, _err

# ---------------------------------------------------------------------------
# Module-level in-memory file cache (shared with annotation_clang._FILE_CACHE)
# Maps (run_id, filepath) → list[str] lines (no trailing newline per line).
# ---------------------------------------------------------------------------
_PY_FILE_CACHE: Dict[tuple, List[str]] = {}

_PY_IMPORT = "from dftracer.python import dftracer, dft_fn as DFTracerFn"
_DFT_INIT  = "_dft_log = dftracer.initialize_log(logfile=None, data_dir=None, process_id=None)"
_DFT_FINI  = "_dft_log.finalize()"


def _last_import_idx(lines: List[str]) -> int:
    """Return the 0-based index AFTER the last top-level import line.

    Tracks preprocessor-style depth to stay out of try/except/if blocks
    where imports sometimes appear.
    """
    last = -1
    depth = 0
    for i, ln in enumerate(lines):
        s = ln.strip()
        # Simple depth tracking for indented blocks
        if depth == 0 and re.match(r'^(?:import |from .+ import )', s):
            last = i
        if s.endswith(':') and not s.startswith('#'):
            depth += 1
        elif depth > 0 and s and not ln[0].isspace():
            depth = 0
    return last + 1  # insert AFTER the last import



def _indent_of(lines: List[str], lineno: int) -> str:
    """Return the leading whitespace of 1-based *lineno*, or "" if out of range.

    ``finalize()`` inserted before a nested ``return`` must match THAT return's
    indentation, not main()'s body indent — otherwise it lands at the wrong depth
    and raises IndentationError.
    """
    if 1 <= lineno <= len(lines):
        line = lines[lineno - 1]
        return line[: len(line) - len(line.lstrip())]
    return ""


def _find_return_lines(fn_node: ast.AST) -> List[int]:
    """Return 1-based line numbers of all ``return`` statements in *fn_node*."""
    lines: List[int] = []
    for node in ast.walk(fn_node):
        # Skip nested function/class bodies — they have their own returns
        if node is fn_node:
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.Return):
            lines.append(node.lineno)
    return lines



def _multiline_string_rows(source: str) -> set:
    """Return the 1-based line numbers spanned by multi-line string literals.

    Re-indenting a function body would rewrite the *contents* of any triple-quoted
    string it spans, so bodies containing one are left untouched.
    """
    import io
    import tokenize
    rows: set = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.STRING and tok.end[0] > tok.start[0]:
                rows.update(range(tok.start[0], tok.end[0] + 1))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return rows


def _extract_functions_from_ast(source: str) -> List[dict]:
    """Parse *source* with ``ast`` and return a list of function-info dicts.

    Each dict has:
      name                  — function name
      qualname              — dotted qualified name (e.g. ``MyClass.method``)
      start_line            — 1-based line of the ``def``/``async def`` keyword
      decorator_insert_line — 1-based line before which the decorator is inserted
                              (= first existing decorator line, or start_line)
      body_first_line       — 1-based line of first statement in body
      end_line              — 1-based last line of the function (end_lineno)
      col_offset            — column of the ``def`` keyword (= indentation)
      is_init               — True for ``__init__``
      has_staticmethod      — True if ``@staticmethod`` is in the decorator list
      is_async              — True for ``async def``
      is_entry_point        — True for ``main`` or module-level ``__main__``
      source                — always ``"ast"``
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    results: List[dict] = []

    def _walk(node: ast.AST, prefix: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = f"{prefix}.{child.name}" if prefix else child.name
                # Where to insert the decorator: before the first existing
                # decorator, or before the 'def' line itself.
                if child.decorator_list:
                    dec_insert = child.decorator_list[0].lineno
                else:
                    dec_insert = child.lineno

                has_static = any(
                    (isinstance(d, ast.Name) and d.id == "staticmethod")
                    or (isinstance(d, ast.Attribute) and d.attr == "staticmethod")
                    for d in child.decorator_list
                )

                body_first = (
                    child.body[0].lineno if child.body else child.lineno + 1
                )
                body_col = (
                    child.body[0].col_offset if child.body else child.col_offset + 4
                )
                return_lines = _find_return_lines(child)

                results.append({
                    "name": child.name,
                    "qualname": qualname,
                    "start_line": child.lineno,
                    "decorator_insert_line": dec_insert,
                    "body_first_line": body_first,
                    "body_col_offset": body_col,
                    "end_line": child.end_lineno or child.lineno,
                    "return_lines": return_lines,
                    "col_offset": child.col_offset,
                    "is_init": child.name == "__init__",
                    "has_staticmethod": has_static,
                    "is_async": isinstance(child, ast.AsyncFunctionDef),
                    "is_entry_point": child.name == "main" and prefix == "",
                    "source": "ast",
                })
                # Recurse into nested functions / class bodies
                _walk(child, qualname)
            elif isinstance(child, ast.ClassDef):
                qualname = f"{prefix}.{child.name}" if prefix else child.name
                _walk(child, qualname)
            else:
                _walk(child, prefix)

    _walk(tree)
    return results



# ---------------------------------------------------------------------------
# Module-level implementation so orchestrators (ml_pipeline) can drive the
# same generic annotation logic without the MCP tool wrapper.
# ---------------------------------------------------------------------------
def _python_annotate_file_impl(
    run_id: str,
    filepath: str,
    category: str = "",
    is_entry: bool = False,
    logfile: str = "None",
    data_dir: str = "None",
    process_id: int = -1,
    annotate_nested: bool = True,
    only_functions: str = "",
    annotated_dir: str = "annotated",
) -> str:
    """Annotate a Python file with dftracer decorators in a single in-memory pass.

    This is the preferred way to instrument a whole Python file.  It:

    1. Loads the file into memory (from cache or disk).
    2. Parses it with ``ast`` to build an authoritative function map.
    3. Computes all insertion points:

       * ``from dftracer.python import dftracer, dft_fn as DFTracerFn``
         after the last top-level import.
       * ``_dft = DFTracerFn("<category>")``
         immediately after the import (one instance per file).
       * For entry files (``is_entry=True``):
         ``_dft_log = dftracer.initialize_log(...)``
         right after the ``_dft`` line.
       * ``@_dft.log`` / ``@_dft.log_init``; static methods get a
         contextual ``with DFTracerFn(...)`` region instead of ``@log_static``
         before each function's first decorator or ``def`` line.
       * For entry files:
         ``_dft_log.finalize()`` before the last ``return`` in ``main()``,
         or appended to the end of the script if no ``main()`` is found.

    4. Sorts all insertion points **highest-line-number first** so that each
       insertion does not shift earlier (lower-numbered) positions.
    5. Applies every insertion to the in-memory line list.
    6. Writes the file once and caches the result.

    The file is modified inside the ``annotated/`` subfolder; the original
    ``source/`` copy is never touched.  Operation is idempotent: if the
    dftracer import is already present the file is returned unchanged.

    Args:
        run_id:          Session identifier returned by ``session_create``.
        filepath:        Path relative to the ``annotated/`` subfolder.
        category:        Category string for ``dft_fn("<category>")``.
                         Defaults to the stem of the filename
                         (e.g. ``"train"`` for ``train.py``).
        is_entry:        ``True`` when this file is the program entry point
                         (has ``main()`` or ``if __name__ == "__main__"``).
                         Controls insertion of ``initialize_log`` /
                         ``finalize()``.
        logfile:         ``logfile`` argument for ``initialize_log``; pass
                         as a Python expression string (default ``"None"``).
        data_dir:        ``data_dir`` argument for ``initialize_log``.
        process_id:      ``process_id`` argument for ``initialize_log``
                         (default ``-1``).
        annotate_nested: If ``True`` (default), annotate nested functions
                         and class methods.  Set to ``False`` to annotate
                         only top-level functions.

    Returns:
        JSON string with keys:
            * ``status``           — ``"ok"`` or ``"error"``.
            * ``message``          — human-readable summary.
            * ``filepath``         — echoed input path.
            * ``insertions``       — total lines inserted.
            * ``functions``        — number of functions decorated.
            * ``total_lines``      — line count after annotation.
            * ``already_annotated``— ``True`` if skipped (already done).
    """
    from pathlib import Path

    ws = _ws(run_id)
    abs_path = ws / annotated_dir / filepath

    cache_key = (run_id, filepath)
    if cache_key in _PY_FILE_CACHE:
        lines = list(_PY_FILE_CACHE[cache_key])
    elif abs_path.exists():
        lines = abs_path.read_text(errors="replace").splitlines()
    else:
        return _err(f"File not found in annotated/: {filepath}")

    text = "\n".join(lines)

    # Idempotency guard
    if "from dftracer.python import" in text and "DFTracerFn" in text:
        return _ok(
            f"{filepath} is already annotated — skipped.",
            filepath=filepath,
            insertions=0,
            functions=0,
            total_lines=len(lines),
            already_annotated=True,
        )

    # Default category = module stem
    cat = category or Path(filepath).stem

    # ── Step 1: parse function map ─────────────────────────────────────
    all_fns = _extract_functions_from_ast(text)
    if not annotate_nested:
        all_fns = [f for f in all_fns if "." not in f["qualname"]]

    # Selective mode: restrict to an explicit allow-list of function names.
    # This is how the AI/ML cost gate (python_estimate_file_costs) is enforced —
    # without it, every getter and one-line helper gets a decorator and the
    # trace drowns in noise.
    if only_functions:
        wanted = {n.strip() for n in only_functions.split(",") if n.strip()}
        # Prefer exact qualname matches. A bare name is honoured only when it is
        # unambiguous in this file — otherwise `__init__` would select the
        # __init__ of every class.
        names = [f["name"] for f in all_fns]
        all_fns = [
            f for f in all_fns
            if f["qualname"] in wanted
            or (f["name"] in wanted and names.count(f["name"]) == 1)
        ]

    # ── Step 2: build import/init block (inserted after last import) ──
    import_idx = _last_import_idx(lines)  # 0-based index to insert at

    init_lines: List[str] = [
        _PY_IMPORT,
        f'_dft = DFTracerFn("{cat}")',
    ]
    if is_entry:
        init_lines.append(
            f"_dft_log = dftracer.initialize_log("
            f"logfile={logfile}, data_dir={data_dir}, process_id={process_id})"
        )

    # ── Step 3: build per-function decorator insertions ───────────────
    # Each entry: (0-based-index, text-to-insert)
    insertions: List[tuple] = []

    # The init block is a multi-line insertion at import_idx — add each
    # line as a separate insertion so they end up in order.  Since we
    # sort highest-first and all have the same index, we add them in
    # REVERSE order so the sort produces the right final sequence.
    for ln in reversed(init_lines):
        insertions.append((import_idx, ln))

    # Static methods are instrumented with a contextual `with` region inside the
    # body, never with @log_static: a decorator has to fight @staticmethod
    # ordering, whereas `dft_fn` is a context manager (__enter__/__exit__) and
    # nests correctly. Collected here, applied after the decorator insertions.
    regions: List[dict] = []
    skipped_static: List[str] = []
    multiline_str_rows = _multiline_string_rows(text)

    for fn in all_fns:
        dec_idx = fn["decorator_insert_line"] - 1  # 0-based
        indent   = " " * fn["col_offset"]

        if fn["has_staticmethod"] and not fn["is_init"]:
            body_rows = range(fn["body_first_line"], fn["end_line"] + 1)
            if multiline_str_rows.intersection(body_rows):
                # Re-indenting would rewrite the contents of a multi-line
                # literal. Leave it alone rather than corrupt the source.
                skipped_static.append(fn["qualname"])
                continue
            regions.append(fn)
            continue

        if fn["is_init"]:
            dec = f"{indent}@_dft.log_init"
        else:
            dec = f"{indent}@_dft.log"

        insertions.append((dec_idx, dec))

    # Entry-point finalize: insert before every return in main(), and
    # before the function's closing line if main() has no explicit return.
    if is_entry:
        main_fn = next((f for f in all_fns if f["is_entry_point"]), None)
        if main_fn:
            body_ind = " " * main_fn.get("body_col_offset", 4)
            fini_line = f"{body_ind}{_DFT_FINI}"
            returns = main_fn.get("return_lines", [])
            if returns:
                for ret_line in returns:
                    ind = _indent_of(lines, ret_line)
                    insertions.append((ret_line - 1, f"{ind}{_DFT_FINI}"))
            else:
                # end_line is the 1-based LAST line of main(); insert AFTER it so
                # finalize() does not become the first statement of the function.
                insertions.append((main_fn["end_line"], fini_line))
        else:
            # No main() found — append at end of file
            insertions.append((len(lines), _DFT_FINI))

    # ── Step 4: sort highest-first, apply in one pass ─────────────────
    # For ties (same index): the item added LAST in the list should land
    # HIGHEST in the file after insertion.  reversed + stable sort achieves
    # this: items with higher idx go first; for ties, last-added goes first
    # which means it gets inserted last at that position, pushing earlier
    # items up (so first-added ends up topmost at that position).
    insertions.sort(key=lambda x: -x[0])

    for idx, txt in insertions:
        lines.insert(idx, txt)

    # ── Step 4b: contextual `with` regions for @staticmethod ──────────
    # Applied after the decorator insertions, against a FRESH parse, so the line
    # numbers are correct. Bottom-up, so earlier functions keep their positions.
    if regions:
        want = {fn["qualname"] for fn in regions}
        try:
            fresh = _extract_functions_from_ast("\n".join(lines))
        except Exception:
            fresh = []
        targets = sorted(
            (f for f in fresh if f["qualname"] in want),
            key=lambda f: -f["body_first_line"],
        )
        for fn in targets:
            b0 = fn["body_first_line"] - 1          # 0-based first body line
            e0 = fn["end_line"] - 1                 # 0-based last body line
            body_ind = " " * fn["body_col_offset"]
            for i in range(b0, min(e0, len(lines) - 1) + 1):
                if lines[i].strip():                # never indent blank lines
                    lines[i] = "    " + lines[i]
            lines.insert(
                b0,
                f'{body_ind}with DFTracerFn("{cat}", name="{fn["name"]}"):',
            )

    # ── Step 5: write once ────────────────────────────────────────────
    _PY_FILE_CACHE[cache_key] = list(lines)
    abs_path.write_text("\n".join(lines) + "\n")

    fn_count = len(all_fns)
    msg = (f"Annotated {filepath}: {len(insertions)} line(s) inserted "
           f"({fn_count} function(s) instrumented, {len(regions)} `with` region(s)).")
    if skipped_static:
        msg += (f" SKIPPED {len(skipped_static)} static method(s) whose body spans a "
                f"multi-line string — annotate by hand: {', '.join(skipped_static)}.")
    return _ok(
        msg,
        filepath=filepath,
        insertions=len(insertions),
        functions=fn_count,
        with_regions=len(regions),
        skipped_static=skipped_static,
        total_lines=len(lines),
        already_annotated=False,
    )


def register_python_tools(mcp: FastMCP) -> None:
    """Register Python annotation tools on *mcp*."""

    @mcp.tool()
    def python_extract_functions(run_id: str, filepath: str) -> str:
        """Extract function definitions with exact line numbers from a Python file.

        Uses Python's built-in ``ast`` module (Python 3.8+ ``end_lineno``
        support) to produce an authoritative function map.  No external
        dependencies required.

        Each returned record contains:

        * ``name``                  — function name
        * ``qualname``              — dotted qualified name (``Class.method``)
        * ``start_line``            — 1-based line of the ``def`` keyword
        * ``decorator_insert_line`` — insert dftracer decorator **before** this
                                       line (= first existing decorator or ``def``)
        * ``body_first_line``       — first line of the function body
        * ``end_line``              — last line of the function
        * ``col_offset``            — column of ``def`` (reflects indentation)
        * ``is_init``               — ``True`` for ``__init__`` methods
        * ``has_staticmethod``      — ``True`` if ``@staticmethod`` already present
        * ``is_async``              — ``True`` for ``async def``
        * ``is_entry_point``        — ``True`` for module-level ``main()``
        * ``source``                — always ``"ast"``

        Decorator insertion rule (from the pydftracer API):
          * ``@_dft.log_init``    for ``is_init`` functions
          * a contextual ``with DFTracerFn(...)`` region for ``has_staticmethod``
          * ``@_dft.log``         for everything else

        Args:
            run_id:   Session identifier returned by ``session_create``.
            filepath: Path relative to the ``annotated/`` subfolder.

        Returns:
            JSON string with keys ``status``, ``message``, ``filepath``,
            ``functions`` (list of dicts), ``count``, ``extractor``.
        """
        ws = _ws(run_id)
        abs_path = ws / "annotated" / filepath

        cache_key = (run_id, filepath)
        if cache_key in _PY_FILE_CACHE:
            source = "\n".join(_PY_FILE_CACHE[cache_key])
        elif abs_path.exists():
            source = abs_path.read_text(errors="replace")
        else:
            return _err(f"File not found in annotated/: {filepath}")

        functions = _extract_functions_from_ast(source)
        return _ok(
            f"Extracted {len(functions)} function(s) from {filepath} using ast.",
            filepath=filepath,
            functions=functions,
            count=len(functions),
            extractor="ast",
        )

    @mcp.tool()
    def python_annotate_file(
        run_id: str,
        filepath: str,
        category: str = "",
        is_entry: bool = False,
        logfile: str = "None",
        data_dir: str = "None",
        process_id: int = -1,
        annotate_nested: bool = True,
        only_functions: str = "",
    ) -> str:
        """Annotate a Python file with generic dftracer decorators.

        Delegates to :func:`_python_annotate_file_impl`.

        ``only_functions`` is a comma-separated allow-list of function names; when
        given, ONLY those functions are decorated. Pass the ``annotate`` list from
        ``python_estimate_file_costs`` to enforce AI/ML cost gating.
        """
        return _python_annotate_file_impl(run_id=run_id, filepath=filepath, category=category, is_entry=is_entry, logfile=logfile, data_dir=data_dir, process_id=process_id, annotate_nested=annotate_nested, only_functions=only_functions)

    @mcp.tool()
    def python_write_annotated_file(run_id: str, filepath: str) -> str:
        """Flush the in-memory annotated Python file buffer to disk.

        Call this after a series of in-memory annotation operations to commit
        all changes with a single write.  If the file is not in the in-memory
        cache, returns an error — call ``python_annotate_file`` first.

        Args:
            run_id:   Session identifier returned by ``session_create``.
            filepath: Path relative to the ``annotated/`` subfolder.

        Returns:
            JSON string with ``status``, ``message``, ``filepath``,
            ``total_lines``.
        """
        cache_key = (run_id, filepath)
        if cache_key not in _PY_FILE_CACHE:
            return _err(
                f"No in-memory state for {filepath} — "
                f"call python_annotate_file first.",
                filepath=filepath,
            )
        ws = _ws(run_id)
        abs_path = ws / "annotated" / filepath
        lines = _PY_FILE_CACHE[cache_key]
        abs_path.write_text("\n".join(lines) + "\n")
        del _PY_FILE_CACHE[cache_key]
        return _ok(
            f"Wrote {len(lines)} lines to {filepath}.",
            filepath=filepath,
            total_lines=len(lines),
        )
