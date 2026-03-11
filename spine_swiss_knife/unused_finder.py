"""Unused Finder tab — find images on disk not referenced in atlas."""

import os
import shutil

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QHeaderView, QMessageBox,
)

from .i18n import tr, language_changed
from .atlas_parser import parse_atlas, collect_images


class UnusedFinderTab:
    def __init__(self, tabs: QTabWidget, get_config):
        self._get_config = get_config
        self._tabs = tabs
        self._unused_images = {}

        self._page = QWidget()
        tabs.addTab(self._page, tr("unused.tab"))
        layout = QVBoxLayout(self._page)
        layout.setContentsMargins(5, 5, 5, 5)

        self._info = QLabel(tr("unused.info"))
        self._info.setWordWrap(True)
        layout.addWidget(self._info)

        btn_row = QHBoxLayout()
        self._move_btn = QPushButton(tr("unused.move_btn"))
        self._move_btn.setEnabled(False)
        self._move_btn.clicked.connect(self._move)
        btn_row.addWidget(self._move_btn)
        self._select_all_btn = QPushButton(tr("unused.select_all"))
        self._select_all_btn.clicked.connect(self._select_all)
        btn_row.addWidget(self._select_all_btn)
        self._unselect_all_btn = QPushButton(tr("unused.unselect_all"))
        self._unselect_all_btn.clicked.connect(self._unselect_all)
        btn_row.addWidget(self._unselect_all_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._stats = QLabel(tr("unused.default_stats"))
        self._stats.setStyleSheet("font-weight: bold; color: #6ec072;")
        layout.addWidget(self._stats)

        self._sub_tabs = QTabWidget()
        layout.addWidget(self._sub_tabs, 1)

        up = QWidget()
        self._sub_tabs.addTab(up, tr("unused.tab_unused"))
        ul = QVBoxLayout(up)
        ul.setContentsMargins(0, 0, 0, 0)
        self._unused_tree = QTreeWidget()
        self._unused_tree.setHeaderLabels([tr("unused.tree.unused")])
        self._unused_tree.header().setSectionResizeMode(QHeaderView.Stretch)
        ul.addWidget(self._unused_tree)

        usp = QWidget()
        self._sub_tabs.addTab(usp, tr("unused.tab_used"))
        usl = QVBoxLayout(usp)
        usl.setContentsMargins(0, 0, 0, 0)
        self._used_tree = QTreeWidget()
        self._used_tree.setHeaderLabels([tr("unused.tree.used")])
        self._used_tree.header().setSectionResizeMode(QHeaderView.Stretch)
        usl.addWidget(self._used_tree)

        mp = QWidget()
        self._sub_tabs.addTab(mp, tr("unused.tab_missing"))
        ml = QVBoxLayout(mp)
        ml.setContentsMargins(0, 0, 0, 0)
        self._missing_tree = QTreeWidget()
        self._missing_tree.setHeaderLabels([tr("unused.tree.missing")])
        self._missing_tree.header().setSectionResizeMode(QHeaderView.Stretch)
        ml.addWidget(self._missing_tree)

        language_changed.connect(self._retranslate)

    def _retranslate(self):
        idx = self._tabs.indexOf(self._page)
        if idx >= 0:
            self._tabs.setTabText(idx, tr("unused.tab"))
        self._info.setText(tr("unused.info"))
        self._move_btn.setText(tr("unused.move_btn"))
        self._select_all_btn.setText(tr("unused.select_all"))
        self._unselect_all_btn.setText(tr("unused.unselect_all"))
        self._stats.setText(tr("unused.default_stats"))
        self._sub_tabs.setTabText(0, tr("unused.tab_unused"))
        self._sub_tabs.setTabText(1, tr("unused.tab_used"))
        self._sub_tabs.setTabText(2, tr("unused.tab_missing"))
        self._unused_tree.setHeaderLabels([tr("unused.tree.unused")])
        self._used_tree.setHeaderLabels([tr("unused.tree.used")])
        self._missing_tree.setHeaderLabels([tr("unused.tree.missing")])

    def _analyze(self):
        atlas_path = self._get_config("atlas")
        images_dir = self._get_config("images")
        if not atlas_path or not os.path.isfile(atlas_path):
            QMessageBox.critical(None, tr("err.title"), tr("err.no_atlas"))
            return
        if not images_dir or not os.path.isdir(images_dir):
            QMessageBox.critical(None, tr("err.title"), tr("err.no_images"))
            return

        atlas_sprites = parse_atlas(atlas_path)
        all_images = collect_images(images_dir)
        image_names = set(all_images.keys())
        used_names = image_names & atlas_sprites
        unused_names = image_names - atlas_sprites
        missing = atlas_sprites - image_names

        self._unused_images = {k: all_images[k] for k in sorted(unused_names)}
        self._unused_names_ordered = sorted(unused_names)

        for tree in (self._unused_tree, self._used_tree, self._missing_tree):
            tree.clear()

        for name in sorted(unused_names):
            item = QTreeWidgetItem(self._unused_tree, [os.path.relpath(all_images[name], images_dir)])
            item.setCheckState(0, Qt.Checked)
        for name in sorted(used_names):
            QTreeWidgetItem(self._used_tree, [os.path.relpath(all_images[name], images_dir)])
        for name in sorted(missing):
            QTreeWidgetItem(self._missing_tree, [name])

        self._stats.setText(
            tr("unused.stats", total=len(all_images), used=len(used_names),
               unused=len(unused_names), missing=len(missing))
        )
        self._move_btn.setEnabled(bool(unused_names))

    def _select_all(self):
        for i in range(self._unused_tree.topLevelItemCount()):
            self._unused_tree.topLevelItem(i).setCheckState(0, Qt.Checked)

    def _unselect_all(self):
        for i in range(self._unused_tree.topLevelItemCount()):
            self._unused_tree.topLevelItem(i).setCheckState(0, Qt.Unchecked)

    def _move(self):
        if not self._unused_images:
            return
        # Collect checked items
        checked_names = []
        for i in range(self._unused_tree.topLevelItemCount()):
            item = self._unused_tree.topLevelItem(i)
            if item.checkState(0) == Qt.Checked:
                if i < len(self._unused_names_ordered):
                    checked_names.append(self._unused_names_ordered[i])
        if not checked_names:
            return

        images_dir = self._get_config("images")
        unused_dir = os.path.join(images_dir, "_UNUSED")
        count = len(checked_names)

        if QMessageBox.question(None, tr("confirm.title"),
            tr("unused.confirm", count=count, path=unused_dir)) != QMessageBox.Yes:
            return

        os.makedirs(unused_dir, exist_ok=True)
        moved = 0
        errors = []
        for name in checked_names:
            src_path = self._unused_images.get(name)
            if not src_path:
                continue
            rel = os.path.relpath(src_path, images_dir)
            dst_path = os.path.join(unused_dir, rel)
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            try:
                shutil.move(src_path, dst_path)
                moved += 1
            except Exception as e:
                errors.append(f"{rel}: {e}")

        # Clean empty dirs
        for dirpath, _, _ in os.walk(images_dir, topdown=False):
            if dirpath == images_dir or "_UNUSED" in dirpath or "_DOWNSCALE" in dirpath:
                continue
            if not os.listdir(dirpath):
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass

        msg = tr("unused.done", moved=moved, count=count)
        if errors:
            msg += f"\n\nErrors ({len(errors)}):\n" + "\n".join(errors[:10])
        QMessageBox.information(None, tr("done.title"), msg)
        self._analyze()
