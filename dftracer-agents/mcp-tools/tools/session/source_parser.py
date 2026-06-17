"""Source-code parser for extracting function definitions with exact line numbers.

Primary method: clang -ast-dump=json (C/C++) or Python's ast module (Python).
Fallback: ctags, then regex-based brace counting.

Returned dicts per function::

    {
        "name":              str,   # function name
        "start_line":        int,   # return-type / signature first line
        "open_brace_line":   int,   # line of opening '{'
        "body_first_line":   int,   # open_brace_line + 1 (insert START here)
        "close_brace_line":  int,   # line of closing '}'
        "exit_lines":        [{"line": int, "type": str}],  # return/exit/abort
        "is_entry_point":    bool,  # True if name == "main" or __main__
        "source":            str,   # "clang" | "ctags" | "regex" | "ast"
    }
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract_functions(filepath: str | Path) -> list[dict]:
    """Return a list of function-info dicts for *filepath*."""
    path = Path(filepath)
    suffix = path.suffix.lower()
    if suffix == ".py":
        return _extract_python(path)
    if suffix in (".c", ".cpp", ".cxx", ".cc", ".h", ".hpp"):
        return _extract_c_cpp(path)
    return []


# ---------------------------------------------------------------------------
# C / C++ extraction
# ---------------------------------------------------------------------------

def _extract_c_cpp(path: Path) -> list[dict]:
    result = _try_clang(path)
    if result is not None:
        return result
    result = _try_ctags(path)
    if result is not None:
        return result
    return _regex_parse_c(path)


# ── clang -ast-dump=json ────────────────────────────────────────────────────

def _try_clang(path: Path) -> Optional[list[dict]]:
    lang = "c" if path.suffix.lower() == ".c" else "c++"
    try:
        proc = subprocess.run(
            ["clang", f"-ast-dump=json", "-fsyntax-only", "-w",
             f"-x{lang}", str(path)],
            capture_output=True, text=True, timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    stdout = proc.stdout
    if not stdout:
        return None

    # Clang sometimes emits diagnostics before the JSON blob
    json_start = stdout.find("{")
    if json_start == -1:
        return None

    try:
        ast_root = json.loads(stdout[json_start:])
    except json.JSONDecodeError:
        return None

    target = str(path)
    functions: list[dict] = []
    _visit_tu(ast_root, functions, target)
    for fn in functions:
        fn["source"] = "clang"
    return functions


def _visit_tu(ast_root: dict, functions: list[dict], target: str) -> None:
    """Walk TranslationUnitDecl children tracking current file context."""
    current_file: str = ""
    for node in ast_root.get("inner", []):
        # Update file context from loc if present
        loc = node.get("loc", {})
        if "file" in loc:
            current_file = loc["file"]

        # Skip nodes from other files (system headers etc.)
        if current_file and target not in current_file:
            continue

        kind = node.get("kind", "")
        if kind in ("FunctionDecl", "CXXMethodDecl",
                    "CXXConstructorDecl", "CXXDestructorDecl"):
            info = _extract_func_node(node)
            if info:
                functions.append(info)
        elif kind == "CXXRecordDecl":
            # Recurse into class to find method definitions
            _visit_class(node, functions, target, current_file)
        elif kind == "NamespaceDecl":
            _visit_namespace(node, functions, target, current_file)


def _visit_class(node: dict, functions: list[dict],
                 target: str, current_file: str) -> None:
    for child in node.get("inner", []):
        loc = child.get("loc", {})
        child_file = loc.get("file", current_file)
        if child_file and target not in child_file:
            continue
        kind = child.get("kind", "")
        if kind in ("CXXMethodDecl", "CXXConstructorDecl", "CXXDestructorDecl",
                    "FunctionDecl"):
            info = _extract_func_node(child)
            if info:
                functions.append(info)
        elif kind == "CXXRecordDecl":
            _visit_class(child, functions, target, child_file or current_file)


def _visit_namespace(node: dict, functions: list[dict],
                     target: str, current_file: str) -> None:
    for child in node.get("inner", []):
        loc = child.get("loc", {})
        child_file = loc.get("file", current_file)
        if child_file and target not in child_file:
            continue
        kind = child.get("kind", "")
        if kind in ("FunctionDecl", "CXXMethodDecl",
                    "CXXConstructorDecl", "CXXDestructorDecl"):
            info = _extract_func_node(child)
            if info:
                functions.append(info)
        elif kind in ("CXXRecordDecl", "NamespaceDecl"):
            _visit_namespace(child, functions, target, child_file or current_file)


def _extract_func_node(node: dict) -> Optional[dict]:
    """Build a function-info dict from a FunctionDecl AST node."""
    inner = node.get("inner", [])
    body = next((c for c in inner if c.get("kind") == "CompoundStmt"), None)
    if body is None:
        return None  # declaration only, not definition

    rng = node.get("range", {})
    begin_line = rng.get("begin", {}).get("line", 0)
    end_line   = rng.get("end",   {}).get("line", 0)

    body_rng = body.get("range", {})
    open_brace  = body_rng.get("begin", {}).get("line", begin_line)
    close_brace = body_rng.get("end",   {}).get("line", end_line)

    exits: list[dict] = []
    _collect_exits(body, exits)
    exits.sort(key=lambda x: x["line"])

    name = node.get("name", "")
    return {
        "name":            name,
        "start_line":      begin_line,
        "open_brace_line": open_brace,
        "body_first_line": open_brace + 1,
        "close_brace_line": close_brace,
        "exit_lines":      exits,
        "is_entry_point":  name == "main",
    }


def _collect_exits(node: dict, exits: list[dict]) -> None:
    """Recursively find ReturnStmt / exit() / abort() in a CompoundStmt."""
    kind = node.get("kind", "")

    if kind == "ReturnStmt":
        loc = node.get("loc") or node.get("range", {}).get("begin", {}) or {}
        line = loc.get("line")
        if line:
            exits.append({"line": line, "type": "return"})
        return  # don't recurse into return value (may contain lambdas)

    if kind == "CallExpr":
        callee = _callee_name(node)
        if callee in ("exit", "_exit", "abort", "quick_exit", "_Exit"):
            loc = node.get("loc") or node.get("range", {}).get("begin", {}) or {}
            line = loc.get("line")
            if line:
                exits.append({"line": line, "type": callee})

    # Don't recurse into nested function/lambda bodies
    if kind in ("LambdaExpr", "FunctionDecl", "CXXMethodDecl",
                "CXXConstructorDecl", "CXXDestructorDecl"):
        return

    for child in node.get("inner", []):
        _collect_exits(child, exits)


def _callee_name(call_expr: dict) -> str:
    for child in call_expr.get("inner", []):
        if child.get("kind") in ("ImplicitCastExpr", "DeclRefExpr"):
            name = child.get("referencedDecl", {}).get("name", "")
            if name:
                return name
            for inner in child.get("inner", []):
                n = inner.get("referencedDecl", {}).get("name", "")
                if n:
                    return n
    return ""


# ── ctags fallback ─────────────────────────────────────────────────────────

def _try_ctags(path: Path) -> Optional[list[dict]]:
    try:
        proc = subprocess.run(
            ["ctags", "--fields=+neK", "--output-format=json",
             "--kinds-C=f", "--kinds-C++=f", str(path)],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    if proc.returncode != 0 or not proc.stdout:
        return None

    lines = path.read_text(errors="replace").splitlines()
    functions: list[dict] = []
    for raw in proc.stdout.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            tag = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if tag.get("kind") not in ("function", "prototype"):
            continue
        if tag.get("kind") == "prototype":
            continue

        start = tag.get("line", 0)
        end   = tag.get("end", start + 1)
        name  = tag.get("name", "")

        # Find opening brace line
        open_brace = _find_open_brace(lines, start - 1, end)
        if open_brace is None:
            continue

        # Find return statements within function body
        exits = _grep_returns_in_range(lines, open_brace, end - 1)

        functions.append({
            "name":            name,
            "start_line":      start,
            "open_brace_line": open_brace + 1,  # 1-based
            "body_first_line": open_brace + 2,  # line after {
            "close_brace_line": end,
            "exit_lines":      exits,
            "is_entry_point":  name == "main",
            "source":          "ctags",
        })

    return functions if functions else None


def _find_open_brace(lines: list[str], start_0: int, end_0: int) -> Optional[int]:
    """Return 0-based line index of the opening '{' for a function starting at start_0."""
    depth = 0
    for i in range(start_0, min(end_0 + 1, len(lines))):
        for ch in lines[i]:
            if ch == "{":
                if depth == 0:
                    return i
                depth += 1
            elif ch == "}":
                depth -= 1
    return None


def _grep_returns_in_range(lines: list[str], open_0: int, close_0: int) -> list[dict]:
    """Find return / exit() / abort() lines in [open_0, close_0] (0-based)."""
    exits: list[dict] = []
    depth = 0  # brace depth within this function
    _TERM = re.compile(r"\b(return|exit|_exit|abort|quick_exit)\b")
    _IN_STR = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"')

    for i in range(open_0, min(close_0 + 1, len(lines))):
        stripped = _IN_STR.sub("", lines[i])  # remove string literals
        stripped = re.sub(r"//.*", "", stripped)  # remove // comments

        for ch in stripped:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1

        if depth <= 1:  # inside the outermost body only
            m = _TERM.search(stripped)
            if m:
                kw = m.group(1)
                if kw == "return":
                    exits.append({"line": i + 1, "type": "return"})
                elif kw in ("exit", "_exit", "abort", "quick_exit"):
                    if "(" in stripped[m.end():]:
                        exits.append({"line": i + 1, "type": kw})
    return exits


# ── regex fallback ──────────────────────────────────────────────────────────

_FUNC_DEF = re.compile(
    r"^(?![\s#/])"           # not a preprocessor/comment line
    r"(?:(?:static|inline|extern|__attribute__\S+)\s+)*"
    r"(?:\w[\w\s\*]+?)\s+"   # return type
    r"(\w+)\s*\(",            # function name
)


def _regex_parse_c(path: Path) -> list[dict]:
    lines = path.read_text(errors="replace").splitlines()
    functions: list[dict] = []
    i = 0
    while i < len(lines):
        m = _FUNC_DEF.match(lines[i])
        if m:
            # Look ahead for the opening '{'
            open_idx = None
            for j in range(i, min(i + 10, len(lines))):
                if "{" in lines[j] and ";" not in lines[j][:lines[j].find("{")]:
                    open_idx = j
                    break
            if open_idx is not None:
                close_idx = _find_close_brace(lines, open_idx)
                if close_idx is not None:
                    name = m.group(1)
                    exits = _grep_returns_in_range(lines, open_idx, close_idx)
                    functions.append({
                        "name":            name,
                        "start_line":      i + 1,
                        "open_brace_line": open_idx + 1,
                        "body_first_line": open_idx + 2,
                        "close_brace_line": close_idx + 1,
                        "exit_lines":      exits,
                        "is_entry_point":  name == "main",
                        "source":          "regex",
                    })
                    i = close_idx + 1
                    continue
        i += 1
    return functions


def _find_close_brace(lines: list[str], open_0: int) -> Optional[int]:
    depth = 0
    for i in range(open_0, len(lines)):
        for ch in lines[i]:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
    return None


# ---------------------------------------------------------------------------
# Python extraction via ast module
# ---------------------------------------------------------------------------

def add_braces_c(path: Path) -> dict:
    """Add ``{`` / ``}`` around braceless ``if`` / ``for`` / ``while`` / ``else`` bodies.

    Uses clang ``-ast-dump=json`` to find ``IfStmt``, ``ForStmt``, ``WhileStmt``,
    and ``DoStmt`` nodes whose body is **not** a ``CompoundStmt`` (braceless single
    statement), then rewrites the file with braces inserted.  Falls back to a
    regex-based line scanner when clang is unavailable.

    This is run **before** annotation so that every ``if (...) return;`` pattern
    already has braces, making it safe to insert ``DFTRACER_C_FUNCTION_END()``
    on the line before the ``return`` without creating a syntax error.

    Args:
        path: Absolute path to a C or C++ source file inside ``annotated/``.

    Returns:
        Dict with keys:
            * ``modified`` (bool) — ``True`` if the file was rewritten.
            * ``insertions`` (int) — number of brace pairs added.
            * ``method`` (str)    — ``"clang"`` or ``"regex"``.
            * ``error`` (str)     — set only when the operation failed.
    """
    lang = "c" if path.suffix.lower() == ".c" else "c++"

    # Try clang first
    result = _add_braces_via_clang(path, lang)
    if result is not None:
        return result

    # Fallback: regex scanner
    return _add_braces_regex(path)


def _add_braces_via_clang(path: Path, lang: str) -> Optional[dict]:
    """Collect braceless control-flow bodies via clang AST then rewrite the file."""
    try:
        proc = subprocess.run(
            ["clang", "-ast-dump=json", "-fsyntax-only", "-w", f"-x{lang}", str(path)],
            capture_output=True, text=True, timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    stdout = proc.stdout
    if not stdout:
        return None

    json_start = stdout.find("{")
    if json_start == -1:
        return None
    try:
        ast_root = json.loads(stdout[json_start:])
    except json.JSONDecodeError:
        return None

    target = str(path)
    # Collect (body_start_line, body_end_line) for each braceless body
    # These are 1-based lines from the clang AST.
    braceless: list[tuple[int, int]] = []
    _collect_braceless(ast_root, braceless, target, current_file="")

    if not braceless:
        return {"modified": False, "insertions": 0, "method": "clang"}

    lines = path.read_text(errors="replace").splitlines(keepends=True)
    new_lines = _insert_braces(lines, braceless)
    path.write_text("".join(new_lines))
    return {"modified": True, "insertions": len(braceless), "method": "clang"}


_CTRL_KINDS = frozenset(
    ("IfStmt", "ForStmt", "WhileStmt", "DoStmt", "ElseStmt")
)


def _collect_braceless(
    node: dict,
    result: list[tuple[int, int]],
    target: str,
    current_file: str,
) -> None:
    kind = node.get("kind", "")

    # Track file context
    loc = node.get("loc", {})
    if "file" in loc:
        current_file = loc["file"]

    # Skip system headers
    if current_file and target not in current_file and current_file != "":
        if kind != "TranslationUnitDecl":
            return

    if kind in ("IfStmt", "ForStmt", "WhileStmt", "DoStmt"):
        inner = node.get("inner", [])
        for child in inner:
            ck = child.get("kind", "")
            # The body of an if/for/while is the last child (or second-to-last
            # for IfStmt which may have a condition var child).
            # A braceless body is any non-CompoundStmt statement child.
            if ck in (
                "CompoundStmt", "IfStmt", "ForStmt", "WhileStmt", "DoStmt",
                # skip non-statement children
                "DeclRefExpr", "BinaryOperator", "UnaryOperator",
                "IntegerLiteral", "ImplicitCastExpr", "ParenExpr",
            ):
                continue
            # It's a statement but not a block → braceless
            rng = child.get("range", {})
            start = rng.get("begin", {}).get("line", 0)
            end   = rng.get("end",   {}).get("line", 0)
            if start and end:
                result.append((start, end))

    for child in node.get("inner", []):
        _collect_braceless(child, result, target, current_file)


def _insert_braces(
    lines: list[str],
    braceless: list[tuple[int, int]],
) -> list[str]:
    """Insert ``{`` and ``}`` around each (start, end) pair (1-based lines)."""
    # Sort descending so insertions don't shift earlier line numbers
    pairs = sorted(set(braceless), key=lambda p: (-p[0], -p[1]))

    for start, end in pairs:
        si = start - 1  # 0-based
        ei = end   - 1

        # Determine indentation from the body line
        body_line = lines[si]
        indent = len(body_line) - len(body_line.lstrip())
        ind = " " * indent

        # Insert closing brace after body end line
        close_nl = lines[ei].rstrip("\n").rstrip("\r")
        # If the end line already ends with { or }, skip (already braced)
        stripped = close_nl.strip()
        if stripped.endswith("{") or stripped.endswith("}"):
            continue

        lines.insert(ei + 1, ind + "}\n")
        lines.insert(si, ind + "{\n")

    return lines


def _add_braces_regex(path: Path) -> dict:
    """Regex-based braceless body fixer (clang fallback).

    Scans line-by-line for the patterns:
      ``if (...)``, ``else``, ``for (...)``, ``while (...)``
    followed by a single statement (no ``{`` at end of the control line and
    the next non-empty line does not start with ``{``).
    """
    text = path.read_text(errors="replace")
    lines = text.splitlines(keepends=True)
    _CTRL = re.compile(
        r"^(\s*)"
        r"(?:(?:if|else\s+if)\s*\(.*\)\s*"
        r"|else\s*"
        r"|for\s*\(.*\)\s*"
        r"|while\s*\(.*\)\s*"
        r")\s*$"
    )
    insertions = 0
    i = 0
    new_lines: list[str] = []

    while i < len(lines):
        line = lines[i]
        m = _CTRL.match(line.rstrip("\n").rstrip("\r"))
        if m:
            indent = m.group(1)
            # Find next non-blank line
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and not lines[j].lstrip().startswith("{"):
                # Single-statement body — wrap with braces
                new_lines.append(line)
                new_lines.append(indent + "{\n")
                i += 1
                # Collect the body statement (one logical line, may end with `;`)
                while i < len(lines):
                    new_lines.append(lines[i])
                    if lines[i].rstrip("\n").rstrip().endswith(";"):
                        i += 1
                        break
                    i += 1
                new_lines.append(indent + "}\n")
                insertions += 1
                continue
        new_lines.append(line)
        i += 1

    if insertions:
        path.write_text("".join(new_lines))
    return {"modified": bool(insertions), "insertions": insertions, "method": "regex"}


def _extract_python(path: Path) -> list[dict]:
    try:
        source = path.read_text(errors="replace")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    lines = source.splitlines()
    functions: list[dict] = []
    _visit_py_node(tree, functions, lines, parent_class=None)
    for fn in functions:
        fn["source"] = "ast"
    return functions


def _visit_py_node(node: ast.AST, functions: list[dict],
                   lines: list[str], parent_class: Optional[str]) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            exits = _py_exits(child)
            name = child.name
            functions.append({
                "name":            name,
                "start_line":      child.lineno,
                "open_brace_line": child.lineno,  # Python: def line
                "body_first_line": child.body[0].lineno if child.body else child.lineno + 1,
                "close_brace_line": child.end_lineno or child.lineno,
                "exit_lines":      exits,
                "is_entry_point":  name == "main" or name == "__main__",
                "parent_class":    parent_class,
            })
            # Recurse into nested functions (but use the class name as parent)
            _visit_py_node(child, functions, lines, parent_class)
        elif isinstance(child, ast.ClassDef):
            _visit_py_node(child, functions, lines, parent_class=child.name)
        else:
            _visit_py_node(child, functions, lines, parent_class)


def _py_exits(func_node: ast.FunctionDef) -> list[dict]:
    exits: list[dict] = []
    for node in ast.walk(func_node):
        if isinstance(node, ast.Return):
            exits.append({"line": node.lineno, "type": "return"})
        elif isinstance(node, ast.Call):
            name = ""
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name in ("exit", "sys.exit", "_exit", "abort"):
                exits.append({"line": node.lineno, "type": name})
    exits.sort(key=lambda x: x["line"])
    return exits
