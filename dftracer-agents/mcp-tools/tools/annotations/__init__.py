"""Per-file dftracer annotation MCP tools.

Each module exposes a single ``register_*`` function that registers one
parallelizable per-file annotation tool onto a FastMCP instance.

Tools:
    ``session_annotate_c_file``      — annotate a single .c file (annotate_c.py)
    ``session_annotate_cpp_file``    — annotate a single .cpp/.cxx/.cc file (annotate_cpp.py)
    ``session_annotate_python_file`` — annotate a single .py file (annotate_python.py)

These tools are designed to be called in parallel by a pipeline orchestrator
(one call per source file, all issued simultaneously).  Each tool returns a
structured report that the orchestrator collects to build the overall annotation
summary.
"""
from .annotate_c import register_c_annotation_tools
from .annotate_cpp import register_cpp_annotation_tools
from .annotate_python import register_python_annotation_tools


def register_annotation_session_tools(mcp) -> None:
    """Register all per-file annotation session tools onto *mcp*."""
    register_c_annotation_tools(mcp)
    register_cpp_annotation_tools(mcp)
    register_python_annotation_tools(mcp)


__all__ = [
    "register_c_annotation_tools",
    "register_cpp_annotation_tools",
    "register_python_annotation_tools",
    "register_annotation_session_tools",
]
