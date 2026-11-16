"""Keyframe Optimizer tab — detect and remove redundant animation keyframes from Spine JSON."""

from __future__ import annotations

import os

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QLineEdit,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QHeaderView, QMessageBox,
)

from PySide6.QtCore import Qt

from .i18n import tr, language_changed
from .spine_json import load_spine_json, save_spine_json, create_backup
from .keyframe_core import (  # noqa: F401  (re-exported)
    find_redundant_keyframes, remove_redundant_keyframes, redundant_indices,
)


# ==========================================================================
# UI Tab
# ==========================================================================

class KeyframeOptimizerTab:
    def __init__(self, tabs: QTabWidget, get_config, on_modified=None):
        self._get_config = get_config
        self._on_modified = on_modified
        self._tabs = tabs
        self._analysis = {}
        self._stats_state = ("keyframe.default_stats", {})

        self._page = QWidget()
        tabs.addTab(self._page, tr("keyframe.tab"))
        layout = QVBoxLayout(self._page)
        layout.setContentsMargins(5, 5, 5, 5)

        self._info = QLabel(tr("keyframe.info2"))
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
        self._info.setText(tr("keyframe.info2"))
        self._tol_label.setText(tr("keyframe.tolerance_label"))
        self._select_all_btn.setText(tr("keyframe.select_all"))
        self._unselect_all_btn.setText(tr("keyframe.unselect_all"))
        self._remove_btn.setText(tr("keyframe.remove_btn"))
        self._set_stats(*self._stats_state)
        self._tree.setHeaderLabels([
            tr("keyframe.tree.animation"), tr("keyframe.tree.total"),
            tr("keyframe.tree.redundant"), tr("keyframe.tree.reduction"),
        ])

    def _set_stats(self, key, kwargs):
        self._stats_state = (key, kwargs)
        self._stats.setText(tr(key, **kwargs))

    def _analyze(self, silent=False):
        """Scan for redundant keys; *silent* suppresses the 'no animations'
        information box (used by the automatic refresh)."""
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
            if not silent:
                QMessageBox.information(None, tr("keyframe.no_anims_title"), tr("keyframe.no_anims"))
            self._analysis = {}
            self._tree.clear()
            self._remove_btn.setEnabled(False)
            return

        self._analysis = find_redundant_keyframes(animations, tolerance, spine_data)
        self._tree.clear()
        grand_total = grand_redundant = 0

        for anim_name in sorted(self._analysis.keys()):
            info = self._analysis[anim_name]
            total = info["total_keys"]
            redundant = info["redundant_keys"]
            grand_total += total
            grand_redundant += redundant
            if redundant == 0:
                continue
            pct = f"{redundant / total * 100:.1f}%" if total > 0 else "0%"
            item = QTreeWidgetItem(self._tree, [anim_name, str(total), str(redundant), pct])
            item.setCheckState(0, Qt.Checked)

        if grand_total > 0:
            self._set_stats("keyframe.stats", dict(
                anims=len(self._analysis), total=grand_total,
                redundant=grand_redundant, pct=f"{grand_redundant / grand_total * 100:.1f}"))
        else:
            self._set_stats("keyframe.stats_zero", dict(anims=len(self._analysis)))

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
            create_backup(json_path)
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
        self._set_stats("keyframe.done_stats", {})
        if self._on_modified:
            self._on_modified()
