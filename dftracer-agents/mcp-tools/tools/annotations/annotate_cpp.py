"""Per-file C++ annotation MCP tool for dftracer instrumentation.

Exposes ``session_annotate_cpp_file`` — a self-contained, parallelizable agent
tool for annotating a single ``.cpp``/``.cxx``/``.cc`` source file with
dftracer C++ RAII macros.

Annotation API (C++)
--------------------
Regular functions use RAII — the destructor fires automatically on scope exit::

    #include <dftracer/dftracer.h>   // add as LAST #include in .cpp/.cxx only

    void my_function(const char *path, size_t size) {
        DFTRACER_CPP_FUNCTION();                        // RAII guard — END is automatic
        DFTRACER_CPP_FUNCTION_UPDATE("comp", "io");     // mandatory comp= (string only)
        DFTRACER_CPP_FUNCTION_UPDATE("path", path);     // key string params
        // No explicit END — destructor fires on scope exit
    }

main() uses REGION (not RAII — RAII would fire after FINI)::

    int main(int argc, char **argv) {
        DFTRACER_CPP_INIT(nullptr, nullptr, nullptr);
        DFTRACER_CPP_REGION_START(main_region);
        ...
        if (error) {
            DFTRACER_CPP_REGION_END(main_region);
            DFTRACER_CPP_FINI();
            return 1;
        }
        DFTRACER_CPP_REGION_END(main_region);
        DFTRACER_CPP_FINI();
        return 0;
    }

With MPI::

    MPI_Init → DFTRACER_CPP_INIT → DFTRACER_CPP_REGION_START →
    ... → DFTRACER_CPP_REGION_END → DFTRACER_CPP_FINI → MPI_Finalize

DFTRACER_CPP_FUNCTION_UPDATE takes string values only (no UPDATE_INT in C++ API).
For numeric params, convert to string or omit.

comp= classification::

    "io"   → file I/O, backend lifecycle, std::filesystem / fstream / ifstream
    "comm" → MPI calls wrapped in C++, network I/O
    "mem"  → memcpy, large buffer alloc/free, mmap
    "cpu"  → checksums, compression, encryption, hashing

Parallelism
-----------
Each call to ``session_annotate_cpp_file`` is fully independent.  An
orchestrator can issue calls for all C++ files simultaneously.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP

from ..session.workspace import _ws, _ok, _err


def register_cpp_annotation_tools(mcp: FastMCP) -> None:

    @mcp.tool()
    def session_annotate_cpp_file(
        run_id: str,
        filepath: str,
        build_errors: str = "",
        user_notes: str = "",
    ) -> str:
        """Annotate a single .cpp/.cxx/.cc source file with dftracer C++ RAII macros.

        This tool is the per-file C++ annotation agent.  It is designed to be
        called in parallel with other per-file annotation calls — each invocation
        is independent and operates on a single file inside the session workspace.

        Procedure (mirrors the annotate-cpp.yaml recipe):

        **Step 0 — Load lessons**
            Read /workspaces/dftracer-agents/.agents/skills/dftracer-annotation-lessons/SKILL.md
            and apply all applicable lessons before writing anything.

        **Step 1 — Read the file**
            Read the current state from annotated/<filepath>.
            Record the original line count — the final written file MUST have MORE lines.

            Fetch C++-specific docs before classifying functions::

                dftracer__docs_search(query="DFTRACER_CPP_FUNCTION RAII REGION_START \\
                  REGION_END INIT FINI C++ API", source="dftracer", fetch_content=True)

        **Step 2 — Prepare and extract function map**
            2a. Add braces via ``clang_add_braces``:
                Ensures every if/for/while/do body has explicit {}.
                Without braces, RAII scope is ambiguous and macro insertion
                can break the AST.

            2b. Extract function map via ``clang_extract_functions``:
                Returns JSON list of all function/method definitions:
                  • name             — function or method name
                  • start_line       — first line of the signature
                  • open_brace_line  — line of the opening '{'
                  • body_first_line  — FIRST line inside the body
                  • close_brace_line — line of the closing '}'
                  • exit_lines       — list of {line, type} for returns in main()
                                       (empty for most C++ functions; RAII auto-ends)
                  • is_entry_point   — true for main()

            C++-specific classification:
              • Annotate class member functions, free functions, constructors, destructors
              • Constructors/destructors that open/close resources → ANNOTATE
              • Operator overloads with I/O (operator<<) → ANNOTATE
              • Template function definitions → ANNOTATE eligible
              • Inline functions in .hpp → SKIP (annotate in .cpp source instead)

        **Step 3 — Review exit paths for main() only**
            C++ RAII guard fires automatically on scope exit — NO manual END
            needed for regular functions.  Exit path mapping is ONLY for main():

              • main() exit_lines type="return" →
                  insert DFTRACER_CPP_REGION_END(main_region) on the line BEFORE
              • main() exit_lines type="exit"/"abort" →
                  insert REGION_END then DFTRACER_CPP_FINI() before the call
              • Regular functions: exit_lines are informational only

        **Step 4 — Apply annotation (per-function incremental loop)**
            Process FUNC_MAP ONE FUNCTION AT A TIME:

            For each function in FUNC_MAP (libraries before entry-points):
              4a. Using EXACT LINE NUMBERS from FUNC_MAP:

                  Regular functions (all except main):
                    • Insert DFTRACER_CPP_FUNCTION() at body_first_line
                    • Insert DFTRACER_CPP_FUNCTION_UPDATE("comp", "<type>") next line
                    • No manual END — RAII destructor fires automatically

                  main() (REGION — not FUNCTION):
                    • Insert DFTRACER_CPP_INIT(nullptr, nullptr, nullptr) as very first
                      statement (or after MPI_Init if MPI is used)
                    • Insert DFTRACER_CPP_REGION_START(main_region) on the next line
                    • For each exit_line: insert DFTRACER_CPP_REGION_END(main_region)
                      on the line BEFORE, then DFTRACER_CPP_FINI() before MPI_Finalize

              4b. Write the updated file via ``session_write_file``

              4c. Syntax check after EVERY function write::

                    WS=/workspaces/dftracer-agents/workspaces/<run_id>
                    g++ -include /tmp/dftracer_stub.h -fsyntax-only -w -std=c++14 \\
                      ${WS}/annotated/<filepath> 2>&1

                  Create the stub header first (once per file)::

                    cat > /tmp/dftracer_stub.h << 'STUB'
                    #define DFTRACER_CPP_FUNCTION()
                    #define DFTRACER_CPP_FUNCTION_UPDATE(k,v)
                    #define DFTRACER_CPP_INIT(a,b,c)
                    #define DFTRACER_CPP_FINI()
                    #define DFTRACER_CPP_REGION_START(x) do{(void)(x);}while(0)
                    #define DFTRACER_CPP_REGION_END(x)   do{(void)(x);}while(0)
                    STUB

              4d. On syntax error: fix ONLY the failing function (max 2 retries),
                  then strip macros from that function only and mark PENDING.

        **Step 5 — Coverage verification**::

            WS=/workspaces/dftracer-agents/workspaces/<run_id>
            F=${WS}/annotated/<filepath>
            # 1. CPP_FUNCTION count vs comp count (must be equal)
            echo "CPP_FUNCTION: $(grep -c 'DFTRACER_CPP_FUNCTION()' $F)"
            echo "comp=:        $(grep -c 'DFTRACER_CPP_FUNCTION_UPDATE.*\\"comp\\"' $F)"
            # 2. REGION macros outside main (should be zero)
            grep -n "DFTRACER_CPP_REGION" $F | grep -v "main"
            # 3. C macros in C++ file (should be zero)
            grep -n "DFTRACER_C_" $F

        **Step 6 — Output per-file report** in this structure::

            FILE: <filepath>
            STATUS: DONE | PARTIAL | FAILED
            ANNOTATED: <N> functions  (io=<n>, comm=<n>, mem=<n>, cpu=<n>)
            SKIPPED:   <function names with Rule 0 justification>
            PENDING:   <functions not yet annotated with reason>
            NEW_ERRORS: <any new compile/runtime errors discovered>
            LESSONS:   <new patterns or pitfalls not yet in the lessons file>

        **Absolute rules (never violate):**
            • DFTRACER_CPP_FUNCTION() is the FIRST statement inside { for regular functions
            • Never use DFTRACER_C_* macros in .cpp/.cxx files
            • Never use DFTRACER_CPP_REGION_* in non-main functions
            • Never use DFTRACER_CPP_FUNCTION() in main() — use REGION there
            • Never add include to .h/.hpp header files
            • DFTRACER_CPP_FUNCTION_UPDATE takes string values only (const char *)
              For numeric params, convert to string or omit — no UPDATE_INT in C++

        **ALWAYS ANNOTATE (mandatory — never apply Rule 0 skip):**
            • Lifecycle:   *_init, *_final, *_initialize, *_finalize
            • Sync/flush:  *_fsync, *_flush, *_sync
            • File ops:    *_delete, *_rename, *_stat, *_mknod, *_getFileSize
            • Vendor FS:   gpfs_*, beegfs_*, lustre_*, hdfs_*, ceph_*, daos_*

        **C++-specific pitfalls:**
            CP1  Used DFTRACER_C_* in .cpp       → replace with DFTRACER_CPP_*
            CP2  Used CPP_FUNCTION() in main()   → replace with REGION_START/END
            CP3  Used REGION in regular function → replace with CPP_FUNCTION()
            CP4  Added manual END after FUNCTION → remove it; RAII handles END automatically
            CP5  Used UPDATE_INT                 → no numeric UPDATE in C++ API; omit or cast
            CP6  Added include to .hpp header    → move to .cpp/.cxx source file
            CP7  comp= UPDATE missing            → CPP_FUNCTION count ≠ comp count; fix gaps
            CP8  REGION_END missing before return → add REGION_END before each return in main()

        Args:
            run_id:       Session identifier (e.g. ``myapp/20260616_120000``).
            filepath:     Path of the ``.cpp``/``.cxx``/``.cc`` file relative to
                          ``annotated/`` subfolder.
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

        cpp_exts = {".cpp", ".cxx", ".cc"}
        if target.suffix.lower() not in cpp_exts:
            return _err(f"session_annotate_cpp_file only handles .cpp/.cxx/.cc files, got: {filepath}")

        try:
            original_content = target.read_text(errors="ignore")
        except OSError as e:
            return _err(f"Cannot read {filepath}: {e}")

        original_lines = len(original_content.splitlines())

        if "DFTRACER_CPP_FUNCTION" in original_content and not build_errors:
            annotated_count = original_content.count("DFTRACER_CPP_FUNCTION()")
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

        # Pre-scan for braceless early exits
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

        context = {
            "run_id": run_id,
            "filepath": filepath,
            "workspace": str(ws),
            "annotated_dir": str(ann_dir),
            "original_line_count": original_lines,
            "has_build_errors": bool(build_errors),
            "build_errors_excerpt": build_errors[:500] if build_errors else "",
            "user_notes": user_notes,
        }

        return _ok(
            f"C++ file ready for annotation — {original_lines} lines. "
            + (f"BUILD ERROR MODE: {len(build_errors.splitlines())} error line(s). " if build_errors else "")
            + (f"Pre-scan: {len(braceless_hits)} braceless early-exit(s) to brace first. " if braceless_hits else "")
            + "Use clang_add_braces, clang_extract_functions, then session_write_file "
            "per function with g++ syntax check after each. "
            "See tool docstring for complete C++ procedure.",
            context=context,
            braceless_early_exits=braceless_hits[:20],
            annotation_procedure={
                "step_0": "Read lessons from dftracer-annotation-lessons/SKILL.md",
                "step_1": f"Read annotated/{filepath} — record line count ({original_lines})",
                "step_2a": f"clang_add_braces(run_id='{run_id}', filepath='{filepath}')",
                "step_2b": f"clang_extract_functions(run_id='{run_id}', filepath='{filepath}')",
                "step_3": "Review exit_lines from FUNC_MAP (main() only needs REGION; regular functions use RAII)",
                "step_4": "Per-function loop: CPP_FUNCTION() for regular; REGION for main; write; g++ check",
                "step_5": "Coverage: CPP_FUNCTION count == comp count; no C_ macros; no REGION outside main",
                "step_6": "Output per-file report with STATUS/ANNOTATED/SKIPPED/PENDING",
            },
            build_error_mode=bool(build_errors),
        )
