import json
import os
import tempfile
import time
import unittest

from spine_swiss_knife import spine_json as sj
from spine_swiss_knife.spine_json import clip_end_marker_after_slot


class ClipEndMarkerTests(unittest.TestCase):
    def test_drawable_region_ends_after_end_slot_renders(self):
        self.assertTrue(
            clip_end_marker_after_slot("WhiteGradient-3", {"type": "region"})
        )

    def test_mesh_ends_after_end_slot_renders(self):
        self.assertTrue(
            clip_end_marker_after_slot("mesh_attachment", {"type": "mesh"})
        )

    def test_empty_end_slot_can_end_immediately(self):
        self.assertFalse(
            clip_end_marker_after_slot(None, None)
        )

    def test_clipping_attachment_end_can_happen_before_new_clip_starts(self):
        self.assertFalse(
            clip_end_marker_after_slot("mask", {"type": "clipping"})
        )

    def test_unknown_attachment_type_is_not_treated_as_drawable(self):
        self.assertFalse(
            clip_end_marker_after_slot("point", {"type": "point"})
        )


class VersionTests(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(sj.spine_version({"skeleton": {"spine": "4.2.43"}}), (4, 2))
        self.assertEqual(sj.spine_version({"skeleton": {"spine": "3.8.99-from-4.2.43"}}), (3, 8))
        self.assertEqual(sj.spine_version({}), (0, 0))
        self.assertEqual(sj.spine_version({"skeleton": {"spine": "x"}}), (0, 0))
        self.assertTrue(sj.is_spine4({"skeleton": {"spine": "4.0.1"}}))
        self.assertFalse(sj.is_spine4({"skeleton": {"spine": "3.8.99"}}))


class SaveBackupTests(unittest.TestCase):
    def test_atomic_save_roundtrip_no_temp_left(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.json")
            sj.save_spine_json(p, {"a": 1})
            sj.save_spine_json(p, {"a": 2})
            self.assertEqual(sj.load_spine_json(p), {"a": 2})
            self.assertEqual(os.listdir(d), ["a.json"])

    def test_backup_and_prune(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.json")
            for i in range(5):
                sj.save_spine_json(p, {"v": i})
                self.assertEqual(sj.create_backup(p, keep=3), p + ".backup")
            self.assertEqual(sj.load_spine_json(p + ".backup"), {"v": 4})
            hist = sj.list_backups(p)
            self.assertEqual(len(hist), 3)
            self.assertEqual(hist, sorted(hist, reverse=True))
            self.assertEqual(sj.load_spine_json(hist[0]), {"v": 4})
            self.assertEqual(sj.list_backups(os.path.join(d, "other.json")), [])


class ImageKeyTests(unittest.TestCase):
    def test_attachment_image_key(self):
        self.assertEqual(sj.attachment_image_key("k", {"path": "p", "name": "n"}), "p")
        self.assertEqual(sj.attachment_image_key("k", {"name": "n"}), "n")
        self.assertEqual(sj.attachment_image_key("k", {}), "k")

    def test_image_key_from_path(self):
        self.assertEqual(sj.image_key_from_path("sub\\dir\\img.png"), "sub/dir/img")
        self.assertEqual(sj.image_key_from_path("sub/dir/a.b.PNG"), "sub/dir/a.b")
        self.assertEqual(sj.image_key_from_path("a.b"), "a.b")

    def test_iter_image_attachments(self):
        skins = {"default": {"s": {"a": {}, "b": {"type": "mesh"}, "c": {"type": "clipping"},
                                   "d": {"type": "linkedmesh"}}}}
        self.assertEqual([x[2] for x in sj.iter_image_attachments(skins)], ["a", "b", "d"])


class SkinAndTimelineTests(unittest.TestCase):
    def test_skin_entries_original_objects(self):
        att = {"s": {"a": {}}}
        d = {"skins": [{"name": "x", "attachments": att, "bones": ["b"]}]}
        (name, got), = sj.skin_entries(d)
        self.assertEqual(name, "x")
        self.assertIs(got, att)
        d2 = {"skins": {"default": att}}
        self.assertIs(sj.skin_entries(d2)[0][1], att)

    def test_draw_order_accessors(self):
        lst = [{"time": 0}]
        self.assertIs(sj.get_draw_order_timeline({"draworder": lst}), lst)
        self.assertEqual(sj.draw_order_key({"draworder": lst}), "draworder")
        self.assertEqual(sj.draw_order_key({"drawOrder": lst}), "drawOrder")
        self.assertIsNone(sj.draw_order_key({}))
        self.assertEqual(sj.get_draw_order_timeline({}), [])

    def test_iter_deform_timelines(self):
        anim = {"ffd": {"d": {"s": {"a": [1]}}}, "deform": {"d": {"s": {"b": [2]}}},
                "attachments": {"d": {"s": {"c": {"deform": [3], "sequence": [4]}}}}}
        got = {(x[2], tuple(x[3])) for x in sj.iter_deform_timelines(anim)}
        self.assertEqual(got, {("a", (1,)), ("b", (2,)), ("c", (3,))})


class WeightTests(unittest.TestCase):
    def test_weighted_rules(self):
        weighted = {"type": "mesh", "uvs": [0] * 6, "hull": 3,
                    "vertices": [1, 0, 1, 1, 1.0, 2, 1, 0, 0, .5, 3, 0, 0, .5,
                                 1, 4, 0, 0, 1.0]}
        self.assertTrue(sj.is_weighted(weighted))
        plain = {"type": "mesh", "uvs": [0] * 6, "vertices": [0] * 6}
        self.assertFalse(sj.is_weighted(plain))
        clip = {"type": "clipping", "vertexCount": 3, "vertices": [0] * 6}
        self.assertFalse(sj.is_weighted(clip))
        self.assertEqual(sj.attachment_vertex_count(clip), 3)
        self.assertEqual(sj.attachment_vertex_count(plain), 3)
        self.assertIsNone(sj.attachment_vertex_count({"type": "region"}))
        self.assertEqual(sj.weighted_bone_indices(weighted["vertices"]), {0, 1, 3, 4})

    def test_remap(self):
        v = [1, 0, 1, 1, 1.0, 2, 1, 0, 0, .5, 3, 0, 0, .5]
        out = sj.remap_weighted_vertices(v, {0: 5, 1: 6, 3: 7})
        self.assertEqual(out, [1, 5, 1, 1, 1.0, 2, 6, 0, 0, .5, 7, 0, 0, .5])
        self.assertEqual(v[1], 0)  # input untouched
        with self.assertRaises(KeyError):
            sj.remap_weighted_vertices(v, {0: 0})
        with self.assertRaises(ValueError):
            sj.remap_weighted_vertices([2, 0, 1, 1, 1.0], {0: 0})

    def test_iter_all_and_weighted(self):
        w = {"type": "mesh", "uvs": [0, 0], "vertices": [1, 0, 0, 0, 1.0]}
        d = {"skins": [{"name": "default", "attachments": {"s": {"a": w, "b": {}}}}]}
        self.assertEqual(len(list(sj.iter_all_attachments(d))), 2)
        self.assertEqual(list(sj.iter_weighted_attachments(d)), [w])


if __name__ == "__main__":
    unittest.main()
