import os
import shutil
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtGui import QPixmap, QColor, QImage
    from PySide6.QtWidgets import QApplication
    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False

if HAS_QT:
    _APP = QApplication.instance() or QApplication([])

    from spine_swiss_knife import draw_order_core
    from spine_swiss_knife.draw_order_renderer import (
        RenderError, _compare_with_original, _compute_skeleton_bbox,
        _mesh_aabb, _qimages_match, _render_all_animations,
        _save_debug_pair, iter_animation_frames, render_frame,
        render_skin_names,
    )
    from spine_swiss_knife.draw_order_optimizer import (
        _get_clip_regions, _split_into_zones, has_draw_order_timelines,
        optimize_draw_order, prepare_baseline,
    )
    from spine_swiss_knife.spine_json import normalize_skins
    from spine_swiss_knife.spine_refs import apply_draw_order_offsets
    from spine_swiss_knife.spine_viewer_animation import (
        BoneTransform, solve_world_transforms,
    )


def _textures():
    out = {}
    for name, color in (("red", "#ff0000"), ("blue", "#0000ff"),
                        ("green", "#00ff00")):
        pm = QPixmap(40, 40)
        pm.fill(QColor(color))
        out[name] = pm
    return out


def _region(name, x=0.0):
    return {name: {"x": x, "y": 0, "width": 40, "height": 40}}


def _base(slots, skins, animations=None, bones=None):
    return {
        "skeleton": {"spine": "3.8.99"},
        "bones": bones or [{"name": "root"}],
        "slots": slots,
        "skins": skins,
        "animations": animations or {},
    }


def _count_alpha(img, x, y):
    return img.pixelColor(x, y).alpha()


@unittest.skipUnless(HAS_QT, "PySide6 not installed")
class SkinVerificationTests(unittest.TestCase):
    def _skin_only_data(self):
        slots = [{"name": "a", "bone": "root", "attachment": "red"},
                 {"name": "b", "bone": "root", "attachment": "blue"}]
        skins = [
            {"name": "default", "attachments": {}},
            {"name": "alt", "attachments": {
                "a": _region("red", 0), "b": _region("blue", 10)}},
        ]
        return _base(slots, skins)

    def test_all_skins_are_listed(self):
        data = self._skin_only_data()
        self.assertEqual(render_skin_names(data), ["default", "alt"])
        only_default = _base([], [{"name": "default", "attachments": {}}])
        self.assertEqual(render_skin_names(only_default), ["default"])
        self.assertEqual(render_skin_names({"skins": {}}), ["default"])

    def test_swap_visible_only_in_named_skin_is_detected(self):
        data = self._skin_only_data()
        tex = _textures()
        bbox = _compute_skeleton_bbox(data, tex)
        self.assertGreater(bbox[2], 40)  # union includes the alt skin art
        base = _render_all_animations(data, tex, data["slots"], [None], 60, bbox)
        try:
            self.assertTrue(base.any_content)
            same, _ = _compare_with_original(
                data, tex, base, data["slots"], [None], 60, bbox, 5, 0.02)
            self.assertTrue(same)
            swapped = [data["slots"][1], data["slots"][0]]
            ok, diffs = _compare_with_original(
                data, tex, base, swapped, [None], 60, bbox, 5, 0.0001)
            self.assertFalse(ok)
            self.assertEqual(diffs, ["setup_pose"])
        finally:
            base.cleanup()

    def test_nothing_rendered_aborts(self):
        data = self._skin_only_data()
        with self.assertRaises(RenderError) as cm:
            prepare_baseline(data, {"unrelated": QPixmap(4, 4)}, [None], 60)
        self.assertIn("Nothing rendered", str(cm.exception))
        res = optimize_draw_order(
            _base([{"name": "a", "bone": "root", "attachment": "zzz",
                    "blend": "additive"},
                   {"name": "b", "bone": "root", "attachment": "zzz"},
                   {"name": "c", "bone": "root", "attachment": "zzz",
                    "blend": "additive"}],
                  [{"name": "default", "attachments": {}}]),
            textures={"unrelated": QPixmap(4, 4)})
        self.assertFalse(res["success"])


@unittest.skipUnless(HAS_QT, "PySide6 not installed")
class ClipWithoutEndTests(unittest.TestCase):
    def _data(self):
        slots = [
            {"name": "clipper", "bone": "root", "attachment": "clip"},
            {"name": "m", "bone": "root", "attachment": "mesh"},
            {"name": "r", "bone": "root", "attachment": "green"},
        ]
        atts = {
            "clipper": {"clip": {"type": "clipping", "vertexCount": 4,
                                 "vertices": [-30, -30, 0, -30, 0, 30, -30, 30]}},
            "m": {"mesh": {"type": "mesh", "path": "red",
                           "uvs": [0, 1, 1, 1, 1, 0, 0, 0],
                           "triangles": [0, 1, 2, 0, 2, 3],
                           "vertices": [-20, -20, 20, -20, 20, 20, -20, 20],
                           "hull": 4}},
            "r": _region("green", 0),
        }
        return _base(slots, [{"name": "default", "attachments": atts}])

    def test_clip_runs_to_end_of_draw_order(self):
        data = self._data()
        world = solve_world_transforms(data["bones"])
        img = render_frame(data, _textures(), world,
                           bbox=(-50, -50, 100, 100))
        # image x = 50 + wx, y = 50 - wy
        self.assertGreater(_count_alpha(img, 40, 50), 0)   # mesh, inside clip
        self.assertEqual(_count_alpha(img, 60, 50), 0)     # mesh, clipped away
        self.assertEqual(_count_alpha(img, 60, 45), 0)     # region, clipped too
        self.assertGreater(_count_alpha(img, 40, 45), 0)

    def test_optimizer_zone_covers_clip_to_last_slot(self):
        slots = [{"name": n, "bone": "root"} for n in "abcde"]
        skins = normalize_skins([{"name": "default", "attachments": {
            "b": {"c": {"type": "clipping", "vertexCount": 3,
                        "vertices": [0, 0, 1, 0, 0, 1]}}}}])
        self.assertEqual(_get_clip_regions(slots, skins), [(1, 4)])
        zones = _split_into_zones(slots, skins)
        # a | b(boundary) | c d (clipped zone) | e(boundary)
        self.assertEqual([[s["name"] for s in z.slots] for z in zones],
                         [["a"], ["b"], ["c", "d"], ["e"]])


@unittest.skipUnless(HAS_QT, "PySide6 not installed")
class BboxAndSamplingTests(unittest.TestCase):
    def _moving(self):
        slots = [{"name": "a", "bone": "root", "attachment": "red"}]
        skins = [{"name": "default", "attachments": {"a": _region("red")}}]
        anims = {"move": {"bones": {"root": {"translate": [
            {"time": 0, "x": 0, "y": 0},
            {"time": 1, "x": 3000, "y": 0}]}}}}
        return _base(slots, skins, anims)

    def test_bbox_covers_animated_extent(self):
        data = self._moving()
        x, y, w, h = _compute_skeleton_bbox(data, _textures())
        self.assertGreaterEqual(x + w, 3000)
        self.assertLessEqual(x, -20)

    def test_bbox_is_clamped_and_logged(self):
        data = self._moving()
        data["animations"]["move"]["bones"]["root"]["translate"][1]["x"] = 50000
        logs = []
        x, y, w, h = _compute_skeleton_bbox(data, _textures(),
                                            on_log=logs.append)
        self.assertLessEqual(w, 8192)
        self.assertTrue(any("clamped" in m for m in logs))

    def test_weighted_mesh_aabb_is_resolved(self):
        bt0, bt1 = BoneTransform(), BoneTransform()
        bt0.a = bt0.d = bt1.a = bt1.d = 1.0
        bt1.worldX = 100.0
        att = {"type": "mesh", "hull": 2, "uvs": [0, 0, 1, 1],
               "vertices": [1, 0, 0, 0, 1.0, 1, 1, 5, 5, 1.0]}
        box = _mesh_aabb(att, bt0, None, [bt0, bt1])
        self.assertEqual(box, (0.0, 0.0, 105.0, 5.0))

    def test_last_frame_is_sampled_at_duration(self):
        data = self._moving()
        tex = _textures()
        bbox = (-50, -50, 4000, 100)
        frames = list(iter_animation_frames(
            data, tex, data["slots"], "move", 7, bbox))
        expected = draw_order_core.sample_times(1.0, 7)
        self.assertEqual(len(frames), len(expected))
        self.assertEqual(expected[-1], 1.0)
        last = frames[-1][1]
        # at t == duration the square is centred at x = 3000 -> px 3050
        self.assertGreater(_count_alpha(last, 3050, 50), 0)
        self.assertEqual(_count_alpha(last, 50, 50), 0)


@unittest.skipUnless(HAS_QT, "PySide6 not installed")
class ImageHandlingTests(unittest.TestCase):
    def test_null_images_never_match(self):
        a, b = QImage(), QImage()
        self.assertTrue(a.isNull())
        details = []
        self.assertFalse(_qimages_match(a, b, 5, 0.02, _log_diffs=details))
        self.assertTrue(details)
        good = QImage(4, 4, QImage.Format_ARGB32_Premultiplied)
        good.fill(0)
        self.assertFalse(_qimages_match(good, a))

    def test_unallocatable_canvas_raises(self):
        data = _base([], [{"name": "default", "attachments": {}}])
        with self.assertRaises(RenderError):
            render_frame(data, {}, {}, bbox=(0, 0, 600000000, 1))

    def test_disk_spill_matches_memory(self):
        slots = [{"name": "a", "bone": "root", "attachment": "red"}]
        skins = [{"name": "default", "attachments": {"a": _region("red")}}]
        anims = {"m": {"bones": {"root": {"translate": [
            {"time": 0, "x": 0}, {"time": 0.5, "x": 30}]}}}}
        data = _base(slots, skins, anims)
        tex = _textures()
        bbox = (-50, -50, 150, 100)
        mem = _render_all_animations(data, tex, data["slots"], [None, "m"], 10, bbox)
        spill = tempfile.mkdtemp(prefix="wpc_spill_")
        disk = _render_all_animations(data, tex, data["slots"], [None, "m"], 10,
                                      bbox, spill_dir=spill)
        try:
            self.assertEqual(len(mem["m"]), len(disk["m"]))
            self.assertGreater(len(os.listdir(spill)), 0)
            for i in range(len(mem["m"])):
                self.assertTrue(_qimages_match(mem["m"][i], disk["m"][i], 0))
        finally:
            disk.cleanup()
        self.assertFalse(os.path.exists(spill))

    def test_debug_pairs_use_safe_directory_names(self):
        img = QImage(4, 4, QImage.Format_ARGB32_Premultiplied)
        img.fill(0xFFFF0000)
        out = tempfile.mkdtemp(prefix="wpc_dbg_")
        try:
            logs = []
            for name in ("win?", "idle: loop", "a/b", "setup_pose", None):
                _save_debug_pair(out, name, 3, img, img, logs.append)
            self.assertEqual(logs, [])
            self.assertEqual(
                sorted(os.listdir(out)),
                sorted(["anim_win_", "anim_idle_ loop", "anim_a_b",
                        "anim_setup_pose", "setup_pose"]))
            self.assertTrue(os.path.isfile(
                os.path.join(out, "anim_win_", "0003_original.png")))
        finally:
            shutil.rmtree(out, ignore_errors=True)


@unittest.skipUnless(HAS_QT, "PySide6 not installed")
class OptimizerDrawOrderTimelineTests(unittest.TestCase):
    def test_lowercase_timeline_is_seen(self):
        data = {"animations": {"x": {"draworder": [{"time": 0,
                                                    "offsets": []}]}}}
        self.assertTrue(has_draw_order_timelines(data))
        self.assertFalse(has_draw_order_timelines({"animations": {"x": {}}}))

    def test_optimize_rewrites_offsets_and_keeps_absolute_order(self):
        slots = [
            {"name": "n1", "bone": "root", "attachment": "red"},
            {"name": "a1", "bone": "root", "attachment": "blue",
             "blend": "additive"},
            {"name": "n2", "bone": "root", "attachment": "green"},
        ]
        skins = [{"name": "default", "attachments": {
            "n1": _region("red", -60), "a1": _region("blue", 0),
            "n2": _region("green", 60)}}]
        anims = {"x": {
            "bones": {"root": {"rotate": [{"time": 0, "angle": 0},
                                          {"time": 0.5, "angle": 10}]}},
            "drawOrder": [
                {"time": 0},
                {"time": 0.2, "offsets": [{"slot": "n1", "offset": 2}]},
            ]}}
        data = _base(slots, skins, anims)
        names = [s["name"] for s in slots]
        before = [apply_draw_order_offsets(names, k.get("offsets"))
                  for k in anims["x"]["drawOrder"]]
        res = optimize_draw_order(data, fps=10, tolerance=5, threshold=0.02,
                                  textures=_textures())
        self.assertTrue(res["success"], res.get("message"))
        out = res["modified_data"]
        new_names = [s["name"] for s in out["slots"]]
        self.assertEqual(new_names, ["n1", "n2", "a1"])
        after = [apply_draw_order_offsets(new_names, k.get("offsets"))
                 for k in out["animations"]["x"]["drawOrder"]]
        self.assertEqual(after, before)
        # the input data is untouched
        self.assertEqual([s["name"] for s in data["slots"]], names)


if __name__ == "__main__":
    unittest.main()
