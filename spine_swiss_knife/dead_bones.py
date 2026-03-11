"""Dead Bones tab — detect bones that serve no purpose and add runtime overhead."""

import os
import shutil

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QHeaderView, QMessageBox,
)

from .i18n import tr, language_changed
from .spine_json import load_spine_json, save_spine_json


# ==========================================================================
# Analysis logic (unchanged)
# ==========================================================================

def _collect_bone_references(spine_data: dict) -> dict:
    refs = {}

    def _ensure(name):
        if name not in refs:
            refs[name] = {"has_slots": False, "has_animation": False, "has_constraint": False}

    for slot in spine_data.get("slots", []):
        bone_name = slot.get("bone")
        if bone_name:
            _ensure(bone_name)
            refs[bone_name]["has_slots"] = True

    for anim_name, anim_data in spine_data.get("animations", {}).items():
        for bone_name in anim_data.get("bones", {}):
            _ensure(bone_name)
            refs[bone_name]["has_animation"] = True

    for ik in spine_data.get("ik", []):
        for bone_name in ik.get("bones", []):
            _ensure(bone_name)
            refs[bone_name]["has_constraint"] = True
        target = ik.get("target")
        if target:
            _ensure(target)
            refs[target]["has_constraint"] = True

    for tc in spine_data.get("transform", []):
        for bone_name in tc.get("bones", []):
            _ensure(bone_name)
            refs[bone_name]["has_constraint"] = True
        target = tc.get("target")
        if target:
            _ensure(target)
            refs[target]["has_constraint"] = True

    for pc in spine_data.get("path", []):
        bones_field = pc.get("bones")
        if isinstance(bones_field, list):
            for bone_name in bones_field:
                _ensure(bone_name)
                refs[bone_name]["has_constraint"] = True
        elif isinstance(bones_field, str):
            _ensure(bones_field)
            refs[bones_field]["has_constraint"] = True

    return refs


def analyze_dead_bones(spine_data):
    bones = spine_data.get("bones", [])
    if not bones:
        return [], []

    bone_names = []
    bone_parent = {}
    children_map = {}

    for bone in bones:
        name = bone["name"]
        parent = bone.get("parent")
        bone_names.append(name)
        bone_parent[name] = parent
        children_map.setdefault(name, [])
        if parent is not None:
            children_map.setdefault(parent, [])
            children_map[parent].append(name)

    refs = _collect_bone_references(spine_data)

    alive_set = set()
    alive_reasons = {}

    def _mark_alive(name):
        if name in alive_set:
            return True
        reasons = []
        r = refs.get(name, {"has_slots": False, "has_animation": False, "has_constraint": False})
        if r["has_slots"]:
            reasons.append("has_slots")
        if r["has_animation"]:
            reasons.append("has_animation")
        if r["has_constraint"]:
            reasons.append("has_constraint")
        has_alive_child = False
        for child in children_map.get(name, []):
            if _mark_alive(child):
                has_alive_child = True
        if has_alive_child:
            reasons.append("has_alive_child")
        if reasons:
            alive_set.add(name)
            alive_reasons[name] = reasons
            return True
        return False

    for name in bone_names:
        if bone_parent[name] is None:
            _mark_alive(name)
    for name in bone_names:
        if name not in alive_set:
            _mark_alive(name)

    dead_list = []
    alive_list = []
    for name in bone_names:
        parent = bone_parent[name] or ""
        if name in alive_set:
            alive_list.append({"name": name, "parent": parent, "reasons": ", ".join(alive_reasons[name])})
        else:
            dead_list.append({"name": name, "parent": parent, "reason": "No slots, animations, or constraints"})
    return dead_list, alive_list


# ==========================================================================
# UI Tab
# ==========================================================================

class DeadBonesTab:
    def __init__(self, tabs: QTabWidget, get_config, on_modified=None):
        self._get_config = get_config
        self._on_modified = on_modified
        self._tabs = tabs

        self._page = QWidget()
        tabs.addTab(self._page, tr("dead.tab"))
        layout = QVBoxLayout(self._page)
        layout.setContentsMargins(5, 5, 5, 5)

        self._info = QLabel(tr("dead.info"))
        self._info.setWordWrap(True)
        layout.addWidget(self._info)

        self._stats = QLabel(tr("dead.default_stats"))
        self._stats.setStyleSheet("font-weight: bold; color: #6ec072;")
        layout.addWidget(self._stats)

        btn_row = QHBoxLayout()
        self._remove_btn = QPushButton(tr("dead.remove_btn"))
        self._remove_btn.setEnabled(False)
        self._remove_btn.clicked.connect(self._remove)
        btn_row.addWidget(self._remove_btn)
        self._btn_select_all = QPushButton(tr("dead.select_all"))
        self._btn_unselect_all = QPushButton(tr("dead.unselect_all"))
        self._btn_select_all.clicked.connect(self._select_all)
        self._btn_unselect_all.clicked.connect(self._unselect_all)
        btn_row.addWidget(self._btn_select_all)
        btn_row.addWidget(self._btn_unselect_all)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._sub_tabs = QTabWidget()
        layout.addWidget(self._sub_tabs, 1)

        dead_page = QWidget()
        self._sub_tabs.addTab(dead_page, tr("dead.tab_dead"))
        dl = QVBoxLayout(dead_page)
        dl.setContentsMargins(0, 0, 0, 0)
        self._dead_tree = QTreeWidget()
        self._dead_tree.setHeaderLabels([tr("dead.tree.bone"), tr("dead.tree.parent"), tr("dead.tree.reason")])
        self._dead_tree.header().setSectionResizeMode(QHeaderView.Stretch)
        dl.addWidget(self._dead_tree)

        alive_page = QWidget()
        self._sub_tabs.addTab(alive_page, tr("dead.tab_alive"))
        al = QVBoxLayout(alive_page)
        al.setContentsMargins(0, 0, 0, 0)
        self._alive_tree = QTreeWidget()
        self._alive_tree.setHeaderLabels([tr("dead.tree.bone"), tr("dead.tree.parent"), tr("dead.tree.why_alive")])
        self._alive_tree.header().setSectionResizeMode(QHeaderView.Stretch)
        al.addWidget(self._alive_tree)

        language_changed.connect(self._retranslate)

    def _current_tree(self) -> QTreeWidget:
        """Return the tree widget on the currently visible sub-tab."""
        return self._dead_tree if self._sub_tabs.currentIndex() == 0 else self._alive_tree

    def _select_all(self):
        tree = self._current_tree()
        for i in range(tree.topLevelItemCount()):
            tree.topLevelItem(i).setCheckState(0, Qt.Checked)

    def _unselect_all(self):
        tree = self._current_tree()
        for i in range(tree.topLevelItemCount()):
            tree.topLevelItem(i).setCheckState(0, Qt.Unchecked)

    def _retranslate(self):
        idx = self._tabs.indexOf(self._page)
        if idx >= 0:
            self._tabs.setTabText(idx, tr("dead.tab"))
        self._info.setText(tr("dead.info"))
        self._stats.setText(tr("dead.default_stats"))
        self._remove_btn.setText(tr("dead.remove_btn"))
        self._btn_select_all.setText(tr("dead.select_all"))
        self._btn_unselect_all.setText(tr("dead.unselect_all"))
        self._sub_tabs.setTabText(0, tr("dead.tab_dead"))
        self._sub_tabs.setTabText(1, tr("dead.tab_alive"))
        self._dead_tree.setHeaderLabels([tr("dead.tree.bone"), tr("dead.tree.parent"), tr("dead.tree.reason")])
        self._alive_tree.setHeaderLabels([tr("dead.tree.bone"), tr("dead.tree.parent"), tr("dead.tree.why_alive")])

    def _analyze(self):
        json_path = self._get_config("json")
        if not json_path or not os.path.isfile(json_path):
            QMessageBox.critical(None, tr("err.title"), tr("err.no_json"))
            return
        try:
            spine_data = load_spine_json(json_path)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("err.parse_json", error=e))
            return

        dead_bones, alive_bones = analyze_dead_bones(spine_data)
        self._dead_tree.clear()
        self._alive_tree.clear()

        for bone in dead_bones:
            item = QTreeWidgetItem(self._dead_tree, [bone["name"], bone["parent"], bone["reason"]])
            item.setCheckState(0, Qt.Checked)
        for bone in alive_bones:
            item = QTreeWidgetItem(self._alive_tree, [bone["name"], bone["parent"], bone["reasons"]])
            item.setCheckState(0, Qt.Unchecked)

        total = len(dead_bones) + len(alive_bones)
        self._stats.setText(tr("dead.stats", total=total, dead=len(dead_bones), alive=len(alive_bones)))
        self._remove_btn.setEnabled(len(dead_bones) > 0)

    def _remove(self):
        checked_names = []
        for i in range(self._dead_tree.topLevelItemCount()):
            item = self._dead_tree.topLevelItem(i)
            if item.checkState(0) == Qt.Checked:
                checked_names.append(item.text(0))
        if not checked_names:
            return

        json_path = self._get_config("json")
        if not json_path or not os.path.isfile(json_path):
            return

        backup_path = json_path + ".backup"
        if QMessageBox.question(
            None, tr("confirm.title"),
            tr("dead.confirm", count=len(checked_names), backup=backup_path),
        ) != QMessageBox.Yes:
            return

        try:
            shutil.copy2(json_path, backup_path)
            spine_data = load_spine_json(json_path)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("err.load_backup", error=e))
            return

        dead_set = set(checked_names)
        original_count = len(spine_data.get("bones", []))
        spine_data["bones"] = [b for b in spine_data.get("bones", []) if b["name"] not in dead_set]
        removed = original_count - len(spine_data["bones"])

        try:
            save_spine_json(json_path, spine_data)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("err.save_json", error=e))
            return

        QMessageBox.information(
            None, tr("done.title"),
            tr("dead.done", count=removed, backup=backup_path))
        if self._on_modified:
            self._on_modified()
        else:
            self._analyze()
