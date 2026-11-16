import copy
import unittest

from spine_swiss_knife.spine_validate import validate_skeleton


def valid38():
    return {
        "skeleton": {"spine": "3.8.99"},
        "bones": [{"name": "root"}, {"name": "b", "parent": "root"}],
        "slots": [{"name": "s1", "bone": "b", "attachment": "img"},
                  {"name": "s2", "bone": "root"}],
        "ik": [{"name": "ik", "bones": ["b"], "target": "root"}],
        "events": {"ev": {}},
        "skins": [{"name": "default", "attachments": {
            "s1": {"img": {"path": "folder/img", "width": 1, "height": 1},
                   "m": {"type": "mesh", "uvs": [0] * 6, "triangles": [0, 1, 2],
                         "vertices": [0] * 6, "hull": 3},
                   "lm": {"type": "linkedmesh", "parent": "m"}},
            "s2": {"clip": {"type": "clipping", "end": "s2", "vertexCount": 3,
                            "vertices": [1, 1, 0, 0, 1.0] * 3}}}}],
        "animations": {"a": {
            "bones": {"b": {"rotate": [{"time": 0, "angle": 1}]}},
            "slots": {"s1": {"attachment": [{"time": 0, "name": "img"}, {"time": 1, "name": None}]}},
            "ik": {"ik": []},
            "drawOrder": [{"time": 0, "offsets": [{"slot": "s1", "offset": 1}]}],
            "events": [{"time": 0, "name": "ev"}]}},
    }


def valid42():
    d = valid38()
    d["skeleton"]["spine"] = "4.2.43"
    d["physics"] = [{"name": "ph", "bone": "b"}]
    d["animations"]["a"]["physics"] = {"ph": []}
    return d


def codes(issues):
    return {i["code"] for i in issues}


class ValidateTests(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(validate_skeleton(valid38()), [])
        self.assertEqual(validate_skeleton(valid42()), [])
        self.assertEqual(validate_skeleton(valid38(), {"folder/img", "m", "lm"}), [])

    def mutate(self, fn, base=valid38):
        d = base()
        fn(d)
        return validate_skeleton(d)

    def test_errors(self):
        cases = {
            "duplicate_bone": lambda d: d["bones"].append({"name": "b", "parent": "root"}),
            "duplicate_slot": lambda d: d["slots"].append({"name": "s1", "bone": "root"}),
            "bone_parent_missing": lambda d: d["bones"][1].update(parent="zz"),
            "bone_parent_order": lambda d: d["bones"].insert(0, {"name": "x", "parent": "root"}),
            "slot_bone_missing": lambda d: d["slots"][0].update(bone="zz"),
            "skin_slot_missing": lambda d: d["skins"][0]["attachments"].update(zz={}),
            "clipping_end_missing": lambda d: d["skins"][0]["attachments"]["s2"]["clip"].update(end="zz"),
            "linkedmesh_parent_missing": lambda d: d["skins"][0]["attachments"]["s1"]["lm"].update(parent="zz"),
            "weights_bone_range": lambda d: d["skins"][0]["attachments"]["s2"]["clip"]["vertices"].__setitem__(1, 9),
            "weights_malformed": lambda d: d["skins"][0]["attachments"]["s2"]["clip"].update(vertices=[1, 1, 0, 0, 1.0] * 3 + [5]),
            "vertices_length": lambda d: d["skins"][0]["attachments"]["s1"]["m"].update(vertices=[0] * 4),
            "constraint_bone_missing": lambda d: d["ik"][0].update(bones=["zz"]),
            "constraint_target_missing": lambda d: d["ik"][0].update(target="zz"),
            "animation_bone_missing": lambda d: d["animations"]["a"]["bones"].update(zz={}),
            "animation_slot_missing": lambda d: d["animations"]["a"]["slots"].update(zz={}),
            "animation_constraint_missing": lambda d: d["animations"]["a"]["ik"].update(zz=[]),
            "draw_order_invalid": lambda d: d["animations"]["a"]["drawOrder"][0]["offsets"][0].update(slot="zz"),
            "event_undeclared": lambda d: d["animations"]["a"]["events"].append({"time": 1, "name": "nope"}),
        }
        for code, fn in cases.items():
            self.assertIn(code, codes(self.mutate(fn)), code)

    def test_path_and_physics(self):
        d = valid42()
        d["path"] = [{"name": "p", "bones": ["b"], "target": "nope"}]
        d["physics"][0]["bone"] = "zz"
        c = codes(validate_skeleton(d))
        self.assertIn("constraint_target_missing", c)
        self.assertIn("constraint_bone_missing", c)

    def test_missing_region(self):
        issues = validate_skeleton(valid38(), {"m"})
        missing = [i for i in issues if i["code"] == "missing_region"]
        self.assertEqual({i["message"] for i in missing},
                         {"image 'folder/img' is not in the atlas", "image 'lm' is not in the atlas"})

    def test_warnings(self):
        d = valid38()
        d["slots"][1]["attachment"] = "ghost"
        d["animations"]["a"]["slots"]["s1"]["attachment"].append({"time": 2, "name": "ghost2"})
        issues = validate_skeleton(d)
        self.assertEqual({i["code"] for i in issues if i["severity"] == "warning"},
                         {"setup_attachment_missing", "timeline_attachment_missing"})
        self.assertFalse([i for i in issues if i["severity"] == "error"])

    def test_never_raises(self):
        for bad in (None, [], 5, {"bones": 5}, {"bones": [None, 3, {"name": []}]},
                    {"slots": [{"name": "s"}], "skins": [5, {"attachments": 3}]},
                    {"skins": {"d": {"s": {"a": 5}}}, "animations": {"x": 1}},
                    {"animations": {"x": {"drawOrder": [{"offsets": [1]}], "events": [3]}}},
                    {"bones": [{"name": "r"}], "slots": [{"name": "s", "bone": "r"}],
                     "skins": [{"name": "d", "attachments": {"s": {"a": {"type": "mesh", "uvs": 3, "vertices": "x"}}}}]}):
            try:
                out = validate_skeleton(copy.deepcopy(bad))
            except Exception as exc:  # pragma: no cover
                self.fail("raised %r for %r" % (exc, bad))
            self.assertIsInstance(out, list)


if __name__ == "__main__":
    unittest.main()
