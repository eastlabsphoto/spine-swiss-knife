"""Blend Switcher tab — detect and fix problematic blend modes (multiply/screen)."""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QHeaderView, QMessageBox,
)

from .i18n import tr, language_changed
from .spine_json import load_spine_json, save_spine_json, create_backup
from .slot_renamer import apply_renames, _strip_blend_suffix


# ==========================================================================
# Analysis logic
# ==========================================================================

def analyze_blend_modes(spine_data: dict) -> list[dict]:
    """Return slots that use multiply or screen blend mode."""
    results = []
    for slot in spine_data.get("slots", []):
        blend = slot.get("blend")
        if blend in ("multiply", "screen"):
            results.append({
                "name": slot.get("name", ""),
                "bone": slot.get("bone", ""),
                "blend": blend,
            })
    return results


def _renames_with_collisions_dropped(spine_data: dict, candidates: dict):
    """Split *candidates* (old -> new) into a collision-free rename map and
    the list of old names whose rename was dropped (slot keeps its name).

    The rename is simultaneous, so chains and swaps stay legal. Dropping a
    rename can create a new collision, hence the fixpoint loop.
    """
    cand = dict(candidates)
    dropped = []
    names = [s.get("name") for s in spine_data.get("slots") or []
             if isinstance(s, dict)]
    while True:
        final = [cand.get(n, n) for n in names]
        counts = {}
        for n in final:
            counts[n] = counts.get(n, 0) + 1
        bad = [old for old, new in cand.items() if counts.get(new, 0) > 1]
        if not bad:
            return cand, dropped
        for old in bad:
            del cand[old]
            dropped.append(old)


def switch_blend_modes(spine_data: dict, names, target_blend: str):
    """Switch multiply/screen slots in *names* to *target_blend* in place.

    Each switched slot is also renamed (old blend suffix replaced by
    ``_<target>``) with every reference updated; when the new name would
    collide with another slot the blend still changes but the name is kept.
    Returns ``(switched_count, kept_old_names)``.
    """
    switch_set = set(names)
    switched = 0
    candidates = {}
    for slot in spine_data.get("slots", []):
        if slot.get("name") in switch_set and slot.get("blend") in ("multiply", "screen"):
            old_name = slot["name"]
            if target_blend == "normal":
                del slot["blend"]  # normal is default, no key needed
            else:
                slot["blend"] = target_blend
            new_name = f"{_strip_blend_suffix(old_name)}_{target_blend}"
            if new_name != old_name:
                candidates[old_name] = new_name
            switched += 1
    rename_map, kept = _renames_with_collisions_dropped(spine_data, candidates)
    if rename_map:
        apply_renames(spine_data, rename_map)
    return switched, kept


# ==========================================================================
# UI Tab
# ==========================================================================

class BlendSwitcherTab:
    def __init__(self, tabs: QTabWidget, get_config, on_modified=None):
        self._get_config = get_config
        self._on_modified = on_modified
        self._tabs = tabs

        self._page = QWidget()
        tabs.addTab(self._page, tr("blend.tab"))
        layout = QVBoxLayout(self._page)
        layout.setContentsMargins(5, 5, 5, 5)

        self._info = QLabel(tr("blend.info"))
        self._info.setWordWrap(True)
        layout.addWidget(self._info)

        self._stats = QLabel(tr("blend.default_stats"))
        self._stats.setStyleSheet("font-weight: bold; color: #6ec072;")
        layout.addWidget(self._stats)

        btn_row = QHBoxLayout()
        self._switch_btn = QPushButton(tr("blend.switch_btn"))
        self._switch_btn.setEnabled(False)
        self._switch_btn.clicked.connect(self._switch)
        btn_row.addWidget(self._switch_btn)
        self._target_label = QLabel(tr("blend.target_label"))
        btn_row.addWidget(self._target_label)
        self._target_combo = QComboBox()
        self._target_combo.addItems(["additive", "normal"])
        btn_row.addWidget(self._target_combo)
        self._btn_select_all = QPushButton(tr("blend.select_all"))
        self._btn_unselect_all = QPushButton(tr("blend.unselect_all"))
        self._btn_select_all.clicked.connect(self._select_all)
        self._btn_unselect_all.clicked.connect(self._unselect_all)
        btn_row.addWidget(self._btn_select_all)
        btn_row.addWidget(self._btn_unselect_all)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels([
            tr("blend.tree.slot"),
            tr("blend.tree.bone"),
            tr("blend.tree.blend"),
        ])
        self._tree.header().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self._tree, 1)

        language_changed.connect(self._retranslate)

    def _select_all(self):
        for i in range(self._tree.topLevelItemCount()):
            self._tree.topLevelItem(i).setCheckState(0, Qt.Checked)

    def _unselect_all(self):
        for i in range(self._tree.topLevelItemCount()):
            self._tree.topLevelItem(i).setCheckState(0, Qt.Unchecked)

    def _retranslate(self):
        idx = self._tabs.indexOf(self._page)
        if idx >= 0:
            self._tabs.setTabText(idx, tr("blend.tab"))
        self._info.setText(tr("blend.info"))
        self._stats.setText(tr("blend.default_stats"))
        self._switch_btn.setText(tr("blend.switch_btn"))
        self._target_label.setText(tr("blend.target_label"))
        self._btn_select_all.setText(tr("blend.select_all"))
        self._btn_unselect_all.setText(tr("blend.unselect_all"))
        self._tree.setHeaderLabels([
            tr("blend.tree.slot"),
            tr("blend.tree.bone"),
            tr("blend.tree.blend"),
        ])

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

        all_slots = spine_data.get("slots", [])
        problem_slots = analyze_blend_modes(spine_data)

        self._tree.clear()

        if not problem_slots:
            self._stats.setText(tr("blend.nothing"))
            self._switch_btn.setEnabled(False)
            return

        for slot in problem_slots:
            item = QTreeWidgetItem(self._tree, [
                slot["name"], slot["bone"], slot["blend"],
            ])
            item.setCheckState(0, Qt.Checked)

        # Count blend modes across all slots
        counts = {"normal": 0, "additive": 0, "multiply": 0, "screen": 0}
        for s in all_slots:
            blend = s.get("blend", "normal")
            if blend in counts:
                counts[blend] += 1
            else:
                counts["normal"] += 1

        self._stats.setText(tr("blend.stats",
                               total=len(all_slots),
                               normal=counts["normal"],
                               additive=counts["additive"],
                               multiply=counts["multiply"],
                               screen=counts["screen"]))
        self._switch_btn.setEnabled(True)

    def _switch(self):
        checked_names = []
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            if item.checkState(0) == Qt.Checked:
                checked_names.append(item.text(0))
        if not checked_names:
            return

        json_path = self._get_config("json")
        if not json_path or not os.path.isfile(json_path):
            return

        backup_path = json_path + ".backup"
        target_blend = self._target_combo.currentText()

        if QMessageBox.question(
            None, tr("confirm.title"),
            tr("blend.confirm_renames", count=len(checked_names),
               target=target_blend, backup=backup_path),
        ) != QMessageBox.Yes:
            return

        try:
            spine_data = load_spine_json(json_path)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("err.load_backup", error=e))
            return

        try:
            switched, kept = switch_blend_modes(
                spine_data, checked_names, target_blend)
            create_backup(json_path)
            save_spine_json(json_path, spine_data)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("err.save_json", error=e))
            return

        if kept:
            message = tr("blend.done_kept", count=switched, target=target_blend,
                         backup=backup_path, kept=len(kept),
                         names=", ".join(sorted(kept)))
        else:
            message = tr("blend.done", count=switched, target=target_blend,
                         backup=backup_path)
        QMessageBox.information(None, tr("done.title"), message)
        if self._on_modified:
            self._on_modified()
        else:
            self._analyze()
