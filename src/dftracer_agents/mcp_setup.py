"""
Configure MCP clients (Claude Code and Goose) to connect to the
dftracer-mcp-server HTTP endpoint.

CLI usage (after pip install)::

    dftracer-configure-mcp                  # localhost:5000 (default)
    dftracer-configure-mcp --port 8080
    dftracer-configure-mcp --no-goose       # skip Goose config
    dftracer-configure-mcp --dry-run        # show what would change

Programmatic usage::

    from dftracer_agents.mcp_setup import configure_claude_code, configure_goose
    configure_claude_code()
    configure_goose()
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


MCP_SERVER_NAME = "dftracer"


def _mcp_url(host: str, port: int, path: str) -> str:
    return f"http://{host}:{port}{path}"


# ---------------------------------------------------------------------------
# Claude Code — ~/.claude/mcp.json
# ---------------------------------------------------------------------------

def configure_claude_code(
    host: str = "localhost",
    port: int = 5000,
    path: str = "/mcp",
    dry_run: bool = False,
) -> Path:
    """Write/merge the dftracer entry into ~/.claude/mcp.json.

    Returns the path to the config file.
    """
    config_path = Path.home() / ".claude" / "mcp.json"
    url = _mcp_url(host, port, path)

    existing: dict = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text())
        except json.JSONDecodeError:
            print(
                f"  WARNING: {config_path} contains invalid JSON — will overwrite.",
                file=sys.stderr,
            )

    servers: dict = existing.setdefault("mcpServers", {})
    prev = servers.get(MCP_SERVER_NAME)
    servers[MCP_SERVER_NAME] = {"url": url}

    if prev == servers[MCP_SERVER_NAME]:
        print(f"  Claude Code: {config_path} already up to date ({url})")
        return config_path

    if dry_run:
        print(f"  [dry-run] Would write to {config_path}:")
        print(f"    mcpServers.{MCP_SERVER_NAME}.url = {url}")
        return config_path

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(existing, indent=2) + "\n")
    action = "Updated" if prev else "Configured"
    print(f"  Claude Code: {action} {config_path}")
    print(f"    {MCP_SERVER_NAME} → {url}")
    return config_path


# ---------------------------------------------------------------------------
# Goose — ~/.config/goose/config.yaml  (or ~/.goose/config.yaml)
# ---------------------------------------------------------------------------

def _goose_config_path() -> Path:
    """Return the active Goose config path (prefer ~/.config/goose, fallback ~/.goose)."""
    primary = Path.home() / ".config" / "goose" / "config.yaml"
    if primary.exists():
        return primary
    fallback = Path.home() / ".goose" / "config.yaml"
    if fallback.exists():
        return fallback
    return primary  # default create location


def configure_goose(
    host: str = "localhost",
    port: int = 5000,
    path: str = "/mcp",
    dry_run: bool = False,
) -> Path | None:
    """Write/merge the dftracer extension into the Goose config.

    Returns the path written, or None if PyYAML is not available.
    """
    try:
        import yaml  # type: ignore
    except ImportError:
        _print_goose_snippet(host, port, path)
        return None

    config_path = _goose_config_path()
    url = _mcp_url(host, port, path)

    config: dict = {}
    if config_path.exists():
        with config_path.open() as f:
            config = yaml.safe_load(f) or {}

    extensions: dict = config.setdefault("extensions", {})
    entry = extensions.get(MCP_SERVER_NAME, {})
    new_entry = {
        "enabled": True,
        "type": "streamable_http",
        "name": MCP_SERVER_NAME,
        "description": "mcp tools for using dftracer",
        "uri": url,
        "bundled": None,
        "available_tools": [],
    }

    if entry == new_entry:
        print(f"  Goose:       {config_path} already up to date ({url})")
        return config_path

    extensions[MCP_SERVER_NAME] = new_entry

    if dry_run:
        print(f"  [dry-run] Would write to {config_path}:")
        print(f"    extensions.{MCP_SERVER_NAME}.uri = {url}")
        return config_path

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

    action = "Updated" if entry else "Configured"
    print(f"  Goose:       {action} {config_path}")
    print(f"    {MCP_SERVER_NAME} → {url}")
    return config_path


def _print_goose_snippet(host: str, port: int, path: str) -> None:
    url = _mcp_url(host, port, path)
    print(
        "  Goose:       PyYAML not installed — add this to ~/.config/goose/config.yaml manually:\n"
        f"\n"
        f"    extensions:\n"
        f"      {MCP_SERVER_NAME}:\n"
        f"        enabled: true\n"
        f"        type: streamable_http\n"
        f"        name: {MCP_SERVER_NAME}\n"
        f"        description: mcp tools for using dftracer\n"
        f"        uri: {url}\n"
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point for the ``dftracer-configure-mcp`` CLI command."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="dftracer-configure-mcp",
        description=(
            "Configure Claude Code and Goose to connect to the dftracer MCP "
            "HTTP server. Writes ~/.claude/mcp.json and ~/.config/goose/config.yaml."
        ),
    )
    parser.add_argument(
        "--host",
        default="localhost",
        help="Host the MCP server will run on (default: localhost)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5000,
        help="Port the MCP server will listen on (default: 5000)",
    )
    parser.add_argument(
        "--path",
        default="/mcp",
        help="URL path for the MCP endpoint (default: /mcp)",
    )
    parser.add_argument(
        "--no-claude",
        action="store_true",
        help="Skip Claude Code configuration",
    )
    parser.add_argument(
        "--no-goose",
        action="store_true",
        help="Skip Goose configuration",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing any files",
    )
    args = parser.parse_args()

    print("Configuring MCP clients for dftracer-mcp-server...")
    print(f"  Server URL:  http://{args.host}:{args.port}{args.path}")
    print()

    if not args.no_claude:
        configure_claude_code(
            host=args.host,
            port=args.port,
            path=args.path,
            dry_run=args.dry_run,
        )

    if not args.no_goose:
        configure_goose(
            host=args.host,
            port=args.port,
            path=args.path,
            dry_run=args.dry_run,
        )

    print()
    print("Done. Start the server with:  dftracer-mcp-server")
    print("  (or add it to your shell startup / systemd user unit)")


if __name__ == "__main__":
    main()
