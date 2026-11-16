"""MCP connection helpers: command for this installation, Claude Desktop config install."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from spine_swiss_knife import launcher

REPO = Path(__file__).resolve().parent.parent


class McpCommandTests(unittest.TestCase):
    def test_source_command_works_from_any_directory(self):
        cmd, args = launcher.mcp_command()
        self.assertEqual(cmd, sys.executable)
        self.assertTrue(Path(args[0]).is_absolute(), args)
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {}}) + "\n"
        with tempfile.TemporaryDirectory() as other_dir:
            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            proc = subprocess.run([cmd, *args], input=request, cwd=other_dir,
                                  env=env, capture_output=True, text=True,
                                  timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        reply = json.loads(proc.stdout.splitlines()[0])
        self.assertEqual(reply["result"]["serverInfo"]["name"], "spine-swiss-knife")


class InstallConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "Claude" / "claude_desktop_config.json"

    def _entry(self):
        cmd, args = launcher.mcp_command()
        return {"command": cmd, "args": args}

    def test_missing_claude_folder_is_reported(self):
        with self.assertRaises(FileNotFoundError):
            launcher.install_claude_desktop_config(str(self.path))
        self.assertFalse(self.path.parent.exists())

    def test_added_to_new_file_then_unchanged(self):
        self.path.parent.mkdir()
        result = launcher.install_claude_desktop_config(str(self.path))
        self.assertEqual(result["status"], "added")
        self.assertEqual(result["backup"], "")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["mcpServers"]["spine-swiss-knife"], self._entry())
        again = launcher.install_claude_desktop_config(str(self.path))
        self.assertEqual(again["status"], "unchanged")

    def test_other_settings_are_kept_and_backup_written(self):
        self.path.parent.mkdir()
        original = {"theme": "dark",
                    "mcpServers": {"other": {"command": "x", "args": ["y"]},
                                   "spine-swiss-knife": {"command": "old", "args": []}}}
        self.path.write_text(json.dumps(original), encoding="utf-8")
        result = launcher.install_claude_desktop_config(str(self.path))
        self.assertEqual(result["status"], "updated")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["mcpServers"]["other"], {"command": "x", "args": ["y"]})
        self.assertEqual(data["mcpServers"]["spine-swiss-knife"], self._entry())
        self.assertEqual(json.loads(Path(result["backup"]).read_text(encoding="utf-8")),
                         original)

    def test_invalid_file_is_never_overwritten(self):
        self.path.parent.mkdir()
        for content in ("{not json", "[1, 2]", '{"mcpServers": []}'):
            self.path.write_text(content, encoding="utf-8")
            with self.assertRaises(ValueError):
                launcher.install_claude_desktop_config(str(self.path))
            self.assertEqual(self.path.read_text(encoding="utf-8"), content)

    def test_empty_file_is_treated_as_new(self):
        self.path.parent.mkdir()
        self.path.write_text("", encoding="utf-8")
        result = launcher.install_claude_desktop_config(str(self.path))
        self.assertEqual(result["status"], "added")
        self.assertIn("spine-swiss-knife",
                      json.loads(self.path.read_text(encoding="utf-8"))["mcpServers"])


if __name__ == "__main__":
    unittest.main()
