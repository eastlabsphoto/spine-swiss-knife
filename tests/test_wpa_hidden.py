import unittest

from spine_swiss_knife.hidden_attachments_core import (
    analyze_hidden_attachments, fix_hidden_attachments,
)


def skel(slot_color="ffffffff", bones=None, anims=None):
    return {
        "skeleton": {"spine": "4.2.43"},
        "bones": bones or [{"name": "root"}, {"name": "b", "parent": "root"}],
        "slots": [{"name": "s", "bone": "b", "attachment": "img", "color": slot_color}],
        "animations": anims or {},
    }


def fix(d):
    c, det, _w = analyze_hidden_attachments(d)
    return c, det, fix_hidden_attachments(d, c, det)


class HiddenTests(unittest.TestCase):
    def test_zero_scale_pop_in(self):
        d = skel(bones=[{"name": "root"}, {"name": "b", "parent": "root", "scaleX": 0, "scaleY": 0}],
                 anims={"pop": {"bones": {"b": {"scale": [{"x": 0, "y": 0}, {"time": 1, "x": 1, "y": 1}]}}}})
        c, det, (slots, kfs) = fix(d)
        self.assertEqual(kfs, 1)
        self.assertEqual(d["animations"]["pop"]["slots"]["s"]["attachment"][0], {"time": 0, "name": "img"})
        self.assertIsNone(d["slots"][0]["attachment"])

    def test_zero_scale_inherited_blocked_by_no_scale(self):
        d = skel(bones=[{"name": "root", "scaleX": 0}, {"name": "b", "parent": "root", "inherit": "noScale"}])
        c, _det, _ = analyze_hidden_attachments(d)
        self.assertEqual(c, [])

    def test_alpha_timeline_4x(self):
        d = skel("ffffff00", anims={"fade": {"slots": {"s": {"alpha": [{"value": 0}, {"time": 1, "value": 1}]}}}})
        _c, _det, (_s, kfs) = fix(d)
        self.assertEqual(kfs, 1)

    def test_rgba2_light_alpha(self):
        d = skel("ffffff00", anims={"x": {"slots": {"s": {"rgba2": [{"light": "ffffffff", "dark": "000000"}]}}}})
        _c, _det, (_s, kfs) = fix(d)
        self.assertEqual(kfs, 1)

    def test_attachment_key_only_later(self):
        d = skel("ffffff00", anims={"a": {"slots": {"s": {
            "rgba": [{"color": "ffffffff"}],
            "attachment": [{"time": 0.5, "name": "other"}]}}}})
        _c, det, (_s, kfs) = fix(d)
        self.assertEqual(kfs, 1)
        keys = d["animations"]["a"]["slots"]["s"]["attachment"]
        self.assertEqual([k["time"] for k in keys], [0, 0.5])
        self.assertEqual(keys[0]["name"], "img")

    def test_already_ok(self):
        d = skel("ffffff00", anims={"a": {"slots": {"s": {
            "rgba": [{"color": "ffffffff"}],
            "attachment": [{"name": "img"}]}}}})
        _c, det, (_s, kfs) = fix(d)
        self.assertEqual(kfs, 0)
        self.assertEqual(det[0]["action"], "already_ok")

    def test_untouched_when_never_revealed(self):
        d = skel("ffffff00", anims={"a": {"slots": {"s": {"rgba": [{"color": "ffffff00"}]}},
                                          "bones": {"b": {"rotate": [{"value": 1}]}}}})
        _c, det, (_s, kfs) = fix(d)
        self.assertEqual(kfs, 0)
        self.assertEqual(det, [])
        self.assertNotIn("attachment", d["animations"]["a"]["slots"]["s"])

    def test_both_causes_need_both_lifted(self):
        d = skel("ffffff00", bones=[{"name": "root"}, {"name": "b", "parent": "root", "scaleX": 0}],
                 anims={"a": {"slots": {"s": {"rgba": [{"color": "ffffffff"}]}}}})
        _c, det, _ = fix(d)
        self.assertEqual(det, [])


if __name__ == "__main__":
    unittest.main()
