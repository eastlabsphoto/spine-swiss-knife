import unittest
import json
from pathlib import Path

import numpy as np

from spine_swiss_knife.draw_order_core import (
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
        self.assertAlmostEqual(DEFAULT_THRESHOLD_PERCENT, 0.15)
        self.assertAlmostEqual(
            threshold_percent_to_ratio(DEFAULT_THRESHOLD_PERCENT),
            0.0015,
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
        )
        self.assertEqual(baseline, "/tmp/test.json.backup")
        self.assertEqual(backup, "/tmp/test.json.backup")
        self.assertFalse(should_create)


if __name__ == "__main__":
    unittest.main()
