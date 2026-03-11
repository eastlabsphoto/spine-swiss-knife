"""Project Analyzer tab — project health check and overview report."""

import os
import subprocess

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTabWidget,
    QTextEdit, QMessageBox,
)
from PySide6.QtGui import QFont, QColor, QTextCharFormat, QTextCursor
from PySide6.QtCore import Qt

from .i18n import tr, language_changed
from .spine_json import load_spine_json, normalize_skins
from .atlas_parser import collect_images

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tga", ".webp"}


# ==========================================================================
# Analysis helpers (unchanged)
# ==========================================================================

def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


def _get_image_dimensions(filepath: str):
    if PILImage is not None:
        try:
            with PILImage.open(filepath) as img:
                return (img.width, img.height)
        except Exception:
            pass
    try:
        result = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", filepath],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            width = height = None
            for line in result.stdout.splitlines():
                line = line.strip()
                if "pixelWidth" in line:
                    width = int(line.split(":")[-1].strip())
                elif "pixelHeight" in line:
                    height = int(line.split(":")[-1].strip())
            if width is not None and height is not None:
                return (width, height)
    except Exception:
        pass
    return None


def _count_keyframes_in_timeline(timeline_dict: dict) -> int:
    total = 0
    for entries in timeline_dict.values():
        if isinstance(entries, list):
            total += len(entries)
        elif isinstance(entries, dict):
            for sub_entries in entries.values():
                if isinstance(sub_entries, list):
                    total += len(sub_entries)
    return total


def _analyze_animations(spine_data: dict) -> dict:
    animations = spine_data.get("animations", {})
    anim_details = []
    total_keyframes = 0

    for anim_name, anim_data in animations.items():
        kf_count = bone_kf = slot_kf = 0

        bones_tl = anim_data.get("bones", {})
        for bone_name, channels in bones_tl.items():
            if isinstance(channels, dict):
                for channel_name, frames in channels.items():
                    if isinstance(frames, list):
                        bone_kf += len(frames)

        slots_tl = anim_data.get("slots", {})
        for slot_name, channels in slots_tl.items():
            if isinstance(channels, dict):
                for channel_name, frames in channels.items():
                    if isinstance(frames, list):
                        slot_kf += len(frames)

        kf_count = bone_kf + slot_kf
        for extra_key in ("deform", "ik", "transform", "path"):
            extra = anim_data.get(extra_key, {})
            if isinstance(extra, dict):
                kf_count += _count_keyframes_in_timeline(extra)

        events = anim_data.get("events", [])
        if isinstance(events, list):
            kf_count += len(events)
        draw_order = anim_data.get("drawOrder", [])
        if isinstance(draw_order, list):
            kf_count += len(draw_order)

        duration = 0.0
        for section_data in anim_data.values():
            if isinstance(section_data, list):
                for frame in section_data:
                    if isinstance(frame, dict):
                        duration = max(duration, frame.get("time", 0))
            elif isinstance(section_data, dict):
                for sub in section_data.values():
                    if isinstance(sub, list):
                        for frame in sub:
                            if isinstance(frame, dict):
                                duration = max(duration, frame.get("time", 0))
                    elif isinstance(sub, dict):
                        for sub2 in sub.values():
                            if isinstance(sub2, list):
                                for frame in sub2:
                                    if isinstance(frame, dict):
                                        duration = max(duration, frame.get("time", 0))

        anim_details.append({
            "name": anim_name, "keyframes": kf_count,
            "bone_keyframes": bone_kf, "slot_keyframes": slot_kf,
            "duration": duration,
        })
        total_keyframes += kf_count

    anim_details.sort(key=lambda a: a["keyframes"], reverse=True)
    return {"count": len(animations), "total_keyframes": total_keyframes, "details": anim_details}


def _analyze_attachments(skins: dict) -> dict:
    type_counts = {}
    total_attachments = 0
    referenced_images = set()
    high_vertex_attachments = []

    for skin_name, skin_data in skins.items():
        for slot_name, attachments in skin_data.items():
            for att_name, att_data in attachments.items():
                total_attachments += 1
                att_type = att_data.get("type")
                if att_type is None:
                    att_type = "region"
                type_counts[att_type] = type_counts.get(att_type, 0) + 1
                if att_type in ("region", "mesh"):
                    referenced_images.add(att_data.get("path", att_name))
                if att_type in ("mesh", "clipping"):
                    vc = att_data.get("vertexCount", 0)
                    if att_type == "mesh":
                        uvs = att_data.get("uvs", [])
                        vc = len(uvs) // 2 if uvs else vc
                    if vc > 0:
                        high_vertex_attachments.append({
                            "skin": skin_name, "slot": slot_name,
                            "name": att_name, "type": att_type, "vertices": vc,
                        })

    high_vertex_attachments.sort(key=lambda a: a["vertices"], reverse=True)
    return {
        "total": total_attachments, "type_counts": type_counts,
        "referenced_images": referenced_images,
        "high_vertex_attachments": high_vertex_attachments,
    }


def _analyze_dead_bones(spine_data: dict) -> list[str]:
    bones = spine_data.get("bones", [])
    bone_names = {b.get("name", "") for b in bones if isinstance(b, dict)}
    slots = spine_data.get("slots", [])
    bones_with_slots = set()
    for slot in slots:
        if isinstance(slot, dict):
            bone = slot.get("bone", "")
            if bone:
                bones_with_slots.add(bone)

    bones_with_anims = set()
    animations = spine_data.get("animations", {})
    for anim_name, anim_data in animations.items():
        bones_with_anims.update(anim_data.get("bones", {}).keys())

    for constraint_type in ("ik", "transform", "path"):
        constraints = spine_data.get(constraint_type, [])
        if isinstance(constraints, list):
            for c in constraints:
                if isinstance(c, dict):
                    cbones = c.get("bones", [])
                    if isinstance(cbones, list):
                        bones_with_anims.update(cbones)
                    for field in ("target", "bone"):
                        v = c.get(field, "")
                        if v:
                            bones_with_anims.add(v)

    used_bones = bones_with_slots | bones_with_anims | {"root"}
    bone_parents = {}
    for b in bones:
        if isinstance(b, dict):
            name = b.get("name", "")
            parent = b.get("parent", "")
            if name:
                bone_parents[name] = parent

    needed = set()
    for bone in used_bones:
        current = bone
        while current and current not in needed:
            needed.add(current)
            current = bone_parents.get(current, "")

    return sorted(bone_names - needed)


def _find_masked_meshes(spine_data: dict, skins: dict) -> list[dict]:
    slots = spine_data.get("slots", [])
    slot_order = {s["name"]: i for i, s in enumerate(slots)}
    mesh_slots = {}
    for skin_name, skin_data in skins.items():
        for slot_name, atts in skin_data.items():
            for att_name, att_data in atts.items():
                if att_data.get("type") == "mesh":
                    mesh_slots.setdefault(slot_name, set()).add(att_name)
    if not mesh_slots:
        return []

    results = []
    for skin_name, skin_data in skins.items():
        for slot_name, atts in skin_data.items():
            for att_name, att_data in atts.items():
                if att_data.get("type") != "clipping":
                    continue
                end_slot = att_data.get("end", "")
                clip_idx = slot_order.get(slot_name, -1)
                end_idx = slot_order.get(end_slot, -1)
                if clip_idx < 0 or end_idx < 0:
                    continue
                for s in slots[clip_idx + 1: end_idx + 1]:
                    sname = s["name"]
                    if sname in mesh_slots:
                        for matt in mesh_slots[sname]:
                            results.append({
                                "clip_slot": slot_name, "clip_att": att_name,
                                "mesh_slot": sname, "mesh_att": matt,
                            })
    return results


def _analyze_images_on_disk(images_dir: str) -> dict:
    all_images = collect_images(images_dir)
    image_infos = []
    total_size = 0
    for name, full_path in all_images.items():
        try:
            fsize = os.path.getsize(full_path)
        except OSError:
            fsize = 0
        total_size += fsize
        dims = _get_image_dimensions(full_path)
        image_infos.append({
            "name": name, "path": full_path,
            "rel_path": os.path.relpath(full_path, images_dir),
            "file_size": fsize, "dimensions": dims,
        })
    image_infos.sort(key=lambda i: i["file_size"], reverse=True)
    return {"count": len(all_images), "total_size": total_size, "images": image_infos}


# ==========================================================================
# UI Tab
# ==========================================================================

class ProjectAnalyzerTab:
    def __init__(self, tabs: QTabWidget, get_config):
        self._get_config = get_config
        self._tabs = tabs
        self._has_report = False

        self._page = QWidget()
        tabs.addTab(self._page, tr("analyzer.tab"))
        layout = QVBoxLayout(self._page)
        layout.setContentsMargins(5, 5, 5, 5)

        self._text = QTextEdit()
        self._text.setReadOnly(True)
        self._text.setFont(QFont("Menlo, Courier New, monospace", 11))
        self._text.setPlainText(tr("analyzer.default_text"))
        layout.addWidget(self._text, 1)

        language_changed.connect(self._retranslate)

    def _retranslate(self):
        idx = self._tabs.indexOf(self._page)
        if idx >= 0:
            self._tabs.setTabText(idx, tr("analyzer.tab"))
        if not self._has_report:
            self._text.setPlainText(tr("analyzer.default_text"))

    def _append(self, text: str, style: str = "normal"):
        cursor = self._text.textCursor()
        cursor.movePosition(QTextCursor.End)
        fmt = QTextCharFormat()
        if style == "header":
            fmt.setFontWeight(QFont.Bold)
            fmt.setFontPointSize(12)
            fmt.setForeground(QColor("#6ec072"))
        elif style == "subheader":
            fmt.setFontWeight(QFont.Bold)
            fmt.setForeground(QColor("#cdd6f4"))
        elif style == "warning":
            fmt.setForeground(QColor("#f9e2af"))
        elif style == "critical":
            fmt.setForeground(QColor("#f38ba8"))
            fmt.setFontWeight(QFont.Bold)
        cursor.insertText(text, fmt)

    def _analyze(self):
        json_path = self._get_config("json")
        images_dir = self._get_config("images")

        if not json_path or not os.path.isfile(json_path):
            QMessageBox.critical(None, tr("err.title"), tr("err.no_json"))
            return

        try:
            spine_data = load_spine_json(json_path)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("err.parse_json", error=e))
            return

        self._text.clear()
        self._has_report = True

        # --- CRITICAL WARNINGS ---
        json_size = os.path.getsize(json_path)
        skins = normalize_skins(spine_data.get("skins", {}))
        masked_meshes = _find_masked_meshes(spine_data, skins)
        has_critical = False

        if json_size > 3 * 1024 * 1024:
            self._append(
                tr("analyzer.warn.json_large_critical", size=_format_size(json_size)) + "\n",
                "critical",
            )
            has_critical = True

        if masked_meshes:
            unique_meshes = {}
            for mm in masked_meshes:
                key = (mm["mesh_slot"], mm["mesh_att"])
                unique_meshes.setdefault(key, []).append(mm["clip_att"])
            self._append(
                tr("analyzer.warn.masked_meshes", count=len(unique_meshes)) + "\n",
                "critical",
            )
            for (mslot, matt), clip_atts in unique_meshes.items():
                clip_names = sorted(set(clip_atts))
                if len(clip_names) <= 3:
                    clips_str = ", ".join(clip_names)
                else:
                    clips_str = f"{clip_names[0]} + {len(clip_names)-1} more"
                self._append(tr("analyzer.warn.mesh_detail", name=matt, slot=mslot, clips=clips_str) + "\n", "critical")
            has_critical = True

        if has_critical:
            self._append("\n")

        # --- SKELETON ---
        self._append(tr("analyzer.section.skeleton") + "\n", "header")
        skeleton = spine_data.get("skeleton", {})
        self._append(tr("analyzer.spine_version", version=skeleton.get('spine', 'unknown')) + "\n")
        self._append(tr("analyzer.skeleton_hash", hash=skeleton.get('hash', 'unknown')) + "\n")
        self._append(tr("analyzer.dimensions", w=skeleton.get('width', 'N/A'), h=skeleton.get('height', 'N/A')) + "\n")
        self._append(tr("analyzer.source_file", name=os.path.basename(json_path)) + "\n\n")

        # --- BONES & SLOTS ---
        self._append(tr("analyzer.section.bones_slots") + "\n", "header")
        bones = spine_data.get("bones", [])
        slots = spine_data.get("slots", [])
        self._append(tr("analyzer.bones", count=len(bones)) + "\n")
        self._append(tr("analyzer.slots", count=len(slots)) + "\n")
        self._append(tr("analyzer.skins", count=len(skins)) + "\n")
        for ctype in ("ik", "transform", "path"):
            cl = spine_data.get(ctype, [])
            if cl:
                self._append(tr("analyzer.constraints", type=ctype.title(), count=len(cl)) + "\n")

        dead_bones = _analyze_dead_bones(spine_data)
        if dead_bones:
            self._append(tr("analyzer.dead_bones", count=len(dead_bones)) + "\n")
        self._append("\n")

        # --- ATTACHMENTS ---
        self._append(tr("analyzer.section.attachments") + "\n", "header")
        att_info = _analyze_attachments(skins)
        self._append(tr("analyzer.total_attachments", count=att_info['total']) + "\n")
        self._append(tr("analyzer.by_type") + "\n")
        for att_type, count in sorted(att_info["type_counts"].items()):
            self._append(tr("analyzer.type_count", type=att_type, count=count) + "\n")
        self._append(tr("analyzer.unique_images", count=len(att_info['referenced_images'])) + "\n")

        heavy_atts = att_info["high_vertex_attachments"][:10]
        if heavy_atts:
            self._append("\n" + tr("analyzer.highest_vertex") + "\n", "subheader")
            for att in heavy_atts:
                self._append(
                    tr("analyzer.vertex_entry", name=att['name'], type=att['type'],
                       verts=att['vertices'], skin=att['skin'], slot=att['slot']) + "\n"
                )
        self._append("\n")

        # --- ANIMATIONS ---
        self._append(tr("analyzer.section.animations") + "\n", "header")
        anim_info = _analyze_animations(spine_data)
        self._append(tr("analyzer.total_anims", count=anim_info['count'], kf=anim_info['total_keyframes']) + "\n")
        if anim_info["details"]:
            h = anim_info["details"][0]
            self._append(tr("analyzer.heaviest", name=h['name'], kf=h['keyframes'], dur=f"{h['duration']:.1f}") + "\n")
            self._append("\n" + tr("analyzer.all_anims") + "\n", "subheader")
            for a in anim_info["details"]:
                self._append(
                    tr("analyzer.anim_entry", name=a['name'], dur=f"{a['duration']:.2f}",
                       kf=a['keyframes'], bone_kf=a['bone_keyframes'], slot_kf=a['slot_keyframes']) + "\n"
                )
        self._append("\n")

        # --- IMAGES ---
        self._append(tr("analyzer.section.images") + "\n", "header")
        if images_dir and os.path.isdir(images_dir):
            img_info = _analyze_images_on_disk(images_dir)
            self._append(tr("analyzer.total_images", count=img_info['count'], size=_format_size(img_info['total_size'])) + "\n")
            top = img_info["images"][:10]
            if top:
                self._append("\n" + tr("analyzer.largest_by_size") + "\n", "subheader")
                for img in top:
                    d = f" ({img['dimensions'][0]}x{img['dimensions'][1]})" if img["dimensions"] else ""
                    self._append(tr("analyzer.image_entry_size", path=img['rel_path'], dims=d, size=_format_size(img['file_size'])) + "\n")
            imgs_d = [i for i in img_info["images"] if i["dimensions"]]
            if imgs_d:
                imgs_d.sort(key=lambda i: i["dimensions"][0] * i["dimensions"][1], reverse=True)
                self._append("\n" + tr("analyzer.largest_by_pixels") + "\n", "subheader")
                for img in imgs_d[:10]:
                    w, h = img["dimensions"]
                    self._append(tr("analyzer.image_entry_pixels", path=img['rel_path'], w=w, h=h, size=_format_size(img['file_size'])) + "\n")
        else:
            self._append(tr("analyzer.no_images_folder") + "\n")
        self._append("\n")

        # --- WARNINGS ---
        self._append(tr("analyzer.section.warnings") + "\n", "header")
        warnings_found = has_critical

        if json_size > 3 * 1024 * 1024:
            self._append(tr("analyzer.warn.json_large", size=_format_size(json_size)) + "\n", "critical")
        if masked_meshes:
            unique_count = len({(mm["mesh_slot"], mm["mesh_att"]) for mm in masked_meshes})
            self._append(tr("analyzer.warn.meshes_clipped", count=unique_count) + "\n", "critical")

        for a in anim_info["details"]:
            if a["keyframes"] > 1000:
                self._append(tr("analyzer.warn.heavy_anim", name=a['name'], kf=a['keyframes']) + "\n", "warning")
                warnings_found = True

        clipping_high = [a for a in att_info["high_vertex_attachments"] if a["type"] == "clipping" and a["vertices"] > 20]
        if clipping_high:
            self._append(tr("analyzer.warn.clipping_high", count=len(clipping_high)) + "\n", "warning")
            for c in clipping_high[:5]:
                self._append(tr("analyzer.warn.clipping_detail", name=c['name'], verts=c['vertices'], slot=c['slot']) + "\n", "warning")
            if len(clipping_high) > 5:
                self._append(tr("analyzer.warn.clipping_more", count=len(clipping_high) - 5) + "\n", "warning")
            warnings_found = True

        if images_dir and os.path.isdir(images_dir):
            large_images = [i for i in img_info["images"]
                           if i["dimensions"] and (i["dimensions"][0] > 1024 or i["dimensions"][1] > 1024)]
            if large_images:
                self._append(tr("analyzer.warn.large_images", count=len(large_images)) + "\n", "warning")
                for img in large_images[:5]:
                    w, h = img["dimensions"]
                    self._append(tr("analyzer.warn.large_image_detail", path=img['rel_path'], w=w, h=h) + "\n", "warning")
                warnings_found = True

        if dead_bones:
            self._append(tr("analyzer.warn.dead_bones", count=len(dead_bones)) + "\n", "warning")
            for b in dead_bones[:10]:
                self._append(tr("analyzer.warn.dead_bone_detail", name=b) + "\n", "warning")
            if len(dead_bones) > 10:
                self._append(tr("analyzer.warn.more", count=len(dead_bones) - 10) + "\n", "warning")
            warnings_found = True

        if not warnings_found:
            self._append(tr("analyzer.no_warnings") + "\n")

        self._text.moveCursor(QTextCursor.Start)
