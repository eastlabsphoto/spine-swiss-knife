import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from spine_swiss_knife import spine_cli


def _touch(path: Path, data: bytes = b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _fake_export(files):
    """Fake _run_cli writing *files* (relative paths) into settings['output']."""
    def run(cmd, timeout=600, on_output=None):
        import json
        settings = json.load(open(cmd[cmd.index("-e") + 1]))
        for rel in files:
            _touch(Path(settings["output"]) / rel, rel.encode())
        return 0, "", ""
    return run


class ExportFirstFramesTests(unittest.TestCase):
    def test_existing_files_survive_and_matching(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out"
            _touch(out / "keep.png")
            _touch(out / "sub" / "keep2.png")
            _touch(out / "notes.txt")
            fake = _fake_export(["win2/0000.png", "win/0000.png",
                                 "fx/win/0000.png", "other/0000.png"])
            with mock.patch.object(spine_cli, "_run_cli", fake), \
                    mock.patch.object(spine_cli, "read_spine_file_version",
                                      return_value=""):
                res = spine_cli.export_first_frames(
                    "exe", "p.spine", str(out), {"class": "export-png"},
                    ["win2", "win", "fx/win"])
            self.assertTrue((out / "keep.png").exists())
            self.assertTrue((out / "sub" / "keep2.png").exists())
            self.assertTrue((out / "notes.txt").exists())
            self.assertEqual(set(res), {"win2", "win", "fx/win"})
            self.assertEqual(Path(res["win2"]).read_bytes(), b"win2/0000.png")
            self.assertEqual(Path(res["win"]).read_bytes(), b"win/0000.png")
            self.assertEqual(Path(res["fx/win"]).read_bytes(), b"fx/win/0000.png")
            self.assertEqual(Path(res["fx/win"]).name, "fx_win.png")
            self.assertFalse((out / "other.png").exists())

    def test_stem_match_and_failure(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out"
            fake = _fake_export(["win2.png", "walk0003.png"])
            with mock.patch.object(spine_cli, "_run_cli", fake), \
                    mock.patch.object(spine_cli, "read_spine_file_version",
                                      return_value=""):
                res = spine_cli.export_first_frames(
                    "exe", "p.spine", str(out), {}, ["win2", "walk"])
            self.assertEqual(set(res), {"win2", "walk"})
            with mock.patch.object(spine_cli, "_run_cli",
                                   lambda *a, **k: (1, "", "boom")), \
                    mock.patch.object(spine_cli, "read_spine_file_version",
                                      return_value=""):
                self.assertEqual(spine_cli.export_first_frames(
                    "exe", "p.spine", str(out), {}, ["win2"]), {})


class ExportProjectTests(unittest.TestCase):
    def test_lists_only_fresh_files_and_prefers_stem(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out"
            _touch(out / "stale.json")
            old = time.time() - 3600
            os.utime(out / "stale.json", (old, old))
            fake = _fake_export(["zeta.json", "proj.json", "alpha.json",
                                 "proj.atlas", "other.atlas"])
            with mock.patch.object(spine_cli, "_run_cli", fake), \
                    mock.patch.object(spine_cli, "read_spine_file_version",
                                      return_value=""):
                r = spine_cli.export_spine_project(
                    "exe", str(Path(d) / "proj.spine"), str(out))
            self.assertTrue(r.success)
            names = [Path(p).name for p in r.output_jsons]
            self.assertEqual(names, ["alpha.json", "proj.json", "zeta.json"])
            self.assertEqual(Path(r.output_json).name, "proj.json")
            self.assertEqual(Path(r.output_atlas).name, "proj.atlas")
            self.assertEqual(len(r.output_atlases), 2)


class RunCliTests(unittest.TestCase):
    def test_stdin_devnull(self):
        import subprocess
        import sys
        rc, out, err = spine_cli._run_cli(
            [sys.executable, "-c", "import sys;print(repr(sys.stdin.read()))"],
            timeout=30)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "''")

    def test_children_collected_before_kill_on_timeout(self):
        import sys
        calls = []
        real = spine_cli._collect_child_pids

        def spy(pid):
            calls.append(pid)
            return real(pid)
        with mock.patch.object(spine_cli, "_collect_child_pids", spy):
            rc, _o, err = spine_cli._run_cli(
                [sys.executable, "-c", "import time;time.sleep(30)"], timeout=1)
        self.assertEqual(rc, -1)
        self.assertIn("Timed out", err)
        self.assertTrue(calls)


if __name__ == "__main__":
    unittest.main()
