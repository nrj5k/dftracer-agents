#!/usr/bin/env python3
"""
Run script for the DFTracer-utils MCP service.
This script initializes and runs all registered MCP services.

Usage:
    python run_mcp_service.py
"""

import sys
import os

# Add the parent directory to the path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    """Main entry point for running the MCP services."""
    try:
        from mcp_tools import get_mcp_server, run_mcp_server
        run_mcp_server()
    except ImportError as e:
        print(f"Import error: {e}")
        print("Make sure the virtual environment is activated and dependencies are installed.")
        sys.exit(1)
    except ValueError as e:
        print(f"Value error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error running MCP service: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()