"""Minimal MCP server (stdio, JSON-RPC 2.0, stdlib only) for Spine Swiss Knife.

Run:  python -m spine_swiss_knife.mcp_server
Register in Claude Code:
  claude mcp add spine-swiss-knife -- python3 -m spine_swiss_knife.mcp_server
(run from the repository root, or set PYTHONPATH to it).

Written without the ``mcp`` SDK so it works on Python 3.9 as well.
"""

from __future__ import annotations

import inspect
import json
import re
import sys

from . import __version__, api

PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")

_PARAM_DOCS = {
    "spine_file": "Path to the .spine project file.",
    "spine_exe": "Path to the Spine executable (auto-detected when omitted).",
    "out_dir": "Output directory.",
    "json_path": "Path to the exported skeleton JSON.",
    "atlas_path": "Path to the .atlas file.",
    "old": "Current attachment name.",
    "new": "New attachment name.",
    "apply": "Write the change; without it only a dry-run is reported.",
    "animation": "Animation name.",
    "skeleton": "Skeleton name (needed when the project has several).",
    "settings": "Spine export settings object (default: built-in settings).",
    "mode": "trim, center or fixed.",
    "tolerance": "Maximum allowed deviation (see the tool description).",
    "images_dir": "Folder with the source images.",
    "folder": "Folder containing skeleton JSON files.",
    "tool": "Name of the JSON tool to run on every skeleton.",
    "recursive": "Also scan sub-folders.",
}


def _param_docs(fn) -> dict:
    """Parameter descriptions: ``name: text`` lines of the docstring, else defaults."""
    docs = dict(_PARAM_DOCS)
    names = set(inspect.signature(fn).parameters)
    for line in (inspect.getdoc(fn) or "").splitlines():
        m = re.match(r"^\s*(\w+)\s*:\s*(\S.*)$", line)
        if m and m.group(1) in names:
            docs[m.group(1)] = m.group(2).strip()
    return docs


def _schema_for(fn) -> dict:
    props: dict = {}
    required: list[str] = []
    docs = _param_docs(fn)
    for pname, p in inspect.signature(fn).parameters.items():
        hint = str(p.annotation)
        if "bool" in hint:
            t = "boolean"
        elif "float" in hint:
            t = "number"
        elif "int" in hint:
            t = "integer"
        elif "dict" in hint:
            t = "object"
        else:
            t = "string"
        props[pname] = {"type": t}
        if pname in docs:
            props[pname]["description"] = docs[pname]
        if p.default is inspect.Parameter.empty:
            required.append(pname)
        elif p.default is not None:
            props[pname]["default"] = p.default
    return {"type": "object", "properties": props, "required": required}


def _tools_list() -> list[dict]:
    out = []
    for name, fn in api.TOOLS.items():
        out.append({"name": name,
                    "description": inspect.getdoc(fn) or name,
                    "inputSchema": _schema_for(fn)})
    return out


def _call_tool(name: str, arguments: dict) -> dict:
    fn = api.TOOLS.get(name)
    if fn is None:
        return {"isError": True,
                "content": [{"type": "text", "text": f"unknown tool {name}"}]}
    try:
        result = fn(**(arguments or {}))
        return {"content": [{"type": "text",
                             "text": json.dumps(result, indent=2,
                                                ensure_ascii=False)}]}
    except Exception as e:
        return {"isError": True,
                "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}]}


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def handle(msg):
    """Return a response dict, or None for notifications."""
    if not isinstance(msg, dict):
        return _error(None, -32600, "invalid request: expected a JSON object")
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params")
    if not isinstance(params, dict):
        params = {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        result = {"protocolVersion": version,
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "spine-swiss-knife",
                                 "version": __version__}}
    elif method == "tools/list":
        result = {"tools": _tools_list()}
    elif method == "tools/call":
        args = params.get("arguments", {})
        if not isinstance(args, dict):
            args = {}
        result = _call_tool(str(params.get("name", "")), args)
    elif method == "ping":
        result = {}
    elif mid is None:
        return None  # notification (e.g. notifications/initialized)
    else:
        return _error(mid, -32601, f"method not found: {method}")
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _safe_handle(msg):
    try:
        return handle(msg)
    except Exception as e:  # never let a request kill the server loop
        mid = msg.get("id") if isinstance(msg, dict) else None
        return _error(mid, -32603, f"internal error: {type(e).__name__}: {e}")


def _process_line(line: str):
    """Return the text to write for one input line, or None."""
    try:
        msg = json.loads(line)
    except ValueError:
        return json.dumps(_error(None, -32700, "parse error"))
    if isinstance(msg, list):
        if not msg:
            return json.dumps(_error(None, -32600, "invalid request: empty batch"))
        replies = [r for r in (_safe_handle(m) for m in msg) if r is not None]
        return json.dumps(replies) if replies else None
    resp = _safe_handle(msg)
    return json.dumps(resp) if resp is not None else None


def main() -> None:
    # MCP stdio is UTF-8; Windows text mode would use the ANSI code page.
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        out = _process_line(line)
        if out is not None:
            sys.stdout.write(out + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
