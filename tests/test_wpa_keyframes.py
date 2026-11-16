import random
import unittest

from spine_swiss_knife.keyframe_core import find_redundant_keyframes, remove_redundant_keyframes

V3 = {"skeleton": {"spine": "3.8.99"}}
V4 = {"skeleton": {"spine": "4.2.43"}}


def run(bones, data, tol=0.1):
    anims = {"a": {"bones": {"b": bones}}}
    res = find_redundant_keyframes(anims, tol, data)
    spec = {"animations": anims}
    n = remove_redundant_keyframes(spec, res)
    return res, n, anims["a"]["bones"]["b"]


class KeyframeTests(unittest.TestCase):
    def test_4x_rotate_value_keeps_middle(self):
        _r, n, b = run({"rotate": [{"value": 0}, {"time": .5, "value": 90}, {"time": 1, "value": 0}]}, V4)
        self.assertEqual(n, 0)
        self.assertEqual(len(b["rotate"]), 3)

    def test_4x_collinear_rotate_removed(self):
        _r, n, b = run({"rotate": [{"value": 0}, {"time": .5, "value": 45}, {"time": 1, "value": 90}]}, V4)
        self.assertEqual(n, 1)

    def test_scale_default_one(self):
        _r, n, _b = run({"scale": [{}, {"time": .5, "x": 0, "y": 0}, {"time": 1}]}, V3)
        self.assertEqual(n, 0)

    def test_quadratic_error_bounded(self):
        keys = [{"time": i / 10, "x": 4 * (i / 10) ** 2, "y": 0} for i in range(51)]
        tol = 0.1
        _r, _n, b = run({"translate": [dict(k) for k in keys]}, V3, tol)
        self.assertGreater(len(b["translate"]), 2)
        kept = b["translate"]
        for k in keys:
            t = k["time"]
            lo = max((x for x in kept if x["time"] <= t), key=lambda x: x["time"])
            hi = min((x for x in kept if x["time"] >= t), key=lambda x: x["time"])
            if hi["time"] == lo["time"]:
                exp = lo["x"]
            else:
                r = (t - lo["time"]) / (hi["time"] - lo["time"])
                exp = lo["x"] + r * (hi["x"] - lo["x"])
            self.assertLess(abs(exp - k["x"]), tol)

    def test_3x_rotation_wrap_kept(self):
        rot = [{"angle": 170}, {"time": 1, "angle": 0}, {"time": 2, "angle": -170}]
        _r, n, _b = run({"rotate": rot}, V3)
        self.assertEqual(n, 0)

    def test_unknown_version_conservative(self):
        rot = [{"angle": 170}, {"time": 1, "angle": 0}, {"time": 2, "angle": -170}]
        _r, n, _b = run({"rotate": rot}, None)
        self.assertEqual(n, 0)

    def test_4x_no_wrap_restriction(self):
        rot = [{"value": 170}, {"time": 1, "value": 0}, {"time": 2, "value": -170}]
        _r, n, _b = run({"rotate": rot}, V4)
        self.assertEqual(n, 1)

    def test_scale_tolerance_is_smaller(self):
        # deviation 0.05 passes for translate at T=0.1 but not for scale
        mid = 0.05
        _r, n, _b = run({"translate": [{"x": 0, "y": 0}, {"time": .5, "x": mid, "y": 0}, {"time": 1, "x": 0, "y": 0}]}, V3)
        self.assertEqual(n, 1)
        _r, n, _b = run({"scale": [{"x": 1, "y": 1}, {"time": .5, "x": 1 + mid, "y": 1}, {"time": 1, "x": 1, "y": 1}]}, V3)
        self.assertEqual(n, 0)

    def test_collinear_translate_removed(self):
        tr = [{"x": 0, "y": 0}, {"time": 1, "x": 10, "y": 10}, {"time": 2, "x": 20, "y": 20}]
        _r, n, b = run({"translate": tr}, V3)
        self.assertEqual(n, 1)
        self.assertEqual(len(b["translate"]), 2)

    def test_curves_untouched(self):
        tr = [{"x": 0, "y": 0, "curve": "stepped"}, {"time": 1, "x": 10, "y": 10}, {"time": 2, "x": 20, "y": 20}]
        _r, n, _b = run({"translate": tr}, V3)
        self.assertEqual(n, 0)
        tr = [{"x": 0, "y": 0}, {"time": 1, "x": 10, "y": 10, "curve": [0.3, 0, 0.7, 1]},
              {"time": 2, "x": 20, "y": 20}, {"time": 3, "x": 30, "y": 30}]
        _r, n, _b = run({"translate": tr}, V3)
        self.assertEqual(n, 0)

    def test_random_property(self):
        rnd = random.Random(7)
        for _ in range(200):
            n = rnd.randint(3, 25)
            t = 0.0
            keys = []
            for _i in range(n):
                keys.append({"time": t, "angle": rnd.choice([0, 10, 50, 170, -170, 95, 200]) + rnd.random() * 3})
                t += rnd.random() + 0.01
            keys = [dict(k) for k in keys]
            orig = [dict(k) for k in keys]
            tol = rnd.choice([0.1, 1.0, 5.0])
            res, _n, b = run({"rotate": keys}, V3, tol)
            kept = b["rotate"]
            for k in orig:
                lo = max((x for x in kept if x["time"] <= k["time"]), key=lambda x: x["time"])
                hi = min((x for x in kept if x["time"] >= k["time"]), key=lambda x: x["time"])
                if hi["time"] == lo["time"]:
                    exp = lo["angle"]
                else:
                    r = (k["time"] - lo["time"]) / (hi["time"] - lo["time"])
                    exp = lo["angle"] + r * (hi["angle"] - lo["angle"])
                self.assertLess(abs(exp - k["angle"]), tol + 1e-9)
            for a, c in zip(kept, kept[1:]):
                orig_delta = abs(c["angle"] - a["angle"])
                self.assertTrue(orig_delta < 180 or c["time"] == a["time"] or
                                any(abs(o2["angle"] - o1["angle"]) >= 180 for o1, o2 in zip(orig, orig[1:])
                                    if a["time"] <= o1["time"] and o2["time"] <= c["time"]))


if __name__ == "__main__":
    unittest.main()
