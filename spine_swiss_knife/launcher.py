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
    # Running from source: MCP clients start the server from an arbitrary
    # working directory, so "-m spine_swiss_knife..." would not be found.
    # The launcher script next to the package works from anywhere.
    script = Path(__file__).resolve().parent.parent / "spine_swiss_knife.py"
    if script.is_file():
        return sys.executable, [str(script), "--mcp"]
    return sys.executable, ["-m", "spine_swiss_knife.mcp_server"]


def install_claude_desktop_config(config_path: Optional[str] = None) -> dict:
    """Add this installation's MCP server to Claude Desktop's config file.

    Other entries in the file are kept. Returns ``{"path", "status", "backup"}``
    where status is ``"added"``, ``"updated"`` or ``"unchanged"``. An existing
    file is copied to ``<file>.ssk-backup`` before it is changed.

    Raises FileNotFoundError when Claude Desktop's config folder does not exist
    (Claude Desktop is not installed) and ValueError when the existing file is
    not a JSON object - the file is never overwritten in those cases.
    """
    import os
    import shutil

    path = Path(config_path or claude_desktop_config_path())
    if not path.parent.is_dir():
        raise FileNotFoundError(f"Claude Desktop folder not found: {path.parent}")

    data: dict = {}
    existed = path.is_file()
    if existed:
        text = path.read_text(encoding="utf-8")
        if text.strip():
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path.name} is not valid JSON ({e})") from e
            if not isinstance(data, dict):
                raise ValueError(f"{path.name} does not contain a JSON object")

    servers = data.get("mcpServers")
    if servers is None:
        servers = data["mcpServers"] = {}
    elif not isinstance(servers, dict):
        raise ValueError(f'"mcpServers" in {path.name} is not an object')

    cmd, args = mcp_command()
    entry = {"command": cmd, "args": args}
    previous = servers.get(MCP_SERVER_NAME)
    if previous == entry:
        return {"path": str(path), "status": "unchanged", "backup": ""}

    backup = ""
    if existed:
        backup = str(path) + ".ssk-backup"
        shutil.copy2(str(path), backup)
    servers[MCP_SERVER_NAME] = entry
    tmp = path.with_name(path.name + ".ssk-tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.replace(str(tmp), str(path))
    return {"path": str(path),
            "status": "updated" if previous is not None else "added",
            "backup": backup}


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
