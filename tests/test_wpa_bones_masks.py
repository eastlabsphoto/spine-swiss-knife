import unittest

from spine_swiss_knife.dead_bones_core import analyze_dead_bones, filter_removable_selection
from spine_swiss_knife.mask_core import analyze_clipping
from spine_swiss_knife.spine_refs import remove_bones


def skeleton():
    return {
        "skeleton": {"spine": "3.8.99"},
        "bones": [{"name": "root"}, {"name": "w", "parent": "root"},
                  {"name": "dead", "parent": "root"}, {"name": "slotbone", "parent": "root"}],
        "slots": [{"name": "s", "bone": "slotbone", "attachment": "m"}],
        "skins": [{"name": "default", "attachments": {"s": {"m": {
            "type": "mesh", "uvs": [0, 0, 1, 0, 1, 1], "triangles": [0, 1, 2], "hull": 3,
            "vertices": [1, 1, 5, 6, 1.0, 1, 1, 7, 8, 1.0, 1, 1, 9, 10, 1.0]}}}}],
        "animations": {},
    }


class DeadBoneTests(unittest.TestCase):
    def test_weighted_only_bone_alive(self):
        dead, alive = analyze_dead_bones(skeleton())
        self.assertEqual([d["name"] for d in dead], ["dead"])
        self.assertIn("w", [a["name"] for a in alive])

    def test_removal_remaps_weights(self):
        d = skeleton()
        remove_bones(d, ["dead"])
        names = [b["name"] for b in d["bones"]]
        verts = d["skins"][0]["attachments"]["s"]["m"]["vertices"]
        self.assertEqual({names[verts[i]] for i in (1, 6, 11)}, {"w"})

    def test_selection_filter(self):
        d = {"bones": [{"name": "root"}, {"name": "a", "parent": "root"}, {"name": "b", "parent": "a"}]}
        keep, skipped = filter_removable_selection(d, {"a"})
        self.assertEqual(keep, set())
        self.assertEqual(skipped, ["a"])
        keep, skipped = filter_removable_selection(d, {"a", "b"})
        self.assertEqual(keep, {"a", "b"})


class MaskTests(unittest.TestCase):
    def test_duplicate_corner_rejected(self):
        att = {"type": "clipping", "vertexCount": 4, "vertices": [0, 0, 100, 0, 100, 100, 92, 94]}
        info = analyze_clipping(att, 10)
        self.assertFalse(info["is_rect_candidate"])

    def test_good_rect(self):
        att = {"type": "clipping", "vertexCount": 4, "vertices": [0, 0, 100, 1, 99, 100, 1, 99]}
        info = analyze_clipping(att, 5)
        self.assertTrue(info["is_rect_candidate"])
        self.assertEqual(len(set(info["snapped_points"])), 4)

    def test_weighted_skipped(self):
        att = {"type": "clipping", "vertexCount": 4,
               "vertices": [1, 0, 0, 0, 1.0] * 4}
        info = analyze_clipping(att, 5)
        self.assertFalse(info["is_rect_candidate"])
        self.assertEqual(info["skip_code"], "weighted")


if __name__ == "__main__":
    unittest.main()
