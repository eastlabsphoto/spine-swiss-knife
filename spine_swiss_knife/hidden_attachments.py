"""Hidden Attachments tab — detect & fix invisible GPU waste from alpha=0 slots."""

import os
import shutil

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QHeaderView, QMessageBox,
)

from .i18n import tr, language_changed
from .spine_json import load_spine_json, save_spine_json


# ==========================================================================
# Analysis logic
# ==========================================================================

def _build_bone_maps(spine_data):
    """Return (children_map, bone_dict_map)."""
    children = {}
    bone_map = {}
    for bone in spine_data.get("bones", []):
        name = bone["name"]
        bone_map[name] = bone
        children.setdefault(name, [])
        parent = bone.get("parent")
        if parent is not None:
            children.setdefault(parent, [])
            children[parent].append(name)
    return children, bone_map


def _find_zero_scale_bones(bone_map):
    """Return set of bone names where scaleX == 0 or scaleY == 0."""
    result = set()
    for name, bone in bone_map.items():
        if bone.get("scaleX", 1) == 0 or bone.get("scaleY", 1) == 0:
            result.add(name)
    return result


def _collect_descendants(bone_name, children_map):
    """Return set of bone_name and all its descendants."""
    result = {bone_name}
    for child in children_map.get(bone_name, []):
        result |= _collect_descendants(child, children_map)
    return result


def _alpha_from_hex(color_hex):
    """Extract alpha (0-255) from an 8-char hex color string."""
    color_hex = str(color_hex)
    if len(color_hex) >= 8:
        return int(color_hex[6:8], 16)
    return 255


def analyze_hidden_attachments(spine_data):
    """Detect slots with active attachments that are invisible.

    Returns (candidates, anim_details, warnings).

    candidates — list of {slot_name, original_attachment, bone_name, reason, ...}
    anim_details — list of {animation, slot_name, action}
    warnings — list of mid-animation alpha-to-zero without attachment null
    """
    children_map, bone_map = _build_bone_maps(spine_data)
    zero_scale_bones = _find_zero_scale_bones(bone_map)

    # Collect all bone names under zero-scale bones
    zero_scale_all = set()
    for zb in zero_scale_bones:
        zero_scale_all |= _collect_descendants(zb, children_map)

    # ── Phase 1: find candidates ──
    candidates = []
    for slot in spine_data.get("slots", []):
        slot_name = slot["name"]
        attachment = slot.get("attachment")
        if attachment is None:
            continue

        bone_name = slot.get("bone", "")
        color = slot.get("color", "ffffffff")
        alpha = _alpha_from_hex(color)

        reason = None
        zero_bone = None
        if alpha == 0:
            reason = "alpha_zero"
        elif bone_name in zero_scale_all:
            ancestor = bone_name
            while ancestor and ancestor not in zero_scale_bones:
                ancestor = bone_map.get(ancestor, {}).get("parent")
            reason = "bone_scale_zero"
            zero_bone = ancestor or bone_name

        if reason is None:
            continue

        cand = {
            "slot_name": slot_name,
            "original_attachment": attachment,
            "bone_name": bone_name,
            "reason": reason,
        }
        if zero_bone:
            cand["zero_scale_bone"] = zero_bone
        candidates.append(cand)

    # ── Phase 2: classify candidate × animation ──
    animations = spine_data.get("animations", {})
    anim_details = []

    for cand in candidates:
        slot_name = cand["slot_name"]
        anims_affected = []

        for anim_name, anim_data in animations.items():
            slot_tl = anim_data.get("slots", {}).get(slot_name, {})

            # Check color timeline for alpha > 0
            color_keys = slot_tl.get("color") or slot_tl.get("rgba") or []
            alpha_goes_visible = False
            for kf in color_keys:
                c = kf.get("color", kf.get("rgba", "ffffffff"))
                if _alpha_from_hex(c) > 0:
                    alpha_goes_visible = True
                    break

            if not alpha_goes_visible:
                continue  # always_hidden in this animation

            att_keys = slot_tl.get("attachment")
            action = "already_ok" if att_keys else "insert_kf"

            anim_details.append({
                "animation": anim_name,
                "slot_name": slot_name,
                "action": action,
            })
            anims_affected.append(anim_name)

        cand["anims_affected"] = anims_affected

    # ── Phase 3: mid-animation warnings (all slots) ──
    warnings = []
    for anim_name, anim_data in animations.items():
        for slot_name, timelines in anim_data.get("slots", {}).items():
            color_keys = timelines.get("color") or timelines.get("rgba") or []
            att_keys = timelines.get("attachment") or []

            for kf in color_keys:
                t = kf.get("time", 0)
                if t == 0:
                    continue
                c = kf.get("color", kf.get("rgba", "ffffffff"))
                if _alpha_from_hex(c) > 0:
                    continue
                # Alpha goes to 0 mid-animation — check for attachment null nearby
                has_null = any(
                    ak.get("name") is None and abs(ak.get("time", 0) - t) < 0.001
                    for ak in att_keys
                )
                if not has_null:
                    warnings.append({
                        "animation": anim_name,
                        "slot_name": slot_name,
                        "time": t,
                    })

    return candidates, anim_details, warnings


def fix_hidden_attachments(spine_data, candidates, anim_details):
    """Apply fixes: null setup-pose attachments, insert animation keyframes.

    Returns (slots_fixed, keyframes_inserted).
    """
    candidate_names = {c["slot_name"] for c in candidates}
    candidate_att = {c["slot_name"]: c["original_attachment"] for c in candidates}

    # Fix setup pose
    slots_fixed = 0
    for slot in spine_data.get("slots", []):
        if slot["name"] in candidate_names and slot.get("attachment") is not None:
            slot["attachment"] = None
            slots_fixed += 1

    # Insert animation keyframes
    keyframes_inserted = 0
    animations = spine_data.get("animations", {})

    for detail in anim_details:
        if detail["action"] != "insert_kf":
            continue

        anim_name = detail["animation"]
        slot_name = detail["slot_name"]
        original = candidate_att.get(slot_name)
        if original is None:
            continue

        anim_data = animations.get(anim_name)
        if anim_data is None:
            continue

        if "slots" not in anim_data:
            anim_data["slots"] = {}
        if slot_name not in anim_data["slots"]:
            anim_data["slots"][slot_name] = {}

        slot_tl = anim_data["slots"][slot_name]
        if "attachment" not in slot_tl:
            slot_tl["attachment"] = []

        slot_tl["attachment"].insert(0, {"time": 0, "name": original})
        keyframes_inserted += 1

    return slots_fixed, keyframes_inserted


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
            shutil.copy2(json_path, backup_path)
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
