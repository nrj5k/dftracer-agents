"""AI/ML-aware dftracer source annotation and file-discovery tools.

Registers four MCP tools:

* ``find_source_files``      — recursively list C / C++ / Python source files
                               in any folder on disk (no session required)
* ``python_annotate_ai_file``— annotate a Python file with AI/ML-region-aware
                               dftracer decorators (``dft_ai.pipeline.train`` etc.)
                               and optionally wrap epoch / fetch for-loops with
                               ``dft_ai.pipeline.epoch.iter()`` /
                               ``dft_ai.dataloader.fetch.iter()``
* ``python_write_ai_file``   — flush the in-memory AI-annotated buffer to disk

AI/ML dftracer annotation API
-------------------------------
The annotation mirrors the pattern used by dlio_benchmark (master branch):

  from dftracer.python import dftracer, dft_fn as DFTracerFn, ai as dft_ai

  _dlp     = DFTracerFn("<category>")       # per-module generic logger
  _dft_log = dftracer.initialize_log(...)   # entry-point files only

  # Function-level decorators (AI/ML regions)
  @dft_ai                            # top-level run() / main() / __call__()
  @dft_ai.pipeline.train             # training loop functions
  @dft_ai.pipeline.evaluate          # evaluation / validation loop functions
  @dft_ai.pipeline.test              # test loop functions
  @dft_ai.compute                    # generic compute step (model(...))
  @dft_ai.compute.forward            # forward pass
  @dft_ai.compute.backward           # backward / gradient step
  @dft_ai.compute.step               # optimizer step
  @dft_ai.data.preprocess            # preprocessing / transform / augment
  @dft_ai.data.item                  # per-item read (__getitem__, read_index)
  @dft_ai.dataloader.fetch           # batch-level data fetching / next()
  @dft_ai.checkpoint.capture         # checkpoint save
  @dft_ai.checkpoint.restart         # checkpoint load / restore
  @dft_ai.device.transfer            # host↔device transfer
  @_dlp.log_init                     # __init__ methods
  @_dlp.log_static                   # @staticmethod methods
  @_dlp.log                          # everything else

  # Loop-level iterator wrappers
  for epoch in dft_ai.pipeline.epoch.iter(range(1, epochs+1)):
  for batch in dft_ai.dataloader.fetch.iter(dataloader):

Line-shift safety
-----------------
All decorator insertions are computed from the original AST parse, sorted
highest-line-first, and applied in a single pass before writing, so early
insertions never shift indices used for later ones.

Loop wrapping is done in-place (same line substitution), so it does not shift
any line numbers and can be applied before or after decorator insertion.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastmcp import FastMCP

from .workspace import _ws, _ok, _err
from .annotation_python import (
    _extract_functions_from_ast,
    _last_import_idx,
    _find_return_lines,
)

# ── Language extension map ─────────────────────────────────────────────────────
_LANG_EXTS: Dict[str, List[str]] = {
    "c":      [".c"],
    "cpp":    [".cpp", ".cxx", ".cc", ".C"],
    "c++":    [".cpp", ".cxx", ".cc", ".C"],
    "python": [".py"],
    "py":     [".py"],
    "all":    [".c", ".cpp", ".cxx", ".cc", ".C", ".py"],
}

# ── AI/ML decorator patterns — ordered, first match wins ──────────────────────
# Pattern matched against fn["name"].lower().
_AI_REGION_PATTERNS: List[Tuple[str, str, str]] = [
    # (name_regex,                                    dft_ai decorator,               label)
    (r'^run$|^main$|^__call__$',                     '@dft_ai',                       'runner'),
    (r'\btrain\b|\btraining\b|^fit$|^_fit$',         '@dft_ai.pipeline.train',        'train'),
    (r'\beval\b|\bevaluat|\bvalidat',                '@dft_ai.pipeline.evaluate',     'evaluate'),
    (r'\btest\b|\btesting\b',                        '@dft_ai.pipeline.test',         'test'),
    (r'^forward$',                                   '@dft_ai.compute.forward',       'forward'),
    (r'^backward$',                                  '@dft_ai.compute.backward',      'backward'),
    (r'^optimizer_step$|^optim_step$',               '@dft_ai.compute.step',          'optim-step'),
    (r'\bcompute\b|\btrain_step\b|\beval_step\b',    '@dft_ai.compute',               'compute'),
    (r'\bpreprocess\b|\btransform\b|\baugment\b|\bcollate\b', '@dft_ai.data.preprocess', 'preprocess'),
    (r'^__getitem__$|read_index|\bload_item\b|\bget_item\b',  '@dft_ai.data.item',    'item'),
    (r'\bfetch\b|\bload_batch\b|\bnext\b',           '@dft_ai.dataloader.fetch',      'fetch'),
    (r'save_checkpoint|save_ckpt|write_ckpt',        '@dft_ai.checkpoint.capture',    'ckpt-save'),
    (r'load_checkpoint|load_ckpt|restore_ckpt',      '@dft_ai.checkpoint.restart',    'ckpt-load'),
    (r'to_device|device_transfer|\btransfer\b',      '@dft_ai.device.transfer',       'device'),
]

# ── Loop iterator wrapping patterns ───────────────────────────────────────────
_EPOCH_VAR_RE = re.compile(r'\bepoch\b', re.IGNORECASE)
_FETCH_VAR_RE = re.compile(r'\b(batch|sample|item|data|loader_item|minibatch)\b', re.IGNORECASE)

# ── Module-level in-memory file cache ─────────────────────────────────────────
_AI_FILE_CACHE: Dict[tuple, List[str]] = {}

_AI_IMPORT = "from dftracer.python import dftracer, dft_fn as DFTracerFn, ai as dft_ai"
_DFT_FINI  = "_dft_log.finalize()"


def _detect_ai_decorator(fn: dict) -> str:
    """Return the best @dft_ai.* decorator string, or '' to use @_dlp.log."""
    name = fn["name"].lower()
    for pattern, decorator, _ in _AI_REGION_PATTERNS:
        if re.search(pattern, name):
            return decorator
    return ""


def _wrap_for_loops(source: str, lines: List[str]) -> Tuple[List[str], List[dict]]:
    """Wrap epoch / fetch for-loop iterators with dft_ai context managers.

    Uses ``ast.get_source_segment`` to extract the exact iterator expression and
    replaces it in-place on the same source line.  No line indices shift.

    Returns (modified_lines, list_of_wrap_records).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return lines, []

    replacements: List[Tuple[int, str, str, str]] = []  # (line_idx, old, new, kind)

    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue

        target = node.target
        if isinstance(target, ast.Name):
            var_name = target.id
        elif isinstance(target, ast.Tuple):
            var_name = " ".join(
                e.id for e in target.elts if isinstance(e, ast.Name)
            )
        else:
            continue

        iter_src = ast.get_source_segment(source, node.iter)
        if not iter_src:
            continue

        line_idx = node.lineno - 1  # 0-based

        if _EPOCH_VAR_RE.search(var_name):
            wrapped = f"dft_ai.pipeline.epoch.iter({iter_src})"
            replacements.append((line_idx, iter_src, wrapped, "epoch"))
        elif _FETCH_VAR_RE.search(var_name):
            wrapped = f"dft_ai.dataloader.fetch.iter({iter_src})"
            replacements.append((line_idx, iter_src, wrapped, "fetch"))

    result = list(lines)
    wraps: List[dict] = []
    seen_lines: set = set()

    for line_idx, old_text, new_text, kind in replacements:
        if line_idx in seen_lines:
            continue
        line = result[line_idx]
        # Skip if already wrapped (idempotency guard)
        if "dft_ai" in line and ".iter(" in line:
            continue
        if old_text in line:
            result[line_idx] = line.replace(old_text, new_text, 1)
            seen_lines.add(line_idx)
            wraps.append({"line": line_idx + 1, "kind": kind,
                          "original": old_text, "wrapped": new_text})

    return result, wraps


def register_ai_tools(mcp: FastMCP) -> None:
    """Register AI/ML annotation and file-discovery tools on *mcp*."""

    @mcp.tool()
    def find_source_files(
        folder: str,
        language: str,
        run_id: str = "",
        recursive: bool = True,
        exclude_patterns: List[str] = None,
    ) -> str:
        """Recursively list source files of the given language in a folder.

        Works without a session (pass an absolute *folder* path) or within a
        session workspace (pass *run_id* and a path relative to the session root).

        Supported languages / extensions:

        +----------+----------------------------------+
        | language | extensions                       |
        +==========+==================================+
        | c        | .c                               |
        +----------+----------------------------------+
        | cpp      | .cpp .cxx .cc .C                 |
        +----------+----------------------------------+
        | python   | .py                              |
        +----------+----------------------------------+
        | all      | all of the above                 |
        +----------+----------------------------------+

        Args:
            folder:           Absolute path to the directory to search, OR a path
                              relative to the session workspace root when *run_id*
                              is provided.
            language:         One of ``"c"``, ``"cpp"``, ``"c++"``, ``"python"``,
                              ``"py"``, ``"all"`` (case-insensitive).
            run_id:           Optional session identifier (from ``session_create``).
                              When supplied, *folder* is resolved relative to the
                              session workspace directory.
            recursive:        If ``True`` (default) search sub-directories.
            exclude_patterns: Optional list of glob patterns to exclude
                              (e.g. ``["**/test*", "**/__pycache__/**"]``).

        Returns:
            JSON with ``status``, ``message``, ``files`` (list of dicts with
            ``path``, ``size_bytes``, ``language``), ``count``, ``language``,
            ``folder``.
        """
        lang_key = language.strip().lower()
        if lang_key not in _LANG_EXTS:
            return _err(
                f"Unknown language '{language}'. "
                f"Supported: {sorted(_LANG_EXTS)}",
                supported=sorted(_LANG_EXTS),
            )
        exts = set(_LANG_EXTS[lang_key])

        # Resolve the folder
        if run_id:
            ws = _ws(run_id)
            base = ws / folder
        else:
            base = Path(folder)

        if not base.exists():
            return _err(f"Folder does not exist: {base}", folder=str(base))
        if not base.is_dir():
            return _err(f"Path is not a directory: {base}", folder=str(base))

        exclude = exclude_patterns or []

        # Walk
        glob_fn = base.rglob if recursive else base.glob
        found: List[dict] = []
        for p in sorted(glob_fn("*")):
            if not p.is_file():
                continue
            if p.suffix not in exts:
                continue
            # Apply exclusion patterns
            rel = p.relative_to(base)
            if any(rel.match(pat) for pat in exclude):
                continue
            # Determine language label
            if p.suffix == ".py":
                file_lang = "python"
            elif p.suffix == ".c":
                file_lang = "c"
            else:
                file_lang = "cpp"
            found.append({
                "path": str(p),
                "relative_path": str(rel),
                "size_bytes": p.stat().st_size,
                "language": file_lang,
            })

        return _ok(
            f"Found {len(found)} {language} file(s) in {base}.",
            files=found,
            count=len(found),
            language=language,
            folder=str(base),
        )

    @mcp.tool()
    def python_annotate_ai_file(
        run_id: str,
        filepath: str,
        category: str = "",
        is_entry: bool = False,
        logfile: str = "None",
        data_dir: str = "None",
        process_id: int = -1,
        annotate_loops: bool = True,
        annotate_nested: bool = True,
    ) -> str:
        """Annotate a Python file with AI/ML-region-aware dftracer decorators.

        Uses the full ``dft_ai`` API from ``dftracer.python`` to insert
        semantically-correct region decorators based on function name patterns.
        Matches the annotation style used in dlio_benchmark (master branch).

        **Decorator selection rules** (first match wins):

        +----------------------------------------------+-------------------------------+
        | Function name matches (case-insensitive)     | Decorator inserted            |
        +==============================================+===============================+
        | ``run``, ``main``, ``__call__``              | ``@dft_ai``                   |
        +----------------------------------------------+-------------------------------+
        | ``train``, ``training``, ``fit``             | ``@dft_ai.pipeline.train``    |
        +----------------------------------------------+-------------------------------+
        | ``eval``, ``evaluate``, ``validate``         | ``@dft_ai.pipeline.evaluate`` |
        +----------------------------------------------+-------------------------------+
        | ``test``, ``testing``                        | ``@dft_ai.pipeline.test``     |
        +----------------------------------------------+-------------------------------+
        | ``forward``                                  | ``@dft_ai.compute.forward``   |
        +----------------------------------------------+-------------------------------+
        | ``backward``                                 | ``@dft_ai.compute.backward``  |
        +----------------------------------------------+-------------------------------+
        | ``optimizer_step``, ``optim_step``           | ``@dft_ai.compute.step``      |
        +----------------------------------------------+-------------------------------+
        | ``compute``, ``train_step``, ``eval_step``   | ``@dft_ai.compute``           |
        +----------------------------------------------+-------------------------------+
        | ``preprocess``, ``transform``, ``augment``,  | ``@dft_ai.data.preprocess``   |
        | ``collate``                                  |                               |
        +----------------------------------------------+-------------------------------+
        | ``__getitem__``, ``read_index``, ``get_item``| ``@dft_ai.data.item``         |
        +----------------------------------------------+-------------------------------+
        | ``fetch``, ``load_batch``, ``next``          | ``@dft_ai.dataloader.fetch``  |
        +----------------------------------------------+-------------------------------+
        | ``save_checkpoint``, ``save_ckpt``           | ``@dft_ai.checkpoint.capture``|
        +----------------------------------------------+-------------------------------+
        | ``load_checkpoint``, ``load_ckpt``, etc.     | ``@dft_ai.checkpoint.restart``|
        +----------------------------------------------+-------------------------------+
        | ``to_device``, ``transfer``                  | ``@dft_ai.device.transfer``   |
        +----------------------------------------------+-------------------------------+
        | ``__init__``                                 | ``@_dlp.log_init``            |
        +----------------------------------------------+-------------------------------+
        | ``@staticmethod`` methods                    | ``@_dlp.log_static``          |
        +----------------------------------------------+-------------------------------+
        | *(everything else)*                          | ``@_dlp.log``                 |
        +----------------------------------------------+-------------------------------+

        **Loop iterator wrapping** (when ``annotate_loops=True``):

        * ``for epoch in <X>:``  →
          ``for epoch in dft_ai.pipeline.epoch.iter(<X>):``
        * ``for batch/sample/item in <X>:``  →
          ``for batch in dft_ai.dataloader.fetch.iter(<X>):``

        The injected import block is::

            from dftracer.python import dftracer, dft_fn as DFTracerFn, ai as dft_ai
            _dlp     = DFTracerFn("<category>")
            _dft_log = dftracer.initialize_log(...)   # entry-point files only

        All operations use the bottom-to-top insertion strategy: indices are
        computed from the original file, sorted highest-first, and applied in
        one pass before writing.  Loop wrapping is done as in-place line
        substitution (no index shift).

        Operation is idempotent: if the dftracer import is already present the
        file is returned unchanged.

        Args:
            run_id:          Session identifier from ``session_create``.
            filepath:        Path relative to the ``annotated/`` subfolder.
            category:        Category string for ``DFTracerFn("<category>")``.
                             Defaults to the filename stem.
            is_entry:        ``True`` for the program entry point (adds
                             ``initialize_log`` and ``finalize()``).
            logfile:         ``logfile`` arg for ``initialize_log``.
            data_dir:        ``data_dir`` arg for ``initialize_log``.
            process_id:      ``process_id`` arg for ``initialize_log``.
            annotate_loops:  When ``True`` (default), wrap epoch and fetch
                             for-loop iterators with ``dft_ai.*.iter()``.
            annotate_nested: When ``True`` (default), annotate nested functions
                             and class methods.

        Returns:
            JSON with ``status``, ``message``, ``filepath``, ``insertions``,
            ``functions``, ``ai_functions``, ``loop_wraps``, ``total_lines``,
            ``already_annotated``.
        """
        ws = _ws(run_id)
        abs_path = ws / "annotated" / filepath

        cache_key = (run_id, filepath)
        if cache_key in _AI_FILE_CACHE:
            lines = list(_AI_FILE_CACHE[cache_key])
        elif abs_path.exists():
            lines = abs_path.read_text(errors="replace").splitlines()
        else:
            return _err(f"File not found in annotated/: {filepath}")

        text = "\n".join(lines)

        # Idempotency guard
        if "from dftracer.python import" in text and "dft_ai" in text:
            return _ok(
                f"{filepath} is already AI-annotated — skipped.",
                filepath=filepath,
                insertions=0,
                functions=0,
                ai_functions=0,
                loop_wraps=0,
                total_lines=len(lines),
                already_annotated=True,
            )

        cat = category or Path(filepath).stem

        # ── Step 1: parse function map ─────────────────────────────────────
        all_fns = _extract_functions_from_ast(text)
        if not annotate_nested:
            all_fns = [f for f in all_fns if "." not in f["qualname"]]

        # ── Step 2: build import/init block ───────────────────────────────
        import_idx = _last_import_idx(lines)

        init_lines: List[str] = [
            _AI_IMPORT,
            f'_dlp = DFTracerFn("{cat}")',
        ]
        if is_entry:
            init_lines.append(
                f"_dft_log = dftracer.initialize_log("
                f"logfile={logfile}, data_dir={data_dir}, process_id={process_id})"
            )

        # ── Step 3: build per-function decorator insertions ───────────────
        insertions: List[tuple] = []

        # Init block — reversed so after sort+insert they land in order
        for ln in reversed(init_lines):
            insertions.append((import_idx, ln))

        ai_count = 0
        decorator_map: List[dict] = []

        for fn in all_fns:
            dec_idx = fn["decorator_insert_line"] - 1
            indent  = " " * fn["col_offset"]

            # Determine decorator
            if fn["is_init"]:
                dec = f"{indent}@_dlp.log_init"
                label = "log_init"
            elif fn["has_staticmethod"]:
                dec = f"{indent}@_dlp.log_static"
                label = "log_static"
            else:
                ai_dec = _detect_ai_decorator(fn)
                if ai_dec:
                    dec = f"{indent}{ai_dec}"
                    label = ai_dec
                    ai_count += 1
                else:
                    dec = f"{indent}@_dlp.log"
                    label = "log"

            insertions.append((dec_idx, dec))
            decorator_map.append({
                "function": fn["qualname"],
                "decorator": label,
                "line": fn["decorator_insert_line"],
            })

        # Entry-point finalize
        if is_entry:
            main_fn = next((f for f in all_fns if f["is_entry_point"]), None)
            if main_fn:
                body_ind  = " " * main_fn.get("body_col_offset", 4)
                fini_line = f"{body_ind}{_DFT_FINI}"
                returns   = main_fn.get("return_lines", [])
                if returns:
                    for ret_line in returns:
                        insertions.append((ret_line - 1, fini_line))
                else:
                    insertions.append((main_fn["end_line"] - 1, fini_line))
            else:
                insertions.append((len(lines), _DFT_FINI))

        # ── Step 4: sort highest-first, apply in one pass ─────────────────
        insertions.sort(key=lambda x: -x[0])
        for idx, txt in insertions:
            lines.insert(idx, txt)

        # ── Step 5: wrap for-loops (in-place substitution, no index shift) ─
        wrap_count = 0
        wrap_records: List[dict] = []
        if annotate_loops:
            new_text = "\n".join(lines)
            lines, wrap_records = _wrap_for_loops(new_text, lines)
            wrap_count = len(wrap_records)

        # ── Step 6: write once ────────────────────────────────────────────
        _AI_FILE_CACHE[cache_key] = list(lines)
        abs_path.write_text("\n".join(lines) + "\n")

        fn_count = len(all_fns)
        total_insertions = len(insertions) + wrap_count
        return _ok(
            f"AI-annotated {filepath}: {len(insertions)} decorator insertion(s), "
            f"{ai_count} AI/ML region(s), {wrap_count} loop wrap(s), "
            f"{fn_count} function(s) total.",
            filepath=filepath,
            insertions=len(insertions),
            functions=fn_count,
            ai_functions=ai_count,
            loop_wraps=wrap_count,
            loop_wrap_details=wrap_records,
            decorator_map=decorator_map,
            total_lines=len(lines),
            already_annotated=False,
        )

    @mcp.tool()
    def python_write_ai_file(run_id: str, filepath: str) -> str:
        """Flush the in-memory AI-annotated Python file buffer to disk.

        Call this after one or more in-memory AI annotation operations if you
        want to do the disk write separately.  ``python_annotate_ai_file``
        already writes to disk automatically, so this tool is for workflows
        that update the cache further before committing.

        Args:
            run_id:   Session identifier from ``session_create``.
            filepath: Path relative to the ``annotated/`` subfolder.

        Returns:
            JSON with ``status``, ``message``, ``filepath``, ``total_lines``.
        """
        cache_key = (run_id, filepath)
        if cache_key not in _AI_FILE_CACHE:
            return _err(
                f"No in-memory AI state for {filepath} — "
                "call python_annotate_ai_file first.",
                filepath=filepath,
            )
        ws = _ws(run_id)
        abs_path = ws / "annotated" / filepath
        lines = _AI_FILE_CACHE[cache_key]
        abs_path.write_text("\n".join(lines) + "\n")
        del _AI_FILE_CACHE[cache_key]
        return _ok(
            f"Wrote {len(lines)} lines to {filepath}.",
            filepath=filepath,
            total_lines=len(lines),
        )
