"""Dead Bones tab — detect bones that serve no purpose and add runtime overhead."""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QHeaderView, QMessageBox,
)

from .i18n import tr, language_changed
from .spine_json import load_spine_json, save_spine_json, create_backup
from .spine_refs import remove_bones
from .dead_bones_core import (  # noqa: F401  (re-exported)
    analyze_dead_bones, filter_removable_selection,
)


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
        try:
            spine_data = load_spine_json(json_path)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("err.parse_json", error=e))
            return
        keep, skipped = filter_removable_selection(spine_data, checked_names)
        if not keep:
            QMessageBox.warning(None, tr("err.title"),
                                tr("dead.skipped", count=len(skipped)))
            return

        if QMessageBox.question(
            None, tr("confirm.title"),
            tr("dead.confirm", count=len(keep), backup=backup_path)
            + ("\n\n" + tr("dead.skipped", count=len(skipped)) if skipped else ""),
        ) != QMessageBox.Yes:
            return

        try:
            removed = remove_bones(spine_data, keep)
        except ValueError as e:
            QMessageBox.critical(None, tr("err.title"), str(e))
            return

        try:
            create_backup(json_path)
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
