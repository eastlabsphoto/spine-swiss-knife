import json
import os
import shutil
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402

from spine_swiss_knife import texture_unpacker as tu  # noqa: E402

ATLAS_3X = """
page.png
size: 64,64
format: RGBA8888
filter: Linear,Linear
repeat: none
head.v2
  rotate: false
  xy: 2, 4
  size: 10, 8
  orig: 12, 10
  offset: 1, 1
  index: -1
walk
  rotate: true
  xy: 20, 4
  size: 10, 8
  orig: 10, 8
  offset: 0, 0
  index: 1
walk
  rotate: false
  xy: 40, 4
  size: 6, 6
  orig: 6, 6
  offset: 0, 0
  index: 2
"""

ATLAS_4X = """page.png
size:64,64
filter:Linear,Linear
pma:true
head.v2
bounds:2,4,10,8
offsets:1,1,12,10
walk
bounds:20,4,10,8
rotate:90
index:1
walk
bounds:40,4,6,6
index:2
"""


def write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class AtlasParseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_3x_and_4x_give_same_frames(self):
        p3 = os.path.join(self.tmp, "a3.atlas")
        p4 = os.path.join(self.tmp, "a4.atlas")
        write(p3, ATLAS_3X)
        write(p4, ATLAS_4X)
        pages3 = tu.parse_spine_atlas(p3)
        pages4 = tu.parse_spine_atlas(p4)
        self.assertEqual(len(pages3), 1)
        self.assertEqual(len(pages4), 1)
        self.assertEqual(pages3[0][0], "page.png")
        names = [f["filename"] for f in pages4[0][1]]
        self.assertEqual(names, ["head.v2", "walk", "walk"])  # no bogus regions
        keys = ("filename", "x", "y", "w", "h", "orig_w", "orig_h",
                "offset_x", "offset_y", "rotate", "index")
        for f3, f4 in zip(pages3[0][1], pages4[0][1]):
            for k in keys:
                if k in ("orig_w", "orig_h") and f4["filename"] == "walk":
                    continue  # 4.x sample omits offsets -> defaults to w/h
                self.assertEqual(f3[k], f4[k], k)
        self.assertEqual(pages4[0][2], 1.0)

    def test_output_names(self):
        self.assertEqual(tu.frame_output_name({"filename": "head.v2", "index": -1}), "head.v2")
        self.assertEqual(tu.frame_output_name({"filename": "walk", "index": 3}), "walk_3")

    def test_extract_keeps_dotted_names_and_indices(self):
        sheet = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        sheet.paste((255, 0, 0, 255), (2, 4, 12, 12))
        sheet.paste((0, 255, 0, 255), (20, 4, 30, 12))   # rotated: occupies 8 wide x 10 tall? (h x w)
        sheet.paste((0, 0, 255, 255), (40, 4, 46, 10))
        sheet.save(os.path.join(self.tmp, "page.png"))
        write(os.path.join(self.tmp, "a.atlas"), ATLAS_4X)
        out = os.path.join(self.tmp, "out")
        n = tu.extract_spine_frames(os.path.join(self.tmp, "a.atlas"), out)
        self.assertEqual(n, 3)
        self.assertEqual(sorted(os.listdir(out)), ["head.v2.png", "walk_1.png", "walk_2.png"])

    def test_extract_does_not_escape_output_dir(self):
        sheet = Image.new("RGBA", (16, 16), (255, 0, 0, 255))
        sheet.save(os.path.join(self.tmp, "p.png"))
        write(os.path.join(self.tmp, "e.atlas"),
              "p.png\nsize:16,16\n../evil\nbounds:0,0,4,4\n")
        out = os.path.join(self.tmp, "out")
        os.makedirs(out)
        n = tu.extract_spine_frames(os.path.join(self.tmp, "e.atlas"), out)
        self.assertEqual(n, 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.png")))

    def test_spine_atlas_rotated_region_is_restored(self):
        # sprite 4 wide x 2 tall: red left half, blue right half
        sprite = Image.new("RGBA", (4, 2), (255, 0, 0, 255))
        sprite.paste((0, 0, 255, 255), (2, 0, 4, 2))
        stored = sprite.rotate(90, expand=True)  # stored rotated 90 CCW -> 2 wide x 4 tall
        sheet = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
        sheet.paste(stored, (3, 5))
        sheet.save(os.path.join(self.tmp, "p.png"))
        write(os.path.join(self.tmp, "r.atlas"),
              "p.png\nsize:16,16\nspr\nbounds:3,5,4,2\nrotate:90\n")
        out = os.path.join(self.tmp, "out")
        tu.extract_spine_frames(os.path.join(self.tmp, "r.atlas"), out)
        res = Image.open(os.path.join(out, "spr.png")).convert("RGBA")
        self.assertEqual(res.size, (4, 2))
        self.assertEqual(list(res.getdata()), list(sprite.getdata()))


class TexturePackerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sprite = Image.new("RGBA", (4, 2), (255, 0, 0, 255))
        self.sprite.paste((0, 0, 255, 255), (2, 0, 4, 2))
        sheet = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
        sheet.paste(self.sprite, (0, 0))
        # rotated clockwise on the sheet (TexturePacker): 2 wide x 4 tall
        sheet.paste(self.sprite.rotate(-90, expand=True), (8, 8))
        sheet.save(os.path.join(self.tmp, "sheet.png"))

    def _frames(self, frame_w, frame_h):
        return [
            {"filename": "plain.png", "frame": {"x": 0, "y": 0, "w": 4, "h": 2},
             "rotated": False, "trimmed": False,
             "spriteSourceSize": {"x": 0, "y": 0, "w": 4, "h": 2},
             "sourceSize": {"w": 4, "h": 2}},
            {"filename": "sub/rot.png", "frame": {"x": 8, "y": 8, "w": frame_w, "h": frame_h},
             "rotated": True, "trimmed": False,
             "spriteSourceSize": {"x": 0, "y": 0, "w": 4, "h": 2},
             "sourceSize": {"w": 4, "h": 2}},
        ]

    def _run(self, data):
        jp = os.path.join(self.tmp, "sheet.json")
        write(jp, json.dumps(data))
        out = os.path.join(self.tmp, "out")
        count, _w = tu.extract_json_frames(data, out, jp)
        return count, out

    def _check(self, out):
        for rel in ("plain.png", os.path.join("sub", "rot.png")):
            img = Image.open(os.path.join(out, rel)).convert("RGBA")
            self.assertEqual(img.size, (4, 2), rel)
            self.assertEqual(list(img.getdata()), list(self.sprite.getdata()), rel)

    def test_array_format_with_rotated_frame_spec_convention(self):
        data = {"frames": self._frames(4, 2), "meta": {"image": "sheet.png", "size": {"w": 16, "h": 16}}}
        count, out = self._run(data)
        self.assertEqual(count, 2)
        self._check(out)

    def test_array_format_with_swapped_frame_size(self):
        data = {"frames": self._frames(2, 4), "meta": {"image": "sheet.png", "size": {"w": 16, "h": 16}}}
        count, out = self._run(data)
        self.assertEqual(count, 2)
        self._check(out)

    def test_hash_format(self):
        frames = {f["filename"]: {k: v for k, v in f.items() if k != "filename"}
                  for f in self._frames(4, 2)}
        data = {"frames": frames, "meta": {"image": "sheet.png", "size": {"w": 16, "h": 16}}}
        count, out = self._run(data)
        self.assertEqual(count, 2)
        self._check(out)


if __name__ == "__main__":
    unittest.main()
