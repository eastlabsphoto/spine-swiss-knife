"""Spine Version Converter tab — convert Spine files between versions via CLI.

Also retains the original JSON-level downgrade helpers (``convert_to_38``,
``convert_to_37``, etc.) used by ``app.py._import_back_to_spine``.
"""

import json as _json
import os
import re
import shutil
import tempfile

from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QComboBox,
    QTabWidget, QTextEdit, QMessageBox, QTreeWidget, QTreeWidgetItem,
    QFileDialog, QHeaderView, QAbstractItemView,
)
from PySide6.QtGui import QTextCharFormat, QColor, QFont, QTextCursor
from PySide6.QtCore import Qt

from .i18n import tr, language_changed
from .spine_json import load_spine_json
from .spine_cli import (
    read_spine_file_version, export_spine_project, import_to_spine_project,
)
from .settings import settings


# ==========================================================================
# Version detection
# ==========================================================================

def _read_source_version(data: dict) -> str:
    v = data.get("skeleton", {}).get("spine", "")
    return str(v).split("-from-")[0] if v else ""


def _normalize_version(tag: str) -> str:
    t = tag.strip()
    if re.match(r"^4(\.|$)", t):
        return "4.x"
    if re.match(r"^3\.8(\.|$)", t):
        return "3.8"
    if re.match(r"^3\.7(\.|$)", t):
        return "3.7"
    if re.match(r"^3\.6(\.|$)", t):
        return "3.6"
    return "unknown"


_RANK = {"4.x": 4, "3.8": 3, "3.7": 2, "3.6": 1, "unknown": 99}


# ==========================================================================
# Conversion helpers
# ==========================================================================

def _ensure_skeleton_tag(data: dict, version: str) -> dict:
    if "skeleton" not in data:
        data["skeleton"] = {}
    old = data["skeleton"].get("spine", "")
    data["skeleton"]["spine"] = f"{version}-from-{old}" if old else version
    return data


def _linearize_curves(node):
    if node is None:
        return
    if isinstance(node, list):
        for item in node:
            _linearize_curves(item)
        return
    if not isinstance(node, dict):
        return
    if "curve" in node and not isinstance(node["curve"], str):
        del node["curve"]
    for k in node:
        _linearize_curves(node[k])


def _rollback_curves(node, parent_name=None, parent_is_array=False):
    if node is None:
        return
    if isinstance(node, list):
        for item in node:
            _rollback_curves(item, parent_name, True)
        return
    if not isinstance(node, dict):
        return
    if parent_is_array:
        if "time" not in node:
            node["time"] = 0.0
        if parent_name == "rotate" and "angle" not in node:
            node["angle"] = 0.0
        if parent_name == "scale":
            if "x" not in node:
                node["x"] = 1.0
            if "y" not in node:
                node["y"] = 1.0
    if "curve" in node:
        c = node["curve"]
        if isinstance(c, (int, float)):
            c2 = node.pop("c2", 0)
            c3 = node.pop("c3", 1)
            c4 = node.pop("c4", 1)
            node["curve"] = [c, c2, c3, c4]
        return
    for k, child in list(node.items()):
        _rollback_curves(child, k, isinstance(child, list))


def _rename_constraint_mixes(obj: dict):
    if not isinstance(obj, dict):
        return
    if "mixRotate" in obj:
        obj["rotateMix"] = obj.pop("mixRotate")
    if "mixX" in obj or "mixY" in obj:
        val = obj.pop("mixX", None)
        if val is None:
            val = obj.pop("mixY", None)
        else:
            obj.pop("mixY", None)
        if val is not None:
            obj["translateMix"] = val
    if "mixScaleX" in obj or "mixScaleY" in obj:
        val = obj.pop("mixScaleX", None)
        if val is None:
            val = obj.pop("mixScaleY", None)
        else:
            obj.pop("mixScaleY", None)
        if val is not None:
            obj["scaleMix"] = val
    if "mixShearX" in obj or "mixShearY" in obj:
        val = obj.pop("mixShearX", None)
        if val is None:
            val = obj.pop("mixShearY", None)
        else:
            obj.pop("mixShearY", None)
        if val is not None:
            obj["shearMix"] = val


# ==========================================================================
# Conversion functions
# ==========================================================================

def convert_to_38(data: dict) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    data = _ensure_skeleton_tag(data, "3.8.99")
    animations = data.get("animations", {})
    if isinstance(animations, dict):
        for anim_name, anim in animations.items():
            if not isinstance(anim, dict):
                continue
            bones = anim.get("bones", {})
            if isinstance(bones, dict):
                for bone_name, timelines in bones.items():
                    if isinstance(timelines, dict):
                        for key in ("translatex", "translatey", "scalex",
                                    "scaley", "shearx", "sheary"):
                            timelines.pop(key, None)
            slots = anim.get("slots", {})
            if isinstance(slots, dict):
                for slot_name, stl in slots.items():
                    if isinstance(stl, dict):
                        stl.pop("rgb", None)
                        stl.pop("rgb2", None)
                        stl.pop("alpha", None)
                        if "rgba" in stl:
                            stl["color"] = stl.pop("rgba")
                        if "rgba2" in stl:
                            stl["twoColor"] = stl.pop("rgba2")
            _linearize_curves(anim)
            bones2 = anim.get("bones", {})
            if isinstance(bones2, dict):
                for b_name, bdata in bones2.items():
                    rot = bdata.get("rotate") if isinstance(bdata, dict) else None
                    if isinstance(rot, list):
                        for f in rot:
                            if isinstance(f, dict) and "value" in f and "angle" not in f:
                                f["angle"] = f.pop("value")
    for key in ("transform", "path"):
        group = data.get(key)
        if isinstance(group, (dict, list)):
            items = group.values() if isinstance(group, dict) else group
            for item in items:
                if isinstance(item, dict):
                    _rename_constraint_mixes(item)
    if isinstance(animations, dict):
        for anim_name, anim in animations.items():
            if not isinstance(anim, dict):
                continue
            for key in ("transform", "path"):
                grp = anim.get(key, {})
                if not isinstance(grp, dict):
                    continue
                for cname, frames in grp.items():
                    if isinstance(frames, list):
                        for frame in frames:
                            if isinstance(frame, dict):
                                _rename_constraint_mixes(frame)
    return data, warnings


def convert_to_37(data: dict) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    data = _ensure_skeleton_tag(data, "3.7.94")
    if isinstance(data.get("skins"), list):
        new_skins = {}
        for s in data["skins"]:
            if isinstance(s, dict) and s.get("name") and s.get("attachments"):
                new_skins[s["name"]] = s["attachments"]
        data["skins"] = new_skins
    _rollback_curves(data.get("animations"))
    return data, warnings


def convert_to_3653(data: dict) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    data = _ensure_skeleton_tag(data, "3.6.53")
    animations = data.get("animations", {})
    if isinstance(animations, dict):
        for anim_name, anim in animations.items():
            if isinstance(anim, dict) and "deform" in anim:
                anim["ffd"] = anim.pop("deform")
    return data, warnings


def convert_to_destination(data: dict, dest: str) -> tuple[dict, list[str]]:
    src_raw = _read_source_version(data)
    src_norm = _normalize_version(src_raw)
    dest_norm = _normalize_version(dest)
    all_warnings: list[str] = []
    src_rank = _RANK.get(src_norm, 99)
    dest_rank = _RANK.get(dest_norm, 1)
    if src_rank < dest_rank and src_norm != "unknown":
        all_warnings.append(f"Cannot upgrade from {src_raw or 'unknown'} to {dest}")
        return data, all_warnings

    def refresh():
        nonlocal src_raw, src_norm
        src_raw = _read_source_version(data)
        src_norm = _normalize_version(src_raw)

    if src_norm in ("4.x", "unknown") and dest_rank <= 3:
        data, w = convert_to_38(data)
        all_warnings.extend(w)
        refresh()
    if src_norm == "3.8" and dest_rank <= 2:
        data, w = convert_to_37(data)
        all_warnings.extend(w)
        refresh()
    if src_norm == "3.7" and dest_rank <= 1:
        data, w = convert_to_3653(data)
        all_warnings.extend(w)
        refresh()
    return data, all_warnings


# ==========================================================================
# UI Tab
# ==========================================================================

class SpineVersionConverterTab:
    """Bulk version converter — upgrade or downgrade .spine / .json files via CLI."""

    DEST_VERSIONS = ["4.2.18", "4.1.24", "4.0.64", "3.8.99", "3.7.94", "3.6.53"]

    def __init__(self, tabs: QTabWidget, get_config):
        self._get_config = get_config
        self._tabs = tabs
        self._files: list[dict] = []  # {path, type, version, item}

        self._page = QWidget()
        tabs.addTab(self._page, tr("converter.tab"))
        layout = QVBoxLayout(self._page)
        layout.setContentsMargins(5, 5, 5, 5)

        # Info label
        self._info = QLabel(tr("converter.info"))
        self._info.setWordWrap(True)
        layout.addWidget(self._info)

        # Toolbar row
        toolbar = QHBoxLayout()
        self._btn_add = QPushButton(tr("converter.add_files"))
        self._btn_add.setCursor(Qt.PointingHandCursor)
        self._btn_add.clicked.connect(self._add_files)
        toolbar.addWidget(self._btn_add)
        self._btn_remove = QPushButton(tr("converter.remove_selected"))
        self._btn_remove.setCursor(Qt.PointingHandCursor)
        self._btn_remove.clicked.connect(self._remove_selected)
        toolbar.addWidget(self._btn_remove)
        self._btn_clear = QPushButton(tr("converter.clear_all"))
        self._btn_clear.setCursor(Qt.PointingHandCursor)
        self._btn_clear.clicked.connect(self._clear_all)
        toolbar.addWidget(self._btn_clear)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        # File list
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels([
            tr("converter.col.filename"),
            tr("converter.col.type"),
            tr("converter.col.version"),
            tr("converter.col.status"),
        ])
        self._tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._tree.setRootIsDecorated(False)
        self._tree.setAlternatingRowColors(True)
        header = self._tree.header()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        layout.addWidget(self._tree, 1)

        # Bottom row: target version + convert button
        bottom = QHBoxLayout()
        self._target_label = QLabel(tr("converter.target_label"))
        bottom.addWidget(self._target_label)
        self._dest_combo = QComboBox()
        self._dest_combo.setEditable(True)
        self._dest_combo.addItems(self.DEST_VERSIONS)
        self._dest_combo.setCurrentText("3.8.99")
        bottom.addWidget(self._dest_combo)
        bottom.addStretch()
        self._convert_btn = QPushButton(tr("converter.convert_btn"))
        self._convert_btn.setProperty("role", "primary")
        self._convert_btn.setCursor(Qt.PointingHandCursor)
        self._convert_btn.clicked.connect(self._convert_all)
        bottom.addWidget(self._convert_btn)
        layout.addLayout(bottom)

        # Stats
        self._stats = QLabel(tr("converter.default_stats"))
        self._stats.setStyleSheet("font-weight: bold; color: #6ec072;")
        layout.addWidget(self._stats)

        # Log area
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setFont(QFont("monospace", 10))
        self._log.setMaximumHeight(140)
        layout.addWidget(self._log)

        language_changed.connect(self._retranslate)

    # ── i18n ──

    def _retranslate(self):
        idx = self._tabs.indexOf(self._page)
        if idx >= 0:
            self._tabs.setTabText(idx, tr("converter.tab"))
        self._info.setText(tr("converter.info"))
        self._btn_add.setText(tr("converter.add_files"))
        self._btn_remove.setText(tr("converter.remove_selected"))
        self._btn_clear.setText(tr("converter.clear_all"))
        self._tree.setHeaderLabels([
            tr("converter.col.filename"),
            tr("converter.col.type"),
            tr("converter.col.version"),
            tr("converter.col.status"),
        ])
        self._target_label.setText(tr("converter.target_label"))
        self._convert_btn.setText(tr("converter.convert_btn"))
        self._stats.setText(tr("converter.default_stats"))

    # ── Log helpers ──

    def _append(self, text: str, color: str = "#cdd6f4", bold: bool = False):
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        if bold:
            fmt.setFontWeight(QFont.Bold)
        cursor = self._log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text + "\n", fmt)
        self._log.setTextCursor(cursor)
        self._log.ensureCursorVisible()

    def _clear_log(self):
        self._log.clear()

    # ── File management ──

    def _add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            None, tr("converter.dialog.add_files"), "",
            tr("converter.filter"),
        )
        if not paths:
            return
        for p in paths:
            # Skip duplicates
            if any(f["path"] == p for f in self._files):
                continue
            ext = Path(p).suffix.lower()
            ftype = ".spine" if ext == ".spine" else ".json"
            version = self._detect_version(p, ftype)
            item = QTreeWidgetItem([
                Path(p).name, ftype, version or "?", tr("converter.status.ready"),
            ])
            self._tree.addTopLevelItem(item)
            self._files.append({"path": p, "type": ftype, "version": version, "item": item})

    def _detect_version(self, path: str, ftype: str) -> str:
        if ftype == ".spine":
            return read_spine_file_version(path)
        # .json — read skeleton.spine
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            return str(data.get("skeleton", {}).get("spine", ""))
        except Exception:
            return ""

    def _remove_selected(self):
        selected = self._tree.selectedItems()
        if not selected:
            return
        for item in selected:
            idx = self._tree.indexOfTopLevelItem(item)
            if idx >= 0:
                self._tree.takeTopLevelItem(idx)
                self._files = [f for f in self._files if f["item"] is not item]

    def _clear_all(self):
        self._tree.clear()
        self._files.clear()

    # ── No-op stub (called by _run_all_tabs_inner) ──

    def _detect(self):
        pass

    # ── Conversion ──

    def _convert_all(self):
        if not self._files:
            QMessageBox.information(None, tr("info.title"), tr("converter.no_files"))
            return

        target = self._dest_combo.currentText().strip()
        if not target:
            QMessageBox.critical(None, tr("err.title"), tr("converter.err.no_target"))
            return

        exe = self._get_config("spine_exe")
        if not exe:
            QMessageBox.critical(None, tr("err.title"), tr("converter.err.no_exe"))
            return

        self._clear_log()
        self._append(tr("converter.log.start", target=target), bold=True)

        ok_count = 0
        fail_count = 0
        from PySide6.QtWidgets import QApplication

        for entry in self._files:
            path = entry["path"]
            ftype = entry["type"]
            item = entry["item"]
            name = Path(path).name

            item.setText(3, tr("converter.status.converting"))
            QApplication.processEvents()

            try:
                if ftype == ".spine":
                    out = self._convert_spine_file(exe, path, target)
                else:
                    out = self._convert_json_file(exe, path, target)
                item.setText(3, tr("converter.status.done"))
                self._append(tr("converter.log.ok", name=name, output=Path(out).name), "#a6e3a1")
                ok_count += 1
            except Exception as e:
                item.setText(3, tr("converter.status.failed"))
                self._append(tr("converter.log.fail", name=name, error=str(e)), "#f38ba8")
                fail_count += 1

        self._stats.setText(tr("converter.stats.done", ok=ok_count, fail=fail_count, total=len(self._files)))
        self._append("")
        self._append(tr("converter.log.finished", ok=ok_count, fail=fail_count), "#a6e3a1", bold=True)

    def _convert_spine_file(self, exe: str, path: str, target: str) -> str:
        """Convert .spine → .spine at target version.

        Flow: export to JSON in tmp → import at target version → output file.
        """
        tmp_dir = tempfile.mkdtemp(prefix="ssk_conv_")
        try:
            # Step 1: export .spine → JSON
            result = export_spine_project(exe, path, tmp_dir, pack=False)
            if not result.success:
                raise RuntimeError(result.stderr or "Export failed")

            json_path = result.output_json
            if not json_path:
                raise RuntimeError("Export produced no JSON")

            # Step 2: import JSON → new .spine at target version
            stem = Path(path).stem
            out_path = str(Path(path).parent / f"{stem}_{target}.spine")
            result2 = import_to_spine_project(
                exe, json_path, out_path, target_version=target,
            )
            if not result2.success:
                raise RuntimeError(result2.stderr or "Import failed")

            return out_path
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _convert_json_file(self, exe: str, path: str, target: str) -> str:
        """Convert .json → .json at target version.

        Flow: patch JSON for target version → import to temp .spine
        at target → export back to JSON.
        """
        tmp_dir = tempfile.mkdtemp(prefix="ssk_conv_")
        try:
            # Step 0: patch JSON for target version compatibility
            from .spine_json import load_spine_json, save_spine_json
            data = load_spine_json(path)
            data, _warnings = convert_to_destination(data, target)
            patched_json = str(Path(tmp_dir) / "patched.json")
            save_spine_json(patched_json, data)

            # Step 1: import patched JSON → temp .spine at target version
            tmp_spine = str(Path(tmp_dir) / "temp.spine")
            result = import_to_spine_project(
                exe, patched_json, tmp_spine, target_version=target,
            )
            if not result.success:
                raise RuntimeError(result.stderr or "Import failed")

            # Step 2: export temp .spine → JSON
            export_dir = str(Path(tmp_dir) / "export")
            result2 = export_spine_project(exe, tmp_spine, export_dir, pack=False)
            if not result2.success:
                raise RuntimeError(result2.stderr or "Export failed")

            if not result2.output_json:
                raise RuntimeError("Export produced no JSON")

            # Step 3: copy to output
            stem = Path(path).stem
            out_path = str(Path(path).parent / f"{stem}_{target}.json")
            shutil.copy2(result2.output_json, out_path)
            return out_path
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


# Backward-compatible alias — keeps existing imports in app.py working
SpineDowngraderTab = SpineVersionConverterTab
