import json
import os
import tempfile
import unittest

from PIL import Image

from spine_swiss_knife.downscale_core import (
    build_plan, process_plan, resize_image, unique_destination,
)


def mk(path, size=(100, 80), mode="RGBA"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new(mode, size).save(path)


class DownscaleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.images = os.path.join(self.root, "images")
        self.json = os.path.join(self.root, "sk.json")
        self.spine = {
            "skeleton": {"spine": "3.8.99"},
            "slots": [{"name": "s", "bone": "root"}],
            "skins": [{"name": "default", "attachments": {"s": {
                "sub/img": {"width": 100, "height": 80, "scaleX": 2},
                "m": {"type": "mesh", "path": "sub/mesh", "width": 100, "height": 80,
                      "uvs": [0, 0, 1, 0, 1, 1], "triangles": [0, 1, 2], "vertices": [0, 0, 1, 0, 1, 1]},
            }}}],
        }
        with open(self.json, "w") as f:
            json.dump(self.spine, f)
        self.src1 = os.path.join(self.images, "_DOWNSCALE", "50", "sub", "img.png")
        self.src2 = os.path.join(self.images, "_DOWNSCALE", "50", "sub", "mesh.png")
        mk(self.src1)
        mk(self.src2)

    def tearDown(self):
        self.tmp.cleanup()

    def test_plan_matching_subfolder_and_mesh(self):
        plan = build_plan(os.path.join(self.images, "_DOWNSCALE"), self.images, self.spine)
        keys = {p["image_key"]: p for p in plan}
        self.assertEqual(set(keys), {"sub/img", "sub/mesh"})
        self.assertTrue(keys["sub/img"]["matches"])
        self.assertTrue(keys["sub/mesh"]["matches"])
        self.assertEqual((keys["sub/img"]["new_w"], keys["sub/img"]["new_h"]), (50, 40))

    def test_process_scale_and_move(self):
        plan = build_plan(os.path.join(self.images, "_DOWNSCALE"), self.images, self.spine)
        res = process_plan(self.json, self.images, plan)
        self.assertTrue(res["json_saved"])
        self.assertEqual(res["moved"], 2)
        data = json.load(open(self.json))
        atts = data["skins"][0]["attachments"]["s"]
        self.assertEqual(atts["sub/img"]["scaleX"], 4)
        self.assertEqual((atts["sub/img"]["width"], atts["sub/img"]["height"]), (50, 40))
        self.assertNotIn("scaleX", atts["m"])
        self.assertEqual(atts["m"]["width"], 50)
        self.assertFalse(os.path.exists(self.src1))
        self.assertTrue(os.path.isfile(os.path.join(self.images, "_ORIGINALS", "sub", "img.png")))
        with Image.open(os.path.join(self.images, "sub", "img.png")) as im:
            self.assertEqual(im.size, (50, 40))
        self.assertTrue(os.path.isfile(self.json + ".backup"))

    def test_existing_original_not_overwritten(self):
        mk(os.path.join(self.images, "_ORIGINALS", "sub", "img.png"), (7, 7))
        plan = build_plan(os.path.join(self.images, "_DOWNSCALE"), self.images, self.spine)
        process_plan(self.json, self.images, plan)
        with Image.open(os.path.join(self.images, "_ORIGINALS", "sub", "img.png")) as im:
            self.assertEqual(im.size, (7, 7))
        self.assertTrue(os.path.isfile(os.path.join(self.images, "_ORIGINALS", "sub", "img_1.png")))

    def test_save_failure_keeps_sources(self):
        import spine_swiss_knife.downscale_core as dc
        plan = build_plan(os.path.join(self.images, "_DOWNSCALE"), self.images, self.spine)
        orig = dc.save_spine_json
        dc.save_spine_json = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        try:
            res = process_plan(self.json, self.images, plan)
        finally:
            dc.save_spine_json = orig
        self.assertFalse(res["json_saved"])
        self.assertTrue(os.path.exists(self.src1))
        self.assertFalse(os.path.exists(os.path.join(self.images, "_ORIGINALS")))

    def test_palette_image_resized_lanczos(self):
        p = os.path.join(self.root, "p.png")
        img = Image.new("P", (4, 4))
        img.putpalette([0, 0, 0, 255, 255, 255] + [0] * 756)
        img.putdata([0, 1] * 8)
        img.save(p)
        out = os.path.join(self.root, "o.png")
        resize_image(p, out, 50)
        with Image.open(out) as im:
            self.assertEqual(im.size, (2, 2))
            self.assertEqual(im.mode, "RGBA")

    def test_unique_destination(self):
        p = os.path.join(self.root, "x.png")
        open(p, "w").close()
        self.assertTrue(unique_destination(p).endswith("x_1.png"))


if __name__ == "__main__":
    unittest.main()
