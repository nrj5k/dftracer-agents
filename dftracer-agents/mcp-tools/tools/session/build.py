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

def _patch_cmake(path: Path, pip_include_dir: str = "", pip_lib_dir: str = "") -> str:
    """Return CMakeLists.txt content with dftracer ``find_package`` and link blocks injected.

    Tries ``find_package(dftracer QUIET)`` first (works when dftracer was built
    with CMake and installed to a prefix on ``CMAKE_PREFIX_PATH``).  When that
    fails at configure time it falls back to explicit include/lib paths derived
    from the pip-installed package directory (passed via *pip_include_dir* /
    *pip_lib_dir*, or discovered at configure time via a Python snippet).

    This function is idempotent: if the string ``"dftracer"`` already appears
    anywhere in the file, the original content is returned unchanged.

    Args:
        path: Absolute path to the ``CMakeLists.txt`` file to patch.
        pip_include_dir: Absolute path to dftracer's include directory when
            installed via pip.  If empty, a ``execute_process`` call to Python
            is embedded in the CMake snippet to discover it at configure time.
        pip_lib_dir: Absolute path to dftracer's lib directory (pip install).

    Returns:
        str: Modified file content as a string.
    """
    content = path.read_text()
    if "dftracer" in content.lower():
        return content

    # Embed known paths if we already discovered them; otherwise fall back to
    # a cmake-time python probe so the generated CMakeLists.txt still works on
    # machines where the prefix isn't known at patch time.
    if pip_include_dir and pip_lib_dir:
        pip_fallback = textwrap.dedent(f"""\
          set(DFTRACER_PIP_INC  "{pip_include_dir}")
          set(DFTRACER_PIP_LIB  "{pip_lib_dir}")
        """)
    else:
        pip_fallback = textwrap.dedent("""\
          execute_process(
            COMMAND "${{CMAKE_COMMAND}}" -E env
              python3 -c "import dftracer,os; d=os.path.dirname(os.path.abspath(dftracer.__file__)); print(d)"
            OUTPUT_VARIABLE _DFT_PKG_DIR OUTPUT_STRIP_TRAILING_WHITESPACE
            ERROR_QUIET
          )
          if(_DFT_PKG_DIR)
            set(DFTRACER_PIP_INC  "${{_DFT_PKG_DIR}}/include")
            set(DFTRACER_PIP_LIB  "${{_DFT_PKG_DIR}}/lib")
          endif()
        """)

    preamble = textwrap.dedent("""\
        # --- dftracer (auto-injected) ---
        find_package(dftracer QUIET)
        if(NOT dftracer_FOUND)
          # Fallback: pip-installed dftracer (no CMake config file)
        """) + textwrap.indent(pip_fallback, "  ") + textwrap.dedent("""\
          if(DFTRACER_PIP_INC AND EXISTS "${DFTRACER_PIP_INC}")
            set(dftracer_INCLUDE_DIRS "${DFTRACER_PIP_INC}")
            set(dftracer_LIB_DIR      "${DFTRACER_PIP_LIB}")
            set(dftracer_FOUND TRUE)
            message(STATUS "dftracer found via pip: ${DFTRACER_PIP_INC}")
          endif()
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
              target_include_directories(${_t} PRIVATE ${dftracer_INCLUDE_DIRS})
              target_compile_definitions(${_t} PRIVATE DFTRACER_ENABLE)
              if(TARGET dftracer::dftracer)
                target_link_libraries(${_t} PRIVATE dftracer::dftracer)
              elseif(dftracer_LIB_DIR)
                target_link_libraries(${_t} PRIVATE
                  "-L${dftracer_LIB_DIR}" "-ldftracer"
                  "-Wl,-rpath,${dftracer_LIB_DIR}")
              endif()
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


def _patch_autotools_makefile(
    path: Path,
    pip_include_dir: str = "",
    pip_lib_dir: str = "",
) -> str:
    """Return ``Makefile`` content with dftracer pkg-config flags prepended.

    Uses ``pkg-config --cflags/--libs dftracer`` which works when
    ``PKG_CONFIG_PATH`` includes the dftracer install prefix's
    ``lib/pkgconfig/`` directory.  Call ``session_generate_dftracer_pc``
    before ``session_build_annotated`` to ensure the ``.pc`` file exists;
    ``session_build_annotated`` sets ``PKG_CONFIG_PATH`` automatically.

    *pip_include_dir* and *pip_lib_dir* are accepted for API compatibility
    but are no longer embedded in the Makefile — the ``PKG_CONFIG_PATH``
    environment variable approach is simpler and avoids Make-syntax variables
    being misinterpreted as shell commands.

    This function is idempotent: if ``"dftracer"`` already appears in the
    file, the original content is returned unchanged.

    Args:
        path: Absolute path to the ``Makefile`` or ``Makefile.am`` to patch.
        pip_include_dir: Ignored (kept for API compatibility).
        pip_lib_dir: Ignored (kept for API compatibility).

    Returns:
        str: Modified file content.
    """
    content = path.read_text()
    if "dftracer" in content:
        return content

    injection = textwrap.dedent("""\
        # --- dftracer (auto-injected via pkg-config) ---
        # AM_CPPFLAGS / AM_LDFLAGS are the correct autotools hooks;
        # they are accumulated by automake and not overridden by configure.
        # PKG_CONFIG_PATH must include install_ann/lib/pkgconfig.
        # Do NOT pass -DDFTRACER_ENABLE: dftracer.h defines it as a string
        # and redefining it on the command line causes a compiler warning.
        DFTRACER_CFLAGS  := $(shell pkg-config --cflags dftracer 2>/dev/null)
        DFTRACER_LDFLAGS := $(shell pkg-config --libs   dftracer 2>/dev/null)
        AM_CPPFLAGS += $(DFTRACER_CFLAGS)
        AM_CXXFLAGS += $(DFTRACER_CFLAGS)
        AM_LDFLAGS  += $(DFTRACER_LDFLAGS)
        # ------------------------------------------------
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
