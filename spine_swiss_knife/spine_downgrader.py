"""Spine Downgrader tab — downgrade Spine JSON versions: 4.x -> 3.8 -> 3.7 -> 3.6."""

import os
import re
import shutil

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QComboBox,
    QTabWidget, QTextEdit, QMessageBox,
)
from PySide6.QtGui import QTextCharFormat, QColor, QFont, QTextCursor

from .i18n import tr, language_changed
from .spine_json import load_spine_json


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

class SpineDowngraderTab:
    DEST_VERSIONS = ["3.8.99", "3.7.94", "3.6.53"]

    def __init__(self, tabs: QTabWidget, get_config):
        self._get_config = get_config
        self._tabs = tabs

        self._page = QWidget()
        tabs.addTab(self._page, tr("downgrader.tab"))
        layout = QVBoxLayout(self._page)
        layout.setContentsMargins(5, 5, 5, 5)

        self._info = QLabel(tr("downgrader.info"))
        self._info.setWordWrap(True)
        layout.addWidget(self._info)

        btn_row = QHBoxLayout()
        self._target_label = QLabel(tr("downgrader.target_label"))
        btn_row.addWidget(self._target_label)
        self._dest_combo = QComboBox()
        self._dest_combo.addItems(self.DEST_VERSIONS)
        self._dest_combo.setCurrentText("3.7.94")
        btn_row.addWidget(self._dest_combo)
        self._convert_btn = QPushButton(tr("downgrader.convert_btn"))
        self._convert_btn.clicked.connect(self._convert)
        btn_row.addWidget(self._convert_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._stats = QLabel(tr("downgrader.default_stats"))
        self._stats.setStyleSheet("font-weight: bold; color: #6ec072;")
        layout.addWidget(self._stats)

        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setFont(QFont("monospace", 10))
        layout.addWidget(self._log, 1)

        language_changed.connect(self._retranslate)

    def _retranslate(self):
        idx = self._tabs.indexOf(self._page)
        if idx >= 0:
            self._tabs.setTabText(idx, tr("downgrader.tab"))
        self._info.setText(tr("downgrader.info"))
        self._target_label.setText(tr("downgrader.target_label"))
        self._convert_btn.setText(tr("downgrader.convert_btn"))
        self._stats.setText(tr("downgrader.default_stats"))

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

    def _detect(self):
        json_path = self._get_config("json")
        if not json_path or not os.path.isfile(json_path):
            QMessageBox.critical(None, tr("err.title"), tr("err.no_json"))
            return
        try:
            data = load_spine_json(json_path)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("err.parse_json", error=e))
            return

        version = _read_source_version(data)
        norm = _normalize_version(version) if version else "unknown"

        self._clear_log()
        self._append(tr("downgrader.log.file", name=os.path.basename(json_path)))
        self._append(tr("downgrader.log.detected", version=version or tr("downgrader.log.not_found")))
        self._append(tr("downgrader.log.family", family=norm))

        if norm == "4.x":
            self._append(tr("downgrader.log.targets_38"))
        elif norm == "3.8":
            self._append(tr("downgrader.log.targets_37"))
        elif norm == "3.7":
            self._append(tr("downgrader.log.targets_36"))
        elif norm == "3.6":
            self._append(tr("downgrader.log.already_36"), "#f9e2af")
        else:
            self._append(tr("downgrader.log.unknown"), "#f9e2af")

        self._stats.setText(tr("downgrader.stats.detected", version=version or 'unknown', family=norm))

    def _convert(self):
        json_path = self._get_config("json")
        if not json_path or not os.path.isfile(json_path):
            QMessageBox.critical(None, tr("err.title"), tr("err.no_json"))
            return
        dest = self._dest_combo.currentText()
        if not dest:
            QMessageBox.critical(None, tr("err.title"), tr("downgrader.err.no_target"))
            return
        try:
            data = load_spine_json(json_path)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("err.parse_json", error=e))
            return

        src_version = _read_source_version(data)
        src_norm = _normalize_version(src_version)
        dest_norm = _normalize_version(dest)

        self._clear_log()
        self._append(tr("downgrader.log.source", name=os.path.basename(json_path)))
        self._append(tr("downgrader.log.source_version", version=src_version or 'unknown', family=src_norm))
        self._append(tr("downgrader.log.target_version", version=dest))
        self._append("")

        if _RANK.get(src_norm, 99) < _RANK.get(dest_norm, 1) and src_norm != "unknown":
            self._append(tr("downgrader.log.cannot_upgrade", src=src_version, dest=dest), "#f38ba8", bold=True)
            self._stats.setText(tr("downgrader.stats.error"))
            return

        if QMessageBox.question(None, tr("confirm.title"),
            tr("downgrader.confirm", src=src_version or 'unknown', dest=dest, backup=json_path + ".backup")) != QMessageBox.Yes:
            return

        try:
            shutil.copy2(json_path, json_path + ".backup")
            self._append(tr("downgrader.log.backup_created"))
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("downgrader.err.backup_failed", error=e))
            return

        try:
            converted, warnings = convert_to_destination(data, dest)
        except Exception as e:
            self._append(tr("downgrader.log.conversion_failed", error=e), "#f38ba8", bold=True)
            self._stats.setText(tr("downgrader.stats.failed"))
            return

        for w in warnings:
            self._append(tr("downgrader.log.warning", msg=w), "#f9e2af")

        stem = os.path.splitext(os.path.basename(json_path))[0]
        out_dir = os.path.dirname(json_path)
        out_path = os.path.join(out_dir, f"{stem}_{dest}.json")

        try:
            from .splitter import _save_spine_format
            _save_spine_format(out_path, converted)
        except Exception as e:
            self._append(tr("downgrader.log.save_failed", error=e), "#f38ba8", bold=True)
            self._stats.setText(tr("downgrader.stats.save_failed"))
            return

        self._append("")
        self._append(tr("downgrader.log.saved", path=out_path), "#a6e3a1", bold=True)
        self._append(tr("downgrader.log.original_backed", path=json_path + ".backup"))
        self._stats.setText(
            tr("downgrader.stats.done", version=dest, filename=os.path.basename(out_path))
        )
