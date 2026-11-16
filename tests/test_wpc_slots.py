import copy
import os
import tempfile
import unittest

try:
    import PySide6  # noqa: F401
    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False

from spine_swiss_knife.spine_refs import apply_draw_order_offsets


def _data():
    return {
        "skeleton": {"spine": "4.1.24"},
        "bones": [{"name": "root"}],
        "slots": [
            {"name": "glow_multiply", "bone": "root", "blend": "multiply"},
            {"name": "glow_additive", "bone": "root", "blend": "additive"},
            {"name": "plain", "bone": "root"},
        ],
        "path": [{"name": "p", "bones": ["root"], "target": "plain"}],
        "skins": [{"name": "default", "attachments": {
            "glow_multiply": {"g": {"type": "clipping", "end": "plain",
                                    "vertexCount": 0, "vertices": []}},
            "glow_additive": {"h": {"width": 1, "height": 1}},
            "plain": {"p": {"width": 1, "height": 1}},
        }}],
        "animations": {
            "a": {
                "slots": {"plain": {"attachment": [{"name": "p"}]}},
                "draworder": [{"time": 0, "offsets": [
                    {"slot": "glow_multiply", "offset": 2}]}],
                "attachments": {"default": {"plain": {"p": {"deform": [
                    {"time": 0, "vertices": [0, 0]}]}}}},
            }
        },
    }


@unittest.skipUnless(HAS_QT, "PySide6 not installed")
class RenamerTests(unittest.TestCase):
    def test_all_reference_kinds_are_renamed(self):
        from spine_swiss_knife.slot_renamer import apply_renames
        d = _data()
        apply_renames(d, {"plain": "plain_normal",
                          "glow_multiply": "glow_multiply_x"})
        self.assertEqual([s["name"] for s in d["slots"]],
                         ["glow_multiply_x", "glow_additive", "plain_normal"])
        self.assertEqual(d["path"][0]["target"], "plain_normal")
        att = d["skins"][0]["attachments"]
        self.assertIn("plain_normal", att)
        self.assertIn("glow_multiply_x", att)
        self.assertEqual(att["glow_multiply_x"]["g"]["end"], "plain_normal")
        anim = d["animations"]["a"]
        self.assertIn("plain_normal", anim["slots"])
        # lowercase draworder
        self.assertEqual(anim["draworder"][0]["offsets"][0]["slot"],
                         "glow_multiply_x")
        # 4.1+ attachment timelines
        self.assertIn("plain_normal", anim["attachments"]["default"])
        self.assertNotIn("plain", anim["attachments"]["default"])

    def test_collision_is_detected_and_apply_raises(self):
        from spine_swiss_knife.slot_renamer import apply_renames, find_collisions
        d = _data()
        before = copy.deepcopy(d)
        rename = {"glow_multiply": "glow_additive"}
        self.assertTrue(find_collisions(d, rename))
        with self.assertRaises(ValueError):
            apply_renames(d, rename)
        self.assertEqual(d, before)

    def test_chain_is_legal(self):
        from spine_swiss_knife.slot_renamer import apply_renames, find_collisions
        d = _data()
        rename = {"glow_multiply": "glow_additive",
                  "glow_additive": "glow_normal"}
        self.assertEqual(find_collisions(d, rename), [])
        apply_renames(d, rename)
        self.assertEqual([s["name"] for s in d["slots"]],
                         ["glow_additive", "glow_normal", "plain"])
        self.assertEqual(sorted(d["skins"][0]["attachments"]),
                         ["glow_additive", "glow_normal", "plain"])


@unittest.skipUnless(HAS_QT, "PySide6 not installed")
class BlendSwitchTests(unittest.TestCase):
    def test_collision_keeps_name_but_switches_blend(self):
        from spine_swiss_knife.blend_switcher import switch_blend_modes
        d = _data()
        switched, kept = switch_blend_modes(d, ["glow_multiply"], "additive")
        self.assertEqual(switched, 1)
        self.assertEqual(kept, ["glow_multiply"])
        names = [s["name"] for s in d["slots"]]
        self.assertEqual(names, ["glow_multiply", "glow_additive", "plain"])
        self.assertEqual(d["slots"][0]["blend"], "additive")
        # skin data of both slots is intact
        self.assertEqual(sorted(d["skins"][0]["attachments"]),
                         ["glow_additive", "glow_multiply", "plain"])

    def test_without_collision_slot_is_renamed_everywhere(self):
        from spine_swiss_knife.blend_switcher import switch_blend_modes
        d = _data()
        d["slots"][1]["name"] = "other"
        d["skins"][0]["attachments"]["other"] = \
            d["skins"][0]["attachments"].pop("glow_additive")
        switched, kept = switch_blend_modes(d, ["glow_multiply"], "additive")
        self.assertEqual((switched, kept), (1, []))
        self.assertEqual(d["slots"][0]["name"], "glow_additive")
        self.assertIn("glow_additive", d["skins"][0]["attachments"])
        off = d["animations"]["a"]["draworder"][0]["offsets"][0]
        self.assertEqual(off["slot"], "glow_additive")

    def test_swap_style_chain_is_applied(self):
        from spine_swiss_knife.blend_switcher import switch_blend_modes
        d = {"bones": [{"name": "root"}], "skins": [],
             "slots": [
                 {"name": "fx", "bone": "root", "blend": "multiply"},
                 {"name": "fx_additive", "bone": "root", "blend": "screen"},
             ]}
        # fx -> fx_additive and fx_additive -> fx_additive (unchanged):
        # collision on the second name, both keep their names
        switched, kept = switch_blend_modes(d, ["fx", "fx_additive"], "additive")
        self.assertEqual(switched, 2)
        self.assertEqual([s["name"] for s in d["slots"]], ["fx", "fx_additive"])
        self.assertEqual(kept, ["fx"])
        self.assertTrue(all(s["blend"] == "additive" for s in d["slots"]))
        # two slots of the same base to the same target
        d = {"bones": [{"name": "root"}], "skins": [],
             "slots": [
                 {"name": "a_multiply", "bone": "root", "blend": "multiply"},
                 {"name": "a_screen", "bone": "root", "blend": "screen"},
             ]}
        switched, kept = switch_blend_modes(
            d, ["a_multiply", "a_screen"], "additive")
        self.assertEqual(len(kept), 2)
        self.assertEqual([s["name"] for s in d["slots"]],
                         ["a_multiply", "a_screen"])


class DrawOrderRenameAbsoluteTests(unittest.TestCase):
    def test_apply_offsets_sanity(self):
        # guards the semantic used by the rename test above
        self.assertEqual(
            apply_draw_order_offsets(["a", "b", "c"],
                                     [{"slot": "a", "offset": 2}]),
            ["b", "c", "a"])


if __name__ == "__main__":
    unittest.main()
