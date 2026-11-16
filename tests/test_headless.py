import json
import unittest

from spine_swiss_knife import api, launcher, mcp_server


class McpServerTests(unittest.TestCase):
    def test_initialize_and_tools_list(self):
        init = mcp_server.handle({"jsonrpc": "2.0", "id": 1,
                                  "method": "initialize", "params": {}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "spine-swiss-knife")
        tools = mcp_server.handle({"jsonrpc": "2.0", "id": 2,
                                   "method": "tools/list"})["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, set(api.TOOLS))
        for t in tools:
            self.assertEqual(t["inputSchema"]["type"], "object")

    def test_notification_has_no_response(self):
        self.assertIsNone(mcp_server.handle(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_unknown_method_errors(self):
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})
        self.assertEqual(r["error"]["code"], -32601)

    def test_tool_error_is_reported_not_raised(self):
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                               "params": {"name": "check_regions", "arguments": {
                                   "json_path": "/nope.json",
                                   "atlas_path": "/nope.atlas"}}})
        self.assertTrue(r["result"]["isError"])


class CheckRegionsTests(unittest.TestCase):
    def test_detects_extension_mismatch(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            j = os.path.join(d, "s.json")
            a = os.path.join(d, "s.atlas")
            json.dump({"skins": [{"name": "default", "attachments": {
                "slot": {"skirt.png": {"width": 1, "height": 1}}}}]},
                open(j, "w"))
            open(a, "w").write("s.png\nsize:8,8\nskirt\nbounds:0,0,1,1\n")
            r = api.check_regions(j, a)
            self.assertFalse(r["ok"])
            self.assertIn("skirt.png", r["missing"])
            self.assertIn("skirt.png", r["hints"])
            self.assertEqual(r["unused_atlas_regions"], ["skirt"])


class LauncherTests(unittest.TestCase):
    def test_gui_when_no_flag(self):
        self.assertIsNone(launcher.dispatch(["prog"]))

    def test_snippets_mention_server_name(self):
        self.assertIn("spine-swiss-knife", launcher.claude_desktop_config_snippet())
        self.assertTrue(launcher.claude_code_command().startswith(
            "claude mcp add spine-swiss-knife --"))


if __name__ == "__main__":
    unittest.main()
