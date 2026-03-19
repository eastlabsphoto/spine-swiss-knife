import unittest

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


if __name__ == "__main__":
    unittest.main()
