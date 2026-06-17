"""Clang-backed annotation tools for dftracer source code instrumentation.

This module registers six MCP tools on a FastMCP instance:

* ``clang_add_braces``              — ensure every braceless if/for/while body has {}
* ``clang_extract_functions``       — build an authoritative function map with exact lines
* ``clang_insert_line``             — insert a single code line at an exact line number
* ``clang_annotate_file``           — full-file annotation: insert all dftracer macros
                                       accounting for line-number shifts automatically
* ``clang_write_annotated_file``    — flush the in-memory file buffer to disk
* ``clang_estimate_function_cost``  — heuristic cost estimate; returns skip/annotate recommendation

These tools are designed to be called by annotation sub-agents before and during
the macro insertion loop (Steps 2a/2b and 4 of annotate-c.yaml / annotate-cpp.yaml).

In-memory state
---------------
``clang_annotate_file`` accumulates all insertions in memory and writes to disk
once via a single ``write_text`` call.  For workflows that need multiple passes
(e.g. add braces, then annotate, then insert metadata), use the in-memory cache:

* Load a file:  ``clang_annotate_file`` (or ``clang_add_braces``) automatically
  operates on the live file — all edits are in-memory until the final write.
* After all tools have finished editing, call ``clang_write_annotated_file`` to
  flush the result to disk.

Line-shift safety
-----------------
When inserting multiple lines into a file, each insertion shifts subsequent line
numbers by 1.  ``clang_insert_line`` operates on the *current* file state, so
callers that loop over a list of insertion points must process them in
**highest-line-number-first** order to avoid stale positions.

``clang_annotate_file`` handles this automatically: it collects every insertion
point from the function map (produced by ``clang_extract_functions``), sorts them
highest-first, and applies all insertions in a single pass before writing the file.
Use ``clang_annotate_file`` for whole-file annotation; use ``clang_insert_line``
only for individual, ad-hoc corrections.
"""
from __future__ import annotations

import re
from typing import Dict, List

from fastmcp import FastMCP

from .workspace import _ws, _ok, _err

# ---------------------------------------------------------------------------
# Module-level in-memory file cache
# ---------------------------------------------------------------------------
# Maps (run_id, filepath) → list[str] of lines (no trailing newline per line).
# Populated by clang_annotate_file / clang_add_braces; flushed by
# clang_write_annotated_file.  This allows multiple tools to collaborate on
# the same file without intermediate disk writes.
_FILE_CACHE: Dict[tuple, List[str]] = {}


def register_clang_tools(mcp: FastMCP) -> None:
    """Register all clang annotation tools on *mcp*."""

    @mcp.tool()
    def clang_add_braces(run_id: str, filepath: str) -> str:
        """Add ``{`` / ``}`` around braceless ``if`` / ``for`` / ``while`` bodies.

        Rewrites *filepath* inside the ``annotated/`` subfolder so that every
        braceless single-statement body gains an explicit ``CompoundStmt``
        block.  Run this **before** annotation so that inserting a
        ``DFTRACER_C_FUNCTION_END()`` before a ``return`` never creates a
        dangling-else or mismatched-brace syntax error.

        Uses clang ``-ast-dump=json`` for precise AST-level detection; falls
        back to a regex line scanner when clang is unavailable.

        Examples of patterns fixed::

            if (err)                     if (err) {
                return -1;       →           return -1;
                                         }

            for (i = 0; i < n; i++)      for (i = 0; i < n; i++) {
                process(i);      →           process(i);
                                         }

        The file is rewritten in place inside ``annotated/``.  The original
        ``source/`` tree is never touched.

        Args:
            run_id:   Session identifier returned by ``session_create``.
            filepath: Path relative to the ``annotated/`` subfolder.

        Returns:
            JSON string with keys:
                * ``status``     — ``"ok"`` or ``"error"``.
                * ``modified``   — ``True`` if the file was changed.
                * ``insertions`` — number of brace pairs added.
                * ``method``     — ``"clang"`` or ``"regex"`` (which backend ran).
        """
        from .source_parser import add_braces_c

        ws = _ws(run_id)
        abs_path = ws / "annotated" / filepath
        if not abs_path.exists():
            return _err(f"File not found in annotated/: {filepath}")

        result = add_braces_c(abs_path)
        if "error" in result:
            return _err(result["error"], filepath=filepath)

        return _ok(
            f"Brace insertion complete: {result['insertions']} pair(s) added "
            f"via {result['method']}.",
            filepath=filepath,
            modified=result["modified"],
            insertions=result["insertions"],
            method=result["method"],
        )

    @mcp.tool()
    def clang_extract_functions(run_id: str, filepath: str) -> str:
        """Extract function definitions with exact line numbers from a source file.

        Uses clang ``-ast-dump=json`` for C/C++ files and Python's ``ast`` module
        for ``.py`` files.  Falls back to ``ctags`` then regex-based brace counting
        if clang is not available.

        Each returned function record has:

        * ``name``            — function name
        * ``start_line``      — first line of the return type / signature
        * ``open_brace_line`` — line number of the opening ``{``
        * ``body_first_line`` — ``open_brace_line + 1`` — insert DFTRACER_*_START here
        * ``close_brace_line``— line number of the closing ``}``
        * ``exit_lines``      — list of ``{"line": N, "type": "return"|"exit"|…}``
                                 indicating where END macros are needed
        * ``is_entry_point``  — ``True`` for ``main`` / ``__main__``
        * ``source``          — which extractor was used (``"clang"``, ``"ctags"``,
                                ``"regex"``, or ``"ast"``)

        This tool is called by the annotation sub-recipes *before* any macro is
        written so the agent has the authoritative function map and does not need to
        manually scan the file for exit paths.

        Args:
            run_id:   Session identifier returned by ``session_create``.
            filepath: Path to the file relative to the ``annotated/`` sub-folder.

        Returns:
            JSON string with keys:
                * ``status``    — ``"ok"`` or ``"error"``.
                * ``message``   — human-readable summary.
                * ``filepath``  — echoed input path.
                * ``functions`` — list of function-info dicts.
                * ``count``     — number of functions found.
                * ``extractor`` — which backend produced the result.
        """
        from .source_parser import extract_functions

        ws = _ws(run_id)
        abs_path = ws / "annotated" / filepath
        if not abs_path.exists():
            return _err(f"File not found in annotated/: {filepath}")

        functions = extract_functions(str(abs_path))
        extractor = functions[0].get("source", "unknown") if functions else "none"

        return _ok(
            f"Extracted {len(functions)} function(s) from {filepath} "
            f"using {extractor}.",
            filepath=filepath,
            functions=functions,
            count=len(functions),
            extractor=extractor,
        )

    @mcp.tool()
    def clang_insert_line(
        run_id: str,
        filepath: str,
        line_number: int,
        content: str,
        position: str = "before",
    ) -> str:
        """Insert a single line of code at an exact line number in an annotated file.

        Allows annotation agents to insert dftracer macros at precise positions
        identified from the ``clang_extract_functions`` output without needing
        to rewrite the entire file.

        .. warning:: Line-shift hazard for multiple insertions
           Each call reads the *current* file state, inserts one line, and writes
           back immediately.  If you need to insert at several positions from the
           same ``clang_extract_functions`` snapshot, process them in
           **highest-line-number-first order** so that earlier insertions do not
           shift the positions of later ones.  For whole-file annotation prefer
           ``clang_annotate_file``, which handles ordering automatically in a
           single in-memory pass.

        The file is modified inside the ``annotated/`` subfolder.  The original
        ``source/`` copy is never touched.

        Args:
            run_id:      Session identifier returned by ``session_create``.
            filepath:    Path relative to the ``annotated/`` subfolder.
            line_number: 1-based line number at which to insert. When
                         ``position="before"`` the new line is inserted *above*
                         this line; when ``position="after"`` it is inserted
                         *below* this line.
            content:     Full text of the line to insert (without trailing newline).
                         Include any desired leading indentation in this string.
            position:    ``"before"`` (default) or ``"after"``.

        Returns:
            JSON string with keys:
                * ``status``      — ``"ok"`` or ``"error"``.
                * ``message``     — human-readable outcome.
                * ``filepath``    — echoed input path.
                * ``line_number`` — effective 1-based insertion point.
                * ``inserted``    — the content that was written.
                * ``total_lines`` — total line count after the insertion.
        """
        ws = _ws(run_id)
        abs_path = ws / "annotated" / filepath
        if not abs_path.exists():
            return _err(f"File not found in annotated/: {filepath}")

        if position not in ("before", "after"):
            return _err(
                f"Invalid position '{position}' — must be 'before' or 'after'",
                filepath=filepath,
            )

        lines = abs_path.read_text(errors="replace").splitlines(keepends=True)
        total = len(lines)

        if position == "before":
            # Insert before line_number (0-based index = line_number - 1)
            insert_at = max(0, min(line_number - 1, total))
        else:
            # Insert after line_number
            insert_at = max(0, min(line_number, total))

        new_line = content + "\n"
        lines.insert(insert_at, new_line)
        abs_path.write_text("".join(lines))

        effective_line = insert_at + 1
        return _ok(
            f"Inserted line at position {effective_line} ({position} line {line_number}) "
            f"in {filepath}.",
            filepath=filepath,
            line_number=effective_line,
            inserted=content,
            total_lines=len(lines),
        )

    @mcp.tool()
    def clang_annotate_file(
        run_id: str,
        filepath: str,
        language: str = "c",
        is_entry: bool = False,
        init_args: str = "NULL, NULL, -1",
    ) -> str:
        """Annotate a C/C++ source file with dftracer macros in a single in-memory pass.

        This is the preferred way to instrument a whole file.  It:

        1. Loads the current file content into memory (no intermediate writes).
        2. Calls ``clang_extract_functions`` internally to get an authoritative
           function map with exact line numbers.
        3. Computes all insertion points:
           * ``#include <dftracer/dftracer.h>`` after the last existing ``#include``.
           * ``DFTRACER_C_FUNCTION_START()`` (or ``DFTRACER_CPP_FUNCTION()``) at
             the first line of every function body (``body_first_line``).
           * ``DFTRACER_C_FUNCTION_END()`` before every ``exit_line`` in the
             function map; or before the ``close_brace_line`` for void/fallthrough
             functions with no recorded exits.
           * For entry-point files (``is_entry=True``): ``DFTRACER_C_INIT(…)``
             before ``DFTRACER_C_FUNCTION_START()`` inside ``main()``, and
             ``DFTRACER_C_FINI()`` before each ``DFTRACER_C_FUNCTION_END()``
             in ``main()``.
        4. Sorts all insertion points **highest-line-number first** so that each
           insertion does not shift the positions of subsequent ones — the classic
           bottom-to-top strategy.
        5. Applies every insertion to the in-memory line list.
        6. Writes the file exactly once.

        The file is modified inside the ``annotated/`` subfolder.  The original
        ``source/`` copy is never touched.  The operation is idempotent: if
        ``#include <dftracer/dftracer.h>`` is already present the file is
        returned unchanged.

        Args:
            run_id:     Session identifier returned by ``session_create``.
            filepath:   Path relative to the ``annotated/`` subfolder.
            language:   ``"c"`` (default) or ``"cpp"``.  Controls which macro
                        family is emitted (``DFTRACER_C_*`` vs
                        ``DFTRACER_CPP_FUNCTION``).
            is_entry:   ``True`` when this file contains ``main()`` and should
                        receive ``DFTRACER_C_INIT`` / ``DFTRACER_C_FINI`` in
                        addition to the per-function START/END macros.
            init_args:  Argument string for ``DFTRACER_C_INIT(…)``; defaults to
                        ``"NULL, NULL, -1"`` (log to default path, trace all
                        dirs, use PID).  Ignored when ``is_entry=False``.

        Returns:
            JSON string with keys:
                * ``status``      — ``"ok"`` or ``"error"``.
                * ``message``     — human-readable summary.
                * ``filepath``    — echoed input path.
                * ``insertions``  — total number of lines inserted.
                * ``functions``   — number of functions annotated.
                * ``total_lines`` — line count of the file after annotation.
                * ``already_annotated`` — ``True`` if the file was skipped
                  because dftracer macros were already present.
        """
        from .source_parser import extract_functions

        ws = _ws(run_id)
        abs_path = ws / "annotated" / filepath
        if not abs_path.exists():
            return _err(f"File not found in annotated/: {filepath}")

        # Load from cache or disk
        cache_key = (run_id, filepath)
        if cache_key in _FILE_CACHE:
            lines = list(_FILE_CACHE[cache_key])
            text = "\n".join(lines)
        else:
            text = abs_path.read_text(errors="replace")
            lines = text.splitlines()

        # Idempotency guard
        if "#include <dftracer/dftracer.h>" in text:
            lines_count = len(lines)
            return _ok(
                f"{filepath} is already annotated — skipped.",
                filepath=filepath,
                insertions=0,
                functions=0,
                total_lines=lines_count,
                already_annotated=True,
            )

        is_cpp = language.lower() in ("cpp", "c++", "cxx")
        START = (
            "DFTRACER_CPP_FUNCTION();"
            if is_cpp
            else "DFTRACER_C_FUNCTION_START();"
        )
        END = "DFTRACER_C_FUNCTION_END();"  # C++ uses RAII, no explicit END needed
        INIT = f"DFTRACER_C_INIT({init_args});"
        FINI = "DFTRACER_C_FINI();"

        # ── Step 1: find where to insert #include (outside any #ifdef block) ──
        # Track preprocessor block depth so we never insert inside a conditional
        # section (which would make the include unavailable in the main code path).
        include_line = "#include <dftracer/dftracer.h>"
        last_inc_idx = -1
        pp_depth = 0
        for i, ln in enumerate(lines):
            s = ln.strip()
            if re.match(r'#\s*(?:ifdef|ifndef|if\b)', s):
                pp_depth += 1
            elif re.match(r'#\s*endif', s):
                pp_depth = max(0, pp_depth - 1)
            elif pp_depth == 0 and re.match(r'#\s*include', s):
                last_inc_idx = i
        include_insert_at = last_inc_idx + 1  # 0-based index

        # ── Step 2: extract function map ──────────────────────────────────────
        # Write current in-memory state to a temp file so extract_functions can
        # parse it accurately (avoids stale on-disk content).
        import tempfile, os
        suffix = abs_path.suffix
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=suffix, delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write("\n".join(lines) + "\n")
            tmp_path = tmp.name
        try:
            functions = extract_functions(tmp_path)
        finally:
            os.unlink(tmp_path)

        # ── Step 3: build insertion list (0-based indices, original line nums) ─
        # Each entry: (index_0based, text_to_insert)
        # We sort highest-first so each insert does not shift later positions.

        def _indent(lines_list, idx_0based, default="    "):
            ln = lines_list[idx_0based] if 0 <= idx_0based < len(lines_list) else ""
            n = len(ln) - len(ln.lstrip())
            return " " * n if n > 0 else default

        insertions: list = [(include_insert_at, include_line)]

        for fn in functions:
            body_first = fn.get("body_first_line")
            close       = fn.get("close_brace_line")
            exits       = fn.get("exit_lines", [])
            fn_name     = fn.get("name", "")
            if body_first is None or close is None:
                continue

            body_idx = body_first - 1  # 0-based index of first body line
            ind = _indent(lines, body_idx)

            is_main_fn = fn_name == "main" and is_entry

            if is_main_fn:
                # INIT must come before START in main()
                insertions.append((body_idx, ind + INIT))
            insertions.append((body_idx, ind + START))

            if not is_cpp:
                if exits:
                    for ex in exits:
                        ex_line = ex.get("line") if isinstance(ex, dict) else ex
                        if ex_line is None:
                            continue
                        ex_idx = ex_line - 1
                        ex_ind = _indent(lines, ex_idx, ind)
                        if is_main_fn:
                            insertions.append((ex_idx, ex_ind + FINI))
                        insertions.append((ex_idx, ex_ind + END))
                else:
                    close_idx = close - 1
                    if is_main_fn:
                        insertions.append((close_idx, ind + FINI))
                    insertions.append((close_idx, ind + END))

        # ── Step 4: sort highest-index-first, apply all in one pass ───────────
        # For same index: END/FINI before INIT/START so START ends up topmost.
        def _sort_key(item):
            idx, txt = item
            # Lower priority number = processed earlier (inserted first at this idx)
            # We want FINI/END to go first so that START/INIT land above them.
            if FINI in txt or END in txt:
                prio = 0
            else:
                prio = 1
            return (-idx, prio)

        insertions.sort(key=_sort_key)

        for idx, txt in insertions:
            lines.insert(idx, txt)

        # Store in cache AND write to disk (single write)
        _FILE_CACHE[cache_key] = list(lines)
        abs_path.write_text("\n".join(lines) + "\n")

        return _ok(
            f"Annotated {filepath}: {len(insertions)} line(s) inserted across "
            f"{len(functions)} function(s).",
            filepath=filepath,
            insertions=len(insertions),
            functions=len(functions),
            total_lines=len(lines),
            already_annotated=False,
        )

    @mcp.tool()
    def clang_write_annotated_file(run_id: str, filepath: str) -> str:
        """Flush the in-memory annotated file buffer to disk.

        Call this after a series of in-memory annotation operations
        (``clang_annotate_file``, ``clang_add_braces``, ``clang_insert_line``)
        to commit all changes with a single write.  If the file is not in the
        in-memory cache (e.g. ``clang_annotate_file`` has not been called yet
        for this file), returns an error.

        Args:
            run_id:   Session identifier returned by ``session_create``.
            filepath: Path relative to the ``annotated/`` subfolder.

        Returns:
            JSON string with keys:
                * ``status``      — ``"ok"`` or ``"error"``.
                * ``message``     — human-readable outcome.
                * ``filepath``    — echoed input path.
                * ``total_lines`` — number of lines written to disk.
        """
        cache_key = (run_id, filepath)
        if cache_key not in _FILE_CACHE:
            return _err(
                f"No in-memory state for {filepath} — call clang_annotate_file first.",
                filepath=filepath,
            )
        ws = _ws(run_id)
        abs_path = ws / "annotated" / filepath
        lines = _FILE_CACHE[cache_key]
        abs_path.write_text("\n".join(lines) + "\n")
        del _FILE_CACHE[cache_key]
        return _ok(
            f"Wrote {len(lines)} lines to {filepath}.",
            filepath=filepath,
            total_lines=len(lines),
        )

    @mcp.tool()
    def clang_estimate_function_cost(
        run_id: str,
        filepath: str,
        function_name: str,
    ) -> str:
        """Estimate the instrumentation value of a C/C++ function.

        Analyses the function body with heuristic pattern matching to decide
        whether it is worth annotating with dftracer macros.  Functions that
        do nothing of substance (simple getters, trivial wrappers, functions
        with fewer than 3 real statements) are marked **skip**; functions that
        perform I/O, MPI communication, memory allocation, or significant
        computation are marked **annotate**.

        The analysis is purely textual (no clang AST required) but is
        calibrated to the patterns that appear in HPC and I/O-intensive C code:

        * **I/O**: ``open``, ``close``, ``read``, ``write``, ``pread``,
          ``pwrite``, ``fopen``, ``fclose``, ``fread``, ``fwrite``, ``stat``,
          ``fstat``, ``lseek``, ``mmap``, ``munmap``, ``fsync``, ``fdatasync``,
          ``fallocate``, ``ioctl``, ``sendfile``
        * **MPI**: ``MPI_``
        * **Memory**: ``malloc``, ``calloc``, ``realloc``, ``free``, ``mmap``,
          ``memcpy``, ``memmove``, ``memset``
        * **Computation**: loops (``for``, ``while``, ``do``), ``sqrt``,
          ``pow``, ``log``, ``exp``, ``fabs``, ``floor``, ``ceil``

        Scoring: each matched category adds weight.  Functions scoring above
        the threshold are recommended for annotation.

        Args:
            run_id:        Session identifier.
            filepath:      Path relative to ``annotated/``.
            function_name: Exact name of the function to evaluate.

        Returns:
            JSON string with keys:
                * ``status``           — ``"ok"`` or ``"error"``.
                * ``function``         — echoed function name.
                * ``recommendation``   — ``"annotate"`` or ``"skip"``.
                * ``score``            — numeric cost estimate (higher = more work).
                * ``reasons``          — list of matched categories.
                * ``statement_count``  — approximate number of non-blank, non-comment
                                         statements found.
        """
        from .source_parser import extract_functions

        ws = _ws(run_id)
        abs_path = ws / "annotated" / filepath
        if not abs_path.exists():
            return _err(f"File not found in annotated/: {filepath}")

        # Use cached content if available
        cache_key = (run_id, filepath)
        if cache_key in _FILE_CACHE:
            all_lines = _FILE_CACHE[cache_key]
        else:
            all_lines = abs_path.read_text(errors="replace").splitlines()

        # Find function boundaries using extract_functions
        fns = extract_functions(str(abs_path))
        target = next(
            (f for f in fns if f.get("name") == function_name), None
        )
        if target is None:
            return _err(
                f"Function '{function_name}' not found in {filepath}.",
                filepath=filepath,
                function=function_name,
            )

        body_first = target.get("body_first_line", 1)
        close       = target.get("close_brace_line", len(all_lines))
        body_lines  = all_lines[body_first - 1 : close - 1]

        # ── Heuristic scoring ────────────────────────────────────────────────
        IO_RE   = re.compile(
            r'\b(?:open|close|read|write|pread|pwrite|fopen|fclose|fread|fwrite'
            r'|stat|fstat|lstat|lseek|mmap|munmap|fsync|fdatasync|fallocate'
            r'|ioctl|sendfile|rename|unlink|mkdir|rmdir)\s*\('
        )
        MPI_RE  = re.compile(r'\bMPI_\w+\s*\(')
        MEM_RE  = re.compile(
            r'\b(?:malloc|calloc|realloc|free|mmap|memcpy|memmove|memset)\s*\('
        )
        LOOP_RE = re.compile(r'\b(?:for|while|do)\s*[({]')
        MATH_RE = re.compile(
            r'\b(?:sqrt|pow|log|exp|fabs|floor|ceil|cbrt|hypot|sin|cos|tan)\s*\('
        )

        body_text = "\n".join(body_lines)

        score = 0
        reasons = []
        if IO_RE.search(body_text):
            score += 30
            reasons.append("io_syscall")
        if MPI_RE.search(body_text):
            score += 25
            reasons.append("mpi_call")
        if MEM_RE.search(body_text):
            score += 15
            reasons.append("memory_alloc")
        if LOOP_RE.search(body_text):
            score += 10
            reasons.append("loop")
        if MATH_RE.search(body_text):
            score += 5
            reasons.append("math")

        # Count real statements (non-blank, non-comment, non-macro lines)
        stmt_count = sum(
            1 for ln in body_lines
            if ln.strip()
            and not ln.strip().startswith("//")
            and not ln.strip().startswith("*")
            and not ln.strip().startswith("/*")
            and not ln.strip().startswith("DFTRACER")
        )
        score += min(stmt_count, 10)  # up to 10 pts for body size

        recommendation = "annotate" if score >= 10 else "skip"

        return _ok(
            f"Function '{function_name}' scored {score} → {recommendation}.",
            function=function_name,
            filepath=filepath,
            recommendation=recommendation,
            score=score,
            reasons=reasons,
            statement_count=stmt_count,
        )
