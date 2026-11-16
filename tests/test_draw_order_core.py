import unittest
import json
from pathlib import Path

import numpy as np

from spine_swiss_knife.spine_refs import apply_draw_order_offsets

from spine_swiss_knife.draw_order_core import (
    BASELINE_MEMORY_BUDGET,
    baseline_is_reorder_of,
    build_reordered_data,
    plan_baseline_storage,
    sample_times,
    sanitize_dir_name,
    DEFAULT_THRESHOLD_PERCENT,
    DEFAULT_TOLERANCE,
    compare_rgba_arrays,
    compose_layers,
    count_blend_groups,
    iter_group_reducing_block_moves,
    plan_composition_batches,
    resolve_baseline_paths,
    threshold_percent_to_ratio,
)


def _slot(name: str, blend: str) -> dict:
    return {"name": name, "blend": blend}


class ComposeLayersTests(unittest.TestCase):
    def test_additive_layers_are_composed_in_slot_order(self):
        half_red = np.array([[[0.5, 0.0, 0.0, 0.5]]], dtype=np.float32)

        result = compose_layers([
            ("additive", half_red),
            ("additive", half_red),
        ])

        np.testing.assert_allclose(
            result,
            np.array([[[1.0, 0.0, 0.0, 1.0]]], dtype=np.float32),
            atol=1e-6,
        )


class BlockMoveSearchTests(unittest.TestCase):
    def test_block_move_candidates_cover_multi_slot_runs(self):
        slots = [
            _slot("a1", "additive"),
            _slot("n1", "normal"),
            _slot("n2", "normal"),
            _slot("a2", "additive"),
        ]

        candidates = list(iter_group_reducing_block_moves(slots))

        self.assertTrue(
            any(count_blend_groups(candidate) == 2 for candidate in candidates)
        )


class CompositionBatchTests(unittest.TestCase):
    def test_only_normal_items_are_grouped_into_shared_batches(self):
        draw_list = [
            {"type": "clip"},
            {"slot_name": "n1", "blend": "normal"},
            {"slot_name": "n2", "blend": "normal"},
            {"type": "clip_end_marker"},
            {"slot_name": "a1", "blend": "additive"},
            {"slot_name": "a2", "blend": "additive"},
        ]

        batches = list(plan_composition_batches(draw_list))

        self.assertEqual(
            [
                ("normal", ["clip", "n1", "n2", "clip_end_marker"]),
                ("additive", ["a1"]),
                ("additive", ["a2"]),
            ],
            [
                (
                    blend,
                    [item["type"] if "type" in item else item["slot_name"]
                     for item in items],
                )
                for blend, items in batches
            ],
        )


class VisibleAreaCompareTests(unittest.TestCase):
    def test_local_visual_change_fails_against_visible_area(self):
        original = np.zeros((100, 100, 4), dtype=np.uint8)
        candidate = np.zeros((100, 100, 4), dtype=np.uint8)

        original[10:20, 10:20, :] = [255, 255, 255, 255]
        candidate[10:20, 10:20, :] = [255, 255, 255, 255]
        candidate[12:16, 12:16, :] = [0, 0, 0, 255]

        match, stats = compare_rgba_arrays(
            original,
            candidate,
            tolerance=5,
            threshold=0.01,
        )

        self.assertFalse(match)
        self.assertEqual(stats["visible_pixels"], 100)
        self.assertEqual(stats["bad_pixels"], 16)


class ThresholdDefaultsTests(unittest.TestCase):
    def test_ui_defaults_match_recommended_settings(self):
        self.assertEqual(DEFAULT_TOLERANCE, 6)
        # The default was deliberately raised to 1 % ("threshold default 1%").
        self.assertAlmostEqual(DEFAULT_THRESHOLD_PERCENT, 1.0)
        self.assertAlmostEqual(
            threshold_percent_to_ratio(DEFAULT_THRESHOLD_PERCENT),
            0.01,
        )


class DrawOrderCopyTests(unittest.TestCase):
    def test_optimal_is_not_surfaced_in_user_copy(self):
        locale_path = (
            Path(__file__).resolve().parents[1] /
            "spine_swiss_knife" / "locales" / "en.json"
        )
        strings = json.loads(locale_path.read_text(encoding="utf-8"))

        self.assertNotIn("Optimal", strings["draw_order.stats"])
        self.assertNotIn("Optimal", strings["draw_order.confirm"])


class BaselinePathTests(unittest.TestCase):
    def test_first_run_uses_current_json_and_creates_backup(self):
        baseline, backup, should_create = resolve_baseline_paths(
            "/tmp/test.json",
            backup_exists=False,
        )
        self.assertEqual(baseline, "/tmp/test.json")
        self.assertEqual(backup, "/tmp/test.json.backup")
        self.assertTrue(should_create)

    def test_subsequent_runs_use_existing_backup_as_baseline(self):
        baseline, backup, should_create = resolve_baseline_paths(
            "/tmp/test.json",
            backup_exists=True,
            backup_is_reorder=True,
        )
        self.assertEqual(baseline, "/tmp/test.json.backup")
        self.assertEqual(backup, "/tmp/test.json.backup")
        self.assertFalse(should_create)

    def test_backup_that_is_not_a_reorder_is_not_the_baseline(self):
        baseline, backup, should_create = resolve_baseline_paths(
            "/tmp/test.json",
            backup_exists=True,
            backup_is_reorder=False,
        )
        self.assertEqual(baseline, "/tmp/test.json")
        self.assertEqual(backup, "/tmp/test.json.backup")
        self.assertTrue(should_create)


def _skeleton(slot_order):
    slots = {n: {"name": n, "bone": "root", "blend": b}
             for n, b in (("a", "normal"), ("b", "additive"),
                          ("c", "normal"), ("d", "additive"))}
    return {
        "skeleton": {"spine": "3.8.99"},
        "bones": [{"name": "root"}],
        "slots": [dict(slots[n]) for n in slot_order],
        "skins": [{"name": "default", "attachments": {}}],
        "animations": {
            "x": {"bones": {"root": {"rotate": [{"angle": 5}]}},
                  "drawOrder": [
                      {"time": 0},
                      {"time": 0.5, "offsets": [{"slot": "a", "offset": 2}]},
                  ]},
        },
    }


class BaselineIsReorderTests(unittest.TestCase):
    def test_pure_reorder_is_accepted(self):
        cur = _skeleton("abcd")
        base = _skeleton("acbd")
        # draw-order timelines may differ, the rest may not
        base["animations"]["x"]["drawOrder"] = [{"time": 0.2}]
        self.assertTrue(baseline_is_reorder_of(cur, base))

    def test_other_changes_are_rejected(self):
        cur = _skeleton("abcd")
        base = _skeleton("acbd")
        base["bones"].append({"name": "extra", "parent": "root"})
        self.assertFalse(baseline_is_reorder_of(cur, base))

        base = _skeleton("acbd")
        base["slots"][0]["blend"] = "multiply"
        self.assertFalse(baseline_is_reorder_of(cur, base))

        base = _skeleton("acbd")
        base["animations"]["x"]["bones"]["root"]["rotate"][0]["angle"] = 9
        self.assertFalse(baseline_is_reorder_of(cur, base))

        base = _skeleton("abc")
        self.assertFalse(baseline_is_reorder_of(cur, base))

    def test_lowercase_draworder_is_ignored(self):
        cur = _skeleton("abcd")
        base = _skeleton("acbd")
        anim = base["animations"]["x"]
        anim["draworder"] = anim.pop("drawOrder")
        self.assertTrue(baseline_is_reorder_of(cur, base))


class ReorderedDataTests(unittest.TestCase):
    def _absolute(self, data):
        names = [s["name"] for s in data["slots"]]
        out = []
        for key in data["animations"]["x"]["drawOrder"]:
            out.append(apply_draw_order_offsets(names, key.get("offsets")))
        return out

    def test_reorder_keeps_absolute_order_of_every_key(self):
        data = _skeleton("abcd")
        data["animations"]["x"]["drawOrder"].append(
            {"time": 1.0, "offsets": [{"slot": "b", "offset": 1},
                                      {"slot": "c", "offset": -1}]})
        before = self._absolute(data)
        new_slots = [data["slots"][i] for i in (0, 2, 1, 3)]
        for deep in (False, True):
            out = build_reordered_data(data, new_slots, deep=deep)
            self.assertEqual([s["name"] for s in out["slots"]], list("acbd"))
            self.assertEqual(self._absolute(out), before)
        # the input was not modified
        self.assertEqual([s["name"] for s in data["slots"]], list("abcd"))
        self.assertEqual(self._absolute(data), before)

    def test_time_before_first_key_keeps_old_setup_order(self):
        data = _skeleton("abcd")
        data["animations"]["x"]["drawOrder"] = [
            {"time": 0.5, "offsets": [{"slot": "a", "offset": 2}]}]
        out = build_reordered_data(
            data, [data["slots"][i] for i in (0, 2, 1, 3)])
        keys = out["animations"]["x"]["drawOrder"]
        self.assertEqual(keys[0]["time"], 0)
        names = list("acbd")
        self.assertEqual(apply_draw_order_offsets(names, keys[0]["offsets"]),
                         list("abcd"))
        self.assertEqual(apply_draw_order_offsets(names, keys[1]["offsets"]),
                         ["b", "c", "a", "d"])

    def test_lowercase_draworder_is_rewritten(self):
        data = _skeleton("abcd")
        anim = data["animations"]["x"]
        anim["draworder"] = anim.pop("drawOrder")
        names = list("abcd")
        before = [apply_draw_order_offsets(names, k.get("offsets"))
                  for k in anim["draworder"]]
        out = build_reordered_data(
            data, [data["slots"][i] for i in (0, 2, 1, 3)])
        new_names = list("acbd")
        after = [apply_draw_order_offsets(new_names, k.get("offsets"))
                 for k in out["animations"]["x"]["draworder"]]
        self.assertEqual(after, before)


class SamplingAndStorageTests(unittest.TestCase):
    def test_last_sample_is_exactly_the_duration(self):
        for dur in (1.0, 0.5, 0.52, 2.0 / 3):
            times = sample_times(dur, 60)
            self.assertEqual(times[0], 0.0)
            self.assertEqual(times[-1], dur)
            self.assertEqual(len(times), len(set(times)))

    def test_small_job_stays_in_memory_at_requested_fps(self):
        fps, mode, est = plan_baseline_storage([1.0, 2.0], 1, 256, 256, 60)
        self.assertEqual((fps, mode), (60, "memory"))
        self.assertLess(est, BASELINE_MEMORY_BUDGET)

    def test_fps_is_lowered_stepwise_then_spilled(self):
        # 1500x1500, 20 animations x 2 s, 1 skin
        durations = [2.0] * 20
        fps, mode, _ = plan_baseline_storage(durations, 1, 1500, 1500, 60,
                                             budget=int(8e9))
        self.assertEqual((fps, mode), (20, "memory"))
        fps, mode, est = plan_baseline_storage(durations, 1, 1500, 1500, 60)
        self.assertEqual((fps, mode), (15, "disk"))
        self.assertGreater(est, BASELINE_MEMORY_BUDGET)


class DirNameTests(unittest.TestCase):
    def test_names_are_sanitised_and_distinct_from_setup_pose(self):
        for raw in ("win?", "idle: loop", "a/b", "x. ", 'q"<>|*'):
            name = sanitize_dir_name(raw)
            self.assertTrue(name.startswith("anim_"))
            for ch in '/\\:*?"<>|':
                self.assertNotIn(ch, name)
            self.assertFalse(name.endswith((".", " ")))
        self.assertNotEqual(sanitize_dir_name("setup_pose"),
                            sanitize_dir_name(None))


class LocalCompareTests(unittest.TestCase):
    def _frames(self, x0, y0, size=60):
        a = np.zeros((1000, 1000, 4), dtype=np.uint8)
        a[:, :, :] = [200, 200, 200, 255]
        b = a.copy()
        b[y0:y0 + size, x0:x0 + size, :] = [0, 0, 0, 255]
        return a, b

    def test_small_element_change_in_large_area_fails(self):
        for x0, y0 in ((10, 10), (100, 100), (70, 40), (500, 531)):
            a, b = self._frames(x0, y0)
            # 3600 of 1_000_000 px (0.36 %) is below the 1 % global threshold
            match, stats = compare_rgba_arrays(a, b, tolerance=6, threshold=0.01)
            self.assertFalse(match, (x0, y0))
            self.assertLess(stats["bad_pixels"] / stats["visible_pixels"], 0.01)

    def test_identical_and_noisy_frames_still_match(self):
        a, _ = self._frames(0, 0)
        match, _ = compare_rgba_arrays(a, a.copy(), tolerance=6, threshold=0.01)
        self.assertTrue(match)
        noisy = a.copy()
        noisy[::7, ::5, 0] += 3  # below tolerance
        match, _ = compare_rgba_arrays(a, noisy, tolerance=6, threshold=0.01)
        self.assertTrue(match)

    def test_sparse_edge_pixels_do_not_trip_cell_check(self):
        a, _ = self._frames(0, 0)
        b = a.copy()
        b[::50, ::50, :3] = 0  # 400 isolated pixels, ~1 % of each cell
        match, _ = compare_rgba_arrays(a, b, tolerance=6, threshold=0.01)
        self.assertTrue(match)


if __name__ == "__main__":
    unittest.main()
