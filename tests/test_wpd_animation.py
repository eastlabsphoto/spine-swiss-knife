import math
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from spine_swiss_knife import spine_viewer_animation as sva  # noqa: E402
from spine_swiss_knife.spine_viewer_animation import (  # noqa: E402
    AnimationState, solve_world_transforms, solve_ik_constraints,
    build_draw_list, _evaluate_draw_order, _interpolate_timeline,
    _bezier_interpolate, assign_texture_keys,
)


def skel(version, **kw):
    d = {"skeleton": {"spine": version}, "bones": [{"name": "root"}],
         "slots": [], "skins": []}
    d.update(kw)
    return d


class CurveTests(unittest.TestCase):
    def test_normalised_ease_curve_endpoints_and_mid(self):
        curve = [0.25, 0.0, 0.75, 1.0]
        self.assertEqual(_bezier_interpolate(curve, 0.0), 0.0)
        self.assertEqual(_bezier_interpolate(curve, 1.0), 1.0)
        mid = _bezier_interpolate(curve, 0.5)
        self.assertAlmostEqual(mid, 0.5, places=3)
        self.assertLess(_bezier_interpolate(curve, 0.25), 0.25)

    def test_old_array_format(self):
        keys = [{"time": 0, "x": 0, "curve": [0.25, 0.0, 0.75, 1.0]},
                {"time": 1, "x": 10}]
        v = _interpolate_timeline(keys, 0.5, ("x",), {"x": 0})["x"]
        self.assertAlmostEqual(v, 5.0, places=2)
        self.assertTrue(0 < _interpolate_timeline(keys, 0.1, ("x",), {"x": 0})["x"] < 1.0)

    def test_38_scalar_format(self):
        keys = [{"time": 0, "x": 0, "curve": 0.9, "c2": 0.0, "c3": 1.0, "c4": 1.0},
                {"time": 1, "x": 10}]
        lin = 5.0
        v = _interpolate_timeline(keys, 0.5, ("x",), {"x": 0})["x"]
        self.assertTrue(0 <= v <= 10)
        self.assertNotAlmostEqual(v, lin, places=2)  # not treated as linear
        self.assertEqual(_interpolate_timeline(keys, 1.0, ("x",), {"x": 0})["x"], 10)

    def test_4x_absolute_two_channel_translate(self):
        # x channel: control points (cx, cy) absolute; y channel: stepped-like flat 0
        keys = [{"time": 1.0, "x": 0, "y": 100,
                 "curve": [1.25, 0, 1.75, 10,      # x: 0 -> 10, ease
                           1.25, 100, 1.75, 100]}, # y: constant 100 -> 100? end value 100
                {"time": 2.0, "x": 10, "y": 100}]
        out = _interpolate_timeline(keys, 1.5, ("x", "y"), {"x": 0, "y": 0}, v4=True)
        self.assertAlmostEqual(out["x"], 5.0, places=2)
        self.assertAlmostEqual(out["y"], 100.0, places=6)
        early = _interpolate_timeline(keys, 1.1, ("x", "y"), {"x": 0, "y": 0}, v4=True)
        self.assertTrue(0 <= early["x"] < 1.0)
        # channel y uses its own 4 numbers: make y end differ and flat control points
        keys[0]["curve"] = [1.25, 0, 1.75, 10, 1.0, 0, 2.0, 0]
        keys[1]["y"] = 50
        keys[0]["y"] = 0
        y_mid = _interpolate_timeline(keys, 1.5, ("x", "y"), {"x": 0, "y": 0}, v4=True)["y"]
        self.assertTrue(0 <= y_mid <= 50)
        self.assertNotAlmostEqual(y_mid, 25.0, places=1)  # control y=0 -> slow start

    def test_stepped(self):
        keys = [{"time": 0, "x": 1, "curve": "stepped"}, {"time": 1, "x": 9}]
        self.assertEqual(_interpolate_timeline(keys, 0.5, ("x",), {"x": 0})["x"], 1)

    def test_rotation_wrap_3x(self):
        anim = {"bones": {"b": {"rotate": [{"time": 0, "angle": 170},
                                           {"time": 1, "angle": -170}]}}}
        st = AnimationState(skel("3.8.99"), anim)
        ov, _, _ = st.evaluate(0.5)
        self.assertAlmostEqual(abs(ov["b"]["rotation"]), 180.0, places=3)

    def test_rotation_no_wrap_4x(self):
        anim = {"bones": {"b": {"rotate": [{"time": 0, "value": 170},
                                           {"time": 1, "value": -170}]}}}
        st = AnimationState(skel("4.2.43"), anim)
        ov, _, _ = st.evaluate(0.5)
        self.assertAlmostEqual(ov["b"]["rotation"], 0.0, places=3)


class Anim4xTests(unittest.TestCase):
    def test_bone_timelines_4x(self):
        anim = {"bones": {"b": {
            "rotate": [{"value": 10}, {"time": 1, "value": 20}],
            "translatex": [{"value": 4}, {"time": 1, "value": 8}],
            "scalex": [{"time": 0.5, "value": 2}],
            "sheary": [{"value": 5}],
        }}}
        ov, _, _ = AnimationState(skel("4.2.43"), anim).evaluate(1.0)
        o = ov["b"]
        self.assertEqual(o["rotation"], 20)
        self.assertEqual(o["x"], 8)
        self.assertNotIn("y", o)
        self.assertEqual(o["scaleX"], 2)
        self.assertNotIn("scaleY", o)
        self.assertEqual(o["shearY"], 5)

    def test_scale_default_is_one(self):
        anim = {"bones": {"b": {"scale": [{"time": 0, "x": 2}]}}}
        ov, _, _ = AnimationState(skel("4.2.43"), anim).evaluate(0)
        self.assertEqual(ov["b"]["scaleY"], 1)

    def test_slot_colours_4x(self):
        anim = {"slots": {"s": {
            "rgba": [{"color": "ff000080"}],
            "alpha": [{"value": 0.25}],
        }, "t": {"rgb": [{"color": "00ff00"}], "alpha": [{"time": 0, "value": 0.5}]},
            "u": {"rgba2": [{"light": "0000ffff", "dark": "000000"}]}}}
        _, ss, _ = AnimationState(skel("4.2.43"), anim).evaluate(0)
        self.assertAlmostEqual(ss["s"]["color"][0], 1.0)
        self.assertAlmostEqual(ss["s"]["alpha"], 0.25)
        self.assertEqual(ss["t"]["rgb"], (0.0, 1.0, 0.0))
        self.assertAlmostEqual(ss["u"]["color"][2], 1.0)

    def test_color_in_draw_list_combines_rgb_and_alpha(self):
        data = skel("4.2.43", slots=[{"name": "s", "bone": "root", "attachment": "a",
                                      "color": "ffffff80"}],
                    skins=[{"name": "default", "attachments": {"s": {"a": {"width": 1, "height": 1}}}}])
        tex = {"a": object()}
        wt = solve_world_transforms(data["bones"])
        dl = build_draw_list(data, wt, tex,
                             slot_states={"s": {"rgb": (0, 1, 0), "alpha": 0.25}})
        self.assertEqual(dl[0]["color"], (0, 1, 0, 0.25))

    def _mesh_data(self, version, skins):
        return skel(version, slots=[{"name": "s", "bone": "root", "attachment": "m"}],
                    skins=skins)

    def test_deform_4_1_attachments_and_duration(self):
        mesh = {"type": "mesh", "uvs": [0, 0, 1, 0, 1, 1], "triangles": [0, 1, 2],
                "vertices": [0, 0, 10, 0, 10, 10], "hull": 3}
        data = self._mesh_data("4.2.43", [{"name": "default",
                                           "attachments": {"s": {"m": mesh}}}])
        anim = {"attachments": {"default": {"s": {"m": {"deform": [
            {"time": 0}, {"time": 2.0, "vertices": [1, 1, 1, 1, 1, 1]}]}}}}}
        st = AnimationState(data, anim)
        self.assertEqual(st.duration, 2.0)
        _, _, deform = st.evaluate(1.0)
        self.assertEqual(deform[("s", "m")], [0.5] * 6)
        wt = solve_world_transforms(data["bones"])
        dl = build_draw_list(data, wt, {"m": object()}, deform_states=deform)
        self.assertEqual(dl[0]["vertices"], [0.5, 0.5, 10.5, 0.5, 10.5, 10.5])

    def test_deform_ffd(self):
        mesh = {"type": "mesh", "uvs": [0, 0, 1, 0, 1, 1], "triangles": [0, 1, 2],
                "vertices": [0, 0, 10, 0, 10, 10], "hull": 3}
        data = self._mesh_data("3.5.51", {"default": {"s": {"m": mesh}}})
        anim = {"ffd": {"default": {"s": {"m": [
            {"time": 0}, {"time": 1.0, "offset": 2, "vertices": [2, 2]}]}}}}
        st = AnimationState(data, anim)
        _, _, deform = st.evaluate(1.0)
        self.assertEqual(deform[("s", "m")], [0, 0, 2, 2, 0, 0])


class InheritModeTests(unittest.TestCase):
    def test_shear_x(self):
        w = solve_world_transforms([{"name": "r", "shearX": 30}])["r"]
        self.assertAlmostEqual(w.a, 0.866, places=3)
        self.assertAlmostEqual(w.b, 0.0, places=6)
        self.assertAlmostEqual(w.c, 0.5, places=6)
        self.assertAlmostEqual(w.d, 1.0, places=6)

    def test_rotation_matrix(self):
        w = solve_world_transforms([{"name": "r", "rotation": 90}])["r"]
        self.assertAlmostEqual(w.a, 0, places=6)
        self.assertAlmostEqual(w.c, 1, places=6)
        self.assertAlmostEqual(w.b, -1, places=6)

    def _tree(self, mode_fields):
        child = {"name": "c", "parent": "p", "x": 10, "y": 0, "rotation": 0}
        child.update(mode_fields)
        return [{"name": "p", "rotation": 90, "scaleX": 2, "scaleY": 2}, child]

    def test_only_translation_keeps_local_matrix(self):
        w = solve_world_transforms(self._tree({"transform": "onlyTranslation"}))
        c = w["c"]
        self.assertAlmostEqual(c.a, 1.0)
        self.assertAlmostEqual(c.b, 0.0, places=6)
        self.assertAlmostEqual(c.c, 0.0, places=6)
        self.assertAlmostEqual(c.d, 1.0)
        # position still follows the parent (rotated 90, scaled 2): (10,0) -> (0, 20)
        self.assertAlmostEqual(c.worldX, 0.0, places=6)
        self.assertAlmostEqual(c.worldY, 20.0, places=6)

    def test_inherit_4_2_field(self):
        w = solve_world_transforms(self._tree({"inherit": "onlyTranslation"}))
        self.assertAlmostEqual(w["c"].a, 1.0)

    def test_old_flags(self):
        w = solve_world_transforms(self._tree({"inheritRotation": False,
                                               "inheritScale": False}))
        self.assertAlmostEqual(w["c"].a, 1.0)
        self.assertAlmostEqual(w["c"].c, 0.0, places=6)

    def test_no_scale(self):
        w = solve_world_transforms(self._tree({"transform": "noScale"}))["c"]
        self.assertAlmostEqual(math.hypot(w.a, w.c), 1.0, places=5)
        # rotation still inherited (parent rotated 90)
        self.assertAlmostEqual(w.c, 1.0, places=5)

    def test_no_rotation_or_reflection(self):
        w = solve_world_transforms(self._tree({"transform": "noRotationOrReflection"}))["c"]
        # world x axis stays aligned with the skeleton x axis, scale inherited
        self.assertAlmostEqual(w.c, 0.0, places=5)
        self.assertAlmostEqual(w.a, 2.0, places=5)

    def test_normal_mode_unchanged(self):
        w = solve_world_transforms(self._tree({}))["c"]
        self.assertAlmostEqual(w.c, 2.0, places=5)


class DrawOrderTests(unittest.TestCase):
    SLOTS = [{"name": n} for n in "abcde"]

    def names(self, order):
        return [s["name"] for s in order]

    def test_runtime_decoding(self):
        keys = [{"time": 0, "offsets": [{"slot": "a", "offset": 2}]}]
        self.assertEqual(self.names(_evaluate_draw_order(self.SLOTS, keys, 0)),
                         list("bcade"))

    def test_two_offsets(self):
        keys = [{"time": 0, "offsets": [{"slot": "a", "offset": 4},
                                        {"slot": "e", "offset": -4}]}]
        self.assertEqual(self.names(_evaluate_draw_order(self.SLOTS, keys, 0)),
                         list("ebcda"))

    def test_collision_falls_back(self):
        keys = [{"time": 0, "offsets": [{"slot": "a", "offset": 1},
                                        {"slot": "b", "offset": 0}]}]
        self.assertEqual(self.names(_evaluate_draw_order(self.SLOTS, keys, 0)),
                         list("abcde"))

    def test_key_without_offsets_is_setup(self):
        keys = [{"time": 0, "offsets": [{"slot": "a", "offset": 1}]}, {"time": 1}]
        self.assertEqual(self.names(_evaluate_draw_order(self.SLOTS, keys, 2)),
                         list("abcde"))

    def test_lowercase_draworder_read(self):
        anim = {"draworder": [{"time": 0.5, "offsets": []}]}
        st = AnimationState(skel("3.6.53"), anim)
        self.assertEqual(len(st._draw_order_keys), 1)
        self.assertEqual(st.duration, 0.5)


class IKTests(unittest.TestCase):
    BONES = [
        {"name": "root"},
        {"name": "a", "parent": "root", "length": 10},
        {"name": "b", "parent": "a", "x": 10, "length": 10},
        {"name": "target", "parent": "root", "x": 10, "y": 10},
    ]
    IK = [{"name": "ik", "bones": ["a", "b"], "target": "target", "mix": 1.0,
           "bendPositive": True}]

    def test_two_bone_reaches_target(self):
        w = solve_world_transforms(self.BONES)
        solve_ik_constraints(self.BONES, w, self.IK)
        b = w["b"]
        tip = (b.worldX + 10 * b.a, b.worldY + 10 * b.c)
        self.assertAlmostEqual(tip[0], 10.0, places=3)
        self.assertAlmostEqual(tip[1], 10.0, places=3)

    def test_two_bone_uses_length_when_unequal(self):
        bones = [dict(b) for b in self.BONES]
        bones[2]["length"] = 5
        bones[3].update({"x": 8, "y": 6})  # distance 10, reach 15
        w = solve_world_transforms(bones)
        solve_ik_constraints(bones, w, self.IK)
        b = w["b"]
        tip = (b.worldX + 5 * b.a, b.worldY + 5 * b.c)
        self.assertAlmostEqual(tip[0], 8.0, places=3)
        self.assertAlmostEqual(tip[1], 6.0, places=3)

    def test_ik_keeps_animated_pose_of_descendants(self):
        bones = [{"name": "root"},
                 {"name": "arm", "parent": "root", "length": 10},
                 {"name": "hand", "parent": "arm", "x": 10, "rotation": 0},
                 {"name": "t", "parent": "root", "x": 0, "y": 50}]
        ov = {"hand": {"rotation": 45}, "arm": {"rotation": 30}}
        w = solve_world_transforms(bones, ov)
        before_hand = (w["hand"].a, w["hand"].c)
        ik = [{"name": "ik", "bones": ["arm"], "target": "t", "mix": 0.0}]
        solve_ik_constraints(bones, w, ik, bone_overrides=ov)
        # mix 0: arm stays at its animated 30 deg, hand keeps its 45 deg animated offset
        self.assertAlmostEqual(w["arm"].c, math.sin(math.radians(30)), places=5)
        self.assertAlmostEqual(w["hand"].a, before_hand[0], places=5)
        self.assertAlmostEqual(w["hand"].c, before_hand[1], places=5)

    def test_one_bone_points_at_target_from_animated_position(self):
        bones = [{"name": "root"}, {"name": "arm", "parent": "root", "x": 5},
                 {"name": "t", "parent": "root", "x": 5, "y": 20}]
        ov = {"arm": {"y": 10}}  # animated translation: arm origin at (5, 10)
        w = solve_world_transforms(bones, ov)
        solve_ik_constraints(bones, w, [{"name": "i", "bones": ["arm"],
                                         "target": "t", "mix": 1.0}])
        self.assertAlmostEqual(w["arm"].c, 1.0, places=5)  # pointing straight up
        self.assertAlmostEqual(w["arm"].worldY, 10.0, places=5)


class DrawListTests(unittest.TestCase):
    def _data(self, skins, slots, **kw):
        d = skel("3.8.99", slots=slots, skins=skins)
        d.update(kw)
        return d

    def test_missing_attachment_not_drawn(self):
        data = self._data([{"name": "default", "attachments": {}}],
                          [{"name": "s", "bone": "root", "attachment": "ghost"}])
        wt = solve_world_transforms(data["bones"])
        self.assertEqual(build_draw_list(data, wt, {"ghost": object()}), [])

    def test_path_then_name_lookup(self):
        data = self._data(
            [{"name": "default", "attachments": {"s": {"x": {"path": "real/img"}}}}],
            [{"name": "s", "bone": "root", "attachment": "x"}])
        wt = solve_world_transforms(data["bones"])
        tex = object()
        dl = build_draw_list(data, wt, {"real/img": tex, "x": object()})
        self.assertIs(dl[0]["pixmap"], tex)

    def test_mesh_without_texture_still_ends_clip(self):
        mesh = {"type": "mesh", "uvs": [0, 0, 1, 0, 1, 1], "triangles": [0, 1, 2],
                "vertices": [0, 0, 1, 0, 1, 1]}
        clip = {"type": "clipping", "end": "m", "vertexCount": 3,
                "vertices": [0, 0, 1, 0, 1, 1]}
        data = self._data(
            [{"name": "default", "attachments": {"c": {"clip": clip}, "m": {"mesh": mesh}}}],
            [{"name": "c", "bone": "root", "attachment": "clip"},
             {"name": "m", "bone": "root", "attachment": "mesh"},
             {"name": "z", "bone": "root"}])
        wt = solve_world_transforms(data["bones"])
        dl = build_draw_list(data, wt, {})
        self.assertEqual([i.get("type") for i in dl], ["clip", "clip_end_marker"])

    def test_clip_without_end_has_no_marker(self):
        clip = {"type": "clipping", "vertexCount": 3, "vertices": [0, 0, 1, 0, 1, 1]}
        data = self._data(
            [{"name": "default", "attachments": {"c": {"clip": clip}}}],
            [{"name": "c", "bone": "root", "attachment": "clip"}])
        wt = solve_world_transforms(data["bones"])
        dl = build_draw_list(data, wt, {})
        self.assertEqual(dl[0]["clip_end"], "")

    def test_linkedmesh_rendered_with_parent_geometry_and_deform(self):
        mesh = {"type": "mesh", "uvs": [0, 0, 1, 0, 1, 1], "triangles": [0, 1, 2],
                "vertices": [0, 0, 10, 0, 10, 10], "hull": 3}
        linked = {"type": "linkedmesh", "parent": "m", "path": "other"}
        data = self._data(
            [{"name": "default", "attachments": {"s": {"m": mesh, "l": linked}}}],
            [{"name": "s", "bone": "root", "attachment": "l"}])
        wt = solve_world_transforms(data["bones"])
        tex = object()
        dl = build_draw_list(data, wt, {"other": tex},
                             deform_states={("s", "m"): [1, 1, 1, 1, 1, 1]})
        self.assertEqual(len(dl), 1)
        self.assertIs(dl[0]["pixmap"], tex)
        self.assertEqual(dl[0]["triangles"], [0, 1, 2])
        self.assertEqual(dl[0]["vertices"], [1, 1, 11, 1, 11, 11])
        linked["timelines"] = False
        dl = build_draw_list(data, wt, {"other": tex},
                             deform_states={("s", "m"): [1, 1, 1, 1, 1, 1]})
        self.assertEqual(dl[0]["vertices"], [0, 0, 10, 0, 10, 10])


class TextureKeyTests(unittest.TestCase):
    def test_names_not_mangled_and_lowest_index_wins(self):
        out = assign_texture_keys([
            ("head.v2", -1, "h"), ("arm.left", -1, "L"), ("arm.right", -1, "R"),
            ("walk", 2, "w2"), ("walk", 1, "w1"),
        ])
        self.assertEqual(out["head.v2"], "h")
        self.assertEqual(out["arm.left"], "L")
        self.assertEqual(out["arm.right"], "R")
        self.assertEqual(out["walk"], "w1")
        self.assertEqual(out["walk_1"], "w1")
        self.assertEqual(out["walk_2"], "w2")

    def test_alias_does_not_override_real_region(self):
        out = assign_texture_keys([("walk", 1, "x"), ("walk_1", -1, "real")])
        self.assertEqual(out["walk_1"], "real")


if __name__ == "__main__":
    unittest.main()
