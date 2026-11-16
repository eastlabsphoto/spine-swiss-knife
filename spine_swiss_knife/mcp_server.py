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
import sys

from . import api

PROTOCOL_VERSION = "2024-11-05"


def _schema_for(fn) -> dict:
    props: dict = {}
    required: list[str] = []
    for pname, p in inspect.signature(fn).parameters.items():
        hint = str(p.annotation)
        if "bool" in hint:
            t = "boolean"
        elif "int" in hint:
            t = "integer"
        elif "dict" in hint:
            t = "object"
        else:
            t = "string"
        props[pname] = {"type": t}
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
    except (api.SpineError, TypeError, ValueError, OSError) as e:
        return {"isError": True,
                "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}]}


def handle(msg: dict):
    """Return a response dict, or None for notifications."""
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL_VERSION,
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "spine-swiss-knife", "version": "0.1.0"}}
    elif method == "tools/list":
        result = {"tools": _tools_list()}
    elif method == "tools/call":
        p = msg.get("params", {})
        result = _call_tool(p.get("name", ""), p.get("arguments", {}))
    elif method == "ping":
        result = {}
    elif mid is None:
        return None  # notification (e.g. notifications/initialized)
    else:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
