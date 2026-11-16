import os
import tempfile
import unittest
from copy import deepcopy

from spine_swiss_knife.merge_core import merge_image_dirs, merge_spine_data
from spine_swiss_knife.spine_validate import validate_skeleton


def source(prefix_bones, tc_bone):
    bones = [{"name": "root"}] + [{"name": b, "parent": "root", "x": 1}
                                  for b in prefix_bones]
    n = len(prefix_bones)
    verts = []
    for i in range(3):
        verts += [1, n, 0.0, 0.0, 1.0]  # last own bone
    return {
        "skeleton": {"spine": "3.8.99"},
        "bones": bones,
        "slots": [{"name": "clip", "bone": "root", "attachment": "c"},
                  {"name": "body", "bone": prefix_bones[0], "attachment": "m"},
                  {"name": "endslot", "bone": "root"}],
        "transform": [{"name": "tc", "bones": [prefix_bones[0]], "target": "root"}],
        "events": {"hit": {"int": 1}},
        "skins": [{"name": "default", "attachments": {
            "clip": {"c": {"type": "clipping", "end": "endslot", "vertexCount": 3,
                           "vertices": [0, 0, 1, 0, 1, 1]}},
            "body": {"m": {"type": "mesh", "uvs": [0, 0, 1, 0, 1, 1],
                           "triangles": [0, 1, 2], "vertices": verts, "hull": 3}},
        }, "transform": ["tc"]}],
        "animations": {"idle": {
            "bones": {prefix_bones[0]: {"rotate": [{"time": 0, "angle": 1}]}},
            "slots": {"body": {"attachment": [{"time": 0, "name": "m"}]}},
            "transform": {"tc": [{"time": 0, "mixRotate": 1}]},
            "draworder": [{"time": 0, "offsets": [{"slot": "body", "offset": 1}]}],
            "events": [{"time": 0, "name": "hit"}],
        }},
    }


class MergeTests(unittest.TestCase):
    def merge(self, a, b):
        before = (deepcopy(a), deepcopy(b))
        merged, warns = merge_spine_data([a, b], ["A", "B"], "out")
        self.assertEqual(before, (a, b))
        return merged, dict_codes(warns), warns

    def test_full_merge_validates(self):
        a = source(["a1", "a2"], "a1")
        b = source(["b1", "b2"], "b1")
        merged, codes, _ = self.merge(a, b)
        errs = [i for i in validate_skeleton(merged) if i["severity"] == "error"]
        self.assertEqual(errs, [], errs)
        names = [x["name"] for x in merged["bones"]]
        # weighted mesh of source B points at b2 (its own last bone)
        mesh_b = merged["skins"][0]["attachments"]["body_B"]["m"]
        self.assertEqual(mesh_b["vertices"][1::5], [names.index("b2")] * 3)
        mesh_a = merged["skins"][0]["attachments"]["body"]["m"]
        self.assertEqual(mesh_a["vertices"][1::5], [names.index("a2")] * 3)

    def test_clipping_end_renamed(self):
        merged, _c, _w = self.merge(source(["a1", "a2"], "a1"), source(["b1", "b2"], "b1"))
        self.assertEqual(merged["skins"][0]["attachments"]["clip_B"]["c"]["end"],
                         "endslot_B")

    def test_constraint_timeline_renamed(self):
        merged, _c, _w = self.merge(source(["a1", "a2"], "a1"), source(["b1", "b2"], "b1"))
        self.assertIn("tc_B", merged["animations"]["idle_B"]["transform"])
        self.assertNotIn("tc", merged["animations"]["idle_B"]["transform"])
        self.assertIn("tc_B", merged["skins"][0]["transform"])
        self.assertIn("tc", merged["animations"]["idle"]["transform"])

    def test_lowercase_draworder_renamed(self):
        merged, _c, _w = self.merge(source(["a1", "a2"], "a1"), source(["b1", "b2"], "b1"))
        offs = merged["animations"]["idle_B"]["draworder"][0]["offsets"]
        self.assertEqual(offs[0]["slot"], "body_B")

    def test_bone_difference_warns(self):
        a = source(["a1"], "a1")
        b = source(["a1"], "a1")
        b["bones"][1]["x"] = 5
        b["bones"][1]["rotation"] = 0  # default == absent: not a difference
        _m, codes, warns = self.merge(a, b)
        diffs = [p for c, p in warns if c == "bone_differs"]
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0]["fields"], "x")

    def test_events_shared_when_identical(self):
        a = source(["a1"], "a1")
        b = source(["b1"], "b1")
        merged, codes, _ = self.merge(a, b)
        self.assertEqual(list(merged["events"]), ["hit"])
        self.assertNotIn("event_renamed", codes)
        b["events"]["hit"] = {"int": 2}
        merged, codes, _ = self.merge(a, b)
        self.assertIn("hit_B", merged["events"])
        self.assertEqual(merged["animations"]["idle_B"]["events"][0]["name"], "hit_B")

    def test_skin_extras_union(self):
        a = source(["a1"], "a1")
        b = source(["b1"], "b1")
        a["skins"][0]["bones"] = ["a1"]
        b["skins"][0]["bones"] = ["b1"]
        merged, _c, _w = self.merge(a, b)
        self.assertEqual(merged["skins"][0]["bones"], ["a1", "b1"])
        self.assertEqual(merged["skins"][0]["transform"], ["tc", "tc_B"])

    def test_image_merge_windows_style_and_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            dirs = []
            for i, content in enumerate([b"one", b"two"]):
                d = os.path.join(td, "s%d" % i)
                os.makedirs(os.path.join(d, "sub"))
                with open(os.path.join(d, "sub", "x.png"), "wb") as f:
                    f.write(content)
                dirs.append(d)
            maps, warns = merge_image_dirs(dirs, ["A", "B"], os.path.join(td, "out"))
            self.assertEqual(maps[1], {"sub/x": "sub/x_B"})
            self.assertTrue(os.path.isfile(os.path.join(td, "out", "sub", "x_B.png")))
            self.assertEqual(warns[0][0], "image_renamed")


def dict_codes(warns):
    return [c for c, _p in warns]


if __name__ == "__main__":
    unittest.main()
