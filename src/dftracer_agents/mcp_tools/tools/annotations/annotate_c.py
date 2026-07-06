"""Per-file C annotation MCP tool for dftracer instrumentation.

Exposes ``session_annotate_c_file`` — a self-contained, parallelizable agent
tool for annotating a single ``.c`` source file with dftracer C macros.

Annotation API (C)
------------------
Every annotated C function follows this pattern::

    #include <dftracer/dftracer.h>   // add as LAST #include

    return_type function_name(params)
    {
        DFTRACER_C_FUNCTION_START();
        DFTRACER_C_FUNCTION_UPDATE_STR("comp", "<io|comm|mem|cpu>");
        DFTRACER_C_FUNCTION_UPDATE_STR("filename", path);   // key string params
        DFTRACER_C_FUNCTION_UPDATE_INT("count", (int)n);    // key numeric params
        ...
        if (err) {
            DFTRACER_C_FUNCTION_END();
            return -1;
        }
        DFTRACER_C_FUNCTION_END();
        return result;
    }

main() without MPI::

    int main(int argc, char **argv) {
        DFTRACER_C_INIT(NULL, NULL, NULL);
        DFTRACER_C_FUNCTION_START();
        ...
        DFTRACER_C_FUNCTION_END();
        DFTRACER_C_FINI();
        return 0;
    }

main() with MPI::

    int main(int argc, char **argv) {
        MPI_Init(&argc, &argv);
        DFTRACER_C_INIT(NULL, NULL, NULL);   // after MPI_Init
        DFTRACER_C_FUNCTION_START();
        ...
        DFTRACER_C_FUNCTION_END();
        DFTRACER_C_FINI();                   // before MPI_Finalize
        MPI_Finalize();
        return 0;
    }

comp= classification::

    "io"   → file I/O, backend lifecycle, vendor FS helpers
    "comm" → MPI wrappers, network I/O, distributed FS (S3/HDFS/RADOS/DFS)
    "mem"  → memcpy, large buffer alloc/free, mmap region setup
    "cpu"  → checksums, compression, encryption, hashing

Parallelism
-----------
Each call to ``session_annotate_c_file`` is fully independent — it reads one
file, annotates it, and writes it back.  An orchestrator can issue calls for
all C files simultaneously (they operate on separate files in the workspace).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP

from ..session.workspace import _ws, _ok, _err


def register_c_annotation_tools(mcp: FastMCP) -> None:

    @mcp.tool()
    def session_annotate_c_file(
        run_id: str,
        filepath: str,
        build_errors: str = "",
        user_notes: str = "",
    ) -> str:
        """Annotate a single .c source file with dftracer C macros.

        This tool is the per-file C annotation agent.  It is designed to be
        called in parallel with other per-file annotation calls — each invocation
        is independent and operates on a single file inside the session workspace.

        Procedure (mirrors the annotate-c.yaml recipe):

        **Step 0 — Load lessons**
            Read /workspaces/dftracer-agents/.agents/skills/dftracer-annotation-lessons/SKILL.md
            and apply all applicable lessons to this file before writing anything.

        **Step 1 — Read the file**
            Read the current state of the file from annotated/<filepath>.
            Record the original line count — the final written file MUST have MORE lines.
            Pre-scan for braceless early-exit lines before writing any macros::

                grep -n "if.*return|if.*continue|if.*break" <file> | grep -v "{" | grep -v "//"

            For every braceless hit: add explicit braces FIRST, then annotate.

        **Step 2 — Prepare and extract function map**
            2a. Add braces via ``clang_add_braces``:
                Ensures every if/for/while/do body has explicit {}.  Without
                braces, inserting DFTRACER_C_FUNCTION_END() creates dangling-else
                or orphaned macros that break compilation.

            2b. Extract function map via ``clang_extract_functions``:
                Returns JSON list of all function definitions with exact line numbers:
                  • name            — function name
                  • start_line      — first line of the signature
                  • open_brace_line — line of the opening '{'
                  • body_first_line — FIRST line inside the body (insert START here)
                  • close_brace_line— line of the closing '}'
                  • exit_lines      — list of {line, type} for every return/exit()/abort()
                  • is_entry_point  — true for main()

            comp= C-specific guidance:
              • Xfer to/from a file (POSIX/MMAP/HDF5/NCMPI)     → "io"
              • Xfer via network/MPI (MPIIO/S3/HDFS/RADOS/DFS)   → "comm"
              • Xfer that is a memcpy into mmap region             → "mem"
              • Backend lifecycle (init/final/initialize/finalize) → "io"

        **Step 3 — Review exit paths from FUNC_MAP**
            For each function in FUNC_MAP, verify exit_lines against the source:
              • type="return"       → insert DFTRACER_C_FUNCTION_END() on the line
                                       BEFORE that return, matching indentation
              • type="exit"/"abort" → insert END + DFTRACER_C_FINI() before the call
                                       (main only)
              • exit_lines empty    → void function → insert END as last statement
                                       before close_brace_line

            MPI_CHECK / NCMPI_CHECK / HGOTO_ERROR / ERR_GOTO hide an internal
            return/goto — do NOT add END before them; only before visible returns.
            goto-style exits (labels done:/err:/out:) → single END at the label,
            NOT before each individual goto statement.

        **Step 4 — Apply annotation (per-function incremental loop)**
            Process FUNC_MAP ONE FUNCTION AT A TIME using the per-function loop:

            For each function in FUNC_MAP (libraries before entry-points):
              4a. Using EXACT LINE NUMBERS from FUNC_MAP:
                    • Insert DFTRACER_C_FUNCTION_START() at body_first_line
                    • Insert DFTRACER_C_FUNCTION_UPDATE_STR("comp", "<type>") next line
                    • For each {line, type} in exit_lines: insert END on the line
                      IMMEDIATELY BEFORE that exit line, matching indentation
                    • If exit_lines empty: insert END as last statement before close_brace_line

                  For main() additionally:
                    • Insert DFTRACER_C_INIT(NULL, NULL, NULL) as VERY FIRST statement
                      (before START), or after MPI_Init(&argc, &argv) if MPI is used
                    • Insert DFTRACER_C_FINI() before MPI_Finalize() or before final return

              4b. Write the updated file via ``session_write_file``

              4c. Syntax check after EVERY function write::

                    WS=/workspaces/dftracer-agents/workspaces/<run_id>
                    gcc -include /tmp/dftracer_stub.h -fsyntax-only -w \\
                      -x c ${WS}/annotated/<filepath> 2>&1

                  Create the stub header first (once per file)::

                    cat > /tmp/dftracer_stub.h << 'STUB'
                    #define DFTRACER_C_FUNCTION_START()
                    #define DFTRACER_C_FUNCTION_END()
                    #define DFTRACER_C_FUNCTION_UPDATE_STR(k,v)
                    #define DFTRACER_C_FUNCTION_UPDATE_INT(k,v)
                    #define DFTRACER_C_INIT(a,b,c)
                    #define DFTRACER_C_FINI()
                    STUB

              4d. On syntax error: fix ONLY the failing function (max 2 retries),
                  then strip macros from that function only and mark PENDING.

        **Step 5 — Coverage verification**::

            WS=/workspaces/dftracer-agents/workspaces/<run_id>
            F=${WS}/annotated/<filepath>
            # 1. START vs comp count (must be equal)
            echo "START: $(grep -c 'DFTRACER_C_FUNCTION_START' $F)"
            echo "comp=: $(grep -c 'DFTRACER_C_FUNCTION_UPDATE_STR.*\\"comp\\"' $F)"
            # 2. No END at column 0
            grep -n "^DFTRACER_C_FUNCTION_END" $F

        **Step 6 — Output per-file report** in this structure::

            FILE: <filepath>
            STATUS: DONE | PARTIAL | FAILED
            ANNOTATED: <N> functions  (io=<n>, comm=<n>, mem=<n>, cpu=<n>)
            SKIPPED:   <function names with Rule 0 justification, one per line>
            PENDING:   <functions not yet annotated with reason>
            NEW_ERRORS: <any new compile/runtime errors discovered>
            LESSONS:   <new patterns or pitfalls not yet in the lessons file>

        **Absolute placement rules (never violate):**
            • START is the FIRST statement inside { — nothing before it (except INIT in main)
            • START goes AFTER {, never before it
            • START never inside if/for/while/switch blocks
            • comp= UPDATE is the FIRST UPDATE immediately after START
            • END indentation matches the return line it precedes; never at column 0
            • Never place END after a return (dead code)
            • Never annotate a .h header file
            • Never annotate a forward declaration (line ending with ";")

        **ALWAYS ANNOTATE (mandatory — never apply Rule 0 skip):**
            • Lifecycle:   *_init, *_final, *_initialize, *_finalize
            • Sync/flush:  *_fsync, *_flush, *_sync
            • File ops:    *_delete, *_rename, *_stat, *_mknod, *_getfilesize
            • Vendor FS:   gpfs_*, beegfs_*, lustre_*, hdfs_*, ceph_*, daos_*

        **SKIP (Rule 0) ONLY if ALL of these are true:**
            • Pure getter/setter ≤ 5 lines, returns a single field
            • No I/O, no data movement, no syscall

        **BUILD ERROR MODE:**
            If build_errors is non-empty, focus on fixing the functions named in
            those errors FIRST using the per-function loop, then continue with any
            unannotated functions.

        **C-specific pitfalls:**
            PC1  END after return         → dead code; swap: END must precede the return
            PC2  END at column 0          → match indentation of the return it precedes
            PC3  START before {           → syntax error; move START to first line inside {}
            PC4  Error macro hides exit   → MPI_CHECK/H5EPRINT expands to hidden return;
                                            no END before them; only before visible returns
            PC5  goto: END before each    → duplicate ENDs; put single END at label instead
            PC6  Fwd decl annotated       → annotated a ";" line; annotate definition instead
            PC7  DFTRACER_INIT value      → correct values: FUNCTION (recommended), PRELOAD,
                                            HYBRID — never use 0 for POSIX tracing

        Args:
            run_id:       Session identifier (e.g. ``myapp/20260616_120000``).
            filepath:     Path of the ``.c`` file relative to ``annotated/`` subfolder.
            build_errors: Compiler stderr from a failed build (BUILD ERROR MODE).
            user_notes:   Free-text feedback from the pipeline orchestrator.

        Returns:
            JSON with ``status``, ``filepath``, ``annotated``, ``skipped``,
            ``pending``, ``lessons``, and ``report`` keys.
        """
        ws = _ws(run_id)
        if not ws.exists():
            return _err(f"Workspace not found for run_id: {run_id}")

        ann_dir = ws / "annotated"
        if not ann_dir.exists():
            return _err("annotated/ directory not found — run session_copy_annotated first.")

        target = ann_dir / filepath
        if not target.exists():
            return _err(f"File not found: annotated/{filepath}")

        if target.suffix.lower() not in {".c"}:
            return _err(f"session_annotate_c_file only handles .c files, got: {filepath}")

        # Read file to check it exists and get line count
        try:
            original_content = target.read_text(errors="ignore")
        except OSError as e:
            return _err(f"Cannot read {filepath}: {e}")

        original_lines = len(original_content.splitlines())

        # Check if already fully annotated
        if "DFTRACER_C_FUNCTION_START" in original_content and not build_errors:
            annotated_count = original_content.count("DFTRACER_C_FUNCTION_START")
            return _ok(
                f"File already annotated with {annotated_count} function(s). "
                "Pass build_errors to enter fix mode.",
                filepath=filepath,
                status="already_annotated",
                annotated=annotated_count,
                skipped=[],
                pending=[],
                lessons=[],
            )

        # Provide context for the annotation agent
        context = {
            "run_id": run_id,
            "filepath": filepath,
            "workspace": str(ws),
            "annotated_dir": str(ann_dir),
            "original_line_count": original_lines,
            "has_build_errors": bool(build_errors),
            "build_errors_excerpt": build_errors[:500] if build_errors else "",
            "user_notes": user_notes,
            "file_size_bytes": target.stat().st_size,
        }

        # Quick pre-scan for braceless early exits
        braceless_hits = []
        for i, line in enumerate(original_content.splitlines(), start=1):
            stripped = line.strip()
            if (
                re.match(r'if\s*\(', stripped)
                and ("return" in stripped or "continue" in stripped or "break" in stripped)
                and "{" not in stripped
                and not stripped.startswith("//")
                and not stripped.startswith("*")
            ):
                braceless_hits.append(f"line {i}: {stripped[:80]}")

        # Check for forward declarations that must NOT be annotated
        fwd_decls = []
        for i, line in enumerate(original_content.splitlines(), start=1):
            stripped = line.strip()
            if (
                stripped.endswith(";")
                and "(" in stripped
                and not stripped.startswith("#")
                and not stripped.startswith("//")
                and not stripped.startswith("*")
                and re.match(r'^[a-zA-Z_]', stripped)
            ):
                fwd_decls.append(f"line {i}: {stripped[:80]}")

        return _ok(
            f"C file ready for annotation — {original_lines} lines. "
            + (f"BUILD ERROR MODE: {len(build_errors.splitlines())} error line(s). " if build_errors else "")
            + (f"Pre-scan: {len(braceless_hits)} braceless early-exit(s) to brace first. " if braceless_hits else "")
            + "Use clang_add_braces, clang_extract_functions, then session_write_file "
            "per function with gcc syntax check after each. "
            "See tool docstring for complete procedure.",
            context=context,
            braceless_early_exits=braceless_hits[:20],
            forward_declarations_count=len(fwd_decls),
            annotation_procedure={
                "step_0": "Read lessons from dftracer-annotation-lessons/SKILL.md",
                "step_1": f"Read annotated/{filepath} — record line count ({original_lines})",
                "step_2a": f"clang_add_braces(run_id='{run_id}', filepath='{filepath}')",
                "step_2b": f"clang_extract_functions(run_id='{run_id}', filepath='{filepath}')",
                "step_3": "Review exit_lines from FUNC_MAP for each function",
                "step_4": "Per-function loop: insert macros, write, gcc syntax check",
                "step_5": "Coverage verification: START count == comp count",
                "step_6": "Output per-file report with STATUS/ANNOTATED/SKIPPED/PENDING",
            },
            build_error_mode=bool(build_errors),
        )


