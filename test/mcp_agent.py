#!/usr/bin/env python3
"""
LLM-powered agent for dftracer-agents MCP services.

Connects to the local LLM defined in openai_client_config.json (default:
ollama / qwen2.5-coder:7b) and uses it as the brain for deciding which MCP
tools to call.  You talk to it in plain English; the LLM translates your
intent into tool calls, runs them via the MCP server, and reports back.

Usage:
    python test/mcp_agent.py
    python test/mcp_agent.py --service utils       # dftracer_utils (default)
    python test/mcp_agent.py --service analyzer    # dfanalyzer
    python test/mcp_agent.py --service both        # all tools
    python test/mcp_agent.py --config path/to/other_config.json

Requires ollama running with the configured model:
    ollama serve
    ollama pull qwen2.5-coder:7b
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from openai import OpenAI
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

# ── paths ──────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = REPO_ROOT / "venv" / "bin" / "python"
VENV_BIN    = REPO_ROOT / "venv" / "bin"
THIS_DIR    = Path(__file__).resolve().parent
DEFAULT_CONFIG = THIS_DIR / "openai_client_config.json"


# ── MCP server params ──────────────────────────────────────────────────────

def _server_params(service: str) -> StdioServerParameters:
    # Reuse the throw-away server scripts written by mcp_repl.py if they exist,
    # otherwise write them now via the same helpers.
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("mcp_repl", THIS_DIR / "mcp_repl.py")
    _repl = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_repl)

    env = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "PATH": f"{VENV_BIN}:{os.environ.get('PATH', '')}",
    }
    if service == "utils":
        script = str(THIS_DIR / "mcp_integration_server.py")
    elif service == "analyzer":
        script = str(THIS_DIR / "_mcp_repl_analyzer_server.py")
        _repl._write_analyzer_server(script)
    else:
        script = str(THIS_DIR / "_mcp_repl_both_server.py")
        _repl._write_both_server(script)
    return StdioServerParameters(
        command=str(VENV_PYTHON),
        args=[script],
        cwd=str(REPO_ROOT),
        env=env,
    )


# ── MCP → OpenAI function schema conversion ─────────────────────────────────

def _mcp_tool_to_openai(tool) -> dict[str, Any]:
    """Convert an MCP FunctionTool to an OpenAI tools-array entry."""
    schema = tool.inputSchema or {}
    # Remove keys OpenAI doesn't accept at the top level of parameters
    params = {
        "type": "object",
        "properties": schema.get("properties", {}),
    }
    if "required" in schema:
        params["required"] = schema["required"]
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": (tool.description or "")[:1024],
            "parameters": params,
        },
    }


# ── result formatter ────────────────────────────────────────────────────────

def _format_result(result) -> str:
    parts = [item.text for item in result.content if hasattr(item, "text")]
    text = "\n".join(parts)
    if result.isError:
        return f"[tool error]\n{text}"
    return text or "(empty output)"


# ── agent loop ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a helpful assistant for the DFTracer I/O tracing toolkit.

You have access to MCP tools that wrap the dftracer_* command-line utilities
and dfanalyzer.  Use them to answer the user's questions.

When the user asks you to do something (run a tool, inspect a file, analyse
a trace), call the appropriate tool rather than guessing the answer.

If a tool call fails, report the error to the user and suggest how to fix it
(e.g. wrong path, missing arguments).

Be concise.  For large tool outputs, summarise the key findings instead of
echoing everything verbatim.
"""

# ── ollama JSON-in-content fallback ─────────────────────────────────────────
# Some ollama models return tool calls as JSON text in content rather than
# using the structured tool_calls field.  This function extracts them.

import re as _re

def _parse_json_tool_calls(content: str, known_tool_names: list[str]) -> list[dict] | None:
    """
    Extract tool call(s) from plain-text content produced by ollama models.

    Handles multiple formats:
      {"name": "tool", "arguments": {...}}          — standard JSON object
      [{"name": "tool", "arguments": {...}}, ...]   — JSON array
      tool_name {"key": "value", ...}               — bare name then JSON args
      tool_name\n{"key": "value"}                   — name then newline then JSON

    Returns a list of {"name": str, "arguments": dict}, or None if nothing found.
    """
    if not content:
        return None
    text = content.strip()

    # Strip markdown fences
    text = _re.sub(r"^```(?:json)?\s*", "", text)
    text = _re.sub(r"\s*```$", "", text).strip()

    # ── format 1: pure JSON {"name": ..., "arguments": ...} ──
    def _extract_from_parsed(parsed) -> list[dict] | None:
        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list):
            return None
        calls = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("function") or item.get("tool")
            args = item.get("arguments") or item.get("parameters") or item.get("args") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            if name and isinstance(args, dict):
                calls.append({"name": name, "arguments": args})
        return calls or None

    try:
        calls = _extract_from_parsed(json.loads(text))
        if calls:
            return calls
    except json.JSONDecodeError:
        pass

    # ── format 2: tool_name {...} or tool_name\n{...} ──
    # e.g.  "info {\n  \"directory\": \"/tmp\"\n}"
    for tool_name in known_tool_names:
        pattern = _re.compile(
            rf"^{_re.escape(tool_name)}\s*(\{{.*\}})",
            _re.DOTALL,
        )
        m = pattern.match(text)
        if m:
            try:
                args = json.loads(m.group(1))
                if isinstance(args, dict):
                    return [{"name": tool_name, "arguments": args}]
            except json.JSONDecodeError:
                pass

    # ── format 3: any JSON object/array buried in prose ──
    match = _re.search(r'(\{[^{}]*(?:\{[^{}]*\}[^{}]*)?\})', text, _re.DOTALL)
    if match:
        try:
            calls = _extract_from_parsed(json.loads(match.group(0)))
            if calls:
                return calls
        except json.JSONDecodeError:
            pass

    return None


async def _execute_tool(
    session: ClientSession,
    tool_names: list[str],
    tool_name: str,
    args: dict,
) -> str:
    """Run a single MCP tool call and return the result as a string."""
    print(f"  [tool] {tool_name}({json.dumps(args, separators=(',', ':'))})")
    if tool_name not in tool_names:
        return f"Error: unknown tool '{tool_name}'"
    try:
        mcp_result = await session.call_tool(tool_name, args)
        text = _format_result(mcp_result)
    except Exception as e:
        text = f"Error calling tool: {e}"
    if len(text) > 8000:
        text = text[:8000] + "\n…(truncated)"
    print(f"  [result] {text[:300].replace(chr(10), ' ')}")
    return text


async def _agent_loop(session: ClientSession, llm: OpenAI, model: str) -> None:
    tools_response = await session.list_tools()
    openai_tools = [_mcp_tool_to_openai(t) for t in tools_response.tools]
    tool_names = [t.name for t in tools_response.tools]

    print()
    print("=" * 60)
    print("  dftracer-agents LLM Agent")
    print("=" * 60)
    print(f"  Model : {model}")
    print(f"  Tools : {len(openai_tools)} MCP tools available")
    print("  Type your question in plain English.  Ctrl-C or 'quit' to exit.")
    print("=" * 60)
    print()

    conversation: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break
        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("Bye.")
            break

        conversation.append({"role": "user", "content": user_input})

        # ── agentic loop: keep calling LLM until no more tool calls ──
        while True:
            print("  [thinking…]", end="\r", flush=True)
            try:
                response = llm.chat.completions.create(
                    model=model,
                    messages=conversation,
                    tools=openai_tools,
                    tool_choice="auto",
                )
            except Exception as e:
                print(f"\n  [LLM error] {type(e).__name__}: {e}")
                print("  Is ollama running?  Try: ollama serve")
                break

            msg = response.choices[0].message
            print(" " * 20, end="\r")  # clear "thinking…"

            # ── case 1: structured tool_calls (OpenAI-native models) ──
            if msg.tool_calls:
                conversation.append(msg.model_dump(exclude_unset=True))
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    result_text = await _execute_tool(
                        session, tool_names, tc.function.name, args
                    )
                    conversation.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result_text,
                    })
                continue  # feed results back to LLM

            # ── case 2: JSON-in-content (ollama fallback) ──
            parsed_calls = _parse_json_tool_calls(msg.content or "", tool_names)
            if parsed_calls:
                # Treat as if the assistant made the call(s)
                conversation.append({"role": "assistant", "content": msg.content})
                for call in parsed_calls:
                    result_text = await _execute_tool(
                        session, tool_names, call["name"], call["arguments"]
                    )
                    # Inject result as a user message (ollama doesn't have a tool role)
                    conversation.append({
                        "role": "user",
                        "content": (
                            f"Tool '{call['name']}' returned:\n{result_text}\n\n"
                            "Please summarise the result for the user."
                        ),
                    })
                continue  # feed results back to LLM

            # ── case 3: plain text answer ──
            reply = msg.content or "(no response)"
            print(f"\nagent> {reply}\n")
            break


# ── entry point ─────────────────────────────────────────────────────────────

async def _main(service: str, config_path: Path) -> None:
    # Load LLM config
    try:
        cfg = json.loads(config_path.read_text())
    except FileNotFoundError:
        print(f"Config not found: {config_path}")
        sys.exit(1)

    llm = OpenAI(
        base_url=cfg["base_url"],
        api_key=cfg.get("api_key", "ollama"),
    )
    model = cfg["model"]

    params = _server_params(service)

    print(f"Starting MCP server ({service})…")
    print(f"Connecting to LLM at {cfg['base_url']} (model={model})")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as errlog:
        errlog_path = errlog.name
    print(f"Server stderr → {errlog_path}")

    with open(errlog_path, "w") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await _agent_loop(session, llm, model)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="LLM-powered agent for dftracer-agents MCP services"
    )
    parser.add_argument(
        "--service",
        choices=["utils", "analyzer", "both"],
        default="utils",
        help="Which MCP service to connect to (default: utils)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Path to openai_client_config.json (default: test/openai_client_config.json)",
    )
    args = parser.parse_args()
    asyncio.run(_main(args.service, args.config))


if __name__ == "__main__":
    main()
