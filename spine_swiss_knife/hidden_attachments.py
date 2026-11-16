"""Hidden Attachments tab — detect & fix invisible GPU waste from alpha=0 slots."""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QHeaderView, QMessageBox,
)

from .i18n import tr, language_changed
from .spine_json import load_spine_json, save_spine_json, create_backup
from .hidden_attachments_core import (  # noqa: F401  (re-exported)
    analyze_hidden_attachments, fix_hidden_attachments,
    _build_bone_maps, _find_zero_scale_bones, _collect_descendants, _alpha_from_hex,
)


# ==========================================================================
# UI Tab
# ==========================================================================

class HiddenAttachmentsTab:
    def __init__(self, tabs: QTabWidget, get_config, on_modified=None):
        self._get_config = get_config
        self._on_modified = on_modified
        self._tabs = tabs
        self._candidates = []
        self._anim_details = []

        self._page = QWidget()
        tabs.addTab(self._page, tr("hidden.tab"))
        layout = QVBoxLayout(self._page)
        layout.setContentsMargins(5, 5, 5, 5)

        self._info = QLabel(tr("hidden.info"))
        self._info.setWordWrap(True)
        layout.addWidget(self._info)

        btn_row = QHBoxLayout()
        self._fix_btn = QPushButton(tr("hidden.fix_btn"))
        self._fix_btn.setEnabled(False)
        self._fix_btn.clicked.connect(self._fix)
        btn_row.addWidget(self._fix_btn)
        self._select_all_btn = QPushButton(tr("hidden.select_all"))
        self._select_all_btn.clicked.connect(self._select_all)
        btn_row.addWidget(self._select_all_btn)
        self._unselect_all_btn = QPushButton(tr("hidden.unselect_all"))
        self._unselect_all_btn.clicked.connect(self._unselect_all)
        btn_row.addWidget(self._unselect_all_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._stats = QLabel(tr("hidden.default_stats"))
        self._stats.setStyleSheet("font-weight: bold; color: #6ec072;")
        layout.addWidget(self._stats)

        self._sub_tabs = QTabWidget()
        layout.addWidget(self._sub_tabs, 1)

        # Hidden Slots sub-tab
        hidden_page = QWidget()
        self._sub_tabs.addTab(hidden_page, tr("hidden.tab_hidden"))
        hl = QVBoxLayout(hidden_page)
        hl.setContentsMargins(0, 0, 0, 0)
        self._hidden_tree = QTreeWidget()
        self._hidden_tree.setHeaderLabels([
            tr("hidden.tree.slot"), tr("hidden.tree.attachment"),
            tr("hidden.tree.bone"), tr("hidden.tree.reason"),
            tr("hidden.tree.anims"),
        ])
        self._hidden_tree.header().setSectionResizeMode(QHeaderView.Stretch)
        hl.addWidget(self._hidden_tree)

        # Animation Details sub-tab
        detail_page = QWidget()
        self._sub_tabs.addTab(detail_page, tr("hidden.tab_details"))
        dl = QVBoxLayout(detail_page)
        dl.setContentsMargins(0, 0, 0, 0)
        self._detail_tree = QTreeWidget()
        self._detail_tree.setHeaderLabels([
            tr("hidden.detail.tree.animation"),
            tr("hidden.detail.tree.slot"),
            tr("hidden.detail.tree.action"),
        ])
        self._detail_tree.header().setSectionResizeMode(QHeaderView.Stretch)
        dl.addWidget(self._detail_tree)

        language_changed.connect(self._retranslate)

    def _retranslate(self):
        idx = self._tabs.indexOf(self._page)
        if idx >= 0:
            self._tabs.setTabText(idx, tr("hidden.tab"))
        self._info.setText(tr("hidden.info"))
        self._fix_btn.setText(tr("hidden.fix_btn"))
        self._select_all_btn.setText(tr("hidden.select_all"))
        self._unselect_all_btn.setText(tr("hidden.unselect_all"))
        self._stats.setText(tr("hidden.default_stats"))
        self._sub_tabs.setTabText(0, tr("hidden.tab_hidden"))
        self._sub_tabs.setTabText(1, tr("hidden.tab_details"))
        self._hidden_tree.setHeaderLabels([
            tr("hidden.tree.slot"), tr("hidden.tree.attachment"),
            tr("hidden.tree.bone"), tr("hidden.tree.reason"),
            tr("hidden.tree.anims"),
        ])
        self._detail_tree.setHeaderLabels([
            tr("hidden.detail.tree.animation"),
            tr("hidden.detail.tree.slot"),
            tr("hidden.detail.tree.action"),
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

        self._candidates, self._anim_details, _ = analyze_hidden_attachments(spine_data)
        self._hidden_tree.clear()
        self._detail_tree.clear()

        for cand in self._candidates:
            if cand["reason"] == "alpha_zero":
                reason_text = tr("hidden.reason.alpha_zero")
            else:
                reason_text = tr("hidden.reason.bone_scale_zero",
                                 bone=cand.get("zero_scale_bone", cand["bone_name"]))
            anims_text = str(len(cand.get("anims_affected", [])))
            item = QTreeWidgetItem(self._hidden_tree, [
                cand["slot_name"], cand["original_attachment"],
                cand["bone_name"], reason_text, anims_text,
            ])
            item.setCheckState(0, Qt.Checked)

        for detail in self._anim_details:
            if detail["action"] == "insert_kf":
                action_text = tr("hidden.action.insert_kf")
            else:
                action_text = tr("hidden.action.already_ok")
            QTreeWidgetItem(self._detail_tree, [
                detail["animation"], detail["slot_name"], action_text,
            ])

        if not self._candidates:
            self._stats.setText(tr("hidden.nothing"))
            self._fix_btn.setEnabled(False)
            return

        needs_kf = sum(1 for d in self._anim_details if d["action"] == "insert_kf")
        already_ok = sum(1 for d in self._anim_details if d["action"] == "already_ok")
        self._stats.setText(tr("hidden.stats",
                               hidden=len(self._candidates),
                               keyframes=needs_kf, handled=already_ok))
        self._fix_btn.setEnabled(True)

    def _select_all(self):
        for i in range(self._hidden_tree.topLevelItemCount()):
            self._hidden_tree.topLevelItem(i).setCheckState(0, Qt.Checked)

    def _unselect_all(self):
        for i in range(self._hidden_tree.topLevelItemCount()):
            self._hidden_tree.topLevelItem(i).setCheckState(0, Qt.Unchecked)

    def _fix(self):
        if not self._candidates:
            return

        # Collect checked slot names
        checked_slots = set()
        for i in range(self._hidden_tree.topLevelItemCount()):
            item = self._hidden_tree.topLevelItem(i)
            if item.checkState(0) == Qt.Checked:
                checked_slots.add(item.text(0))  # slot name is column 0
        if not checked_slots:
            return

        filtered_candidates = [c for c in self._candidates if c["slot_name"] in checked_slots]
        filtered_details = [d for d in self._anim_details if d["slot_name"] in checked_slots]

        json_path = self._get_config("json")
        needs_kf = sum(1 for d in filtered_details if d["action"] == "insert_kf")
        backup_path = json_path + ".backup"

        if QMessageBox.question(
            None, tr("confirm.title"),
            tr("hidden.confirm", slots=len(filtered_candidates),
               keyframes=needs_kf, backup=backup_path),
        ) != QMessageBox.Yes:
            return

        try:
            create_backup(json_path)
            spine_data = load_spine_json(json_path)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("err.load_backup", error=e))
            return

        slots_fixed, keyframes_inserted = fix_hidden_attachments(
            spine_data, filtered_candidates, filtered_details)

        try:
            save_spine_json(json_path, spine_data)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("err.save_json", error=e))
            return

        QMessageBox.information(
            None, tr("done.title"),
            tr("hidden.done", slots=slots_fixed,
               keyframes=keyframes_inserted, backup=backup_path))

        # Re-analyze all tabs
        if self._on_modified:
            self._on_modified()
        else:
            self._analyze()
