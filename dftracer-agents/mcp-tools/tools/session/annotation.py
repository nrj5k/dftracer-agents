"""
C/C++ and Python annotation helpers for dftracer instrumentation.
"""
from __future__ import annotations

import difflib
import re
import textwrap
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# Annotation helpers — C / C++
# ---------------------------------------------------------------------------

_C_INCLUDE = "#include <dftracer/dftracer.h>"

# Keywords that must never be treated as function names by the annotation regex
_C_KEYWORDS: frozenset = frozenset({
    "if", "else", "for", "while", "do", "switch", "return", "case",
    "break", "continue", "goto", "default", "sizeof", "typeof", "alignof",
    "typedef", "struct", "union", "enum", "namespace", "class", "template",
    "new", "delete", "throw", "try", "catch", "operator", "using", "friend",
    "public", "private", "protected", "virtual", "override", "final",
    "explicit", "inline", "volatile", "extern", "register", "typename",
    "decltype", "static_assert", "constexpr", "noexcept", "nullptr",
})

# MPI / parallel-launcher stripping for smoke tests
_MPI_LAUNCHER_RE = re.compile(
    r"^\s*(?:mpirun|mpiexec|orterun|srun|jsrun|aprun|prun|flux\s+run)\b"
)
_MPI_NP_RE = re.compile(
    r"\s+(?:-np?|-n|--ntasks|--npernode|--ntasks-per-node|-N|--nodes)\s+\d+"
)
_MPI_MISC_FLAGS_RE = re.compile(
    r"\s+--(?:oversubscribe|allow-run-as-root|bind-to\s+\S+|map-by\s+\S+|"
    r"host\s+\S+|hostfile\s+\S+|rankfile\s+\S+)"
)


def _strip_mpi_launcher(cmd: str) -> tuple:
    """Remove MPI/parallel launcher prefix from a shell command string.

    Returns (clean_cmd, was_stripped).  The underlying binary is run as a
    single process with no MPI context.
    """
    if not _MPI_LAUNCHER_RE.search(cmd):
        return cmd, False
    clean = _MPI_LAUNCHER_RE.sub("", cmd)
    clean = _MPI_NP_RE.sub("", clean)
    clean = _MPI_MISC_FLAGS_RE.sub("", clean)
    return clean.strip(), True


# Matches any single-line dftracer macro injected by _annotate_c_source
_DFTRACER_MACRO_LINE_RE = re.compile(
    r"^\s*DFTRACER_(?:C|CPP)_(?:FUNCTION_(?:START|END)|FUNCTION_UPDATE_(?:STR|INT)|"
    r"CPP_FUNCTION_UPDATE|FUNCTION|INIT|FINI|METADATA)\s*\([^)]*\)\s*;\s*$",
    re.MULTILINE,
)

# GCC/Clang error line: "path/file.c:42:5: error: ..."
_COMPILER_ERROR_RE = re.compile(
    r"^([^\s:][^:]*\.(?:c|cpp|cxx|cc|h|hpp)):(\d+):\d+:\s+error:",
    re.MULTILINE,
)


def _strip_dftracer_c_macros(content: str) -> str:
    """Remove all dftracer macros injected by _annotate_c_source. Leaves other code intact."""
    content = content.replace(f"{_C_INCLUDE}\n", "")
    content = _DFTRACER_MACRO_LINE_RE.sub("", content)
    content = content.replace("\n/* TODO: add DFTRACER_FINI() before process exit */\n", "\n")
    content = content.replace("\n# TODO: call DFTRACER_FINI() before process exit\n", "\n")
    # Collapse any runs of blank lines left by macro removal
    content = re.sub(r"\n{3,}", "\n\n", content)
    return content


def _fix_dftracer_annotation_errors(ann: Path, compiler_stderr: str) -> List[str]:
    """
    Parse compiler output, find files where our injected dftracer macros caused
    errors, strip those macros from the affected files, and return the list of
    relative paths that were cleaned.  The cleaned files can be rebuilt immediately.
    The build system will compile them without dftracer instrumentation; Goose
    should then manually re-annotate the troublesome files.
    """
    if not compiler_stderr.strip():
        return []

    # Determine whether the errors are dftracer-related at all
    dftracer_related = (
        "DFTRACER" in compiler_stderr
        or "data_fn" in compiler_stderr
        or "initialize_region" in compiler_stderr
        or "finalize_region" in compiler_stderr
    )

    # Collect which source files have errors
    error_files: Dict[str, Set[int]] = {}
    for m in _COMPILER_ERROR_RE.finditer(compiler_stderr):
        error_files.setdefault(m.group(1), set()).add(int(m.group(2)))

    if not error_files and not dftracer_related:
        return []

    fixed: List[str] = []
    for raw_path, error_lines in error_files.items():
        # Resolve file path relative to annotated/ directory
        candidate: Optional[Path] = ann / raw_path
        if not candidate.exists():
            hits = list(ann.rglob(Path(raw_path).name))
            candidate = hits[0] if hits else None
        if not candidate or not candidate.exists():
            continue

        content = candidate.read_text(errors="ignore")
        lines = content.splitlines()

        # Only strip if the failing line is on or near one of our macros, OR if
        # the overall stderr mentions dftracer symbols (link/type errors)
        near_macro = any(
            1 <= ln <= len(lines) and (
                "DFTRACER" in lines[ln - 1]
                or "data_fn" in lines[ln - 1]
                or (ln > 1 and "DFTRACER" in lines[ln - 2])
                or (ln < len(lines) and "DFTRACER" in lines[ln])
            )
            for ln in error_lines
        )
        if not (near_macro or dftracer_related):
            continue

        new_content = _strip_dftracer_c_macros(content)
        if new_content != content:
            candidate.write_text(new_content)
            fixed.append(str(candidate.relative_to(ann)))

    return fixed


# Regex patterns for detecting trackable parameters in function signatures
_STR_PARAM_RE = re.compile(
    r"\b(?:const\s+)?char\s*\*+\s*(\w*(?:file|path|name|dir|mode|cmd|key|buf)\w*)\b",
    re.IGNORECASE,
)
_INT_PARAM_RE = re.compile(
    r"\b(?:size_t|off_t|ssize_t|uint64_t|int64_t|int|long)\s+(\w*(?:size|count|len|offset|fd|flags|bytes|num)\w*)\b",
    re.IGNORECASE,
)


def _metadata_update_calls(params: str, is_cpp: bool, indent: str = "  ") -> List[str]:
    """Generate DFTRACER_*_FUNCTION_UPDATE_* calls for trackable function parameters."""
    calls: List[str] = []
    seen: set = set()
    for m in _STR_PARAM_RE.finditer(params):
        name = m.group(1)
        if name and name not in seen:
            seen.add(name)
            if is_cpp:
                calls.append(f'{indent}DFTRACER_CPP_FUNCTION_UPDATE("{name}", {name});')
            else:
                calls.append(f'{indent}DFTRACER_C_FUNCTION_UPDATE_STR("{name}", {name});')
    for m in _INT_PARAM_RE.finditer(params):
        name = m.group(1)
        if name and name not in seen:
            seen.add(name)
            if not is_cpp:
                calls.append(f'{indent}DFTRACER_C_FUNCTION_UPDATE_INT("{name}", (int){name});')
    return calls


def _annotate_c_source(content: str, filepath: Path, is_entry: bool) -> str:
    """Inject dftracer C/C++ macros into source.  Idempotent."""
    if _C_INCLUDE in content:
        return content

    is_cpp = filepath.suffix.lower() in {".cpp", ".cxx", ".cc"}

    lines = content.splitlines(keepends=True)
    last_inc = max(
        (i for i, ln in enumerate(lines) if ln.strip().startswith("#include")),
        default=-1,
    )
    out: List[str] = []
    for i, ln in enumerate(lines):
        out.append(ln)
        if i == last_inc:
            out.append(f"{_C_INCLUDE}\n")

    result = "".join(out)

    # Step 1: inject DFTRACER_C_FUNCTION_START() / DFTRACER_CPP_FUNCTION() and
    # parameter metadata at the opening of every real function body.
    # Group 2 captures the function name so we can reject C/C++ keywords (if/else/for/…).
    _FN_HEADER_RE = re.compile(
        r"(\b\w[\w\s\*:<>]*\s+(\w+)\s*\(([^)]*)\)\s*(?:const\s*)?(?:override\s*)?(?:noexcept\s*)?\{)"
    )

    def _inject_fn_open(m: re.Match) -> str:
        header = m.group(1)
        fn_name = m.group(2)
        params = m.group(3)
        # Skip control-flow keywords that look like functions to the regex
        if fn_name in _C_KEYWORDS:
            return header
        if is_cpp:
            injected = f"{header}\n  DFTRACER_CPP_FUNCTION();"
        else:
            injected = f"{header}\n  DFTRACER_C_FUNCTION_START();"
        meta_calls = _metadata_update_calls(params, is_cpp)
        if meta_calls:
            injected += "\n" + "\n".join(meta_calls)
        return injected

    result = _FN_HEADER_RE.sub(_inject_fn_open, result)

    # Step 2 (entry file only): insert DFTRACER_C_INIT *before* DFTRACER_C_FUNCTION_START
    # in main() so the tracer is initialized before any region/metadata calls are made.
    if is_entry:
        init_call = (
            "DFTRACER_CPP_INIT(nullptr, nullptr, nullptr);"
            if is_cpp
            else "DFTRACER_C_INIT(nullptr, nullptr, nullptr);"
        )
        result = re.sub(
            r"(int\s+main\s*\([^)]*\)\s*\{)",
            r"\1\n  " + init_call,
            result,
            count=1,
        )

    # Step 3 (C only): inject FUNCTION_END before every return, FUNCTION_END at the
    # closing } of void/fallthrough functions, and FINI in the entry (main) function.
    if not is_cpp:
        result = _finalize_c_ends_and_fini(result, is_entry)

    return result


def _finalize_c_ends_and_fini(content: str, is_entry: bool) -> str:
    """
    Post-annotation pass for C source files.  Operates only within function bodies
    that already contain DFTRACER_C_FUNCTION_START() (so it's safe to re-run).

    For every such function:
      - Injects DFTRACER_C_FUNCTION_END() before every `return` statement.
      - Injects DFTRACER_C_FUNCTION_END() before the closing } when the function
        has no explicit return (void / fallthrough).

    Additionally, for the entry file's main() function (detected by DFTRACER_C_INIT):
      - Injects DFTRACER_C_FINI() immediately before DFTRACER_C_FUNCTION_END() at
        every exit point (return statements and process-exit calls).
      - Injects DFTRACER_C_FINI() before process-exit calls (exit/abort/_exit/…)
        throughout the file, since they terminate without returning to main.
    """
    END = "DFTRACER_C_FUNCTION_END()"
    FINI = "DFTRACER_C_FINI()"
    _EXIT_RE = re.compile(r"\b(?:exit|_exit|_Exit|quick_exit|abort)\s*\(")

    lines = content.splitlines(keepends=True)
    insertions: List[Tuple[int, str]] = []  # (line_index, text to insert before that line)

    # Find each annotated function by its DFTRACER_C_FUNCTION_START() line
    for si, ln in enumerate(lines):
        if "DFTRACER_C_FUNCTION_START()" not in ln:
            continue

        fn_indent = len(ln) - len(ln.lstrip())
        ind = " " * fn_indent  # same indent as the START call

        # Determine if this is main (DFTRACER_C_INIT is within ±4 lines of START)
        search = range(max(0, si - 4), min(len(lines), si + 5))
        is_main = is_entry and any("DFTRACER_C_INIT" in lines[k] for k in search)

        # Brace-count from just after START to find the function's closing }
        depth = 1
        j = si + 1
        while j < len(lines) and depth > 0:
            for ch in lines[j]:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        break
            if depth > 0:
                j += 1
        fn_end = j  # index of the line that contains the closing }

        # Scan lines inside this function
        for k in range(si + 1, fn_end):
            s = lines[k].strip()
            if not s or s.startswith("//") or s.startswith("*"):
                continue
            k_ind = " " * (len(lines[k]) - len(lines[k].lstrip()))
            prev = lines[k - 1].strip() if k > 0 else ""

            # Before return statements (skip if END is already the previous line)
            if re.match(r"return\b", s) and END not in prev:
                # list.insert(k, x) pushes the previous occupant of k to k+1.
                # Appending in stable-sort order means the first-appended runs first.
                # To get END → FINI → return we need the LAST insert to land at k
                # (i.e. END), so append FINI first, then END.
                if is_main and FINI not in prev:
                    insertions.append((k, f"{k_ind}{FINI};\n"))
                insertions.append((k, f"{k_ind}{END};\n"))

            # Before process-exit calls anywhere — process terminates here so FINI is needed
            elif _EXIT_RE.search(s) and FINI not in prev:
                insertions.append((k, f"{k_ind}{FINI};\n"))

        # Handle void / fallthrough functions: add END before the closing }
        # if the last real statement is not already a return or END
        m = fn_end - 1
        while m > si and not lines[m].strip():
            m -= 1
        last = lines[m].strip() if m > si else ""
        if last and END not in last and not re.match(r"return\b", last):
            close_ind = ind  # same indent as START
            if is_main and FINI not in last:
                insertions.append((fn_end, f"{close_ind}{FINI};\n"))
            insertions.append((fn_end, f"{close_ind}{END};\n"))

    # Apply insertions in reverse order so earlier indices stay valid
    result = list(lines)
    for idx, text in sorted(insertions, key=lambda x: x[0], reverse=True):
        result.insert(idx, text)

    return "".join(result)


# ---------------------------------------------------------------------------
# Annotation helpers — Python
# ---------------------------------------------------------------------------

_PY_IMPORT = "from dftracer.logger import dft_fn, DFTRACER_INIT, DFTRACER_FINI"


def _annotate_python_source(content: str, is_entry: bool) -> str:
    """Inject dftracer Python decorators.  Idempotent."""
    if "dftracer" in content:
        return content

    lines = content.splitlines(keepends=True)
    last_imp = max(
        (i for i, ln in enumerate(lines)
         if ln.strip().startswith(("import ", "from "))),
        default=-1,
    )
    out: List[str] = []
    for i, ln in enumerate(lines):
        out.append(ln)
        if i == last_imp:
            out.append(f"\n{_PY_IMPORT}\n")
            if is_entry:
                out.append(
                    "DFTRACER_INIT(log_file=None, data_dirs=None, process_id=-1)\n\n"
                )
    result = "".join(out)

    # Decorate top-level function definitions
    result = re.sub(r"^(def\s)", r"@dft_fn\n\1", result, flags=re.MULTILINE)

    if is_entry:
        result += "\n# TODO: call DFTRACER_FINI() before process exit\n"
    return result
