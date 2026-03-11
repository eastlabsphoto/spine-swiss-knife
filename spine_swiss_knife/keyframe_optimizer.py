"""Keyframe Optimizer tab — detect and remove redundant animation keyframes from Spine 3.x JSON."""

import os
import shutil

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QLineEdit,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QHeaderView, QMessageBox,
)

from PySide6.QtCore import Qt

from .i18n import tr, language_changed
from .spine_json import load_spine_json, save_spine_json


# ==========================================================================
# Keyframe redundancy detection (unchanged)
# ==========================================================================

_BONE_TIMELINE_FIELDS = {
    "translate": ("x", "y"), "rotate": ("angle",),
    "scale": ("x", "y"), "shear": ("x", "y"),
}
_SLOT_TIMELINE_FIELDS = {"color": ("color",)}
_DEFAULTS = {"x": 0, "y": 0, "angle": 0, "color": "ffffffff"}


def _color_to_floats(color_hex: str) -> tuple[float, ...]:
    color_hex = color_hex.lower()
    r = int(color_hex[0:2], 16) / 255.0
    g = int(color_hex[2:4], 16) / 255.0
    b = int(color_hex[4:6], 16) / 255.0
    a = int(color_hex[6:8], 16) / 255.0
    return (r, g, b, a)


def _get_numeric_values(keyframe: dict, fields: tuple[str, ...]) -> list[float]:
    values: list[float] = []
    for field in fields:
        raw = keyframe.get(field, _DEFAULTS.get(field, 0))
        if field == "color":
            values.extend(_color_to_floats(str(raw)))
        else:
            values.append(float(raw))
    return values


def _is_redundant(prev, cur, nxt, fields, tolerance):
    t_prev = prev.get("time", 0)
    t_cur = cur.get("time", 0)
    t_next = nxt.get("time", 0)
    dt = t_next - t_prev
    if dt == 0:
        return False
    t_ratio = (t_cur - t_prev) / dt
    vals_prev = _get_numeric_values(prev, fields)
    vals_cur = _get_numeric_values(cur, fields)
    vals_next = _get_numeric_values(nxt, fields)
    for vp, vc, vn in zip(vals_prev, vals_cur, vals_next):
        expected = vp + t_ratio * (vn - vp)
        if abs(expected - vc) >= tolerance:
            return False
    return True


def _has_curve(keyframe):
    return "curve" in keyframe


def find_redundant_keyframes(animations, tolerance):
    results = {}
    for anim_name, anim_data in animations.items():
        anim_result = {"bones": {}, "slots": {}, "total_keys": 0, "redundant_keys": 0}

        bones = anim_data.get("bones", {})
        for bone_name, timelines in bones.items():
            bone_redundant = {}
            for tl_type, fields in _BONE_TIMELINE_FIELDS.items():
                keys = timelines.get(tl_type)
                if not keys or len(keys) < 3:
                    if keys:
                        anim_result["total_keys"] += len(keys)
                    continue
                anim_result["total_keys"] += len(keys)
                redundant_indices = []
                for i in range(1, len(keys) - 1):
                    if _has_curve(keys[i - 1]) or _has_curve(keys[i]):
                        continue
                    if _is_redundant(keys[i - 1], keys[i], keys[i + 1], fields, tolerance):
                        redundant_indices.append(i)
                if redundant_indices:
                    bone_redundant[tl_type] = redundant_indices
                    anim_result["redundant_keys"] += len(redundant_indices)
            if bone_redundant:
                anim_result["bones"][bone_name] = bone_redundant

        slots = anim_data.get("slots", {})
        for slot_name, timelines in slots.items():
            slot_redundant = {}
            for tl_type, fields in _SLOT_TIMELINE_FIELDS.items():
                keys = timelines.get(tl_type)
                if not keys or len(keys) < 3:
                    if keys:
                        anim_result["total_keys"] += len(keys)
                    continue
                anim_result["total_keys"] += len(keys)
                redundant_indices = []
                for i in range(1, len(keys) - 1):
                    if _has_curve(keys[i - 1]) or _has_curve(keys[i]):
                        continue
                    if _is_redundant(keys[i - 1], keys[i], keys[i + 1], fields, tolerance):
                        redundant_indices.append(i)
                if redundant_indices:
                    slot_redundant[tl_type] = redundant_indices
                    anim_result["redundant_keys"] += len(redundant_indices)
            att_keys = timelines.get("attachment")
            if att_keys:
                anim_result["total_keys"] += len(att_keys)
            if slot_redundant:
                anim_result["slots"][slot_name] = slot_redundant

        if anim_result["total_keys"] > 0:
            results[anim_name] = anim_result
    return results


def remove_redundant_keyframes(spine_data, analysis):
    animations = spine_data.get("animations", {})
    removed = 0
    for anim_name, info in analysis.items():
        anim_data = animations.get(anim_name)
        if anim_data is None:
            continue
        bones = anim_data.get("bones", {})
        for bone_name, timelines in info["bones"].items():
            bone_data = bones.get(bone_name)
            if bone_data is None:
                continue
            for tl_type, indices in timelines.items():
                keys = bone_data.get(tl_type)
                if keys is None:
                    continue
                for idx in reversed(indices):
                    if idx < len(keys):
                        keys.pop(idx)
                        removed += 1
        slots = anim_data.get("slots", {})
        for slot_name, timelines in info["slots"].items():
            slot_data = slots.get(slot_name)
            if slot_data is None:
                continue
            for tl_type, indices in timelines.items():
                keys = slot_data.get(tl_type)
                if keys is None:
                    continue
                for idx in reversed(indices):
                    if idx < len(keys):
                        keys.pop(idx)
                        removed += 1
    return removed


# ==========================================================================
# UI Tab
# ==========================================================================

class KeyframeOptimizerTab:
    def __init__(self, tabs: QTabWidget, get_config, on_modified=None):
        self._get_config = get_config
        self._on_modified = on_modified
        self._tabs = tabs
        self._analysis = {}

        self._page = QWidget()
        tabs.addTab(self._page, tr("keyframe.tab"))
        layout = QVBoxLayout(self._page)
        layout.setContentsMargins(5, 5, 5, 5)

        self._info = QLabel(tr("keyframe.info"))
        self._info.setWordWrap(True)
        layout.addWidget(self._info)

        btn_row = QHBoxLayout()
        self._tol_label = QLabel(tr("keyframe.tolerance_label"))
        btn_row.addWidget(self._tol_label)
        self._tol_edit = QLineEdit("0.1")
        self._tol_edit.setFixedWidth(60)
        btn_row.addWidget(self._tol_edit)
        self._remove_btn = QPushButton(tr("keyframe.remove_btn"))
        self._remove_btn.setEnabled(False)
        self._remove_btn.clicked.connect(self._remove)
        btn_row.addWidget(self._remove_btn)
        self._select_all_btn = QPushButton(tr("keyframe.select_all"))
        self._select_all_btn.clicked.connect(self._select_all)
        btn_row.addWidget(self._select_all_btn)
        self._unselect_all_btn = QPushButton(tr("keyframe.unselect_all"))
        self._unselect_all_btn.clicked.connect(self._unselect_all)
        btn_row.addWidget(self._unselect_all_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._tol_edit.editingFinished.connect(self._on_tolerance_changed)

        self._stats = QLabel(tr("keyframe.default_stats"))
        self._stats.setStyleSheet("font-weight: bold; color: #6ec072;")
        layout.addWidget(self._stats)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels([
            tr("keyframe.tree.animation"), tr("keyframe.tree.total"),
            tr("keyframe.tree.redundant"), tr("keyframe.tree.reduction"),
        ])
        self._tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, 4):
            self._tree.header().setSectionResizeMode(i, QHeaderView.ResizeToContents)
        layout.addWidget(self._tree, 1)

        language_changed.connect(self._retranslate)

    def _select_all(self):
        for i in range(self._tree.topLevelItemCount()):
            self._tree.topLevelItem(i).setCheckState(0, Qt.Checked)

    def _unselect_all(self):
        for i in range(self._tree.topLevelItemCount()):
            self._tree.topLevelItem(i).setCheckState(0, Qt.Unchecked)

    def _on_tolerance_changed(self):
        if self._analysis:
            self._analyze()

    def _retranslate(self):
        idx = self._tabs.indexOf(self._page)
        if idx >= 0:
            self._tabs.setTabText(idx, tr("keyframe.tab"))
        self._info.setText(tr("keyframe.info"))
        self._tol_label.setText(tr("keyframe.tolerance_label"))
        self._select_all_btn.setText(tr("keyframe.select_all"))
        self._unselect_all_btn.setText(tr("keyframe.unselect_all"))
        self._remove_btn.setText(tr("keyframe.remove_btn"))
        self._stats.setText(tr("keyframe.default_stats"))
        self._tree.setHeaderLabels([
            tr("keyframe.tree.animation"), tr("keyframe.tree.total"),
            tr("keyframe.tree.redundant"), tr("keyframe.tree.reduction"),
        ])

    def _analyze(self):
        json_path = self._get_config("json")
        if not json_path or not os.path.isfile(json_path):
            QMessageBox.critical(None, tr("err.title"), tr("err.no_json"))
            return
        try:
            tolerance = float(self._tol_edit.text())
        except ValueError:
            QMessageBox.critical(None, tr("err.title"), tr("mask.err.tolerance"))
            return
        try:
            spine_data = load_spine_json(json_path)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("err.parse_json", error=e))
            return

        animations = spine_data.get("animations", {})
        if not animations:
            QMessageBox.information(None, tr("keyframe.no_anims_title"), tr("keyframe.no_anims"))
            self._analysis = {}
            self._tree.clear()
            self._remove_btn.setEnabled(False)
            return

        self._analysis = find_redundant_keyframes(animations, tolerance)
        self._tree.clear()
        grand_total = grand_redundant = 0

        for anim_name in sorted(self._analysis.keys()):
            info = self._analysis[anim_name]
            total = info["total_keys"]
            redundant = info["redundant_keys"]
            grand_total += total
            grand_redundant += redundant
            pct = f"{redundant / total * 100:.1f}%" if total > 0 else "0%"
            item = QTreeWidgetItem(self._tree, [anim_name, str(total), str(redundant), pct])
            item.setCheckState(0, Qt.Checked)

        if grand_total > 0:
            self._stats.setText(
                tr("keyframe.stats", anims=len(self._analysis), total=grand_total,
                   redundant=grand_redundant, pct=f"{grand_redundant / grand_total * 100:.1f}")
            )
        else:
            self._stats.setText(tr("keyframe.stats_zero", anims=len(self._analysis)))

        self._remove_btn.setEnabled(grand_redundant > 0)

    def _remove(self):
        if not self._analysis:
            return
        json_path = self._get_config("json")

        # Build filtered analysis containing only checked animations
        checked_names = set()
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            if item.checkState(0) == Qt.Checked:
                checked_names.add(item.text(0))
        filtered_analysis = {
            name: info for name, info in self._analysis.items()
            if name in checked_names
        }
        if not filtered_analysis:
            return
        grand_redundant = sum(info["redundant_keys"] for info in filtered_analysis.values())
        if grand_redundant == 0:
            return

        if QMessageBox.question(None, tr("confirm.title"),
            tr("keyframe.confirm", count=grand_redundant, backup=json_path + ".backup")) != QMessageBox.Yes:
            return

        try:
            shutil.copy2(json_path, json_path + ".backup")
            spine_data = load_spine_json(json_path)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("err.load_backup", error=e))
            return

        removed = remove_redundant_keyframes(spine_data, filtered_analysis)
        try:
            save_spine_json(json_path, spine_data)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("err.save_json", error=e))
            return

        QMessageBox.information(None, tr("done.title"),
            tr("keyframe.done", count=removed, backup=json_path + ".backup"))
        self._analysis = {}
        self._tree.clear()
        self._remove_btn.setEnabled(False)
        self._stats.setText(tr("keyframe.done_stats"))
        if self._on_modified:
            self._on_modified()
