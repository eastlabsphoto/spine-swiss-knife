import copy
import random
import unittest

from spine_swiss_knife import spine_refs as R


def sample():
    return {
        "skeleton": {"spine": "3.8.99"},
        "bones": [
            {"name": "root"},
            {"name": "a", "parent": "root"},
            {"name": "b", "parent": "root"},
            {"name": "dead", "parent": "root"},
            {"name": "c", "parent": "b"},
        ],
        "slots": [{"name": "s1", "bone": "a"}, {"name": "s2", "bone": "root"},
                  {"name": "s3", "bone": "root"}],
        "ik": [{"name": "ik1", "bones": ["a"], "target": "b"}],
        "skins": [{"name": "default", "attachments": {
            "s1": {"m": {"type": "mesh", "uvs": [0] * 4, "hull": 2,
                         "vertices": [1, 4, 0, 0, 1.0, 1, 2, 0, 0, 1.0]}},
            "s2": {"clip": {"type": "clipping", "end": "s3", "vertexCount": 2,
                            "vertices": [1, 2, 0, 0, 1.0, 1, 4, 0, 0, 1.0]}}}}],
        "animations": {"anim": {"bones": {"dead": {"rotate": [{"angle": 1}]},
                                          "a": {"rotate": []}},
                                "slots": {"s1": {"attachment": []}}}},
    }


class BoneTests(unittest.TestCase):
    def test_references_and_dead(self):
        d = sample()
        refs = R.collect_bone_references(d)
        self.assertEqual(refs["a"], {"slot", "constraint", "animation"})
        self.assertEqual(refs["c"], {"weights"})
        self.assertEqual(refs["b"], {"constraint", "weights"})
        dead, alive = R.find_dead_bones(d)
        self.assertEqual(dead, [])
        # 'dead' is referenced by an animation timeline -> alive
        self.assertIn("dead", alive)
        del d["animations"]["anim"]["bones"]["dead"]
        dead, alive = R.find_dead_bones(d)
        self.assertEqual(dead, ["dead"])
        self.assertIn("has_alive_child", alive["b"])
        self.assertIn("has_alive_child", alive["root"])

    def test_remove_and_remap(self):
        d = sample()
        del d["animations"]["anim"]["bones"]["dead"]
        d["animations"]["anim"]["bones"]["dead"] = {}
        n = R.remove_bones(d, ["dead", "nonexistent"])
        self.assertEqual(n, 1)
        self.assertEqual([b["name"] for b in d["bones"]], ["root", "a", "b", "c"])
        self.assertNotIn("dead", d["animations"]["anim"]["bones"])
        mesh = d["skins"][0]["attachments"]["s1"]["m"]
        self.assertEqual(mesh["vertices"][1], 3)  # c: 4 -> 3
        self.assertEqual(mesh["vertices"][6], 2)  # b unchanged
        self.assertEqual(d["skins"][0]["attachments"]["s2"]["clip"]["vertices"][1], 2)

    def test_remove_blocked(self):
        d = sample()
        with self.assertRaises(ValueError) as cm:
            R.remove_bones(d, ["a", "b"])
        msg = str(cm.exception)
        self.assertIn("'a'", msg)
        self.assertIn("parent of remaining bone 'c'", msg)
        self.assertEqual(len(d["bones"]), 5)  # untouched

    def test_index_map(self):
        old = [{"name": "x"}, {"name": "y"}, {"name": "z"}]
        new = [{"name": "z"}, {"name": "x"}]
        self.assertEqual(R.bone_index_map(old, new), {0: 1, 2: 0})
        d = sample()
        with self.assertRaises(KeyError):
            R.remap_bone_indices(d, {2: 0})


class RenameTests(unittest.TestCase):
    def full(self):
        d = sample()
        d["path"] = [{"name": "p", "bones": ["a"], "target": "s2"}]
        d["animations"]["anim"]["drawOrder"] = [
            {"time": 0, "offsets": [{"slot": "s1", "offset": 2}]}]
        d["animations"]["anim"]["draworder"] = [
            {"time": 1, "offsets": [{"slot": "s2", "offset": 1}]}]
        d["animations"]["anim"]["deform"] = {"default": {"s1": {"m": [{}]}}}
        d["animations"]["anim"]["attachments"] = {"default": {"s1": {"m": {"deform": []}}}}
        d["animations"]["anim"]["ffd"] = {"default": {"s2": {"clip": []}}}
        return d

    def test_collisions(self):
        d = sample()
        self.assertTrue(R.find_rename_collisions(d, {"s1": "x", "s2": "x"}))
        self.assertTrue(R.find_rename_collisions(d, {"s1": "s2"}))
        self.assertEqual(R.find_rename_collisions(d, {"s1": "s2", "s2": "s1"}), [])
        self.assertEqual(R.find_rename_collisions(d, {"s1": "s2", "s2": "s3", "s3": "q"}), [])
        with self.assertRaises(ValueError):
            R.rename_slots(d, {"s1": "s2"})

    def test_rename_everything_with_swap(self):
        d = self.full()
        R.rename_slots(d, {"s1": "s2", "s2": "s1", "s3": "end"})
        self.assertEqual([s["name"] for s in d["slots"]], ["s2", "s1", "end"])
        atts = d["skins"][0]["attachments"]
        self.assertEqual(set(atts), {"s1", "s2"})
        self.assertIn("m", atts["s2"])
        self.assertEqual(atts["s1"]["clip"]["end"], "end")
        self.assertEqual(d["path"][0]["target"], "s1")
        a = d["animations"]["anim"]
        self.assertEqual(set(a["slots"]), {"s2"})
        self.assertEqual(a["drawOrder"][0]["offsets"][0]["slot"], "s2")
        self.assertEqual(a["draworder"][0]["offsets"][0]["slot"], "s1")
        self.assertEqual(set(a["deform"]["default"]), {"s2"})
        self.assertEqual(set(a["attachments"]["default"]), {"s2"})
        self.assertEqual(set(a["ffd"]["default"]), {"s1"})

    def test_rename_dict_skins(self):
        d = {"slots": [{"name": "a", "bone": "r"}],
             "skins": {"default": {"a": {"x": {}}}}}
        R.rename_slots(d, {"a": "b"})
        self.assertEqual(set(d["skins"]["default"]), {"b"})

    def test_list_skin_extras_preserved(self):
        d = self.full()
        d["skins"][0]["bones"] = ["a"]
        R.rename_slots(d, {"s1": "z"})
        self.assertEqual(d["skins"][0]["bones"], ["a"])


class DrawOrderTests(unittest.TestCase):
    def test_decode_basic(self):
        names = ["a", "b", "c", "d"]
        self.assertEqual(R.apply_draw_order_offsets(names, None), names)
        self.assertEqual(R.apply_draw_order_offsets(names, []), names)
        # move a to position 2
        self.assertEqual(R.apply_draw_order_offsets(names, [{"slot": "a", "offset": 2}]),
                         ["b", "c", "a", "d"])

    def test_decode_errors(self):
        names = ["a", "b", "c"]
        bad = [
            [{"slot": "zz", "offset": 0}],
            [{"slot": "b", "offset": 0}, {"slot": "a", "offset": 0}],
            [{"slot": "a", "offset": 5}],
            [{"slot": "a", "offset": -1}],
            [{"slot": "a", "offset": 1}, {"slot": "b", "offset": 0}],
        ]
        for offs in bad:
            with self.assertRaises(ValueError, msg=str(offs)):
                R.apply_draw_order_offsets(names, offs)

    def test_roundtrip_property(self):
        rng = random.Random(12345)
        for n in range(1, 13):
            names = ["s%d" % i for i in range(n)]
            for _ in range(200):
                target = names[:]
                rng.shuffle(target)
                offs = R.encode_draw_order_offsets(names, target)
                self.assertEqual(R.apply_draw_order_offsets(names, offs), target)
                self.assertEqual([o["slot"] for o in offs],
                                 sorted((o["slot"] for o in offs), key=names.index))
                if target == names:
                    self.assertEqual(offs, [])

    def test_encode_minimal(self):
        names = ["a", "b", "c", "d"]
        offs = R.encode_draw_order_offsets(names, ["b", "c", "a", "d"])
        self.assertEqual(len(offs), 1)  # only 'a' moves

    def test_rewrite_keeps_absolute_order(self):
        rng = random.Random(7)
        for _ in range(100):
            n = rng.randint(1, 10)
            old = ["s%d" % i for i in range(n)]
            new = old[:]
            rng.shuffle(new)
            keys = []
            absolute = []
            for t in range(4):
                tgt = old[:]
                rng.shuffle(tgt)
                absolute.append(tgt)
                offs = R.encode_draw_order_offsets(old, tgt)
                k = {"time": t}
                if offs:
                    k["offsets"] = offs
                keys.append(k)
            d = {"animations": {"x": {"drawOrder": keys}, "y": {"draworder": copy.deepcopy(keys)}}}
            R.rewrite_draw_order_offsets(d, old, new)
            for anim, field in (("x", "drawOrder"), ("y", "draworder")):
                for k, tgt, orig in zip(d["animations"][anim][field], absolute, keys):
                    if "offsets" not in orig:
                        continue  # keys without offsets are left alone by design
                    self.assertNotEqual(k.get("offsets"), [])
                    self.assertEqual(R.apply_draw_order_offsets(new, k.get("offsets")), tgt)

    def test_rewrite_count_and_removal(self):
        old = ["a", "b", "c"]
        d = {"animations": {"x": {"drawOrder": [
            {"time": 0}, {"time": 1, "offsets": [{"slot": "a", "offset": 1}]}]}}}
        # new order makes that absolute order equal to setup order
        n = R.rewrite_draw_order_offsets(d, old, ["b", "a", "c"])
        self.assertEqual(n, 1)
        self.assertNotIn("offsets", d["animations"]["x"]["drawOrder"][1])
        self.assertNotIn("offsets", d["animations"]["x"]["drawOrder"][0])


if __name__ == "__main__":
    unittest.main()
