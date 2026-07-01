from .diagnose import register_diagnose_tools
from .iteration import register_iteration_tools
from .levels import register_level_tools
from .memory import register_memory_tools


def register_optimization_tools(mcp) -> None:
    register_diagnose_tools(mcp)
    register_iteration_tools(mcp)
    register_level_tools(mcp)
    register_memory_tools(mcp)
