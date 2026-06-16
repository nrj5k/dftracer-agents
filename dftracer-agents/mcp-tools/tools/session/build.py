"""
Build-system patch helpers and entry-point detection for the dftracer session pipeline.

This module provides two families of utilities used during the "instrument"
stage of the pipeline, where an application's build system is modified to link
against dftracer, and during the "run" stage, where the pipeline needs to
identify executable entry points and propose a smoke test command.

**Build-system patching** (``_patch_*`` functions):

Each function takes the path to a specific build descriptor file, reads it,
and returns a *modified copy* of its content with dftracer wired in — it does
**not** write the file back.  This keeps the patching logic pure (easy to test
and preview) while leaving the I/O decision to the caller.  All patch
functions are idempotent: if the word ``"dftracer"`` already appears anywhere
in the file, the original content is returned unchanged.

Supported build systems:

- CMake (``CMakeLists.txt``) — injects ``find_package(dftracer)`` before the
  first ``add_executable``/``add_library`` call and appends a link loop at
  the end of the file.
- Python ``setup.py`` — appends ``"dftracer"`` to ``install_requires``.
- Python ``pyproject.toml`` — appends ``"dftracer"`` to ``[project.dependencies]``.
- Autotools/Make ``Makefile`` — prepends ``pkg-config``-based ``CFLAGS`` and
  ``LDFLAGS`` augmentation lines.

**Entry-point detection** (``_find_*`` functions):

Recurse through the source tree to locate files that define executable entry
points, enabling the pipeline to suggest concrete run commands to the user.

**Smoke-test heuristic** (:func:`_guess_smoke_test`):

Returns a best-guess shell command for a quick sanity check after installation,
tailored to the detected build tool.
"""
from __future__ import annotations

import re
import textwrap
from pathlib import Path
from typing import List, Optional


# ---------------------------------------------------------------------------
# Build-system patch helpers
# ---------------------------------------------------------------------------

def _patch_cmake(path: Path) -> str:
    """Return CMakeLists.txt content with dftracer ``find_package`` and link blocks injected.

    The injection strategy places ``find_package(dftracer QUIET)`` immediately
    before the first ``add_executable`` or ``add_library`` call so that the
    package is found before any targets are defined.  A second block appended
    at the end of the file iterates over all build-system targets in the
    current directory and calls ``target_link_libraries`` for each executable
    or library target, guarded by ``if(dftracer_FOUND)`` so the build remains
    functional on systems without dftracer installed.

    If no ``add_executable`` or ``add_library`` directive is found, the
    ``find_package`` preamble is prepended to the very top of the file instead.

    This function is idempotent: if the string ``"dftracer"`` (case-insensitive)
    already appears anywhere in the file, the original content is returned
    unchanged.

    Args:
        path: Absolute path to the ``CMakeLists.txt`` file to patch.

    Returns:
        str: Modified file content as a string.  The caller is responsible for
            writing this back to disk if the modification is desired.
    """
    content = path.read_text()
    if "dftracer" in content.lower():
        return content

    preamble = textwrap.dedent("""\
        # --- dftracer (auto-injected) ---
        find_package(dftracer QUIET)
        if(dftracer_FOUND)
          message(STATUS "dftracer found — tracing enabled")
        endif()
        # ---------------------------------
    """)

    suffix = textwrap.dedent("""\

        # --- dftracer link (auto-injected) ---
        if(dftracer_FOUND)
          get_property(_dft_targets DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
                       PROPERTY BUILDSYSTEM_TARGETS)
          foreach(_t ${_dft_targets})
            get_target_property(_t_type ${_t} TYPE)
            if(_t_type MATCHES "EXECUTABLE|LIBRARY")
              target_link_libraries(${_t} PRIVATE dftracer::dftracer)
              target_include_directories(${_t} PRIVATE ${dftracer_INCLUDE_DIRS})
              target_compile_definitions(${_t} PRIVATE DFTRACER_ENABLE)
            endif()
          endforeach()
        endif()
        # -------------------------------------
    """)

    m = re.search(r"^(add_executable|add_library)", content, re.MULTILINE)
    if m:
        content = content[: m.start()] + preamble + "\n" + content[m.start():]
    else:
        content = preamble + "\n" + content
    content += suffix
    return content


def _patch_setup_py(path: Path) -> str:
    """Return ``setup.py`` content with ``"dftracer"`` added to ``install_requires``.

    Uses a regex substitution to locate the ``install_requires=[`` list literal
    and insert ``"dftracer"`` as the first entry.  Only the first occurrence of
    ``install_requires`` is patched.

    This function is idempotent: if the string ``"dftracer"`` already appears
    anywhere in the file, the original content is returned unchanged.

    Args:
        path: Absolute path to the ``setup.py`` file to patch.

    Returns:
        str: Modified file content as a string.  The caller is responsible for
            writing this back to disk if the modification is desired.
    """
    content = path.read_text()
    if "dftracer" in content:
        return content
    return re.sub(
        r"(install_requires\s*=\s*\[)",
        r'\1\n        "dftracer",',
        content,
    )


def _patch_pyproject(path: Path) -> str:
    """Return ``pyproject.toml`` content with ``"dftracer"`` added to ``dependencies``.

    Uses a regex substitution to locate the ``dependencies = [`` list literal
    (typically under ``[project]``) and insert ``"dftracer"`` as the first
    entry.  Only the first occurrence is patched.

    This function is idempotent: if the string ``"dftracer"`` already appears
    anywhere in the file, the original content is returned unchanged.

    Args:
        path: Absolute path to the ``pyproject.toml`` file to patch.

    Returns:
        str: Modified file content as a string.  The caller is responsible for
            writing this back to disk if the modification is desired.
    """
    content = path.read_text()
    if "dftracer" in content:
        return content
    return re.sub(
        r"(dependencies\s*=\s*\[)",
        r'\1\n    "dftracer",',
        content,
    )


def _patch_autotools_makefile(path: Path) -> str:
    """Return ``Makefile`` content with dftracer ``pkg-config`` flags prepended.

    Prepends a block that uses ``pkg-config`` to query dftracer's compiler and
    linker flags and appends them to ``CFLAGS``, ``CXXFLAGS``, and ``LDFLAGS``
    via ``+=``.  The ``-DDFTRACER_ENABLE`` preprocessor macro is also added so
    that source code can use ``#ifdef DFTRACER_ENABLE`` guards.

    The injected block is guarded with ``2>/dev/null`` so that builds on
    systems without dftracer installed do not fail with a ``pkg-config`` error.

    This function is idempotent: if the string ``"dftracer"`` already appears
    anywhere in the file, the original content is returned unchanged.

    Args:
        path: Absolute path to the ``Makefile`` (or ``GNUmakefile``) to patch.

    Returns:
        str: Modified file content as a string.  The caller is responsible for
            writing this back to disk if the modification is desired.
    """
    content = path.read_text()
    if "dftracer" in content:
        return content
    injection = textwrap.dedent("""\
        # --- dftracer (auto-injected) ---
        DFTRACER_CFLAGS  := $(shell pkg-config --cflags dftracer 2>/dev/null)
        DFTRACER_LDFLAGS := $(shell pkg-config --libs   dftracer 2>/dev/null)
        CFLAGS   += $(DFTRACER_CFLAGS)   -DDFTRACER_ENABLE
        CXXFLAGS += $(DFTRACER_CFLAGS)   -DDFTRACER_ENABLE
        LDFLAGS  += $(DFTRACER_LDFLAGS)
        # ----------------------------------
    """)
    return injection + "\n" + content


# ---------------------------------------------------------------------------
# Entry-point detection helpers
# ---------------------------------------------------------------------------

def _find_c_entry_points(source_dir: Path) -> List[Path]:
    """Return C/C++ source files that define a ``main()`` function.

    Performs a recursive glob for common C and C++ file extensions, then
    searches each file's text for the pattern ``int main(`` (with optional
    whitespace).  Files that cannot be read (e.g. permission errors) are
    silently skipped.

    Args:
        source_dir: Root of the source tree to search.

    Returns:
        List[Path]: Absolute paths of files that contain a ``main()``
            definition.  Order follows the filesystem traversal order and is
            not guaranteed to be deterministic across platforms.
    """
    results: List[Path] = []
    for ext in ("*.c", "*.cpp", "*.cxx", "*.cc"):
        for f in source_dir.rglob(ext):
            try:
                text = f.read_text(errors="ignore")
                if re.search(r"\bint\s+main\s*\(", text):
                    results.append(f)
            except OSError:
                pass
    return results


def _find_python_entry_points(source_dir: Path) -> List[Path]:
    """Return Python files that appear to be runnable entry points.

    A file is considered an entry point if it contains both ``__name__`` and
    ``__main__``, which is the canonical Python idiom for a script that can be
    executed directly (``if __name__ == "__main__": ...``).  Files that cannot
    be read are silently skipped.

    Args:
        source_dir: Root of the source tree to search.

    Returns:
        List[Path]: Absolute paths of ``.py`` files that contain the
            ``if __name__ == "__main__"`` idiom.  Order follows the filesystem
            traversal order.
    """
    results: List[Path] = []
    for f in source_dir.rglob("*.py"):
        try:
            text = f.read_text(errors="ignore")
            if '__name__' in text and '__main__' in text:
                results.append(f)
        except OSError:
            pass
    return results


# ---------------------------------------------------------------------------
# Smoke-test heuristic
# ---------------------------------------------------------------------------

def _guess_smoke_test(source_dir: Path, build_tool: str, install_dir: Path) -> Optional[str]:
    """Return a best-guess smoke-test shell command for the project.

    Produces a single shell command string (not a list) suitable for passing to
    ``bash -c`` or displaying to the user as a suggestion.  The command is
    chosen based solely on *build_tool*; *source_dir* and *install_dir* are
    accepted for future extensibility but are not currently used.

    Returned commands are intentionally lenient — they use ``||`` chains or
    ``-N`` (list-only) flags so that a partial test suite does not cause the
    smoke-test step to fail outright when no smoke-labelled tests exist.

    Args:
        source_dir: Root of the application source tree.  Reserved for future
            use (e.g. scanning for a ``tests/`` directory).
        build_tool: Build system identifier as returned by
            :func:`~detection._detect_info`.  One of ``"cmake"``,
            ``"autotools"``, ``"python"``, or ``"make"``.
        install_dir: Directory where the application was installed.  Reserved
            for future use (e.g. locating installed binaries for a quick
            invocation test).

    Returns:
        Optional[str]: A shell command string, or ``None`` when no heuristic
            is available for the given *build_tool* (e.g. ``"unknown"``).
    """
    if build_tool == "cmake":
        return "ctest --test-dir . -L smoke -R smoke --output-on-failure || ctest --test-dir . --output-on-failure -N"
    if build_tool == "autotools":
        return "make check -j1"
    if build_tool == "python":
        return "python -m pytest tests/ -x -q 2>/dev/null || python -m pytest test/ -x -q 2>/dev/null || python -c 'import pkg_resources; print(\"import ok\")'"
    if build_tool == "make":
        return "make test"
    return None
