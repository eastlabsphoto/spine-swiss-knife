import os
import tempfile
import unittest

from spine_swiss_knife.atlas_parser import collect_images, parse_atlas, parse_atlas_pages

ATLAS_3X = """
page.png
size: 1024,1024
format: RGBA8888
filter: Linear,Linear
repeat: none
a/b.c
  rotate: true
  xy: 2, 2
  size: 100, 50
  orig: 104, 54
  offset: 2, 2
  index: -1
seq
  rotate: false
  xy: 10, 10
  size: 5, 5
  orig: 5, 5
  offset: 0, 0
  index: 1
seq
  rotate: false
  xy: 20, 10
  size: 5, 5
  orig: 5, 5
  offset: 0, 0
  index: 2

page2.png
size: 64,64
format: RGBA8888
filter: Linear,Linear
repeat: none
other
  rotate: false
  xy: 0, 0
  size: 8, 8
  orig: 8, 8
  offset: 0, 0
  index: -1
"""

ATLAS_4X = """page.png
size:1024,1024
filter:Linear,Linear
pma:true
scale:0.5
region/name
bounds:2,2,100,50
offsets:2,2,104,54
rotate:90
index:3
plain
bounds:5,6,7,8
"""

ATLAS_4X_INDENTED = """page.png
  size: 32, 32
  pma: false
  scale: 2
  r1
    bounds: 1, 2, 3, 4
    rotate: false
"""


def _write(d, text, crlf=False, bom=False):
    p = os.path.join(d, "t.atlas")
    if crlf:
        text = text.replace("\n", "\r\n")
    with open(p, "wb") as f:
        f.write((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
    return p


class AtlasTests(unittest.TestCase):
    def test_3x(self):
        with tempfile.TemporaryDirectory() as d:
            pages = parse_atlas_pages(_write(d, ATLAS_3X))
            self.assertEqual([p["name"] for p in pages], ["page.png", "page2.png"])
            self.assertEqual(pages[0]["size"], (1024, 1024))
            r = pages[0]["regions"]
            self.assertEqual([x["name"] for x in r], ["a/b.c", "seq", "seq"])
            self.assertEqual((r[0]["x"], r[0]["y"], r[0]["w"], r[0]["h"]), (2, 2, 100, 50))
            self.assertTrue(r[0]["rotate"])
            self.assertEqual((r[0]["orig_w"], r[0]["offset_x"]), (104, 2))
            self.assertEqual([r[1]["index"], r[2]["index"]], [1, 2])
            self.assertEqual(parse_atlas(_write(d, ATLAS_3X)), {"a/b.c", "seq", "other"})

    def test_4x_regression(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, ATLAS_4X)
            self.assertEqual(parse_atlas(p), {"region/name", "plain"})
            page = parse_atlas_pages(p)[0]
            self.assertEqual(page["size"], (1024, 1024))
            self.assertTrue(page["pma"])
            self.assertEqual(page["scale"], 0.5)
            r = page["regions"][0]
            self.assertEqual((r["x"], r["y"], r["w"], r["h"]), (2, 2, 100, 50))
            self.assertEqual((r["orig_w"], r["orig_h"], r["offset_x"], r["offset_y"]), (104, 54, 2, 2))
            self.assertTrue(r["rotate"])
            self.assertEqual(r["degrees"], 90)
            self.assertEqual(r["index"], 3)
            p2 = page["regions"][1]
            self.assertEqual((p2["orig_w"], p2["orig_h"], p2["offset_x"], p2["index"]), (7, 8, 0, -1))
            self.assertFalse(p2["rotate"])

    def test_4x_indented(self):
        with tempfile.TemporaryDirectory() as d:
            page = parse_atlas_pages(_write(d, ATLAS_4X_INDENTED))[0]
            self.assertEqual(page["scale"], 2.0)
            self.assertFalse(page["pma"])
            self.assertEqual([r["name"] for r in page["regions"]], ["r1"])
            self.assertEqual(page["regions"][0]["w"], 3)

    def test_crlf_and_bom(self):
        with tempfile.TemporaryDirectory() as d:
            for text in (ATLAS_3X, ATLAS_4X):
                a = parse_atlas(_write(d, text))
                b = parse_atlas(_write(d, text, crlf=True, bom=True))
                self.assertEqual(a, b)


class CollectImagesTests(unittest.TestCase):
    def test_collect(self):
        with tempfile.TemporaryDirectory() as d:
            for rel in ("a.png", "sub/b.c.PNG", "_UNUSED/x.png", "_ssk_backups/y.png",
                        "sub/_DOWNSCALE/z.png", "sub/readme.txt"):
                p = os.path.join(d, *rel.split("/"))
                os.makedirs(os.path.dirname(p), exist_ok=True)
                open(p, "wb").close()
            self.assertEqual(set(collect_images(d)), {"a", "sub/b.c"})


if __name__ == "__main__":
    unittest.main()
