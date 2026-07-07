"""Smoke-test-driven annotation scoping for large C/C++ codebases.

For projects with hundreds of source files (e.g. Montage's ~700 files across
100+ independently linked executables), annotating every file wastes tool
calls and risks tripping annotator bugs (brace-insertion edge cases) in code
that is never exercised by the smoke test anyway. This module figures out
which source files actually matter *before* annotation starts:

1. Extract the set of binary names referenced by the smoke-test command
   (e.g. ``mProjExec``, ``mAdd``) — either from an explicit command string,
   or (preferred, most accurate) by ``strace -f -e trace=execve`` around a
   real run of the smoke test against the *original* (unannotated) build.
2. For each binary, locate the ``Makefile`` recipe that links it and parse
   out every ``.o`` object file feeding that link line.
3. Resolve each ``.o`` back to its source ``.c``/``.cpp`` file, honouring
   relative-path prefixes (``../util/foo.o`` → ``util/foo.c`` from the
   Makefile's own directory).
4. Return the de-duplicated union of source files across all invoked
   binaries — this is the annotation target set.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .workspace import _run


_LINK_RECIPE_RE = re.compile(r'\$\(CC\)\s+.*-o\s+\S+\s+(.+?)(?:\\\s*\n(.*))*$', re.MULTILINE)
_OBJ_TOKEN_RE = re.compile(r'([./\w-]+\.o)\b')


def _find_makefile_targets(source_root: Path, binary_names: Set[str]) -> Dict[str, List[str]]:
    """Search every Makefile under *source_root* for link recipes producing
    one of *binary_names*, returning ``{binary_name: [".o" tokens as written]}``.
    """
    results: Dict[str, List[str]] = {}
    for makefile in source_root.rglob("Makefile"):
        try:
            text = makefile.read_text(errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines):
            # Look for "$(CC) ... -o <name> <objs...>" possibly continued
            # across multiple backslash-continued lines.
            if "-o" not in line or "$(CC)" not in line and "gcc" not in line and "$(CXX)" not in line:
                continue
            m = re.search(r'-o\s+(\S+)', line)
            if not m:
                continue
            target_name = m.group(1)
            if target_name not in binary_names:
                continue
            # Collect this line plus any backslash-continuation lines.
            recipe_lines = [line]
            j = i
            while recipe_lines[-1].rstrip().endswith("\\") and j + 1 < len(lines):
                j += 1
                recipe_lines.append(lines[j])
            full_recipe = " ".join(recipe_lines)
            objs = _OBJ_TOKEN_RE.findall(full_recipe)
            results.setdefault(target_name, [])
            for o in objs:
                if o not in results[target_name]:
                    results[target_name].append(o)
            results[target_name] = (results.get(target_name, []), makefile.parent)
        # normalise: some targets may have been stored as (list, dir) above
    # Second pass to normalise storage shape (list of (obj, resolved_dir))
    return results


def _resolve_obj_to_source(obj_token: str, makefile_dir: Path, source_root: Path) -> Optional[str]:
    """Resolve a ``.o`` token (possibly with ``../`` prefixes) from the
    Makefile's own directory to a source-relative ``.c``/``.cpp`` path.
    """
    obj_path = (makefile_dir / obj_token).resolve()
    for ext in (".c", ".cpp", ".cxx", ".cc"):
        candidate = obj_path.with_suffix(ext)
        if candidate.exists():
            try:
                return str(candidate.relative_to(source_root))
            except ValueError:
                return None
    return None


def identify_smoke_test_source_files(
    run_id: str,
    ws: Path,
    smoke_cmd: str,
    use_strace: bool = True,
    timeout: int = 300,
) -> Dict[str, Any]:
    """Determine the minimal set of source files needed to annotate for a
    smoke test, by discovering which compiled binaries it actually invokes.

    Returns a dict with:
        * ``binaries`` — list of binary names discovered
        * ``source_files`` — de-duplicated relative source file paths
        * ``binary_to_files`` — per-binary breakdown
        * ``method`` — ``"strace"`` or ``"static"``
    """
    source_root = ws / "source"
    bin_dir = ws / "install" / "bin"
    if not bin_dir.exists():
        bin_dir = ws / "source" / "bin"

    binary_names: Set[str] = set()
    method = "static"

    if use_strace:
        strace_log = ws / "tmp" / "smoke_test_execve.log"
        strace_log.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["strace", "-f", "-e", "trace=execve", "-o", str(strace_log), "bash", "-c", smoke_cmd]
        r = _run(cmd, cwd=ws, timeout=timeout)
        if strace_log.exists():
            text = strace_log.read_text(errors="replace")
            for m in re.finditer(r'execve\("([^"]+)"', text):
                path = m.group(1)
                name = Path(path).name
                # Only count binaries that live under this workspace's bin/.
                if bin_dir.name and (str(bin_dir) in path or (bin_dir / name).exists()):
                    binary_names.add(name)
            if binary_names:
                method = "strace"

    if not binary_names:
        # Static fallback: pull out any token in smoke_cmd matching a real
        # binary under bin_dir.
        if bin_dir.exists():
            available = {p.name for p in bin_dir.iterdir() if p.is_file()}
            for tok in re.findall(r'[\w.-]+', smoke_cmd):
                base = Path(tok).name
                if base in available:
                    binary_names.add(base)

    binary_to_files: Dict[str, List[str]] = {}
    all_files: Set[str] = set()

    for makefile in source_root.rglob("Makefile"):
        try:
            text = makefile.read_text(errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if "-o" not in line:
                continue
            m = re.search(r'-o\s+(\S+)\s', line + " ")
            if not m:
                continue
            target_name = m.group(1)
            if target_name not in binary_names:
                continue
            recipe_lines = [line]
            j = i
            while recipe_lines[-1].rstrip().endswith("\\") and j + 1 < len(lines):
                j += 1
                recipe_lines.append(lines[j])
            full_recipe = " ".join(recipe_lines)
            objs = _OBJ_TOKEN_RE.findall(full_recipe)
            files_for_binary = binary_to_files.setdefault(target_name, [])
            for obj_token in objs:
                src = _resolve_obj_to_source(obj_token, makefile.parent, source_root)
                if src and src not in files_for_binary:
                    files_for_binary.append(src)
                    all_files.add(src)
            # Also include the binary's own directory .c files sharing the
            # target name pattern (montage<Name>.c alongside m<Name>.c),
            # since some modules' main() and library implementation are
            # compiled together but only one .o is explicitly listed if the
            # Makefile uses a wildcard rule.
            for c_file in makefile.parent.glob("*.c"):
                rel = str(c_file.relative_to(source_root))
                if rel not in files_for_binary and c_file.stem.lower().endswith(target_name.lower().lstrip("m")):
                    files_for_binary.append(rel)
                    all_files.add(rel)

    return {
        "status": "ok",
        "method": method,
        "binaries": sorted(binary_names),
        "binary_to_files": binary_to_files,
        "source_files": sorted(all_files),
        "total_binaries": len(binary_names),
        "total_source_files": len(all_files),
    }


def register_annotation_filter_tools(mcp) -> None:
    """Register the ``session_identify_smoke_test_files`` MCP tool."""

    @mcp.tool()
    def session_identify_smoke_test_files(
        run_id: str,
        smoke_cmd: str,
        use_strace: bool = True,
    ) -> str:
        """Identify the minimal set of source files a smoke test actually exercises.

        For large C/C++ codebases with many independently-linked executables
        (e.g. Montage: ~700 files, 100+ binaries), annotating every file is
        wasteful and increases exposure to annotator edge cases in code paths
        that are never run. This tool narrows the annotation scope to just
        the files needed to build the binaries the smoke test invokes.

        Method:
            1. If ``use_strace`` (default), run *smoke_cmd* under
               ``strace -f -e trace=execve`` against the **original**
               (``source/`` + already-built ``install/bin/``) tree to capture
               every binary actually invoked at runtime. Falls back to a
               static scan of *smoke_cmd* text for binary names matching
               files in ``install/bin/`` if strace is unavailable or the
               command doesn't touch any workspace binaries.
            2. For each invoked binary, parse the ``Makefile`` link recipe
               that builds it (searches every ``Makefile`` under ``source/``)
               to extract every ``.o`` object file feeding that link.
            3. Resolve each object file back to its ``.c``/``.cpp`` source,
               honoring the Makefile's own relative-path context (handles
               ``../util/foo.o`` style references).
            4. Return the de-duplicated union of source files across all
               invoked binaries.

        Args:
            run_id: Session identifier returned by ``session_create``.
            smoke_cmd: The smoke-test command line (same string you'd pass to
                ``session_run_smoke_test``). Must be runnable against the
                original ``install/bin/`` (i.e. call this *before* annotating).
            use_strace: Prefer dynamic discovery via ``strace`` (default
                ``True``). Set ``False`` to skip straight to the static
                text-scan fallback (useful when strace isn't installed or
                the smoke test can't actually be executed yet).

        Returns:
            JSON string with keys:
                * ``status`` — ``"ok"``.
                * ``method`` — ``"strace"`` or ``"static"``.
                * ``binaries`` — sorted list of binary names discovered.
                * ``binary_to_files`` — dict mapping each binary name to its
                  list of required source files.
                * ``source_files`` — sorted de-duplicated union of all
                  required source files (paths relative to ``source/``) —
                  pass this list to ``clang_annotate_file`` in a loop instead
                  of ``clang_annotate_project`` to scope annotation.
                * ``total_binaries`` / ``total_source_files`` — counts.
        """
        import json as _json
        from .workspace import _ws

        ws = _ws(run_id)
        result = identify_smoke_test_source_files(run_id, ws, smoke_cmd, use_strace=use_strace)
        return _json.dumps(result, indent=2)
