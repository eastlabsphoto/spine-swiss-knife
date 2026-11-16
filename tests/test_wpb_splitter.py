import json
import os
import tempfile
import unittest
from copy import deepcopy

from spine_swiss_knife.spine_validate import validate_skeleton
from spine_swiss_knife.splitter_core import (
    build_split_json, compute_dependencies, format_spine_json,
    sanitize_group_names, split_to_directory,
)


def sample():
    bones = [{"name": n} if n == "root" else {"name": n, "parent": "root"}
             for n in ["root", "static", "arm"]]
    bones += [{"name": "hand", "parent": "arm"}, {"name": "ik_target", "parent": "root"},
              {"name": "wb", "parent": "root"}]
    mesh_verts = [1, 5, 1.0, 2.0, 1.0, 1, 5, 3.0, 4.0, 1.0, 1, 5, 5.0, 6.0, 1.0]
    return {
        "skeleton": {"spine": "3.8.99"},
        "bones": bones,
        "slots": [
            {"name": "bg", "bone": "static", "attachment": "bgimg"},
            {"name": "armslot", "bone": "arm", "attachment": "armmesh"},
            {"name": "other", "bone": "wb", "attachment": "o"},
        ],
        "ik": [{"name": "arm_ik", "bones": ["arm", "hand"], "target": "ik_target"}],
        "skins": [{"name": "default", "attachments": {
            "bg": {"bgimg": {"width": 4, "height": 4}},
            "armslot": {"armmesh": {"type": "mesh", "path": "parts/arm",
                                    "uvs": [0, 0, 1, 0, 1, 1], "triangles": [0, 1, 2],
                                    "vertices": mesh_verts, "hull": 3}},
            "other": {"o": {"width": 1, "height": 1}},
        }, "bones": ["wb", "arm"], "ik": ["arm_ik"]}],
        "animations": {"wave": {"bones": {"arm": {"rotate": [{"time": 0, "angle": 1}]}}}},
    }


class SplitterTests(unittest.TestCase):
    def split(self, data, anims, **kw):
        before = deepcopy(data)
        deps = compute_dependencies(data, anims, **kw)
        out = build_split_json(data, anims, deps)
        self.assertEqual(before, data)
        return deps, out

    def errors(self, out):
        return [i for i in validate_skeleton(out) if i["severity"] == "error"]

    def test_full_sample(self):
        deps, out = self.split(sample(), ["wave"])
        names = [b["name"] for b in out["bones"]]
        self.assertIn("ik_target", names)
        self.assertEqual(len(out["ik"]), 1)
        self.assertIn("parts/arm", deps["image_keys_needed"])
        self.assertIn("bg", [s["name"] for s in out["slots"]])
        self.assertEqual(self.errors(out), [])
        # skin extras kept and filtered
        sk = out["skins"][0]
        self.assertEqual(sk["ik"], ["arm_ik"])
        self.assertEqual(sorted(sk["bones"]), sorted(["wb", "arm"]))

    def test_weighted_remap(self):
        data = sample()
        data["bones"].insert(1, {"name": "pad", "parent": "root"})  # shifts indices
        att = data["skins"][0]["attachments"]["armslot"]["armmesh"]
        for v in range(3):
            att["vertices"][v * 5 + 1] = 6  # index of "wb" after the insert
        deps, out = self.split(data, ["wave"], include_static_visible=False)
        names = [b["name"] for b in out["bones"]]
        self.assertIn("wb", names)
        self.assertNotIn("pad", names)
        verts = out["skins"][0]["attachments"]["armslot"]["armmesh"]["vertices"]
        self.assertEqual(verts[1::5], [names.index("wb")] * 3)
        self.assertEqual(self.errors(out), [])

    def test_static_visible_option(self):
        data = sample()
        _d, off = self.split(data, ["wave"], include_static_visible=False)
        self.assertNotIn("bg", [s["name"] for s in off["slots"]])
        data["slots"][1]["color"] = "ffffff00"
        deps, on = self.split(data, ["wave"], include_static_visible=True)
        self.assertIn("bg", [s["name"] for s in on["slots"]])

    def test_invisible_static_not_kept(self):
        data = sample()
        data["slots"][0]["color"] = "ffffff00"
        _d, out = self.split(data, ["wave"])
        self.assertNotIn("bg", [s["name"] for s in out["slots"]])

    def test_draworder_lowercase_and_deform(self):
        data = sample()
        data["animations"]["wave"] = {"draworder": [
            {"time": 0, "offsets": [{"slot": "other", "offset": -2}]}]}
        _d, out = self.split(data, ["wave"], include_static_visible=False)
        self.assertEqual([s["name"] for s in out["slots"]], ["other"])
        self.assertEqual(self.errors(out), [])
        data["animations"]["wave"] = {"attachments": {"default": {"armslot": {
            "armmesh": {"deform": [{"time": 0}]}}}}}
        _d, out = self.split(data, ["wave"], include_static_visible=False)
        self.assertIn("armslot", [s["name"] for s in out["slots"]])

    def test_constraint_timeline_involves_constraint(self):
        data = sample()
        data["animations"]["wave"] = {"ik": {"arm_ik": [{"time": 0, "mix": 1}]}}
        _d, out = self.split(data, ["wave"], include_static_visible=False)
        self.assertEqual(len(out["ik"]), 1)
        self.assertIn("ik_target", [b["name"] for b in out["bones"]])
        self.assertEqual(self.errors(out), [])

    def test_path_and_clipping(self):
        data = {
            "skeleton": {"spine": "3.8.99"},
            "bones": [{"name": "root"}, {"name": "a", "parent": "root"},
                      {"name": "b", "parent": "root"}, {"name": "c", "parent": "root"}],
            "slots": [{"name": "clip", "bone": "a", "attachment": "cl"},
                      {"name": "mid", "bone": "b", "attachment": "m"},
                      {"name": "endslot", "bone": "c", "attachment": "e"},
                      {"name": "pathslot", "bone": "c", "attachment": "p"}],
            "path": [{"name": "pc", "bones": ["a"], "target": "pathslot"}],
            "skins": {"default": {
                "clip": {"cl": {"type": "clipping", "end": "endslot", "vertexCount": 3,
                                "vertices": [0, 0, 1, 0, 1, 1]}},
                "mid": {"m": {"width": 1, "height": 1}},
                "endslot": {"e": {"width": 1, "height": 1}},
                "pathslot": {"p": {"type": "path", "vertexCount": 3,
                                   "lengths": [1], "vertices": [0, 0, 1, 1, 2, 2]}},
            }},
            "animations": {"x": {"bones": {"a": {"rotate": [{"time": 0, "angle": 2}]}}}},
        }
        _d, out = self.split(data, ["x"], include_static_visible=False)
        self.assertEqual([s["name"] for s in out["slots"]],
                         ["clip", "mid", "endslot", "pathslot"])
        self.assertEqual(len(out["path"]), 1)
        self.assertEqual(self.errors(out), [])

    def test_linkedmesh_parent_and_images(self):
        data = sample()
        atts = data["skins"][0]["attachments"]["armslot"]
        atts["lm"] = {"type": "linkedmesh", "parent": "armmesh", "name": "lmimg",
                      "uvs": [0, 0, 1, 0, 1, 1], "vertices": [0, 0, 1, 0, 1, 1]}
        data["animations"]["wave"]["slots"] = {"armslot": {"attachment": [
            {"time": 0, "name": "lm"}]}}
        deps, out = self.split(data, ["wave"])
        self.assertIn("armmesh", out["skins"][0]["attachments"]["armslot"])
        self.assertIn("lmimg", deps["image_keys_needed"])

    def test_dict_skins_and_save_roundtrip(self):
        data = sample()
        data["bones"][1]["name"] = 'sta"tic\\'
        data["slots"][0]["bone"] = 'sta"tic\\'
        data["slots"][0]["name"] = 'b"g'
        data["skins"][0]["attachments"]['b"g'] = data["skins"][0]["attachments"].pop("bg")
        data["skins"][0]["attachments"]['b"g']['bg"img'] = data["skins"][0][
            "attachments"]['b"g'].pop("bgimg")
        data["slots"][0]["attachment"] = 'bg"img'
        data["animations"]['w"v/big'] = data["animations"].pop("wave")
        _d, out = self.split(data, ['w"v/big'])
        txt = format_spine_json(out)
        self.assertEqual(json.loads(txt), out)
        d37 = deepcopy(data)
        d37["skins"] = {"default": d37["skins"][0]["attachments"]}
        _d, out37 = self.split(d37, ['w"v/big'])
        self.assertEqual(json.loads(format_spine_json(out37)), out37)

    def test_sanitize(self):
        m = sanitize_group_names(["win/big", "win:big", "..x ", "a", "A"])
        self.assertEqual(len(set(v.lower() for v in m.values())), 5)
        for v in m.values():
            self.assertFalse(set(v) & set('/\\:*?"<>|'))
            self.assertFalse(v.startswith(".") or v.endswith("."))

    def test_split_to_directory_nested_name(self):
        data = sample()
        data["animations"]["win/big"] = data["animations"]["wave"]
        with tempfile.TemporaryDirectory() as td:
            imgs = os.path.join(td, "imgs", "parts")
            os.makedirs(imgs)
            open(os.path.join(imgs, "arm.png"), "wb").write(b"x")
            created, errors, _ = split_to_directory(
                data, {"win/big": ["win/big"]}, os.path.join(td, "out"),
                images_dir=os.path.join(td, "imgs"))
            self.assertEqual((created, errors), (1, []))
            self.assertTrue(os.path.isfile(os.path.join(td, "out", "win_big", "win_big.json")))
            self.assertTrue(os.path.isfile(
                os.path.join(td, "out", "win_big", "images", "parts", "arm.png")))


if __name__ == "__main__":
    unittest.main()
