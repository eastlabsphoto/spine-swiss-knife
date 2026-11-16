"""Headless entry dispatch and MCP command helpers (no Qt imports).

The GUI executable doubles as a CLI / MCP server:

    SpineSwissKnife --mcp            # MCP server on stdio
    SpineSwissKnife --cli <tool> ... # same as python -m spine_swiss_knife.cli

On Windows the GUI exe is a windowed build without stdio, so a second
console build ``SpineSwissKnifeMCP.exe`` ships next to it for MCP use.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

MCP_SERVER_NAME = "spine-swiss-knife"
WINDOWS_MCP_EXE = "SpineSwissKnifeMCP.exe"


def dispatch(argv: list[str]) -> Optional[int]:
    """Handle ``--mcp`` / ``--cli``. Returns an exit code, or None for the GUI."""
    if len(argv) > 1 and argv[1] == "--mcp":
        from . import mcp_server
        mcp_server.main()
        return 0
    if len(argv) > 1 and argv[1] == "--cli":
        from . import cli
        return cli.main(argv[2:])
    return None


def mcp_command() -> tuple[str, list[str]]:
    """Command + args that start the MCP server for *this* installation."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        if sys.platform == "win32":
            console_exe = exe.with_name(WINDOWS_MCP_EXE)
            return (str(console_exe if console_exe.exists() else exe), [])
        return str(exe), ["--mcp"]
    return sys.executable, ["-m", "spine_swiss_knife.mcp_server"]


def claude_desktop_config_snippet() -> str:
    cmd, args = mcp_command()
    return json.dumps({"mcpServers": {MCP_SERVER_NAME: {"command": cmd, "args": args}}},
                      indent=2)


def claude_code_command() -> str:
    cmd, args = mcp_command()
    quoted = " ".join(f'"{p}"' if " " in p else p for p in [cmd, *args])
    return f"claude mcp add {MCP_SERVER_NAME} -- {quoted}"


def claude_desktop_config_path() -> str:
    if sys.platform == "win32":
        import os
        return os.path.join(os.environ.get("APPDATA", "%APPDATA%"), "Claude",
                            "claude_desktop_config.json")
    if sys.platform == "darwin":
        return str(Path.home() / "Library" / "Application Support" / "Claude"
                   / "claude_desktop_config.json")
    return str(Path.home() / ".config" / "Claude" / "claude_desktop_config.json")
