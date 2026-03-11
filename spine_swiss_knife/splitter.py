"""
Splitter tab — split a Spine project into sub-projects by animation groups.
Each sub-project gets only the animations, bones, slots, attachments, and images it needs.
"""

import os
import shutil
import json as _json

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QLineEdit,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QHeaderView, QMessageBox,
    QGroupBox, QFileDialog,
)
from PySide6.QtCore import Qt

from .i18n import tr, language_changed
from .spine_json import load_spine_json, save_spine_json, normalize_skins, is_skins_list, denormalize_skins


def _save_spine_format(path: str, data: dict):
    """Save Spine JSON in the same whitespace format that Spine itself exports."""
    raw = _json.dumps(data, ensure_ascii=False, separators=(", ", ": "))

    out = []
    out.append("{")

    top_keys = list(data.keys())
    for ki, key in enumerate(top_keys):
        val = data[key]
        comma = "," if ki < len(top_keys) - 1 else ""

        if key == "skeleton":
            skel_str = _json.dumps(val, ensure_ascii=False, separators=(", ", ": "))
            if skel_str.startswith("{") and skel_str.endswith("}"):
                skel_str = "{ " + skel_str[1:-1] + " }"
            out.append(f'"{key}": {skel_str}{comma}')

        elif key == "skins" and isinstance(val, list):
            out.append(f'"skins": [')
            for si, skin in enumerate(val):
                skin_comma = "," if si < len(val) - 1 else ""
                skin_name = skin.get("name", "default")
                skin_atts = skin.get("attachments", {})
                out.append("{")
                out.append(f'"name": "{skin_name}",')
                out.append('"attachments": {')
                slot_keys = list(skin_atts.keys())
                for sli, slot_name in enumerate(slot_keys):
                    slot_atts = skin_atts[slot_name]
                    sl_comma = "," if sli < len(slot_keys) - 1 else ""
                    att_str = _json.dumps(slot_atts, ensure_ascii=False, separators=(", ", ": "))
                    out.append(f'"{slot_name}": {att_str}{sl_comma}')
                out.append("}")
                out.append(f"}}{skin_comma}")
            out.append(f"]{comma}")

        elif isinstance(val, list):
            out.append(f'"{key}": [')
            for vi, item in enumerate(val):
                item_comma = "," if vi < len(val) - 1 else ""
                item_str = _json.dumps(item, ensure_ascii=False, separators=(", ", ": "))
                if item_str.startswith("{") and item_str.endswith("}"):
                    item_str = "{ " + item_str[1:-1] + " }"
                out.append(f'\t{item_str}{item_comma}')
            out.append(f"]{comma}")

        elif isinstance(val, dict) and key in ("skins", "animations", "events"):
            out.append(f'"{key}": {{')
            dict_keys = list(val.keys())
            for di, dk in enumerate(dict_keys):
                dv = val[dk]
                dcomma = "," if di < len(dict_keys) - 1 else ""
                if isinstance(dv, dict):
                    out.append(f'"{dk}": {{')
                    inner_keys = list(dv.keys())
                    for ii, ik in enumerate(inner_keys):
                        iv = dv[ik]
                        icomma = "," if ii < len(inner_keys) - 1 else ""
                        iv_str = _json.dumps(iv, ensure_ascii=False, separators=(", ", ": "))
                        out.append(f'"{ik}": {iv_str}{icomma}')
                    out.append(f"}}{dcomma}")
                else:
                    dv_str = _json.dumps(dv, ensure_ascii=False, separators=(", ", ": "))
                    out.append(f'"{dk}": {dv_str}{dcomma}')
            out.append(f"}}{comma}")

        else:
            val_str = _json.dumps(val, ensure_ascii=False, separators=(", ", ": "))
            out.append(f'"{key}": {val_str}{comma}')

    out.append("}")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


# ==========================================================================
# Core logic — determine dependencies for a set of animations
# ==========================================================================

def _build_bone_hierarchy(bones_list: list) -> dict:
    parent_map = {}
    bone_data_map = {}
    for bone in bones_list:
        name = bone["name"]
        parent_map[name] = bone.get("parent")
        bone_data_map[name] = bone
    return parent_map, bone_data_map


def _ancestors(bone_name: str, parent_map: dict) -> set:
    result = set()
    current = bone_name
    while current:
        result.add(current)
        current = parent_map.get(current)
    return result


def _get_slot_bone_map(slots_list: list) -> dict:
    return {s["name"]: s["bone"] for s in slots_list}


def _get_slot_default_attachment(slots_list: list) -> dict:
    return {s["name"]: s.get("attachment") for s in slots_list}


def compute_dependencies(spine_data: dict, anim_names: list[str]) -> dict:
    bones_list = spine_data.get("bones", [])
    slots_list = spine_data.get("slots", [])
    skins = normalize_skins(spine_data.get("skins", {}))
    animations = spine_data.get("animations", {})

    parent_map, bone_data_map = _build_bone_hierarchy(bones_list)
    slot_bone_map = _get_slot_bone_map(slots_list)
    slot_default_att = _get_slot_default_attachment(slots_list)

    animated_bones = set()
    animated_slots = set()
    slot_att_refs = {}

    for anim_name in anim_names:
        anim = animations.get(anim_name, {})
        for bone_name in anim.get("bones", {}):
            animated_bones.add(bone_name)
        for slot_name, channels in anim.get("slots", {}).items():
            animated_slots.add(slot_name)
            if "attachment" in channels:
                for key in channels["attachment"]:
                    att_name = key.get("name")
                    if att_name is not None:
                        slot_att_refs.setdefault(slot_name, set()).add(att_name)
        for entry in anim.get("drawOrder", []):
            for offset in entry.get("offsets", []):
                if "slot" in offset:
                    animated_slots.add(offset["slot"])
        for slot_name in anim.get("deform", {}).get("default", {}):
            animated_slots.add(slot_name)

    bones_needed = set()
    for bone in animated_bones:
        bones_needed |= _ancestors(bone, parent_map)
    for slot in animated_slots:
        bone = slot_bone_map.get(slot)
        if bone:
            bones_needed |= _ancestors(bone, parent_map)

    slots_needed = set()
    for slot in slots_list:
        if slot["bone"] in bones_needed:
            slots_needed.add(slot["name"])

    attachments_needed = {}
    for slot_name in slots_needed:
        atts = set()
        default_att = slot_default_att.get(slot_name)
        if default_att:
            atts.add(default_att)
        if slot_name in slot_att_refs:
            atts |= slot_att_refs[slot_name]
        if not atts:
            for skin_name, skin_data in skins.items():
                if slot_name in skin_data:
                    atts |= set(skin_data[slot_name].keys())
        if atts:
            attachments_needed[slot_name] = atts

    image_keys = set()
    for skin_name, skin_data in skins.items():
        for slot_name, slot_atts in skin_data.items():
            if slot_name not in attachments_needed:
                continue
            needed = attachments_needed[slot_name]
            for att_name, att_data in slot_atts.items():
                if att_name in needed:
                    att_type = att_data.get("type")
                    if att_type is None or att_type == "region":
                        image_key = att_data.get("path", att_name)
                        image_keys.add(image_key)

    return {
        "bones_needed": bones_needed,
        "slots_needed": slots_needed,
        "attachments_needed": attachments_needed,
        "image_keys_needed": image_keys,
    }


def build_split_json(spine_data: dict, anim_names: list[str], deps: dict,
                     group_name: str = "", images_path: str = "") -> dict:
    bones_needed = deps["bones_needed"]
    slots_needed = deps["slots_needed"]
    attachments_needed = deps["attachments_needed"]
    result = {}

    if "skeleton" in spine_data:
        result["skeleton"] = dict(spine_data["skeleton"])
        if images_path:
            result["skeleton"]["images"] = images_path

    result["bones"] = [b for b in spine_data.get("bones", []) if b["name"] in bones_needed]
    result["slots"] = [s for s in spine_data.get("slots", []) if s["name"] in slots_needed]

    skins_normalized = normalize_skins(spine_data.get("skins", {}))
    filtered_skins = {}
    for skin_name, skin_data in skins_normalized.items():
        filtered_skin = {}
        for slot_name, slot_atts in skin_data.items():
            if slot_name not in slots_needed:
                continue
            needed_atts = attachments_needed.get(slot_name, set())
            filtered_atts = {}
            for att_name, att_data in slot_atts.items():
                if att_name in needed_atts or not needed_atts:
                    filtered_atts[att_name] = att_data
            if filtered_atts:
                filtered_skin[slot_name] = filtered_atts
        if filtered_skin:
            filtered_skins[skin_name] = filtered_skin

    if is_skins_list(spine_data):
        result["skins"] = denormalize_skins(filtered_skins)
    else:
        result["skins"] = filtered_skins

    if "events" in spine_data:
        used_events = set()
        for anim_name in anim_names:
            anim = spine_data.get("animations", {}).get(anim_name, {})
            for ev in anim.get("events", []):
                if "name" in ev:
                    used_events.add(ev["name"])
        if used_events:
            result["events"] = {k: v for k, v in spine_data["events"].items() if k in used_events}

    if "ik" in spine_data:
        result["ik"] = [
            c for c in spine_data["ik"]
            if c.get("target") in bones_needed
            or any(b in bones_needed for b in c.get("bones", []))
        ]
        if not result["ik"]:
            del result["ik"]

    if "transform" in spine_data:
        result["transform"] = [
            c for c in spine_data["transform"]
            if c.get("target") in bones_needed
            or any(b in bones_needed for b in c.get("bones", []))
        ]
        if not result["transform"]:
            del result["transform"]

    if "path" in spine_data:
        result["path"] = [
            c for c in spine_data["path"]
            if any(b in bones_needed for b in
                   (c.get("bones", []) if isinstance(c.get("bones"), list) else [c.get("bones", "")]))
        ]
        if not result["path"]:
            del result["path"]

    result["animations"] = {
        name: spine_data["animations"][name]
        for name in anim_names
        if name in spine_data.get("animations", {})
    }

    return result


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

        json_basename = os.path.splitext(os.path.basename(json_path))[0]

        all_images = {}
        if images_dir and os.path.isdir(images_dir):
            image_exts = {".png", ".jpg", ".jpeg", ".bmp", ".tga", ".webp"}
            for root, dirs, files in os.walk(images_dir):
                dirs[:] = [d for d in dirs if d not in ("_UNUSED", "_DOWNSCALE")]
                for fname in files:
                    ext = os.path.splitext(fname)[1].lower()
                    if ext in image_exts:
                        full_path = os.path.join(root, fname)
                        rel = os.path.relpath(full_path, images_dir)
                        key = os.path.splitext(rel)[0]
                        all_images[key] = full_path

        created = 0
        errors = []
        images_path_override = self._images_path_edit.text().strip()

        for group_name, anim_names in sorted(groups.items()):
            group_dir = os.path.join(output_dir, group_name)
            group_images_dir = os.path.join(group_dir, "images")
            os.makedirs(group_images_dir, exist_ok=True)
            try:
                deps = compute_dependencies(self._spine_data, anim_names)
                split_json = build_split_json(
                    self._spine_data, anim_names, deps,
                    group_name=group_name, images_path=images_path_override,
                )
                json_out = os.path.join(group_dir, f"{group_name}.json")
                _save_spine_format(json_out, split_json)
                images_copied = 0
                for image_key in deps["image_keys_needed"]:
                    if image_key in all_images:
                        src = all_images[image_key]
                        rel = os.path.relpath(src, images_dir)
                        dst = os.path.join(group_images_dir, rel)
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        shutil.copy2(src, dst)
                        images_copied += 1
                created += 1
            except Exception as e:
                errors.append(f"Group '{group_name}': {e}")

        msg = tr("splitter.done", created=created, total=len(groups), dir=output_dir)
        if errors:
            msg += f"\n\nErrors ({len(errors)}):\n" + "\n".join(errors[:5])
        QMessageBox.information(None, tr("done.title"), msg)
        self._stats.setText(tr("splitter.done_stats", count=created, dir=output_dir))
