"""Image Flattener — move images from subdirectories to root images folder and update JSON paths."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QHeaderView, QMessageBox,
)

from .i18n import tr, language_changed
from .flatten_core import (  # noqa: F401 - re-exported for existing importers
    scan_subfolder_images, detect_conflicts, build_flatten_plan,
    execute_flatten, flatten_images,
)


# ==========================================================================
# UI Tab
# ==========================================================================

class ImageFlattenerTab:
    def __init__(self, tabs: QTabWidget, get_config, on_modified=None):
        self._get_config = get_config
        self._on_modified = on_modified
        self._tabs = tabs
        self._plan: list[dict] = []
        self._conflicts: dict[str, list[str]] = {}

        self._page = QWidget()
        tabs.addTab(self._page, tr("flattener.tab"))
        layout = QVBoxLayout(self._page)
        layout.setContentsMargins(5, 5, 5, 5)

        self._info = QLabel(tr("flattener.info"))
        self._info.setWordWrap(True)
        layout.addWidget(self._info)

        self._stats = QLabel(tr("flattener.default_stats"))
        self._stats.setStyleSheet("font-weight: bold; color: #6ec072;")
        layout.addWidget(self._stats)

        btn_row = QHBoxLayout()
        self._analyze_btn = QPushButton(tr("flattener.analyze_btn"))
        self._analyze_btn.clicked.connect(self._analyze)
        btn_row.addWidget(self._analyze_btn)
        self._flatten_btn = QPushButton(tr("flattener.flatten_btn"))
        self._flatten_btn.setEnabled(False)
        self._flatten_btn.setProperty("role", "primary")
        self._flatten_btn.setCursor(Qt.PointingHandCursor)
        self._flatten_btn.clicked.connect(self._flatten)
        btn_row.addWidget(self._flatten_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels([
            tr("flattener.tree.source"),
            tr("flattener.tree.destination"),
            tr("flattener.tree.status"),
        ])
        self._tree.header().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self._tree, 1)

        language_changed.connect(self._retranslate)

    def _retranslate(self):
        idx = self._tabs.indexOf(self._page)
        if idx >= 0:
            self._tabs.setTabText(idx, tr("flattener.tab"))
        self._info.setText(tr("flattener.info"))
        self._stats.setText(tr("flattener.default_stats"))
        self._analyze_btn.setText(tr("flattener.analyze_btn"))
        self._flatten_btn.setText(tr("flattener.flatten_btn"))
        self._tree.setHeaderLabels([
            tr("flattener.tree.source"),
            tr("flattener.tree.destination"),
            tr("flattener.tree.status"),
        ])

    def _analyze(self):
        images_dir = self._get_config("images")
        json_path = self._get_config("json")
        if not images_dir or not os.path.isdir(images_dir):
            QMessageBox.warning(None, tr("err.title"), tr("err.no_images"))
            return
        if not json_path or not os.path.isfile(json_path):
            QMessageBox.warning(None, tr("err.title"), tr("err.no_json"))
            return

        subfolder_files = scan_subfolder_images(images_dir)
        if not subfolder_files:
            self._tree.clear()
            self._stats.setText(tr("flattener.nothing"))
            self._flatten_btn.setEnabled(False)
            self._plan = []
            return

        self._conflicts = detect_conflicts(images_dir, subfolder_files)
        # Build plan without suffix first (to show conflicts)
        self._plan = build_flatten_plan(
            images_dir, subfolder_files, self._conflicts, add_suffix=False,
        )

        self._populate_tree()

        conflict_count = sum(1 for p in self._plan if p["conflict"])
        skipped_count = sum(1 for p in self._plan if p["skipped"])
        movable_count = sum(1 for p in self._plan if not p["skipped"])

        # Collect unique subfolders
        folders = {f["folder"] for f in subfolder_files}

        self._stats.setText(tr("flattener.stats",
                               total=len(subfolder_files),
                               folders=len(folders),
                               conflicts=conflict_count,
                               movable=movable_count))
        self._flatten_btn.setEnabled(movable_count > 0 or conflict_count > 0)

    def _populate_tree(self):
        self._tree.clear()
        for item in self._plan:
            if item["skipped"]:
                status = tr("flattener.status.conflict")
                dest_text = tr("flattener.status.skipped")
            elif item["conflict"]:
                status = tr("flattener.status.conflict_resolved")
                dest_text = Path(item["dest_path"]).name
            else:
                status = tr("flattener.status.ok")
                dest_text = Path(item["dest_path"]).name

            row = QTreeWidgetItem(self._tree, [
                item["rel_path"],
                dest_text,
                status,
            ])
            if item["skipped"]:
                for col in range(3):
                    row.setForeground(col, Qt.red)
            elif item["conflict"]:
                for col in range(3):
                    row.setForeground(col, Qt.yellow)

    def _flatten(self):
        images_dir = self._get_config("images")
        json_path = self._get_config("json")
        if not images_dir or not json_path:
            return

        has_conflicts = any(p["conflict"] for p in self._plan)

        if has_conflicts:
            reply = QMessageBox.question(
                None,
                tr("flattener.conflict_title"),
                tr("flattener.conflict_question",
                   count=sum(1 for p in self._plan if p["conflict"])),
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            )
            if reply == QMessageBox.Cancel:
                return
            add_suffix = (reply == QMessageBox.Yes)

            # Rebuild plan with suffix choice
            subfolder_files = scan_subfolder_images(images_dir)
            self._plan = build_flatten_plan(
                images_dir, subfolder_files, self._conflicts, add_suffix=add_suffix,
            )
            self._populate_tree()

            # If still has skipped items (user chose No), only move non-conflicting
            skipped = [p for p in self._plan if p["skipped"]]
            if skipped:
                reply2 = QMessageBox.question(
                    None,
                    tr("confirm.title"),
                    tr("flattener.confirm_partial",
                       movable=sum(1 for p in self._plan if not p["skipped"]),
                       skipped=len(skipped)),
                    QMessageBox.Yes | QMessageBox.No,
                )
                if reply2 != QMessageBox.Yes:
                    return

        active_plan = [p for p in self._plan if not p["skipped"]]
        if not active_plan:
            QMessageBox.information(None, tr("info.title"), tr("flattener.nothing_to_move"))
            return

        if not has_conflicts:
            reply = QMessageBox.question(
                None,
                tr("confirm.title"),
                tr("flattener.confirm",
                   count=len(active_plan),
                   backup=json_path + ".backup"),
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        try:
            result = flatten_images(images_dir, json_path, self._plan)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), str(e))
            return

        backup = result["backup"] or (json_path + ".backup")
        msg = tr("flattener.done",
                 moved=result["moved"],
                 json_updated=result["json_updated"],
                 backup=backup)
        if result["needs_repack"]:
            msg += "\n\n" + tr("flattener.repack")
        if result["failed"]:
            msg += "\n\n" + tr("flattener.failed_header") + "\n" + "\n".join(result["failed"])
        if result["blocked"]:
            msg += "\n\n" + tr("flattener.warnings_header") + "\n" + "\n".join(result["blocked"])

        QMessageBox.information(None, tr("done.title"), msg)

        if self._on_modified:
            self._on_modified()
        else:
            self._analyze()
