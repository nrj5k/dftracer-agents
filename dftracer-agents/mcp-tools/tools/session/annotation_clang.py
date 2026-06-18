"""Clang-backed annotation tools for dftracer source code instrumentation.

This module registers six MCP tools on a FastMCP instance:

* ``clang_add_braces``              — ensure every braceless if/for/while body has {}
* ``clang_extract_functions``       — build an authoritative function map with exact lines
* ``clang_insert_line``             — insert a single code line at an exact line number
* ``clang_annotate_file``           — full-file annotation: insert all dftracer macros
                                       accounting for line-number shifts automatically
* ``clang_annotate_project``        — annotate every source file in annotated/ in one call
* ``clang_write_annotated_file``    — flush the in-memory file buffer to disk
* ``clang_estimate_function_cost``  — AST-based cost estimate; returns skip/annotate recommendation
* ``clang_syntax_check``            — verify C/C++/Python syntax using clang / ast module

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
# Annotation-decision constants and helpers
# ---------------------------------------------------------------------------

#: Functions whose cost score is below this threshold are skipped unless they
#: match a lifecycle or vendor-FS override rule.
ANNOTATION_SCORE_THRESHOLD = 20

#: Names / suffixes that identify lifecycle functions — always annotate
#: regardless of cost score (Rule R6).
_LIFECYCLE_RE = re.compile(
    r"(_init|_final|_initialize|_finalize"
    r"|_open|_close|_create|_destroy"
    r"|_open_backend|_close_backend"
    r"|_sync|_flush|_fsync"
    r"|_delete|_rename|_stat|_mknod|_getFileSize)$",
    re.IGNORECASE,
)

#: Vendor filesystem function-name prefixes — always annotate (Rule R7).
_VENDOR_PREFIX_RE = re.compile(
    r"^(gpfs_|beegfs_|llapi_|cuFile|hdfs_|daos_|ceph_|gfarm_)",
    re.IGNORECASE,
)


def _should_annotate(fn: dict) -> bool:
    """Return True if *fn* (a function-info dict) should receive dftracer macros.

    Rules applied in order:
    1. Lifecycle functions (*_init, *_final, …)    → always annotate (Rule R6)
    2. Vendor FS calls present in body             → always annotate (Rule R7)
    3. Cost score ≥ ANNOTATION_SCORE_THRESHOLD     → annotate
    4. Otherwise                                   → skip
    """
    name = fn.get("name", "")
    cost = fn.get("cost_info", {})

    if _LIFECYCLE_RE.search(name):
        return True
    if _VENDOR_PREFIX_RE.match(name):
        return True
    if cost.get("vendor_calls", 0) > 0:
        return True
    return cost.get("score", 0) >= ANNOTATION_SCORE_THRESHOLD


def _derive_comp(fn: dict) -> str:
    """Return the dftracer ``comp`` category string for *fn*.

    Priority order mirrors the backend hierarchy from the annotation SKILL:
    MPI comms > vendor FS > POSIX I/O > memory ops > CPU.
    """
    cost = fn.get("cost_info", {})
    if cost.get("mpi_calls", 0) > 0:
        return "comm"
    if cost.get("vendor_calls", 0) > 0 or cost.get("io_calls", 0) > 0:
        return "io"
    if cost.get("mem_calls", 0) > 0:
        return "mem"
    return "cpu"


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
        from .source_parser import add_braces_c, ClangNotFoundError

        ws = _ws(run_id)
        abs_path = ws / "annotated" / filepath
        if not abs_path.exists():
            return _err(f"File not found in annotated/: {filepath}")

        try:
            result = add_braces_c(abs_path)
        except ClangNotFoundError as exc:
            return _err(str(exc), filepath=filepath)
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
        from .source_parser import extract_functions, ClangNotFoundError

        ws = _ws(run_id)
        abs_path = ws / "annotated" / filepath
        if not abs_path.exists():
            return _err(f"File not found in annotated/: {filepath}")

        try:
            functions = extract_functions(str(abs_path))
        except ClangNotFoundError as exc:
            return _err(str(exc), filepath=filepath)

        extractor = functions[0].get("source", "unknown") if functions else "clang"

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
        from .source_parser import add_braces_c, extract_functions, ClangNotFoundError

        ws = _ws(run_id)
        abs_path = ws / "annotated" / filepath
        if not abs_path.exists():
            return _err(f"File not found in annotated/: {filepath}")

        # Idempotency guard — read from cache or disk
        cache_key = (run_id, filepath)
        if cache_key in _FILE_CACHE:
            text = "\n".join(_FILE_CACHE[cache_key])
        else:
            text = abs_path.read_text(errors="replace")

        if "#include <dftracer/dftracer.h>" in text:
            return _ok(
                f"{filepath} is already annotated — skipped.",
                filepath=filepath,
                insertions=0,
                functions=0,
                total_lines=len(text.splitlines()),
                already_annotated=True,
                braces_added=0,
            )

        # ── Step 0: add braces to braceless control-flow bodies ──────────────
        # Required before function extraction: inserting FUNCTION_END before a
        # return inside a braceless if/for/while would otherwise create a
        # dangling-else or brace-mismatch syntax error.
        # Flush any in-memory cache to disk so add_braces_c sees current content.
        if cache_key in _FILE_CACHE:
            abs_path.write_text("\n".join(_FILE_CACHE[cache_key]) + "\n")
            del _FILE_CACHE[cache_key]

        try:
            brace_result = add_braces_c(abs_path)
        except ClangNotFoundError as exc:
            return _err(str(exc), filepath=filepath)

        braces_added = brace_result.get("insertions", 0)

        # Reload from disk (brace insertion may have shifted line numbers)
        text = abs_path.read_text(errors="replace")
        lines = text.splitlines()

        is_cpp = language.lower() in ("cpp", "c++", "cxx")
        START = (
            "DFTRACER_CPP_FUNCTION();"
            if is_cpp
            else "DFTRACER_C_FUNCTION_START();"
        )
        END = "DFTRACER_C_FUNCTION_END();"  # C++ uses RAII, no explicit END needed
        INIT = f"DFTRACER_C_INIT({init_args});"
        FINI = "DFTRACER_C_FINI();"

        def _make_update(fn: dict, ind: str) -> str:
            comp = _derive_comp(fn)
            if is_cpp:
                return f'{ind}DFTRACER_CPP_FUNCTION_UPDATE("comp", "{comp}");'
            return f'{ind}DFTRACER_C_FUNCTION_UPDATE_STR("comp", "{comp}");'

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
        except ClangNotFoundError as exc:
            os.unlink(tmp_path)
            return _err(str(exc), filepath=filepath)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

        # ── Step 3: build insertion list (0-based indices, original line nums) ─
        # Each entry: (index_0based, text_to_insert)
        # We sort highest-first so each insert does not shift later positions.

        def _indent(lines_list, idx_0based, default="    "):
            ln = lines_list[idx_0based] if 0 <= idx_0based < len(lines_list) else ""
            n = len(ln) - len(ln.lstrip())
            return " " * n if n > 0 else default

        insertions: list = [(include_insert_at, include_line)]
        skipped_functions: list[str] = []

        for fn in functions:
            body_first = fn.get("body_first_line")
            close       = fn.get("close_brace_line")
            exits       = fn.get("exit_lines", [])
            fn_name     = fn.get("name", "")
            if body_first is None or close is None:
                continue

            # ── Cost filter (Rule 0 / R6 / R7) ───────────────────────────────
            # Skip trivial functions unless they are lifecycle or vendor-FS.
            # entry-point main() is always annotated.
            if fn_name != "main" and not _should_annotate(fn):
                skipped_functions.append(fn_name)
                continue

            body_idx = body_first - 1  # 0-based index of first body line
            ind = _indent(lines, body_idx)

            is_main_fn = fn_name == "main" and is_entry

            if is_main_fn:
                # INIT must come before START in main()
                insertions.append((body_idx, ind + INIT))
            insertions.append((body_idx, ind + START))
            # UPDATE_STR("comp") must appear right after START.
            # We mark it with a sentinel prefix "UPDATE:" so the sort key
            # can place it below START (processed before START in the
            # bottom-to-top pass, so START ends up above it in the file).
            insertions.append((body_idx, "UPDATE:" + _make_update(fn, ind)))

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
        # For same index, insertion order (bottom-to-top within the index)
        # determines final vertical order — the LAST inserted ends up on top:
        #   prio 0: END / FINI  → processed first, end up at bottom
        #   prio 1: UPDATE      → processed second, pushed above END
        #   prio 2: START / INIT → processed last, pushed above UPDATE
        def _sort_key(item):
            idx, txt = item
            if FINI in txt or END in txt:
                prio = 0
            elif txt.startswith("UPDATE:"):
                prio = 1
            else:
                prio = 2
            return (-idx, prio)

        insertions.sort(key=_sort_key)

        for idx, txt in insertions:
            # Strip the UPDATE sentinel prefix before writing to the file.
            lines.insert(idx, txt.removeprefix("UPDATE:"))

        # Store in cache AND write to disk (single write)
        _FILE_CACHE[cache_key] = list(lines)
        abs_path.write_text("\n".join(lines) + "\n")

        annotated_count = len(functions) - len(skipped_functions)
        return _ok(
            f"Annotated {filepath}: {len(insertions)} line(s) inserted across "
            f"{annotated_count} function(s); {len(skipped_functions)} trivial "
            f"function(s) skipped ({braces_added} brace pair(s) added).",
            filepath=filepath,
            insertions=len(insertions),
            functions=annotated_count,
            skipped=len(skipped_functions),
            skipped_names=skipped_functions,
            total_lines=len(lines),
            already_annotated=False,
            braces_added=braces_added,
        )

    @mcp.tool()
    def clang_annotate_project(
        run_id: str,
        language: str = "c",
        init_args: str = "NULL, NULL, -1",
        exclude_patterns: List[str] = None,
    ) -> str:
        """Annotate every C/C++ source file in the ``annotated/`` workspace in one call.

        Discovers all ``.c`` / ``.cpp`` / ``.cxx`` / ``.cc`` files under
        ``annotated/``, determines which contain ``main()`` (entry-point files),
        and annotates them in the correct order:

        1. **Library / inner files first** — annotated with ``is_entry=False``.
        2. **Entry-point files last** — annotated with ``is_entry=True`` so
           ``DFTRACER_C_INIT`` / ``DFTRACER_C_FINI`` are inserted around ``main()``.

        Each file is processed by ``clang_annotate_file`` which:

        * Adds braces to braceless control-flow bodies (required before macro insertion).
        * Skips trivial functions automatically (cost filter, Rules 0 / R6 / R7).
        * Inserts ``DFTRACER_C_FUNCTION_UPDATE_STR("comp", …)`` with the correct
          category derived from the AST cost info (mpi → "comm", io → "io",
          mem → "mem", cpu → "cpu").
        * Is idempotent — already-annotated files are silently skipped.

        Paths can be excluded by passing glob-style substrings in
        ``exclude_patterns`` (e.g. ``["test/", "vendor/"]``).  The following
        patterns are always excluded regardless:
        ``/test/``, ``/tests/``, ``/vendor/``, ``/third_party/``,
        ``/CMakeFiles/``, ``/.git/``.

        Args:
            run_id:           Session identifier returned by ``session_create``.
            language:         ``"c"`` or ``"cpp"``.  Applied to all files.
            init_args:        Argument string for ``DFTRACER_C_INIT(…)``.
            exclude_patterns: Extra path substrings to skip.

        Returns:
            JSON string with keys:

            * ``status``         — ``"ok"`` or ``"error"``.
            * ``total_files``    — number of source files discovered.
            * ``annotated``      — number of files newly annotated.
            * ``skipped``        — number of files already annotated or excluded.
            * ``errors``         — list of ``{"file": …, "error": …}`` dicts for
              any file that failed.
            * ``file_results``   — per-file summary dicts.
        """
        import json as _json

        ws = _ws(run_id)
        ann_dir = ws / "annotated"
        if not ann_dir.exists():
            return _err(f"annotated/ directory not found in workspace {run_id}")

        # Always-excluded path fragments
        _ALWAYS_EXCLUDE = (
            "/test/", "/tests/", "/vendor/", "/third_party/",
            "/CMakeFiles/", "/.git/",
        )
        extra_exclude = list(exclude_patterns) if exclude_patterns else []

        def _is_excluded(p: Path) -> bool:
            s = str(p)
            for pat in _ALWAYS_EXCLUDE:
                if pat in s:
                    return True
            for pat in extra_exclude:
                if pat in s:
                    return True
            return False

        # Discover source files
        C_EXTS = {".c", ".cpp", ".cxx", ".cc"}
        all_files = sorted(
            p for p in ann_dir.rglob("*")
            if p.suffix.lower() in C_EXTS and not _is_excluded(p)
        )

        if not all_files:
            return _ok(
                "No source files found in annotated/.",
                total_files=0, annotated=0, skipped=0, errors=[], file_results=[],
            )

        # Detect entry-point files (contain "int main(")
        def _is_entry(p: Path) -> bool:
            try:
                text = p.read_text(errors="replace")
                return bool(re.search(r'\bint\s+main\s*\(', text))
            except OSError:
                return False

        regular_files = []
        entry_files = []
        for p in all_files:
            (entry_files if _is_entry(p) else regular_files).append(p)

        # Annotate in order: regular first, entry-points last
        file_results = []
        annotated_count = 0
        skipped_count = 0
        errors = []

        for p in regular_files + entry_files:
            rel = str(p.relative_to(ann_dir))
            is_entry_file = p in entry_files
            try:
                raw = clang_annotate_file(
                    run_id=run_id,
                    filepath=rel,
                    language=language,
                    is_entry=is_entry_file,
                    init_args=init_args,
                )
                result = _json.loads(raw)
                already = result.get("already_annotated", False)
                if result.get("status") == "ok":
                    if already:
                        skipped_count += 1
                    else:
                        annotated_count += 1
                    file_results.append({
                        "file": rel,
                        "status": "ok",
                        "already_annotated": already,
                        "functions": result.get("functions", 0),
                        "skipped_functions": result.get("skipped", 0),
                        "insertions": result.get("insertions", 0),
                    })
                else:
                    errors.append({"file": rel, "error": result.get("message", "unknown error")})
                    skipped_count += 1
                    file_results.append({"file": rel, "status": "error", "error": result.get("message")})
            except Exception as exc:
                errors.append({"file": rel, "error": str(exc)})
                skipped_count += 1
                file_results.append({"file": rel, "status": "error", "error": str(exc)})

        total = len(all_files)
        msg = (
            f"Project annotation complete: {annotated_count}/{total} file(s) annotated, "
            f"{skipped_count} skipped/already-done, {len(errors)} error(s)."
        )
        return _ok(
            msg,
            total_files=total,
            annotated=annotated_count,
            skipped=skipped_count,
            errors=errors,
            file_results=file_results,
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
        """Estimate the instrumentation value of a C/C++/Python function.

        Uses the clang ``-ast-dump=json`` AST (for C/C++) or Python's built-in
        ``ast`` module (for ``.py`` files) to count meaningful structural
        features — no regex or text scanning.

        **What is measured (from the AST):**

        * **I/O syscalls** — ``CallExpr`` nodes whose callee is in the POSIX /
          stdio I/O set (``open``, ``read``, ``write``, ``fopen``, ``stat``,
          ``mmap``, …)
        * **MPI calls** — ``CallExpr`` nodes starting with ``MPI_`` / ``NCMPI_``
        * **Memory ops** — ``malloc``, ``calloc``, ``memcpy``, ``memset``, …
        * **Vendor FS** — callee prefixed with ``gpfs_``, ``beegfs_``, ``llapi_``,
          ``cuFile``, ``hdfs_``, ``daos_``, ``ceph_``, ``gfarm_``
        * **Loops** — ``ForStmt`` / ``WhileStmt`` / ``DoStmt`` (C) or
          ``ast.For`` / ``ast.While`` (Python)
        * **Branches** — ``IfStmt`` / ``SwitchStmt`` (C) or ``ast.If`` (Python)
        * **Call count** — total ``CallExpr`` / ``ast.Call`` nodes
        * **Node count** — total AST nodes as a body-size proxy

        Scoring weights: io×30, mpi×25, vendor×30, mem×15, loop×10, branch×3,
        call×2, body-size bonus (capped at 20).

        **Annotation decision:**

        * Lifecycle functions (``*_init``, ``*_final``, ``*_open``, …) →
          always ``annotate`` regardless of score (Rule R6).
        * Vendor FS calls present → always ``annotate`` (Rule R7).
        * Score ≥ ``ANNOTATION_SCORE_THRESHOLD`` (default 20) → ``annotate``.
        * Otherwise → ``skip``.

        Args:
            run_id:        Session identifier returned by ``session_create``.
            filepath:      Path relative to ``annotated/``.
            function_name: Exact name of the function to evaluate.

        Returns:
            JSON string with keys:

            * ``status``         — ``"ok"`` or ``"error"``.
            * ``function``       — echoed function name.
            * ``recommendation`` — ``"annotate"`` or ``"skip"``.
            * ``score``          — numeric cost estimate (higher = more work).
            * ``threshold``      — the score threshold used.
            * ``cost_info``      — full breakdown: ``io_calls``, ``mpi_calls``,
              ``mem_calls``, ``vendor_calls``, ``loop_count``, ``branch_count``,
              ``call_count``, ``node_count``.
            * ``override_reason``— non-empty when lifecycle / vendor rule fired.
        """
        from .source_parser import extract_functions, ClangNotFoundError

        ws = _ws(run_id)
        abs_path = ws / "annotated" / filepath
        if not abs_path.exists():
            return _err(f"File not found in annotated/: {filepath}")

        try:
            fns = extract_functions(str(abs_path))
        except ClangNotFoundError as exc:
            return _err(str(exc), filepath=filepath, function=function_name)

        target = next((f for f in fns if f.get("name") == function_name), None)
        if target is None:
            return _err(
                f"Function '{function_name}' not found in {filepath}.",
                filepath=filepath,
                function=function_name,
            )

        cost = target.get("cost_info", {})
        score = cost.get("score", 0)

        # Determine recommendation and any override reason
        override_reason = ""
        if _LIFECYCLE_RE.search(function_name):
            recommendation = "annotate"
            override_reason = "lifecycle function (Rule R6)"
        elif _VENDOR_PREFIX_RE.match(function_name):
            recommendation = "annotate"
            override_reason = "vendor FS function (Rule R7)"
        elif cost.get("vendor_calls", 0) > 0:
            recommendation = "annotate"
            override_reason = "vendor FS call in body (Rule R7)"
        elif score >= ANNOTATION_SCORE_THRESHOLD:
            recommendation = "annotate"
        else:
            recommendation = "skip"

        return _ok(
            f"Function '{function_name}' scored {score} → {recommendation}"
            + (f" [{override_reason}]" if override_reason else "") + ".",
            function=function_name,
            filepath=filepath,
            recommendation=recommendation,
            score=score,
            threshold=ANNOTATION_SCORE_THRESHOLD,
            cost_info=cost,
            override_reason=override_reason,
        )

    @mcp.tool()
    def clang_syntax_check(
        run_id: str,
        filepath: str,
        language: str = "auto",
        extra_include_dirs: List[str] = None,
    ) -> str:
        """Check the syntax of an annotated source file using the real compiler front-end.

        Uses the actual language toolchain — no regex or shell pattern matching:

        * **C files**      — ``gcc -fsyntax-only`` (or ``clang -fsyntax-only``)
        * **C++ files**    — ``g++ -fsyntax-only -std=c++14``
        * **Python files** — Python's built-in ``ast.parse()`` (in-process, no
          subprocess)

        A dftracer macro stub header is automatically injected for C/C++ files
        so that dftracer annotations (``DFTRACER_C_FUNCTION_START``, etc.) do not
        block the syntax check before the real dftracer library is installed.

        The session's dftracer ``include/`` directory is added to the include
        path automatically when ``dftracer_install_prefix`` is recorded in
        session state.  MPI include paths are detected via
        ``mpicc --showme:incdirs``.

        Args:
            run_id:             Session identifier returned by ``session_create``.
            filepath:           Path to the file relative to the ``annotated/``
                                subfolder.
            language:           ``"c"``, ``"cpp"``, ``"python"``, or ``"auto"``
                                (default).  ``"auto"`` infers the language from
                                the file extension.
            extra_include_dirs: Optional additional ``-I`` paths injected into
                                the C/C++ compiler invocation.

        Returns:
            JSON string with keys:

            * ``status``   — always ``"ok"`` (errors are in ``passed``/``errors``).
            * ``passed``   — ``True`` if no syntax errors were found.
            * ``language`` — detected or supplied language string.
            * ``errors``   — list of compiler error lines; empty on success.
            * ``command``  — the compiler command that was run (C/C++ only).
        """
        import ast as _ast
        import os
        import subprocess
        import tempfile
        from .workspace import _load_state

        ws = _ws(run_id)
        abs_path = ws / "annotated" / filepath
        if not abs_path.exists():
            return _err(f"File not found in annotated/: {filepath}", filepath=filepath)

        # ── Detect language ───────────────────────────────────────────────────
        if language == "auto":
            suffix = abs_path.suffix.lower()
            if suffix == ".c":
                lang = "c"
            elif suffix in (".cpp", ".cxx", ".cc"):
                lang = "cpp"
            elif suffix == ".py":
                lang = "python"
            else:
                return _err(
                    f"Cannot auto-detect language for extension '{abs_path.suffix}'"
                    " — supply language= explicitly.",
                    filepath=filepath,
                )
        else:
            lang = language.lower()
            if lang in ("c++", "cxx"):
                lang = "cpp"

        # ── Python: ast.parse() in-process (no subprocess, no regex) ─────────
        if lang == "python":
            source = abs_path.read_text(errors="replace")
            try:
                _ast.parse(source, filename=str(abs_path))
                return _ok(
                    f"Python syntax OK: {filepath}",
                    filepath=filepath,
                    passed=True,
                    language="python",
                    errors=[],
                    command="ast.parse()",
                )
            except SyntaxError as exc:
                err_msg = f"{abs_path}:{exc.lineno}: SyntaxError: {exc.msg}"
                return _ok(
                    f"Python syntax error in {filepath}",
                    filepath=filepath,
                    passed=False,
                    language="python",
                    errors=[err_msg],
                    command="ast.parse()",
                )

        # ── C / C++: clang/gcc -fsyntax-only ─────────────────────────────────
        # Write a dftracer stub header to a temp file so that dftracer macros
        # in the annotated file compile cleanly even without the real library.
        DFTRACER_STUB = (
            "#ifndef _DFTRACER_SYNTAX_STUB_H\n"
            "#define _DFTRACER_SYNTAX_STUB_H\n"
            "#define DFTRACER_C_INIT(a,b,c)              do{}while(0)\n"
            "#define DFTRACER_C_FINI()                   do{}while(0)\n"
            "#define DFTRACER_C_FUNCTION_START()         do{}while(0)\n"
            "#define DFTRACER_C_FUNCTION_END()           do{}while(0)\n"
            "#define DFTRACER_C_FUNCTION_UPDATE_STR(k,v) do{}while(0)\n"
            "#define DFTRACER_C_FUNCTION_UPDATE_INT(k,v) do{}while(0)\n"
            "#define DFTRACER_CPP_INIT(a,b,c)            do{}while(0)\n"
            "#define DFTRACER_CPP_FINI()                 do{}while(0)\n"
            "#define DFTRACER_CPP_FUNCTION()             do{}while(0)\n"
            "#define DFTRACER_CPP_FUNCTION_UPDATE(k,v)   do{}while(0)\n"
            "#define DFTRACER_CPP_REGION_START(n)        do{}while(0)\n"
            "#define DFTRACER_CPP_REGION_END(n)          do{}while(0)\n"
            "#endif\n"
        )

        compiler = "g++" if lang == "cpp" else "gcc"
        lang_flag = ["-x", "c++" if lang == "cpp" else "c"]
        std_flag  = ["-std=c++14"] if lang == "cpp" else []

        # Collect include paths
        include_dirs: List[str] = list(extra_include_dirs) if extra_include_dirs else []

        # Inject dftracer include from session state
        try:
            state = _load_state(run_id)
            prefix = state.get("dftracer_install_prefix", "")
            if prefix:
                inc = os.path.join(prefix, "include")
                if os.path.isdir(inc):
                    include_dirs.append(inc)
        except Exception:
            pass

        # Auto-detect MPI include dir via mpicc --showme:incdirs (clang AST, not regex)
        try:
            mpi_r = subprocess.run(
                ["mpicc", "--showme:incdirs"],
                capture_output=True, text=True, timeout=5,
            )
            if mpi_r.returncode == 0:
                for d in mpi_r.stdout.strip().split():
                    if os.path.isdir(d):
                        include_dirs.append(d)
        except Exception:
            pass

        inc_flags = [f"-I{d}" for d in include_dirs]

        stub_fd, stub_path = tempfile.mkstemp(suffix=".h", prefix="dftracer_stub_")
        try:
            with os.fdopen(stub_fd, "w") as fh:
                fh.write(DFTRACER_STUB)

            cmd = (
                [compiler]
                + lang_flag
                + ["-fsyntax-only", "-w"]
                + std_flag
                + ["-include", stub_path]
                + inc_flags
                + [str(abs_path)]
            )
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
            )
        finally:
            try:
                os.unlink(stub_path)
            except OSError:
                pass

        # Collect meaningful diagnostics; filter out stub-header noise
        combined = "\n".join(filter(None, [result.stdout, result.stderr])).strip()
        error_lines = [
            ln for ln in combined.splitlines()
            if ln.strip() and "dftracer_stub_" not in ln
        ]

        passed = result.returncode == 0
        msg = (
            f"{lang.upper()} syntax OK: {filepath}"
            if passed
            else f"{lang.upper()} syntax error(s) in {filepath}"
        )
        return _ok(
            msg,
            filepath=filepath,
            passed=passed,
            language=lang,
            errors=error_lines,
            command=" ".join(cmd),
        )
