"""Offscreen smoke test of the application shell (no Spine, no event loop)."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from PySide6.QtCore import QSettings
    from PySide6.QtGui import QImage, QColor
    from PySide6.QtWidgets import QApplication, QMessageBox, QDialog
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


SKELETON = {
    "skeleton": {"spine": "3.8.99", "hash": "x", "images": "./sprites/"},
    "bones": [{"name": "root"}],
    "slots": [{"name": "head", "bone": "root", "attachment": "head"}],
    "skins": [{"name": "default", "attachments": {"head": {"head": {
        "x": 0, "y": 0, "width": 4, "height": 4}}}}],
    "animations": {"idle": {"bones": {"root": {"rotate": [
        {"time": 0, "angle": 0}, {"time": 1, "angle": 10}]}}}},
}

ATLAS = (
    "\n"
    "page.png\n"
    "size: 8,8\n"
    "format: RGBA8888\n"
    "filter: Linear,Linear\n"
    "repeat: none\n"
    "head\n"
    "  rotate: false\n"
    "  xy: 0, 0\n"
    "  size: 4, 4\n"
    "  orig: 4, 4\n"
    "  offset: 0, 0\n"
    "  index: -1\n"
)


def write_png(path, size=8):
    img = QImage(size, size, QImage.Format_RGBA8888)
    img.fill(QColor(255, 0, 0, 255))
    assert img.save(path)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class AppSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qapp = QApplication.instance() or QApplication([])
        cls.tmp = tempfile.TemporaryDirectory()
        from spine_swiss_knife.settings import settings
        cls.settings = settings
        cls._orig_qs = settings._qs
        # never touch the user's real QSettings
        settings._qs = QSettings(os.path.join(cls.tmp.name, "test_settings.ini"),
                                 QSettings.IniFormat)
        cls.patches = [
            mock.patch("spine_swiss_knife.spine_cli.detect_spine_executable", return_value=None),
            mock.patch("spine_swiss_knife.welcome_page.detect_spine_executable", return_value=None),
        ]
        for p in cls.patches:
            p.start()

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        cls.settings._qs = cls._orig_qs
        cls.tmp.cleanup()

    def setUp(self):
        from spine_swiss_knife import debug_log
        from spine_swiss_knife.app import SpineSwissKnifeApp
        self.errors = []
        self._collect = self.errors.append
        debug_log._signal.error.connect(self._collect)
        self.window = SpineSwissKnifeApp()

    def tearDown(self):
        from spine_swiss_knife import debug_log
        debug_log._signal.error.disconnect(self._collect)
        self.window.close()

    def _project(self, name="proj"):
        d = os.path.join(self.tmp.name, name)
        os.makedirs(os.path.join(d, "sprites"), exist_ok=True)
        json_path = os.path.join(d, "skel.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(SKELETON, f)
        with open(os.path.join(d, "skel.atlas"), "w", encoding="utf-8") as f:
            f.write(ATLAS)
        write_png(os.path.join(d, "page.png"))
        write_png(os.path.join(d, "sprites", "head.png"), 4)
        write_png(os.path.join(d, "sprites", "stray.png"), 4)
        return d, json_path

    def _fail_dialog(self, *a, **k):
        raise AssertionError("modal dialog shown during automatic refresh: %r" % (a[1:3],))

    def test_enter_json_mode_refresh_no_errors_no_dialogs(self):
        d, json_path = self._project()
        with mock.patch.object(QMessageBox, "information", self._fail_dialog), \
                mock.patch.object(QMessageBox, "warning", self._fail_dialog), \
                mock.patch.object(QMessageBox, "critical", self._fail_dialog), \
                mock.patch.object(QMessageBox, "question", self._fail_dialog), \
                mock.patch.object(QMessageBox, "about", self._fail_dialog):
            self.window._enter_json_mode(json_path)
            self.window._retranslate()
        self.assertEqual(self.errors, [])
        # atlas + images (skeleton.images -> ./sprites) were detected
        self.assertTrue(self.window._atlas_edit.text().endswith("skel.atlas"))
        self.assertTrue(self.window._images_edit.text().endswith("sprites"))
        # validator status line is visible and green for the valid skeleton
        self.assertEqual(self.window._validation_state[0], "ok")
        self.assertFalse(self.window._validation_btn.isHidden())
        # Unused finder is wired and found the stray image
        self.assertEqual(sorted(self.window._tab_unused._unused_images), ["stray"])

    def test_skeleton_without_animations_is_silent(self):
        d, json_path = self._project("noanim")
        data = dict(SKELETON, animations={})
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        with mock.patch.object(QMessageBox, "information", self._fail_dialog), \
                mock.patch.object(QMessageBox, "critical", self._fail_dialog):
            self.window._enter_json_mode(json_path)
        self.assertEqual(self.errors, [])

    def test_unreadable_json_logged_once_without_dialogs(self):
        d, json_path = self._project("broken")
        with open(json_path, "w", encoding="utf-8") as f:
            f.write("{not json")
        with mock.patch.object(QMessageBox, "critical", self._fail_dialog), \
                mock.patch.object(QMessageBox, "warning", self._fail_dialog):
            self.window._enter_json_mode(json_path)
        self.assertEqual(len(self.errors), 1)
        self.assertEqual(self.window._validation_state, ("unreadable",))

    def test_validation_reports_problems(self):
        d, json_path = self._project("invalid")
        data = json.loads(json.dumps(SKELETON))
        data["slots"][0]["bone"] = "nope"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        self.window._enter_json_mode(json_path)
        self.assertEqual(self.window._validation_state[0], "issues")
        self.assertGreaterEqual(self.window._validation_state[1], 1)
        self.assertIn("nope", self.window._validation_details)

    def test_retranslate_survives_failing_tab(self):
        class Bad:
            def _retranslate(self):
                raise RuntimeError("boom")
        self.window._tab_instances.append(Bad())
        self.window._retranslate()
        self.assertTrue(any("boom" in e for e in self.errors))

    def test_sidebar_matches_tabs(self):
        from spine_swiss_knife.app import _SIDEBAR_KEYS
        self.assertEqual(len(_SIDEBAR_KEYS), len(self.window._tab_instances))
        self.assertEqual(self.window._tabs.count(), len(_SIDEBAR_KEYS))


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class AppHelpers(unittest.TestCase):
    def test_pair_atlas_by_stem(self):
        from spine_swiss_knife.app import pair_atlas
        atlases = ["/x/a.atlas", "/x/b.atlas.txt"]
        self.assertEqual(pair_atlas("/x/b.json", atlases, "d"), "/x/b.atlas.txt")
        self.assertEqual(pair_atlas("/x/a.json", atlases, "d"), "/x/a.atlas")
        self.assertEqual(pair_atlas("/x/c.json", atlases, "d"), "d")

    def test_import_plan(self):
        from spine_swiss_knife.app import import_plan
        self.assertEqual(import_plan("/e/a.json", ["/e/a.json"]), ["/e/a.json"])
        self.assertEqual(import_plan("/e/b.json", ["/e/a.json", "/e/b.json"]),
                         ["/e/a.json", "/e/b.json"])
        self.assertEqual(import_plan("/other/c.json", ["/e/a.json", "/e/b.json"]),
                         ["/other/c.json"])

    def test_restore_spine_backup_replaces_partial_file(self):
        from spine_swiss_knife.app import restore_spine_backup
        with tempfile.TemporaryDirectory() as d:
            backup = os.path.join(d, "p_backup.spine")
            spine = os.path.join(d, "p.spine")
            with open(backup, "wb") as f:
                f.write(b"original")
            with open(spine, "wb") as f:
                f.write(b"partial")
            self.assertTrue(restore_spine_backup(backup, spine))
            with open(spine, "rb") as f:
                self.assertEqual(f.read(), b"original")
            os.remove(spine)
            self.assertTrue(restore_spine_backup(backup, spine))
            self.assertFalse(restore_spine_backup(os.path.join(d, "missing"), spine + "2"))

    def test_restore_json_backup_keeps_current_file_in_history(self):
        from spine_swiss_knife.app import restore_json_backup
        from spine_swiss_knife.spine_json import create_backup, list_backups
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.json")
            with open(p, "w") as f:
                f.write('{"v": 1}')
            create_backup(p)
            old = list_backups(p)[0]
            with open(p, "w") as f:
                f.write('{"v": 2}')
            restore_json_backup(p, old)
            with open(p) as f:
                self.assertEqual(json.load(f), {"v": 1})
            # the v2 state was preserved as a newer backup
            newest = list_backups(p)[0]
            with open(newest) as f:
                self.assertEqual(json.load(f), {"v": 2})

    def test_resolve_images_dir(self):
        from spine_swiss_knife.app import resolve_images_dir
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "art"))
            jp = os.path.join(d, "s.json")
            self.assertEqual(resolve_images_dir(jp, {"skeleton": {"images": "./art/"}}),
                             os.path.join(d, "art"))
            self.assertEqual(resolve_images_dir(jp, {"skeleton": {"images": "./nope/"}}), "")
            self.assertEqual(resolve_images_dir(jp, {"skeleton": {}}), "")
            self.assertEqual(resolve_images_dir(jp, []), "")


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class ImportBackFlow(unittest.TestCase):
    """The multi-skeleton re-import with a FAKE Spine CLI (not a real Spine)."""

    @classmethod
    def setUpClass(cls):
        cls.qapp = QApplication.instance() or QApplication([])
        cls.tmp = tempfile.TemporaryDirectory()
        from spine_swiss_knife.settings import settings
        cls.settings = settings
        cls._orig_qs = settings._qs
        settings._qs = QSettings(os.path.join(cls.tmp.name, "s.ini"), QSettings.IniFormat)
        cls.patch = mock.patch("spine_swiss_knife.welcome_page.detect_spine_executable",
                               return_value=None)
        cls.patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.patch.stop()
        cls.settings._qs = cls._orig_qs
        cls.tmp.cleanup()

    def _run(self, fail_on):
        from spine_swiss_knife import app as app_mod
        from spine_swiss_knife.spine_cli import SpineCliResult
        d = tempfile.mkdtemp(dir=self.tmp.name)
        spine = os.path.join(d, "p.spine")
        with open(spine, "wb") as f:
            f.write(b"ORIGINAL")
        jsons = []
        for n in ("a", "b"):
            jp = os.path.join(d, n + ".json")
            with open(jp, "w") as f:
                json.dump(SKELETON, f)
            jsons.append(jp)
        calls = []

        def fake_import(exe, json_path, spine_file, **kw):
            calls.append(os.path.basename(json_path))
            if len(calls) == fail_on:
                with open(spine_file, "wb") as f:
                    f.write(b"PARTIAL")
                return SpineCliResult(success=False, stderr="boom")
            with open(spine_file, "ab") as f:
                f.write(b"+" + os.path.basename(json_path).encode())
            return SpineCliResult(success=True)

        win = app_mod.SpineSwissKnifeApp()
        win._spine_project_path = spine
        win._export_jsons = list(jsons)
        win._json_edit.setText(jsons[0])
        msgs = []
        with mock.patch.object(app_mod, "import_to_spine_project", fake_import), \
                mock.patch.object(QDialog, "exec", return_value=QDialog.Accepted), \
                mock.patch.object(QMessageBox, "information", lambda *a, **k: msgs.append("info")), \
                mock.patch.object(QMessageBox, "warning", lambda *a, **k: msgs.append("warn")):
            win._import_back_to_spine()
        win.close()
        with open(spine, "rb") as f:
            return calls, msgs, f.read()

    def test_all_skeletons_imported(self):
        calls, msgs, content = self._run(fail_on=0)
        self.assertEqual(calls, ["a.json", "b.json"])
        self.assertEqual(msgs, ["info"])
        self.assertEqual(content, b"+a.json+b.json")

    def test_failure_restores_backup_over_partial_file(self):
        calls, msgs, content = self._run(fail_on=2)
        self.assertEqual(calls, ["a.json", "b.json"])
        self.assertEqual(msgs, ["warn"])
        self.assertEqual(content, b"ORIGINAL")


if __name__ == "__main__":
    unittest.main()
