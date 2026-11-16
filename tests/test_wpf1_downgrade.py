"""Tests for the pure 4.x -> 3.8 conversion (downgrade_core)."""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spine_swiss_knife.downgrade_core import (  # noqa: E402
    convert_to_destination, convert_to_37, convert_to_38,
)


def skel(anims, version="4.2.43", **extra):
    data = {
        "skeleton": {"spine": version},
        "bones": [{"name": "root"}, {"name": "b", "parent": "root"}],
        "slots": [{"name": "s", "bone": "b", "color": "ff8000ff"}],
        "skins": [{"name": "default", "attachments": {}}],
        "animations": {"a": anims},
    }
    data.update(extra)
    return data


def has_warning(warnings, text):
    return any(text in w for w in warnings)


class CurveConversion(unittest.TestCase):
    def test_single_channel_numbers(self):
        data = skel({"bones": {"b": {"rotate": [
            {"time": 1, "value": 10, "curve": [1.5, 12, 2.5, 18]},
            {"time": 3, "value": 20},
        ]}}})
        out, w = convert_to_38(data)
        k = out["animations"]["a"]["bones"]["b"]["rotate"][0]
        self.assertAlmostEqual(k["curve"], 0.25)
        self.assertAlmostEqual(k["c2"], 0.2)
        self.assertAlmostEqual(k["c3"], 0.75)
        self.assertAlmostEqual(k["c4"], 0.8)
        self.assertEqual(k["angle"], 10)
        self.assertNotIn("value", k)
        self.assertTrue(has_warning(w, "bezier curve(s) converted"))

    def test_flat_segment_drops_curve(self):
        data = skel({"bones": {"b": {"rotate": [
            {"time": 0, "value": 5, "curve": [0.2, 5, 0.6, 5]},
            {"time": 1, "value": 5},
        ]}}})
        out, w = convert_to_38(data)
        k = out["animations"]["a"]["bones"]["b"]["rotate"][0]
        self.assertNotIn("curve", k)
        self.assertTrue(w)

    def test_stepped_stays(self):
        data = skel({"bones": {"b": {"rotate": [
            {"time": 0, "value": 0, "curve": "stepped"}, {"time": 1, "value": 9}]}}})
        out, _ = convert_to_38(data)
        self.assertEqual(out["animations"]["a"]["bones"]["b"]["rotate"][0]["curve"], "stepped")

    def test_multi_channel_uses_first_changing_channel_and_warns(self):
        # x constant, y changes: the y curve (second 4-number slice) is used
        curve = [0, 0, 1, 1,  0.25, 2.5, 0.75, 7.5]
        data = skel({"bones": {"b": {"translate": [
            {"time": 0, "x": 3, "y": 0, "curve": curve},
            {"time": 1, "x": 3, "y": 10}]}}})
        out, w = convert_to_38(data)
        k = out["animations"]["a"]["bones"]["b"]["translate"][0]
        self.assertAlmostEqual(k["curve"], 0.25)
        self.assertAlmostEqual(k["c2"], 0.25)
        self.assertAlmostEqual(k["c3"], 0.75)
        self.assertAlmostEqual(k["c4"], 0.75)
        self.assertFalse(has_warning(w, "multi-channel"))

        data = skel({"bones": {"b": {"translate": [
            {"time": 0, "x": 0, "y": 0, "curve": curve},
            {"time": 1, "x": 4, "y": 10}]}}})
        out, w = convert_to_38(data)
        self.assertTrue(has_warning(w, "multi-channel"))

    def test_rotation_over_180_warns(self):
        data = skel({"bones": {"b": {"rotate": [
            {"time": 0, "value": 0}, {"time": 1, "value": 270}]}}})
        _, w = convert_to_38(data)
        self.assertTrue(has_warning(w, "180 degrees"))

    def test_deform_curve_and_move_from_attachments(self):
        data = skel({"attachments": {"default": {"s": {"m": {
            "deform": [{"time": 0, "offset": 0, "vertices": [1, 1],
                        "curve": [0.5, 0.1, 1.0, 0.9]},
                       {"time": 2, "vertices": [2, 2]}],
            "sequence": [{"time": 0, "mode": "loop"}],
        }}}}})
        out, w = convert_to_38(data)
        anim = out["animations"]["a"]
        self.assertNotIn("attachments", anim)
        frames = anim["deform"]["default"]["s"]["m"]
        self.assertEqual(len(frames), 2)
        self.assertAlmostEqual(frames[0]["curve"], 0.25)
        self.assertAlmostEqual(frames[0]["c2"], 0.1)
        self.assertAlmostEqual(frames[0]["c3"], 0.5)
        self.assertAlmostEqual(frames[0]["c4"], 0.9)
        self.assertTrue(has_warning(w, "sequence"))

    def test_deform_40_curves_converted(self):
        data = skel({"deform": {"default": {"s": {"m": [
            {"time": 0, "vertices": [1], "curve": [0.5, 0, 0.5, 1]},
            {"time": 1, "vertices": [2]}]}}}}, version="4.0.64")
        out, _ = convert_to_38(data)
        k = out["animations"]["a"]["deform"]["default"]["s"]["m"][0]
        self.assertAlmostEqual(k["curve"], 0.5)
        self.assertEqual(k["c2"], 0)


class AxisAndColourMerge(unittest.TestCase):
    def test_axis_merge(self):
        data = skel({"bones": {"b": {
            "translatex": [{"time": 0, "value": 0}, {"time": 2, "value": 10}],
            "translatey": [{"time": 1, "value": 5}],
            "scalex": [{"time": 0, "value": 2}],
        }}})
        out, w = convert_to_38(data)
        tls = out["animations"]["a"]["bones"]["b"]
        self.assertNotIn("translatex", tls)
        self.assertNotIn("translatey", tls)
        tr = tls["translate"]
        self.assertEqual([k.get("time", 0) for k in tr], [0, 1, 2])
        self.assertAlmostEqual(tr[1]["x"], 5.0)   # sampled linearly
        self.assertAlmostEqual(tr[0]["y"], 5.0)   # clamped before first key
        self.assertEqual(tls["scale"], [{"x": 2.0, "y": 1.0}])  # missing y -> default 1
        self.assertTrue(has_warning(w, "separate-axis"))

    def test_axis_dropped_when_combined_exists(self):
        data = skel({"bones": {"b": {
            "translate": [{"time": 0, "x": 1, "y": 1}],
            "translatex": [{"time": 0, "value": 9}],
        }}})
        out, w = convert_to_38(data)
        tls = out["animations"]["a"]["bones"]["b"]
        self.assertNotIn("translatex", tls)
        self.assertEqual(tls["translate"], [{"time": 0, "x": 1, "y": 1}])
        self.assertTrue(has_warning(w, "already exists"))

    def test_rgb_alpha_merge(self):
        data = skel({"slots": {"s": {
            "rgb": [{"time": 0, "color": "ff0000"}, {"time": 1, "color": "0000ff"}],
            "alpha": [{"time": 0.5, "value": 0.5}],
        }}})
        out, w = convert_to_38(data)
        stl = out["animations"]["a"]["slots"]["s"]
        self.assertNotIn("rgb", stl)
        self.assertNotIn("alpha", stl)
        keys = stl["color"]
        self.assertEqual([k.get("time", 0) for k in keys], [0, 0.5, 1])
        self.assertEqual(keys[0]["color"], "ff000080")   # alpha clamped to first key 0.5
        self.assertEqual(keys[2]["color"], "0000ff80")
        self.assertTrue(has_warning(w, "merged into colour"))

    def test_alpha_only_uses_setup_rgb(self):
        data = skel({"slots": {"s": {"alpha": [{"time": 0, "value": 1}, {"time": 1, "value": 0}]}}})
        out, _ = convert_to_38(data)
        keys = out["animations"]["a"]["slots"]["s"]["color"]
        self.assertEqual(keys[0]["color"], "ff8000ff")
        self.assertEqual(keys[1]["color"], "ff800000")

    def test_rgb2_to_two_color(self):
        data = skel({"slots": {"s": {
            "rgb2": [{"time": 0, "light": "ffffff", "dark": "101010"}]}}})
        out, w = convert_to_38(data)
        stl = out["animations"]["a"]["slots"]["s"]
        self.assertEqual(stl["twoColor"][0]["dark"], "101010")
        self.assertEqual(len(stl["twoColor"][0]["light"]), 8)
        self.assertTrue(w)

    def test_rgba_renamed_with_curve(self):
        curve = [0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0.5, 0.2, 0.5, 0.8]
        data = skel({"slots": {"s": {"rgba": [
            {"time": 0, "color": "ffffff00", "curve": curve},
            {"time": 1, "color": "ffffffff"}]}}})
        out, _ = convert_to_38(data)
        stl = out["animations"]["a"]["slots"]["s"]
        self.assertNotIn("rgba", stl)
        self.assertAlmostEqual(stl["color"][0]["curve"], 0.5)
        self.assertAlmostEqual(stl["color"][0]["c2"], 0.2)


class ConstraintsAndMisc(unittest.TestCase):
    def test_path_timelines(self):
        data = skel({"path": {"p": {
            "position": [{"time": 0, "value": 0.5}],
            "spacing": [{"time": 0, "value": 2}],
            "mix": [{"time": 0, "mixRotate": 0.3, "mixX": 0.4, "mixY": 0.4}],
        }}})
        out, _ = convert_to_38(data)
        p = out["animations"]["a"]["path"]["p"]
        self.assertEqual(p["position"][0]["position"], 0.5)
        self.assertEqual(p["spacing"][0]["spacing"], 2)
        self.assertEqual(p["mix"][0], {"time": 0, "rotateMix": 0.3, "translateMix": 0.4})

    def test_physics_and_inherit(self):
        data = skel({"physics": {"ph": [{"time": 0}]}}, physics=[{"name": "ph", "bone": "b"}])
        data["bones"][1]["inherit"] = "noScale"
        data["bones"][0]["inherit"] = "normal"
        out, w = convert_to_38(data)
        self.assertNotIn("physics", out)
        self.assertNotIn("physics", out["animations"]["a"])
        self.assertEqual(out["bones"][1]["transform"], "noScale")
        self.assertNotIn("inherit", out["bones"][1])
        self.assertNotIn("inherit", out["bones"][0])
        self.assertTrue(has_warning(w, "physics"))

    def test_mix_xy_difference_warns(self):
        data = skel({}, transform=[{"name": "t", "bones": ["b"], "target": "root",
                                    "mixRotate": 1, "mixX": 0.2, "mixY": 0.7}])
        out, w = convert_to_38(data)
        self.assertEqual(out["transform"][0]["translateMix"], 0.2)
        self.assertTrue(has_warning(w, "different X and Y"))


class Destination(unittest.TestCase):
    def test_unknown_source_untouched_with_warning(self):
        data = {"skeleton": {"spine": "3.5.51"},
                "animations": {"a": {"bones": {"b": {"rotate": [
                    {"time": 0, "angle": 1, "curve": [0.1, 0, 0.9, 1]},
                    {"time": 1, "angle": 2}]}}}}}
        before = copy.deepcopy(data)
        out, w = convert_to_destination(data, "3.8.99")
        self.assertEqual(out, before)
        self.assertTrue(w)

    def test_missing_source_untouched(self):
        data = {"bones": []}
        out, w = convert_to_destination(data, "3.8.99")
        self.assertEqual(out, {"bones": []})
        self.assertTrue(w)

    def test_4x_to_37_chain(self):
        data = skel({"bones": {"b": {"rotate": [
            {"time": 0, "value": 0, "curve": [0.25, 0, 0.75, 10]},
            {"time": 1, "value": 10}]}}})
        out, w = convert_to_destination(data, "3.7.94")
        self.assertIsInstance(out["skins"], dict)
        k = out["animations"]["a"]["bones"]["b"]["rotate"][0]
        self.assertEqual(k["curve"], [0.25, 0.0, 0.75, 1.0])
        self.assertTrue(out["skeleton"]["spine"].startswith("3.7.94"))

    def test_upgrade_refused(self):
        data = {"skeleton": {"spine": "3.8.99"}}
        out, w = convert_to_destination(data, "4.2.18")
        self.assertTrue(has_warning(w, "Cannot upgrade"))

    def test_37_skin_extras_warn(self):
        data = {"skeleton": {"spine": "3.8.99"},
                "skins": [{"name": "default", "attachments": {}, "bones": ["b"]}]}
        out, w = convert_to_37(data)
        self.assertTrue(has_warning(w, "skin-specific"))


if __name__ == "__main__":
    unittest.main()
