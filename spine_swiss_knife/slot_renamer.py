"""Slot Renamer tab — add blend mode suffix to slot names.

Renames every slot by appending its blend mode as a suffix
(e.g. ``_additive``, ``_normal``).  All references throughout the JSON
are updated: slots, skins, animations (slot timelines, drawOrder,
deform), and clipping ``end`` references.
"""

import os
import shutil

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QHeaderView, QMessageBox,
)

from .i18n import tr, language_changed
from .spine_json import load_spine_json, save_spine_json, is_skins_list


# ==========================================================================
# Core logic
# ==========================================================================

BLEND_SUFFIXES = ("_normal", "_additive", "_multiply", "_screen")


def _strip_blend_suffix(name: str) -> str:
    """Remove any existing blend-mode suffix from *name*."""
    for suffix in BLEND_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _blend_for_slot(slot: dict) -> str:
    """Return the effective blend mode string for a slot dict."""
    return slot.get("blend", "normal")


def compute_renames(spine_data: dict) -> list[dict]:
    """Return a list of slots that would be renamed.

    Each entry is ``{name, bone, blend, new_name}``.
    Only slots whose current name does not already end with the correct
    suffix are returned.
    """
    results = []
    for slot in spine_data.get("slots", []):
        name = slot.get("name", "")
        blend = _blend_for_slot(slot)
        base = _strip_blend_suffix(name)
        new_name = f"{base}_{blend}"
        if new_name != name:
            results.append({
                "name": name,
                "bone": slot.get("bone", ""),
                "blend": blend,
                "new_name": new_name,
            })
    return results


def apply_renames(spine_data: dict, rename_map: dict[str, str]):
    """Rename slots throughout *spine_data* according to *rename_map*.

    *rename_map* maps old slot name -> new slot name.
    Updates all six reference locations in place.
    """
    if not rename_map:
        return

    # 1. slots[].name
    for slot in spine_data.get("slots", []):
        old = slot.get("name", "")
        if old in rename_map:
            slot["name"] = rename_map[old]

    # 2. skins — slot name is used as dict key
    if is_skins_list(spine_data):
        for skin in spine_data.get("skins", []):
            _rename_skin_slots(skin.get("attachments", {}), rename_map)
    else:
        for skin_name, skin_data in spine_data.get("skins", {}).items():
            _rename_skin_slots(skin_data, rename_map)

    # 3 + 4 + 5. animations — slots, drawOrder, deform
    for anim_name, anim in spine_data.get("animations", {}).items():
        # 3. slot timelines
        if "slots" in anim:
            anim["slots"] = _rename_dict_keys(anim["slots"], rename_map)
        # 4. drawOrder offsets
        for entry in anim.get("drawOrder", []):
            for offset in entry.get("offsets", []):
                old = offset.get("slot", "")
                if old in rename_map:
                    offset["slot"] = rename_map[old]
        # 5. deform — slot name is a key (nested under skin name)
        if "deform" in anim:
            for skin_name, skin_deform in anim["deform"].items():
                anim["deform"][skin_name] = _rename_dict_keys(
                    skin_deform, rename_map)

    # 6. clipping "end" references in skins
    if is_skins_list(spine_data):
        for skin in spine_data.get("skins", []):
            _rename_clipping_ends(skin.get("attachments", {}), rename_map)
    else:
        for skin_name, skin_data in spine_data.get("skins", {}).items():
            _rename_clipping_ends(skin_data, rename_map)


def _rename_dict_keys(d: dict, rename_map: dict[str, str]) -> dict:
    """Return a new dict with keys renamed according to *rename_map*."""
    new = {}
    for key, value in d.items():
        new_key = rename_map.get(key, key)
        new[new_key] = value
    return new


def _rename_skin_slots(skin_data: dict, rename_map: dict[str, str]):
    """Rename slot keys in a skin attachments dict in place."""
    keys_to_rename = [k for k in skin_data if k in rename_map]
    for old_key in keys_to_rename:
        skin_data[rename_map[old_key]] = skin_data.pop(old_key)


def _rename_clipping_ends(skin_data: dict, rename_map: dict[str, str]):
    """Update clipping attachment 'end' values in a skin dict."""
    for slot_atts in skin_data.values():
        if not isinstance(slot_atts, dict):
            continue
        for att_data in slot_atts.values():
            if not isinstance(att_data, dict):
                continue
            if att_data.get("type") == "clipping":
                end = att_data.get("end", "")
                if end in rename_map:
                    att_data["end"] = rename_map[end]


# ==========================================================================
# UI Tab
# ==========================================================================

class SlotRenamerTab:
    def __init__(self, tabs: QTabWidget, get_config, on_modified=None):
        self._get_config = get_config
        self._on_modified = on_modified
        self._tabs = tabs

        self._page = QWidget()
        tabs.addTab(self._page, tr("renamer.tab"))
        layout = QVBoxLayout(self._page)
        layout.setContentsMargins(5, 5, 5, 5)

        self._info = QLabel(tr("renamer.info"))
        self._info.setWordWrap(True)
        layout.addWidget(self._info)

        self._stats = QLabel(tr("renamer.default_stats"))
        self._stats.setStyleSheet("font-weight: bold; color: #6ec072;")
        layout.addWidget(self._stats)

        btn_row = QHBoxLayout()
        self._rename_btn = QPushButton(tr("renamer.rename_btn"))
        self._rename_btn.setEnabled(False)
        self._rename_btn.clicked.connect(self._rename)
        btn_row.addWidget(self._rename_btn)
        self._btn_select_all = QPushButton(tr("renamer.select_all"))
        self._btn_unselect_all = QPushButton(tr("renamer.unselect_all"))
        self._btn_select_all.clicked.connect(self._select_all)
        self._btn_unselect_all.clicked.connect(self._unselect_all)
        btn_row.addWidget(self._btn_select_all)
        btn_row.addWidget(self._btn_unselect_all)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels([
            tr("renamer.tree.slot"),
            tr("renamer.tree.blend"),
            tr("renamer.tree.new_name"),
        ])
        self._tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._tree.header().setSectionResizeMode(2, QHeaderView.Stretch)
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
            self._tabs.setTabText(idx, tr("renamer.tab"))
        self._info.setText(tr("renamer.info"))
        self._stats.setText(tr("renamer.default_stats"))
        self._rename_btn.setText(tr("renamer.rename_btn"))
        self._btn_select_all.setText(tr("renamer.select_all"))
        self._btn_unselect_all.setText(tr("renamer.unselect_all"))
        self._tree.setHeaderLabels([
            tr("renamer.tree.slot"),
            tr("renamer.tree.blend"),
            tr("renamer.tree.new_name"),
        ])

    def _analyze(self):
        json_path = self._get_config("json")
        if not json_path or not os.path.isfile(json_path):
            return
        try:
            spine_data = load_spine_json(json_path)
        except Exception:
            return

        renames = compute_renames(spine_data)
        self._tree.clear()

        if not renames:
            self._stats.setText(tr("renamer.nothing"))
            self._rename_btn.setEnabled(False)
            return

        for r in renames:
            item = QTreeWidgetItem(self._tree, [
                r["name"], r["blend"], r["new_name"],
            ])
            item.setCheckState(0, Qt.Checked)

        self._stats.setText(tr("renamer.stats", count=len(renames)))
        self._rename_btn.setEnabled(True)

    def _rename(self):
        checked = []
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            if item.checkState(0) == Qt.Checked:
                checked.append((item.text(0), item.text(2)))  # (old, new)
        if not checked:
            return

        json_path = self._get_config("json")
        if not json_path or not os.path.isfile(json_path):
            return

        backup_path = json_path + ".backup"

        if QMessageBox.question(
            None, tr("confirm.title"),
            tr("renamer.confirm", count=len(checked), backup=backup_path),
        ) != QMessageBox.Yes:
            return

        try:
            shutil.copy2(json_path, backup_path)
            spine_data = load_spine_json(json_path)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"),
                                 tr("err.load_backup", error=e))
            return

        rename_map = dict(checked)
        apply_renames(spine_data, rename_map)

        try:
            save_spine_json(json_path, spine_data)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"),
                                 tr("err.save_json", error=e))
            return

        QMessageBox.information(
            None, tr("done.title"),
            tr("renamer.done", count=len(rename_map), backup=backup_path))
        if self._on_modified:
            self._on_modified()
        else:
            self._analyze()
