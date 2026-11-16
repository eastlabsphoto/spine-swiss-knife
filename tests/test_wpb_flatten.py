import json
import os
import tempfile
import unittest

from spine_swiss_knife.flatten_core import (
    build_flatten_plan, detect_conflicts, flatten_images, scan_subfolder_images,
)
from spine_swiss_knife.spine_json import image_key_from_path
from spine_swiss_knife.unused_core import compare_images, json_image_keys


def touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x")


def skel():
    return {"skeleton": {"spine": "3.8.99"}, "bones": [{"name": "root"}],
            "slots": [{"name": "s", "bone": "root"}],
            "skins": [{"name": "default", "attachments": {"s": {
                "a": {"path": "fx/spark"}, "b": {"name": "fx/glow"},
                "fx/plain": {}}}, "bones": ["root"]}]}


class FlattenTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.td.name, "images")
        self.json = os.path.join(self.td.name, "s.json")
        with open(self.json, "w") as f:
            json.dump(skel(), f)

    def tearDown(self):
        self.td.cleanup()

    def plan(self, suffix=True):
        files = scan_subfolder_images(self.root)
        return build_flatten_plan(self.root, files, detect_conflicts(self.root, files), suffix)

    def test_flatten_updates_json_and_moves(self):
        for n in ("spark", "glow", "plain"):
            touch(os.path.join(self.root, "fx", n + ".png"))
        r = flatten_images(self.root, self.json, self.plan())
        self.assertEqual(r["moved"], 3)
        self.assertEqual(r["json_updated"], 3)
        self.assertTrue(r["needs_repack"])
        self.assertTrue(os.path.isfile(self.json + ".backup"))
        data = json.load(open(self.json))
        atts = data["skins"][0]["attachments"]["s"]
        self.assertEqual(atts["a"]["path"], "spark")
        self.assertEqual(atts["b"]["path"], "glow")
        self.assertEqual(atts["fx/plain"]["path"], "plain")
        self.assertEqual(data["skins"][0]["bones"], ["root"])  # extras preserved
        self.assertTrue(os.path.isfile(os.path.join(self.root, "spark.png")))

    def test_blocked_destination_leaves_json_untouched(self):
        touch(os.path.join(self.root, "fx", "spark.png"))
        files = scan_subfolder_images(self.root)
        plan = build_flatten_plan(self.root, files, {}, True)
        touch(os.path.join(self.root, "spark.png"))  # appears after planning
        before = open(self.json).read()
        r = flatten_images(self.root, self.json, plan)
        self.assertEqual(r["moved"], 0)
        self.assertEqual(len(r["blocked"]), 1)
        self.assertEqual(open(self.json).read(), before)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "fx", "spark.png")))

    def test_conflict_suffix(self):
        touch(os.path.join(self.root, "spark.png"))
        touch(os.path.join(self.root, "fx", "spark.png"))
        plan = self.plan(True)
        self.assertEqual(plan[0]["new_key"], "spark_fx")


class UnusedTests(unittest.TestCase):
    def test_compare(self):
        r = compare_images({"a", "b", "c"}, atlas_regions={"a", "z"}, json_keys={"b"})
        self.assertEqual(r["used"], {"a", "b"})
        self.assertEqual(r["unused"], {"c"})
        self.assertEqual(r["missing"], {"z"})

    def test_json_only(self):
        keys = json_image_keys(skel())
        self.assertEqual(keys, {"fx/spark", "fx/glow", "fx/plain"})
        r = compare_images({"fx/spark", "other"}, None, keys)
        self.assertEqual(r["unused"], {"other"})
        self.assertEqual(r["missing"], {"fx/glow", "fx/plain"})

    def test_windows_style_keys(self):
        disk = {image_key_from_path("fx\\spark.png"), image_key_from_path("fx\\old.PNG")}
        r = compare_images(disk, {"fx/spark"}, None)
        self.assertEqual(r["used"], {"fx/spark"})
        self.assertEqual(r["unused"], {"fx/old"})

    def test_needs_reference(self):
        with self.assertRaises(ValueError):
            compare_images({"a"})


if __name__ == "__main__":
    unittest.main()
