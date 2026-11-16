import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spine_swiss_knife import api, mcp_server, cli, static_exporter_core as core


def _write(d, name, data):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return p


class FixAttachmentNameTests(unittest.TestCase):
    def _data(self):
        return {
            "skeleton": {"spine": "3.8.99"},
            "bones": [{"name": "star"}],
            "slots": [{"name": "star", "bone": "star", "attachment": "star"}],
            "skins": [{"name": "default", "attachments": {"star": {
                "star": {"name": "star", "path": "star", "width": 1},
                "ünï": {"width": 2},
                "lm": {"type": "linkedmesh", "parent": "star"}}}}],
            "animations": {"a": {
                "bones": {"star": [{"time": 0}]},
                "slots": {"star": {"attachment": [{"name": "star"},
                                                  {"name": None}]}},
                "deform": {"default": {"star": {"star": [{"time": 0}]}}}}},
        }

    def test_structural_rename(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "s.json", self._data())
            dry = api.fix_attachment_name(p, "star", "sun")
            self.assertFalse(dry["applied"])
            self.assertEqual(dry["occurrences"], 7)
            self.assertIn('"star"', open(p, encoding="utf-8").read())
            r = api.fix_attachment_name(p, "star", "sun", apply=True)
            self.assertTrue(r["applied"])
            self.assertTrue(os.path.exists(r["backup"]))
            data = json.load(open(p, encoding="utf-8"))
            self.assertEqual(data["bones"][0]["name"], "star")
            self.assertEqual(data["slots"][0]["name"], "star")
            self.assertEqual(data["slots"][0]["attachment"], "sun")
            self.assertIn("star", data["skins"][0]["attachments"])
            atts = data["skins"][0]["attachments"]["star"]
            self.assertIn("sun", atts)
            self.assertNotIn("star", atts)
            self.assertEqual(atts["sun"]["name"], "sun")
            self.assertEqual(atts["lm"]["parent"], "sun")
            anim = data["animations"]["a"]
            self.assertIn("star", anim["bones"])
            self.assertEqual(anim["slots"]["star"]["attachment"][0]["name"], "sun")
            self.assertIn("sun", anim["deform"]["default"]["star"])

    def test_non_ascii_and_conflict(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "s.json", self._data())
            r = api.fix_attachment_name(p, "ünï", "uni", apply=True)
            self.assertEqual(r["occurrences"], 1)
            data = json.load(open(p, encoding="utf-8"))
            self.assertIn("uni", data["skins"][0]["attachments"]["star"])
            with self.assertRaises(api.SpineError):
                api.fix_attachment_name(p, "star", "lm")


class ValidateAndRegionsTests(unittest.TestCase):
    def test_validate_tool(self):
        self.assertIn("validate", api.TOOLS)
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "s.json", {"skeleton": {"spine": "3.8.99"},
                                     "bones": [{"name": "root"}], "slots": [],
                                     "skins": [{"name": "default"}]})
            r = api.validate(p)
            self.assertEqual(set(r), {"ok", "json", "errors", "warnings"})
            self.assertIsInstance(r["errors"], list)

    def test_check_regions_uses_path(self):
        with tempfile.TemporaryDirectory() as d:
            j = _write(d, "s.json", {"skins": [{"name": "default", "attachments": {
                "slot": {"att": {"path": "real/img"}}}}]})
            a = os.path.join(d, "s.atlas")
            open(a, "w").write("s.png\nsize:8,8\nreal/img\nbounds:0,0,1,1\n")
            self.assertTrue(api.check_regions(j, a)["ok"])


class RunSpineTests(unittest.TestCase):
    def test_timeout_and_oserror(self):
        with tempfile.TemporaryDirectory() as d:
            sp = os.path.join(d, "p.spine")
            open(sp, "wb").write(b"x")
            with mock.patch.object(api.subprocess, "run",
                                   side_effect=subprocess.TimeoutExpired("x", 1)):
                with self.assertRaises(api.SpineError):
                    api._run_spine(["-i", sp], sp, "/fake/spine")
            with mock.patch.object(api.subprocess, "run",
                                   side_effect=OSError("nope")):
                with self.assertRaises(api.SpineError):
                    api._run_spine(["-i", sp], sp, "/fake/spine")

    def test_stdin_devnull(self):
        with mock.patch.object(api.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            api._run_spine(["-i", "nonexistent.spine"], "nonexistent.spine", "/fake")
            self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)


class ExportStaticTests(unittest.TestCase):
    def _png(self, path):
        from PIL import Image
        img = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
        img.paste((255, 0, 0, 255), (5, 5, 15, 15))
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        img.save(path)

    def test_slash_in_animation_and_full_settings(self):
        seen = []

        def fake(args, spine_file, spine_exe, timeout=600):
            settings = json.load(open(args[args.index("-e") + 1]))
            seen.append(settings)
            self._png(Path(settings["output"]) / "f" / "0000.png")
            return 0, "", ""
        with tempfile.TemporaryDirectory() as d:
            sp = os.path.join(d, "p.spine")
            open(sp, "wb").write(b"x")
            with mock.patch.object(api, "_run_spine", fake):
                r = api.export_static(sp, "win/big", os.path.join(d, "o"),
                                      spine_exe="/fake")
            self.assertTrue(os.path.isfile(r["static"]))
            self.assertEqual(os.path.basename(r["static"]), "p_win_big.png")
            for key in core.DEFAULT_EXPORT_SETTINGS:
                self.assertIn(key, seen[0])
            self.assertEqual(seen[0]["animation"], "win/big")

    def test_multiple_skeletons_error(self):
        def fake(args, spine_file, spine_exe, timeout=600):
            settings = json.load(open(args[args.index("-e") + 1]))
            for sk in ("a", "b"):
                self._png(Path(settings["output"]) / sk / "x" / "0000.png")
            return 0, "", ""
        with tempfile.TemporaryDirectory() as d:
            sp = os.path.join(d, "p.spine")
            open(sp, "wb").write(b"x")
            with mock.patch.object(api, "_run_spine", fake):
                with self.assertRaises(api.SpineError) as cm:
                    api.export_static(sp, "idle", os.path.join(d, "o"),
                                      spine_exe="/fake")
            self.assertIn("a", str(cm.exception))
            self.assertIn("b", str(cm.exception))


class McpRobustnessTests(unittest.TestCase):
    def test_tool_exception_is_error_result(self):
        with mock.patch.dict(api.TOOLS, {"boom": lambda: (_ for _ in ()).throw(
                subprocess.TimeoutExpired("x", 1))}):
            r = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "boom", "arguments": {}}})
        self.assertTrue(r["result"]["isError"])

    def test_non_object_and_batch_and_bad_json(self):
        r = json.loads(mcp_server._process_line("[1]"))
        self.assertEqual(r[0]["error"]["code"], -32600)
        self.assertEqual(json.loads(mcp_server._process_line('"x"'))["error"]["code"], -32600)
        self.assertEqual(json.loads(mcp_server._process_line("{bad"))["error"]["code"], -32700)
        b = json.loads(mcp_server._process_line(json.dumps(
            [{"jsonrpc": "2.0", "id": 1, "method": "ping"},
             {"jsonrpc": "2.0", "method": "notifications/initialized"}])))
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0]["id"], 1)

    def test_internal_error_keeps_id(self):
        with mock.patch.object(mcp_server, "handle", side_effect=RuntimeError("x")):
            r = mcp_server._safe_handle({"id": 7, "method": "ping"})
        self.assertEqual(r["error"]["code"], -32603)
        self.assertEqual(r["id"], 7)

    def test_version_and_protocol(self):
        from spine_swiss_knife import __version__
        r = mcp_server.handle({"id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2025-03-26"}})
        self.assertEqual(r["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(r["result"]["serverInfo"]["version"], __version__)
        r = mcp_server.handle({"id": 1, "method": "initialize",
                               "params": {"protocolVersion": "1999"}})
        self.assertEqual(r["result"]["protocolVersion"], mcp_server.PROTOCOL_VERSION)

    def test_param_descriptions(self):
        tools = {t["name"]: t for t in mcp_server._tools_list()}
        props = tools["check_regions"]["inputSchema"]["properties"]
        self.assertIn("description", props["json_path"])


class CliTests(unittest.TestCase):
    def test_generic_exception_json(self):
        import io
        from contextlib import redirect_stdout
        with mock.patch.dict(api.TOOLS, {"project_info": lambda spine_file: 1 / 0}):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["project-info", "--spine-file", "x"])
        self.assertEqual(rc, 1)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["type"], "ZeroDivisionError")


class BlurTests(unittest.TestCase):
    def test_red_edge_stays_red_and_alpha_mass(self):
        from PIL import Image
        img = Image.new("RGBA", (10, 40), (0, 0, 0, 0))
        img.paste((255, 0, 0, 255), (0, 15, 10, 25))
        out = core._vertical_motion_blur(img, 5)
        self.assertEqual(out.size, img.size)
        px = out.load()
        for y in range(40):
            r, g, b, a = px[0, y]
            if a > 0:
                self.assertGreaterEqual(r, 250)
        before = sum(img.getchannel("A").getdata())
        after = sum(out.getchannel("A").getdata())
        self.assertLess(abs(before - after), 40 * 10)


if __name__ == "__main__":
    unittest.main()
