import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from spine_swiss_knife import api, cli, mcp_server

ROOT = Path(__file__).resolve().parent.parent


def skeleton():
    return {
        "skeleton": {"spine": "3.8.99"},
        "bones": [{"name": "root"}, {"name": "used", "parent": "root"},
                  {"name": "dead", "parent": "root"}],
        "slots": [
            {"name": "s1", "bone": "used", "attachment": "a"},
            {"name": "s2", "bone": "used", "attachment": "b", "color": "ffffff00"},
            {"name": "clip", "bone": "used", "attachment": "m"},
        ],
        "skins": [{"name": "default", "attachments": {
            "s1": {"a": {"width": 4, "height": 4}},
            "s2": {"b": {"width": 4, "height": 4}},
            "clip": {"m": {"type": "clipping", "end": "s2", "vertexCount": 4,
                           "vertices": [-50, -50, 50.5, -49, 50, 50, -50, 49]}},
        }}],
        "animations": {"idle": {"bones": {"used": {"rotate": [
            {"time": 0, "angle": 0}, {"time": 0.5, "angle": 5},
            {"time": 1, "angle": 10}]}}}},
    }


def write(d, name, data):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return p


def read(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


class OptimizerTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.d = self._td.name
        self.p = write(self.d, "s.json", skeleton())
        self.orig = read(self.p)

    def tearDown(self):
        self._td.cleanup()

    def test_keyframes(self):
        r = api.optimize_keyframes(self.p)
        self.assertFalse(r["applied"])
        self.assertEqual(r["redundant_keys"], 1)
        self.assertEqual(read(self.p), self.orig)
        r = api.optimize_keyframes(self.p, tolerance=0.1, apply=True)
        self.assertTrue(r["applied"])
        self.assertEqual(r["validation_errors"], [])
        keys = read(self.p)["animations"]["idle"]["bones"]["used"]["rotate"]
        self.assertEqual(len(keys), 2)
        self.assertTrue(os.path.isfile(self.p + ".backup"))
        self.assertTrue(os.path.isdir(os.path.join(self.d, "_ssk_backups")))
        json.dumps(r)

    def test_dead_bones(self):
        r = api.remove_dead_bones(self.p)
        self.assertEqual([c["bone"] for c in r["changes"]], ["dead"])
        self.assertEqual(read(self.p), self.orig)
        r = api.remove_dead_bones(self.p, apply=True)
        self.assertTrue(r["applied"])
        self.assertEqual([b["name"] for b in read(self.p)["bones"]],
                         ["root", "used"])

    def test_hidden(self):
        r = api.fix_hidden_attachments(self.p)
        self.assertEqual(r["hidden_slots"], 1)
        self.assertEqual(read(self.p), self.orig)
        api.fix_hidden_attachments(self.p, apply=True)
        slots = {s["name"]: s for s in read(self.p)["slots"]}
        self.assertIsNone(slots["s2"].get("attachment"))
        self.assertEqual(slots["s1"]["attachment"], "a")

    def test_masks(self):
        r = api.snap_rect_masks(self.p)
        self.assertEqual(r["snappable"], 1)
        self.assertEqual(read(self.p), self.orig)
        api.snap_rect_masks(self.p, tolerance=10.0, apply=True)
        att = read(self.p)["skins"][0]["attachments"]["clip"]["m"]
        self.assertEqual(att["vertices"], [-50, -50, 50, -50, 50, 50, -50, 50])
        self.assertEqual(api.snap_rect_masks(self.p)["snappable"], 0)

    def test_nothing_to_do_does_not_write(self):
        api.remove_dead_bones(self.p, apply=True)
        os.remove(self.p + ".backup")
        r = api.remove_dead_bones(self.p, apply=True)
        self.assertFalse(r["applied"])
        self.assertFalse(os.path.exists(self.p + ".backup"))

    def test_introduced_errors_restore(self):
        before = Path(self.p).read_bytes()

        def bad(data, work):
            data["bones"].append({"name": "x", "parent": "nope"})
        with mock.patch.object(api, "remove_bones", bad):
            with self.assertRaises(api.SpineError):
                api.remove_dead_bones(self.p, apply=True)
        self.assertEqual(Path(self.p).read_bytes(), before)

    def test_unused_images_and_report(self):
        img = os.path.join(self.d, "images")
        os.makedirs(os.path.join(img, "_UNUSED"))
        from PIL import Image
        for n in ("a.png", "zzz.png", os.path.join("_UNUSED", "q.png")):
            Image.new("RGBA", (2, 2)).save(os.path.join(img, n))
        r = api.unused_images(self.p, img)
        self.assertEqual(r["unused"], ["zzz"])
        self.assertEqual(r["missing"], ["b"])
        rep = api.report(self.p, images_dir=img)
        self.assertEqual(rep["counts"]["bones"], 3)
        self.assertEqual(rep["counts"]["total_keys"], 3)
        self.assertEqual(rep["savings"], {"redundant_keys": 1, "dead_bones": 1,
                                          "hidden_slots": 1,
                                          "snappable_masks": 1})
        self.assertEqual(rep["spine_version"], "3.8.99")
        self.assertEqual(rep["unused_images"], 1)
        json.dumps(rep)

    def test_missing_file(self):
        with self.assertRaises(api.SpineError):
            api.report(os.path.join(self.d, "nope.json"))


class BatchTests(unittest.TestCase):
    def test_batch(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, "a.json", skeleton())
            write(d, "b.json", skeleton())
            write(d, "other.json", {"hello": 1})
            with open(os.path.join(d, "broken.json"), "w") as f:
                f.write("{")
            os.makedirs(os.path.join(d, "_ssk_backups"))
            write(os.path.join(d, "_ssk_backups"), "c.json", skeleton())
            os.makedirs(os.path.join(d, "sub"))
            write(os.path.join(d, "sub"), "d.json", skeleton())
            r = api.batch(d)
            self.assertEqual(r["totals"]["files"], 2)
            self.assertEqual(api.batch(d, recursive=True)["totals"]["files"], 3)
            r = api.batch(d, tool="remove_dead_bones")
            self.assertEqual(r["totals"]["dead_bones"], 2)
            self.assertEqual(r["totals"]["applied"], 0)
            r = api.batch(d, tool="remove_dead_bones", apply=True)
            self.assertEqual(r["totals"]["applied"], 2)
            self.assertEqual(len(read(os.path.join(d, "a.json"))["bones"]), 2)
            with self.assertRaises(api.SpineError):
                api.batch(d, tool="export_json")

    def test_failure_is_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, "a.json", skeleton())
            write(d, "b.json", skeleton())
            real = api.report

            def flaky(p, *a, **k):
                if p.endswith("a.json"):
                    raise RuntimeError("boom")
                return real(p, *a, **k)
            with mock.patch.dict(api._BATCH_TOOLS, {"report": flaky}):
                r = api.batch(d)
            self.assertEqual(r["totals"], {"files": 2, "failed": 1})
            self.assertFalse(r["results"][0]["ok"])
            self.assertTrue(r["results"][1]["ok"])


class FrontendTests(unittest.TestCase):
    def test_schema(self):
        tools = {t["name"]: t for t in mcp_server._tools_list()}
        for n in ("optimize_keyframes", "remove_dead_bones",
                  "fix_hidden_attachments", "snap_rect_masks", "unused_images",
                  "report", "batch"):
            self.assertIn(n, tools)
        props = tools["optimize_keyframes"]["inputSchema"]["properties"]
        self.assertEqual(props["tolerance"]["type"], "number")
        self.assertEqual(props["tolerance"]["default"], 0.1)
        self.assertEqual(props["apply"]["type"], "boolean")
        self.assertEqual(
            tools["snap_rect_masks"]["inputSchema"]["properties"]["tolerance"]["type"],
            "number")
        self.assertEqual(tools["batch"]["inputSchema"]["required"], ["folder"])

    def test_cli_float(self):
        with tempfile.TemporaryDirectory() as d:
            p = write(d, "s.json", skeleton())
            args = cli._build_parser().parse_args(
                ["optimize-keyframes", "--json-path", p, "--tolerance", "0.5"])
            self.assertEqual(args.tolerance, 0.5)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["optimize-keyframes", "--json-path", p,
                               "--tolerance", "0.5"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(buf.getvalue())["tolerance"], 0.5)

    def test_mcp_call(self):
        with tempfile.TemporaryDirectory() as d:
            p = write(d, "s.json", skeleton())
            res = mcp_server._call_tool("report", {"json_path": p})
            self.assertNotIn("isError", res)
            self.assertEqual(json.loads(res["content"][0]["text"])["counts"]["bones"], 3)


class NoQtTests(unittest.TestCase):
    def test_mcp_server_does_not_import_pyside(self):
        code = ("import sys; import spine_swiss_knife.mcp_server, "
                "spine_swiss_knife.cli; "
                "assert not any(m.startswith('PySide6') for m in sys.modules)")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
