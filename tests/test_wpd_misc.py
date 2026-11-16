import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from spine_swiss_knife import project_analyzer as pa  # noqa: E402
from spine_swiss_knife import spine_viewer as sv  # noqa: E402


class AnalyzerTests(unittest.TestCase):
    def test_deform_counted_in_all_formats_and_duration(self):
        data = {"skeleton": {"spine": "4.2.43"}, "bones": [{"name": "root"}],
                "animations": {
                    "a": {"deform": {"default": {"s": {"m": [{"time": 0}, {"time": 3.0}]}}}},
                    "b": {"ffd": {"default": {"s": {"m": [{"time": 1.5}]}}}},
                    "c": {"attachments": {"default": {"s": {"m": {"deform": [
                        {"time": 0}, {"time": 2.0}, {"time": 4.0}]}}}}},
                    "d": {"draworder": [{"time": 0.7}], "events": [{"time": 0.9, "name": "e"}]},
                }}
        res = pa._analyze_animations(data)
        det = {d["name"]: d for d in res["details"]}
        self.assertEqual(det["a"]["keyframes"], 2)
        self.assertEqual(det["a"]["duration"], 3.0)
        self.assertEqual(det["b"]["keyframes"], 1)
        self.assertEqual(det["b"]["duration"], 1.5)
        self.assertEqual(det["c"]["keyframes"], 3)
        self.assertEqual(det["c"]["duration"], 4.0)
        self.assertEqual(det["d"]["keyframes"], 2)
        self.assertEqual(det["d"]["duration"], 0.9)
        self.assertEqual(res["total_keyframes"], 8)

    def test_linkedmesh_images_and_masks(self):
        skins = {"default": {"s": {
            "m": {"type": "mesh", "uvs": [0, 0, 1, 0, 1, 1], "path": "img/m"},
            "l": {"type": "linkedmesh", "parent": "m", "path": "img/l"},
            "r": {"width": 1, "height": 1},
        }, "c": {"clip": {"type": "clipping", "end": "s", "vertexCount": 3,
                          "vertices": [0, 0, 1, 0, 1, 1]}}}}
        res = pa._analyze_attachments(skins)
        self.assertEqual(res["referenced_images"], {"img/m", "img/l", "r"})
        data = {"slots": [{"name": "c", "bone": "root"}, {"name": "s", "bone": "root"}]}
        masked = pa._find_masked_meshes(data, skins)
        self.assertEqual({m["mesh_att"] for m in masked}, {"m", "l"})

    def test_dead_bones_uses_shared_logic(self):
        data = {"bones": [{"name": "root"}, {"name": "a", "parent": "root"},
                          {"name": "b", "parent": "a"}, {"name": "dead", "parent": "root"}],
                "slots": [{"name": "s", "bone": "b"}], "skins": [], "animations": {}}
        self.assertEqual(pa._analyze_dead_bones(data), ["dead"])


class JavaViewerLaunchTests(unittest.TestCase):
    def test_windows_path_is_not_a_regex_template(self):
        text = ('<properties>\n<entry key="lastFile">old</entry>\n</properties>\n')
        out = sv.set_last_file_entry(text, r"C:\Users\me\a&b.json")
        self.assertIn(r'<entry key="lastFile">C:\Users\me\a&amp;b.json</entry>', out)

    def test_insert_and_create(self):
        out = sv.set_last_file_entry("<properties>\n</properties>\n", "/x/a<b>.json")
        self.assertIn("/x/a&lt;b&gt;.json", out)
        fresh = sv.set_last_file_entry("", "/x/a.json")
        self.assertTrue(fresh.startswith("<?xml"))
        self.assertIn('<entry key="lastFile">/x/a.json</entry>', fresh)

    def test_full_viewer_version_gate(self):
        self.assertTrue(sv.full_viewer_supported({"skeleton": {"spine": "3.6.53"}}))
        self.assertFalse(sv.full_viewer_supported({"skeleton": {"spine": "3.8.99"}}))
        self.assertFalse(sv.full_viewer_supported({"skeleton": {"spine": "4.2.43"}}))


if __name__ == "__main__":
    unittest.main()
