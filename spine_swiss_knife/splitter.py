"""
Splitter tab — split a Spine project into sub-projects by animation groups.
Each sub-project gets only the animations, bones, slots, attachments, and images it needs.
"""

from __future__ import annotations

import os

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QLineEdit,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QHeaderView, QMessageBox,
    QGroupBox, QFileDialog, QCheckBox,
)
from PySide6.QtCore import Qt

from .i18n import tr, language_changed
from .spine_json import load_spine_json
from .splitter_core import (  # noqa: F401 - re-exported for existing importers
    save_spine_format, format_spine_json, compute_dependencies,
    build_split_json, split_to_directory, sanitize_name, sanitize_group_names,
    _build_bone_hierarchy, _ancestors, _get_slot_bone_map,
    _get_slot_default_attachment,
)


def _save_spine_format(path: str, data: dict):
    """Save Spine JSON in the same whitespace format that Spine itself exports."""
    save_spine_format(path, data)


# ==========================================================================
# UI Tab
# ==========================================================================

class SplitterTab:
    def __init__(self, tabs: QTabWidget, get_config):
        self._get_config = get_config
        self._tabs = tabs
        self._anim_info = {}
        self._spine_data = None

        self._page = QWidget()
        tabs.addTab(self._page, tr("splitter.tab"))
        layout = QVBoxLayout(self._page)
        layout.setContentsMargins(5, 5, 5, 5)

        self._info = QLabel(tr("splitter.info"))
        self._info.setWordWrap(True)
        layout.addWidget(self._info)

        btn_row = QHBoxLayout()
        self._output_label = QLabel(tr("splitter.output_label"))
        btn_row.addWidget(self._output_label)
        self._output_edit = QLineEdit()
        self._output_edit.setMinimumWidth(200)
        btn_row.addWidget(self._output_edit, 1)
        self._browse_btn = QPushButton(tr("splitter.browse"))
        self._browse_btn.clicked.connect(self._browse_output)
        btn_row.addWidget(self._browse_btn)
        self._split_btn = QPushButton(tr("splitter.split_btn"))
        self._split_btn.setEnabled(False)
        self._split_btn.clicked.connect(self._split)
        btn_row.addWidget(self._split_btn)
        layout.addLayout(btn_row)

        skel_row = QHBoxLayout()
        self._images_path_label = QLabel(tr("splitter.images_label"))
        skel_row.addWidget(self._images_path_label)
        self._images_path_edit = QLineEdit()
        skel_row.addWidget(self._images_path_edit, 1)
        self._images_hint_label = QLabel(tr("splitter.images_hint"))
        skel_row.addWidget(self._images_hint_label)
        layout.addLayout(skel_row)

        self._static_check = QCheckBox(tr("splitter.include_static"))
        self._static_check.setChecked(True)
        layout.addWidget(self._static_check)

        self._stats = QLabel(tr("splitter.default_stats"))
        self._stats.setStyleSheet("font-weight: bold; color: #6ec072;")
        layout.addWidget(self._stats)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels([
            tr("splitter.tree.animation"), tr("splitter.tree.duration"),
            tr("splitter.tree.keyframes"), tr("splitter.tree.bone_kf"),
            tr("splitter.tree.slot_kf"), tr("splitter.tree.group"),
        ])
        self._tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, 6):
            self._tree.header().setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self._tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        layout.addWidget(self._tree, 1)

        self._assign_box = QGroupBox(tr("splitter.assign_group"))
        assign_layout = QHBoxLayout(self._assign_box)
        self._group_name_label = QLabel(tr("splitter.group_label"))
        assign_layout.addWidget(self._group_name_label)
        self._group_edit = QLineEdit("1")
        self._group_edit.setFixedWidth(80)
        assign_layout.addWidget(self._group_edit)
        self._assign_btn = QPushButton(tr("splitter.assign_btn"))
        self._assign_btn.clicked.connect(self._assign_group)
        assign_layout.addWidget(self._assign_btn)
        self._clear_btn = QPushButton(tr("splitter.clear_btn"))
        self._clear_btn.clicked.connect(self._clear_group)
        assign_layout.addWidget(self._clear_btn)
        assign_layout.addSpacing(20)
        self._quick_label = QLabel(tr("splitter.quick_label"))
        assign_layout.addWidget(self._quick_label)
        self._each_btn = QPushButton(tr("splitter.each_own_btn"))
        self._each_btn.clicked.connect(self._each_own_group)
        assign_layout.addWidget(self._each_btn)
        assign_layout.addStretch()
        layout.addWidget(self._assign_box)

        language_changed.connect(self._retranslate)

    def _retranslate(self):
        idx = self._tabs.indexOf(self._page)
        if idx >= 0:
            self._tabs.setTabText(idx, tr("splitter.tab"))
        self._info.setText(tr("splitter.info"))
        self._output_label.setText(tr("splitter.output_label"))
        self._browse_btn.setText(tr("splitter.browse"))
        self._split_btn.setText(tr("splitter.split_btn"))
        self._images_path_label.setText(tr("splitter.images_label"))
        self._images_hint_label.setText(tr("splitter.images_hint"))
        self._stats.setText(tr("splitter.default_stats"))
        self._static_check.setText(tr("splitter.include_static"))
        self._tree.setHeaderLabels([
            tr("splitter.tree.animation"), tr("splitter.tree.duration"),
            tr("splitter.tree.keyframes"), tr("splitter.tree.bone_kf"),
            tr("splitter.tree.slot_kf"), tr("splitter.tree.group"),
        ])
        self._assign_box.setTitle(tr("splitter.assign_group"))
        self._group_name_label.setText(tr("splitter.group_label"))
        self._assign_btn.setText(tr("splitter.assign_btn"))
        self._clear_btn.setText(tr("splitter.clear_btn"))
        self._quick_label.setText(tr("splitter.quick_label"))
        self._each_btn.setText(tr("splitter.each_own_btn"))

    def _browse_output(self):
        path = QFileDialog.getExistingDirectory(None, tr("app.dialog.select_images"))
        if path:
            self._output_edit.setText(path)

    def _load(self):
        json_path = self._get_config("json")
        if not json_path or not os.path.isfile(json_path):
            QMessageBox.critical(None, tr("err.title"), tr("err.no_json"))
            return
        try:
            self._spine_data = load_spine_json(json_path)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("err.parse_json", error=e))
            return

        animations = self._spine_data.get("animations", {})
        self._anim_info = {}
        self._tree.clear()

        for anim_name, anim_data in animations.items():
            bone_kf = 0
            for bone_name, channels in anim_data.get("bones", {}).items():
                for ch_name, keys in channels.items():
                    if isinstance(keys, list):
                        bone_kf += len(keys)
            slot_kf = 0
            for slot_name, channels in anim_data.get("slots", {}).items():
                for ch_name, keys in channels.items():
                    if isinstance(keys, list):
                        slot_kf += len(keys)
            max_time = 0
            for section in (anim_data.get("bones", {}), anim_data.get("slots", {})):
                for name, channels in section.items():
                    for ch_name, keys in channels.items():
                        if isinstance(keys, list):
                            for key in keys:
                                t = key.get("time", 0)
                                if t > max_time:
                                    max_time = t
            total_kf = bone_kf + slot_kf
            self._anim_info[anim_name] = {
                "duration": max_time, "keyframes": total_kf,
                "bone_kf": bone_kf, "slot_kf": slot_kf,
            }
            QTreeWidgetItem(self._tree, [
                anim_name, f"{max_time:.2f}s", str(total_kf),
                str(bone_kf), str(slot_kf), "",
            ])

        if not self._output_edit.text():
            json_dir = os.path.dirname(json_path)
            self._output_edit.setText(os.path.join(json_dir, "_SPLIT"))
        skel_images = self._spine_data.get("skeleton", {}).get("images", "")
        if skel_images and not self._images_path_edit.text():
            self._images_path_edit.setText(skel_images)
        self._stats.setText(tr("splitter.loaded", count=len(self._anim_info)))
        self._split_btn.setEnabled(bool(self._anim_info))

    def _assign_group(self):
        sel = self._tree.selectedItems()
        if not sel:
            QMessageBox.information(None, tr("info.title"), tr("splitter.select_first"))
            return
        group = self._group_edit.text().strip()
        if not group:
            return
        for item in sel:
            item.setText(5, group)

    def _clear_group(self):
        for item in self._tree.selectedItems():
            item.setText(5, "")

    def _each_own_group(self):
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            item.setText(5, item.text(0))

    def _split(self):
        if self._spine_data is None:
            return
        json_path = self._get_config("json")
        # Always re-read JSON to pick up changes from other tools
        try:
            self._spine_data = load_spine_json(json_path)
        except Exception:
            pass
        images_dir = self._get_config("images")
        output_dir = self._output_edit.text().strip()

        if not output_dir:
            QMessageBox.critical(None, tr("err.title"), tr("splitter.err.no_output"))
            return

        groups = {}
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            anim_name = item.text(0)
            group = item.text(5)
            if group:
                groups.setdefault(group, []).append(anim_name)

        if not groups:
            QMessageBox.information(None, tr("info.title"), tr("splitter.no_groups"))
            return

        all_anims = set(self._anim_info.keys())
        assigned = set()
        for anims in groups.values():
            assigned |= set(anims)
        unassigned = all_anims - assigned
        if unassigned:
            ulist = "\n".join(sorted(unassigned)[:10]) + ("\n..." if len(unassigned) > 10 else "")
            if QMessageBox.question(None, tr("splitter.unassigned_title"),
                tr("splitter.unassigned", count=len(unassigned), list=ulist)) != QMessageBox.Yes:
                return

        summary_lines = []
        for group_name in sorted(groups.keys()):
            anims = groups[group_name]
            summary_lines.append(tr("splitter.group_summary", name=group_name, count=len(anims)))

        if QMessageBox.question(None, tr("splitter.confirm_title"),
            tr("splitter.confirm", count=len(groups), summary="\n".join(summary_lines), dir=output_dir)) != QMessageBox.Yes:
            return

        created, errors, _details = split_to_directory(
            self._spine_data, groups, output_dir,
            images_dir=images_dir,
            images_path=self._images_path_edit.text().strip(),
            include_static_visible=self._static_check.isChecked(),
        )

        msg = tr("splitter.done", created=created, total=len(groups), dir=output_dir)
        if errors:
            msg += f"\n\nErrors ({len(errors)}):\n" + "\n".join(errors[:5])
        QMessageBox.information(None, tr("done.title"), msg)
        self._stats.setText(tr("splitter.done_stats", count=created, dir=output_dir))
