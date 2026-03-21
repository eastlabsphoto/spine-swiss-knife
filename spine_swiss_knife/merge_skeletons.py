"""Merge Skeletons — combine multiple Spine JSON files into one project."""

import hashlib
import os
import shutil
from copy import deepcopy
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QLineEdit,
    QTabWidget, QTextEdit, QMessageBox, QTreeWidget, QTreeWidgetItem,
    QFileDialog, QHeaderView, QAbstractItemView, QApplication,
    QProgressDialog,
)
from PySide6.QtGui import QTextCharFormat, QColor, QFont, QTextCursor
from PySide6.QtCore import Qt

from .i18n import tr, language_changed
from .spine_json import (
    load_spine_json, save_spine_json, normalize_skins, denormalize_skins,
    is_skins_list, attachment_image_key,
)
from .spine_cli import (
    read_spine_file_version, export_spine_project,
)
from .settings import settings


# ── Image types that reference image files ──
_IMAGE_ATT_TYPES = {None, "region", "mesh", "linkedmesh"}


# ==========================================================================
# Image merge
# ==========================================================================

def _file_hash(path: str) -> str:
    """SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def merge_image_dirs(
    source_dirs: list[str],
    source_labels: list[str],
    output_dir: str,
) -> tuple[list[dict[str, str]], list[str]]:
    """Merge image directories into *output_dir*.

    Returns:
      image_rename_maps: one dict per source, mapping old_image_key -> new_image_key
                         (only entries that changed are included)
      warnings: list of warning strings
    """
    image_exts = {".png", ".jpg", ".jpeg", ".bmp", ".tga", ".webp"}
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []
    image_rename_maps: list[dict[str, str]] = [{} for _ in source_dirs]

    # Collect all images: list of (source_idx, rel_path, abs_path, image_key)
    all_images: list[tuple[int, str, str, str]] = []
    for idx, sdir in enumerate(source_dirs):
        if not sdir or not Path(sdir).is_dir():
            continue
        root = Path(sdir)
        for dirpath, dirnames, filenames in os.walk(sdir):
            dirnames[:] = [d for d in dirnames if not d.startswith("_")]
            for fname in filenames:
                fp = Path(dirpath) / fname
                if fp.suffix.lower() in image_exts:
                    rel = fp.relative_to(root)
                    key = str(rel.with_suffix("")).replace("\\", "/")
                    all_images.append((idx, str(rel), str(fp), key))

    # Group by image_key
    by_key: dict[str, list[tuple[int, str, str]]] = {}
    for idx, rel, abspath, key in all_images:
        by_key.setdefault(key, []).append((idx, rel, abspath))

    # Track output names for uniqueness
    used_output_keys: set[str] = set()

    for key, entries in sorted(by_key.items()):
        if len(entries) == 1:
            # No conflict — copy to output
            idx, rel, abspath = entries[0]
            dst = out / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(abspath, str(dst))
            used_output_keys.add(key)
            continue

        # Multiple sources have the same key — compare content
        hashes = [(idx, rel, abspath, _file_hash(abspath)) for idx, rel, abspath in entries]
        unique_hashes: dict[str, list[tuple[int, str, str]]] = {}
        for idx, rel, abspath, h in hashes:
            unique_hashes.setdefault(h, []).append((idx, rel, abspath))

        if len(unique_hashes) == 1:
            # All identical — copy once
            _, rel, abspath = entries[0]
            dst = out / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(abspath, str(dst))
            used_output_keys.add(key)
            continue

        # Different content — first unique hash keeps original name, rest get suffix
        first = True
        for h, group in unique_hashes.items():
            if first:
                # First group keeps original name
                _, rel, abspath = group[0]
                dst = out / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(abspath, str(dst))
                used_output_keys.add(key)
                first = False
                continue

            # Subsequent groups get suffix
            for idx, rel, abspath in group:
                rel_p = Path(rel)
                label = source_labels[idx]
                new_stem = f"{rel_p.stem}_{label}"
                new_rel = str(rel_p.with_stem(new_stem)).replace("\\", "/")
                new_key = str(Path(new_rel).with_suffix("")).replace("\\", "/")

                # Ensure uniqueness
                candidate = new_key
                counter = 2
                while candidate in used_output_keys:
                    candidate = f"{new_key}_{counter}"
                    new_rel = str(Path(rel).with_stem(f"{new_stem}_{counter}")).replace("\\", "/")
                    counter += 1
                new_key = candidate
                new_rel_final = new_rel if counter == 2 else new_rel

                dst = out / new_rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(abspath, str(dst))
                used_output_keys.add(new_key)
                image_rename_maps[idx][key] = new_key
                warnings.append(
                    tr("merge.warn.image_renamed", old=key, new=new_key,
                       source=source_labels[idx])
                )

    return image_rename_maps, warnings


# ==========================================================================
# Rename helpers for animation data
# ==========================================================================

def _rename_slots_in_animation(anim_data: dict, renames: dict[str, str]):
    """Rename slot references inside a single animation."""
    if "slots" in anim_data:
        anim_data["slots"] = {
            renames.get(k, k): v for k, v in anim_data["slots"].items()
        }
    if "drawOrder" in anim_data:
        for entry in anim_data["drawOrder"]:
            for offset in entry.get("offsets", []):
                old = offset.get("slot")
                if old in renames:
                    offset["slot"] = renames[old]
    if "deform" in anim_data:
        new_deform = {}
        for skin_name, slots in anim_data["deform"].items():
            new_deform[skin_name] = {
                renames.get(k, k): v for k, v in slots.items()
            }
        anim_data["deform"] = new_deform


def _rename_events_in_animation(anim_data: dict, renames: dict[str, str]):
    """Rename event references inside a single animation."""
    if "events" in anim_data:
        for ev in anim_data["events"]:
            old = ev.get("name")
            if old in renames:
                ev["name"] = renames[old]


def _apply_image_renames_to_skins(skins: dict, image_renames: dict[str, str]):
    """Update image path references in skin attachment data."""
    for skin_name, skin_data in skins.items():
        for slot_name, attachments in skin_data.items():
            for att_name, att_data in attachments.items():
                att_type = att_data.get("type")
                if att_type not in _IMAGE_ATT_TYPES:
                    continue
                img_key = attachment_image_key(att_name, att_data)
                if img_key in image_renames:
                    new_key = image_renames[img_key]
                    if new_key == att_name:
                        att_data.pop("path", None)
                    else:
                        att_data["path"] = new_key


# ==========================================================================
# Core merge
# ==========================================================================

def _unique_name(base: str, existing: set[str]) -> str:
    """Return *base* if not in *existing*, otherwise append _2, _3, etc."""
    if base not in existing:
        return base
    counter = 2
    while f"{base}_{counter}" in existing:
        counter += 1
    return f"{base}_{counter}"


def merge_spine_data(
    sources: list[dict],
    source_labels: list[str],
    output_name: str,
    image_rename_maps: list[dict[str, str]] | None = None,
) -> tuple[dict, list[str]]:
    """Merge multiple Spine JSON dicts into one.

    Returns (merged_data, warnings).
    """
    if not sources:
        return {}, []

    warnings: list[str] = []
    base_version = sources[0].get("skeleton", {}).get("spine", "")

    # --- Skeleton metadata ---
    merged: dict = {
        "skeleton": {
            "spine": base_version,
            "images": "./images/",
        },
    }

    # --- Bones ---
    bone_set: set[str] = set()
    bone_parents: dict[str, str] = {}  # name -> parent
    merged_bones: list[dict] = []

    # --- Slots ---
    slot_set: set[str] = set()
    merged_slots: list[dict] = []

    # --- Skins (normalized dict format) ---
    merged_skins: dict[str, dict] = {}

    # --- Events ---
    event_set: set[str] = set()
    merged_events: dict = {}

    # --- Animations ---
    anim_set: set[str] = set()
    merged_anims: dict = {}

    # --- Constraints ---
    ik_set: set[str] = set()
    merged_ik: list[dict] = []
    transform_set: set[str] = set()
    merged_transform: list[dict] = []
    path_set: set[str] = set()
    merged_path: list[dict] = []

    # Track what was in the first skeleton for the list format check
    first_was_skins_list = is_skins_list(sources[0])

    for src_idx, (src, label) in enumerate(zip(sources, source_labels)):
        slot_renames: dict[str, str] = {}
        event_renames: dict[str, str] = {}
        img_renames = (image_rename_maps[src_idx]
                       if image_rename_maps else {})

        # ── Bones ──
        for bone in src.get("bones", []):
            name = bone["name"]
            parent = bone.get("parent", "")
            if name in bone_set:
                # Check parent consistency
                if parent != bone_parents.get(name, ""):
                    warnings.append(
                        tr("merge.warn.bone_parent_mismatch",
                           bone=name, source=label,
                           expected=bone_parents.get(name, "(root)"),
                           got=parent or "(root)"))
                continue
            bone_set.add(name)
            bone_parents[name] = parent
            merged_bones.append(deepcopy(bone))

        # ── Slots ──
        for slot in src.get("slots", []):
            name = slot["name"]
            new_slot = deepcopy(slot)
            if name in slot_set:
                new_name = _unique_name(f"{name}_{label}", slot_set)
                slot_renames[name] = new_name
                new_slot["name"] = new_name
                warnings.append(
                    tr("merge.warn.slot_renamed",
                       old=name, new=new_name, source=label))
            slot_set.add(new_slot["name"])
            merged_slots.append(new_slot)

        # ── Skins ──
        src_skins = normalize_skins(src.get("skins", {}))

        # Apply image renames to source skins
        if img_renames:
            _apply_image_renames_to_skins(src_skins, img_renames)

        for skin_name, skin_data in src_skins.items():
            if skin_name not in merged_skins:
                merged_skins[skin_name] = {}
            dest_skin = merged_skins[skin_name]

            for old_slot, attachments in skin_data.items():
                slot = slot_renames.get(old_slot, old_slot)
                if slot not in dest_skin:
                    dest_skin[slot] = {}
                dest_skin[slot].update(deepcopy(attachments))

        # ── Events ──
        for event_name, event_data in src.get("events", {}).items():
            if event_name in event_set:
                new_name = _unique_name(f"{event_name}_{label}", event_set)
                event_renames[event_name] = new_name
                warnings.append(
                    tr("merge.warn.event_renamed",
                       old=event_name, new=new_name, source=label))
                event_name = new_name
            event_set.add(event_name)
            merged_events[event_name] = deepcopy(event_data)

        # ── Animations ──
        for anim_name, anim_data in src.get("animations", {}).items():
            new_data = deepcopy(anim_data)

            if slot_renames:
                _rename_slots_in_animation(new_data, slot_renames)
            if event_renames:
                _rename_events_in_animation(new_data, event_renames)

            final_name = anim_name
            if anim_name in anim_set:
                final_name = _unique_name(f"{anim_name}_{label}", anim_set)
                warnings.append(
                    tr("merge.warn.anim_renamed",
                       old=anim_name, new=final_name, source=label))
            anim_set.add(final_name)
            merged_anims[final_name] = new_data

        # ── IK constraints ──
        for c in src.get("ik", []):
            new_c = deepcopy(c)
            name = c["name"]
            if name in ik_set:
                new_name = _unique_name(f"{name}_{label}", ik_set)
                new_c["name"] = new_name
                name = new_name
            ik_set.add(name)
            merged_ik.append(new_c)

        # ── Transform constraints ──
        for c in src.get("transform", []):
            new_c = deepcopy(c)
            name = c["name"]
            if name in transform_set:
                new_name = _unique_name(f"{name}_{label}", transform_set)
                new_c["name"] = new_name
                name = new_name
            transform_set.add(name)
            merged_transform.append(new_c)

        # ── Path constraints ──
        for c in src.get("path", []):
            new_c = deepcopy(c)
            name = c["name"]
            # Rename target slot if needed
            if new_c.get("target") in slot_renames:
                new_c["target"] = slot_renames[new_c["target"]]
            if name in path_set:
                new_name = _unique_name(f"{name}_{label}", path_set)
                new_c["name"] = new_name
                name = new_name
            path_set.add(name)
            merged_path.append(new_c)

    # --- Assemble output ---
    merged["bones"] = merged_bones
    merged["slots"] = merged_slots

    if first_was_skins_list:
        merged["skins"] = denormalize_skins(merged_skins)
    else:
        merged["skins"] = merged_skins

    if merged_events:
        merged["events"] = merged_events
    if merged_ik:
        merged["ik"] = merged_ik
    if merged_transform:
        merged["transform"] = merged_transform
    if merged_path:
        merged["path"] = merged_path

    merged["animations"] = merged_anims

    return merged, warnings


# ==========================================================================
# UI Tab
# ==========================================================================

class MergeSkeletonsTab:
    def __init__(self, tabs: QTabWidget, get_config):
        self._get_config = get_config
        self._tabs = tabs
        self._files: list[dict] = []  # {path, type, version, images_dir, item}

        self._page = QWidget()
        tabs.addTab(self._page, tr("merge.tab"))
        layout = QVBoxLayout(self._page)
        layout.setContentsMargins(5, 5, 5, 5)

        # Info
        self._info = QLabel(tr("merge.info"))
        self._info.setWordWrap(True)
        layout.addWidget(self._info)

        # Toolbar
        toolbar = QHBoxLayout()
        self._btn_add = QPushButton(tr("merge.add_files"))
        self._btn_add.setCursor(Qt.PointingHandCursor)
        self._btn_add.clicked.connect(self._add_files)
        toolbar.addWidget(self._btn_add)
        self._btn_remove = QPushButton(tr("merge.remove_selected"))
        self._btn_remove.setCursor(Qt.PointingHandCursor)
        self._btn_remove.clicked.connect(self._remove_selected)
        toolbar.addWidget(self._btn_remove)
        self._btn_clear = QPushButton(tr("merge.clear_all"))
        self._btn_clear.setCursor(Qt.PointingHandCursor)
        self._btn_clear.clicked.connect(self._clear_all)
        toolbar.addWidget(self._btn_clear)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        # File list
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels([
            tr("merge.col.filename"),
            tr("merge.col.type"),
            tr("merge.col.version"),
            tr("merge.col.images"),
        ])
        self._tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._tree.setRootIsDecorated(False)
        self._tree.setAlternatingRowColors(True)
        header = self._tree.header()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        layout.addWidget(self._tree, 1)

        # Config row: skeleton name + output dir
        config_row = QHBoxLayout()
        self._name_label = QLabel(tr("merge.name_label"))
        config_row.addWidget(self._name_label)
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText(tr("merge.name_placeholder"))
        self._name_edit.setMaximumWidth(250)
        config_row.addWidget(self._name_edit)
        self._output_label = QLabel(tr("merge.output_label"))
        config_row.addWidget(self._output_label)
        self._output_edit = QLineEdit()
        self._output_edit.setPlaceholderText(tr("merge.output_placeholder"))
        config_row.addWidget(self._output_edit, 1)
        self._btn_output = QPushButton(tr("app.browse"))
        self._btn_output.clicked.connect(self._browse_output)
        config_row.addWidget(self._btn_output)
        layout.addLayout(config_row)

        # Merge button + stats
        bottom = QHBoxLayout()
        self._merge_btn = QPushButton(tr("merge.merge_btn"))
        self._merge_btn.setProperty("role", "primary")
        self._merge_btn.setCursor(Qt.PointingHandCursor)
        self._merge_btn.clicked.connect(self._merge)
        bottom.addWidget(self._merge_btn)
        bottom.addStretch()
        layout.addLayout(bottom)

        self._stats = QLabel(tr("merge.default_stats"))
        self._stats.setStyleSheet("font-weight: bold; color: #6ec072;")
        layout.addWidget(self._stats)

        # Log
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setFont(QFont("monospace", 10))
        self._log.setMaximumHeight(180)
        layout.addWidget(self._log)

        language_changed.connect(self._retranslate)

    # ── i18n ──

    def _retranslate(self):
        idx = self._tabs.indexOf(self._page)
        if idx >= 0:
            self._tabs.setTabText(idx, tr("merge.tab"))
        self._info.setText(tr("merge.info"))
        self._btn_add.setText(tr("merge.add_files"))
        self._btn_remove.setText(tr("merge.remove_selected"))
        self._btn_clear.setText(tr("merge.clear_all"))
        self._tree.setHeaderLabels([
            tr("merge.col.filename"),
            tr("merge.col.type"),
            tr("merge.col.version"),
            tr("merge.col.images"),
        ])
        self._name_label.setText(tr("merge.name_label"))
        self._name_edit.setPlaceholderText(tr("merge.name_placeholder"))
        self._output_label.setText(tr("merge.output_label"))
        self._output_edit.setPlaceholderText(tr("merge.output_placeholder"))
        self._btn_output.setText(tr("app.browse"))
        self._merge_btn.setText(tr("merge.merge_btn"))
        self._stats.setText(tr("merge.default_stats"))

    # ── Log helpers ──

    def _append(self, text: str, color: str = "#cdd6f4", bold: bool = False):
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        if bold:
            fmt.setFontWeight(QFont.Bold)
        cursor = self._log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text + "\n", fmt)
        self._log.setTextCursor(cursor)
        self._log.ensureCursorVisible()

    def _clear_log(self):
        self._log.clear()

    # ── File management ──

    def _add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            None, tr("merge.dialog.add_files"), "",
            tr("merge.filter"),
        )
        for path in paths:
            # Skip duplicates
            if any(f["path"] == path for f in self._files):
                continue
            self._add_file(path)

    def _add_file(self, path: str):
        p = Path(path)
        ftype = p.suffix.lstrip(".")
        version = ""
        images_dir = ""

        if ftype == "spine":
            version = read_spine_file_version(path) or "?"
        elif ftype == "json":
            try:
                data = load_spine_json(path)
                version = data.get("skeleton", {}).get("spine", "?")
            except Exception:
                version = "?"

        # Auto-detect images dir
        candidate = p.parent / "images"
        if candidate.is_dir():
            images_dir = str(candidate)

        item = QTreeWidgetItem(self._tree, [
            p.name, ftype, version,
            Path(images_dir).name if images_dir else "—",
        ])
        self._files.append({
            "path": path,
            "type": ftype,
            "version": version,
            "images_dir": images_dir,
            "label": p.stem,
            "item": item,
        })

    def _remove_selected(self):
        selected = self._tree.selectedItems()
        for sel in selected:
            for i, f in enumerate(self._files):
                if f["item"] is sel:
                    self._files.pop(i)
                    break
            idx = self._tree.indexOfTopLevelItem(sel)
            if idx >= 0:
                self._tree.takeTopLevelItem(idx)

    def _clear_all(self):
        self._files.clear()
        self._tree.clear()

    def _browse_output(self):
        path = QFileDialog.getExistingDirectory(None, tr("merge.dialog.select_output"))
        if path:
            self._output_edit.setText(path)

    # ── Merge ──

    def _merge(self):
        self._clear_log()

        if len(self._files) < 2:
            QMessageBox.warning(None, tr("err.title"), tr("merge.err.need_files"))
            return

        output_name = self._name_edit.text().strip()
        if not output_name:
            QMessageBox.warning(None, tr("err.title"), tr("merge.err.no_name"))
            return

        output_dir = self._output_edit.text().strip()
        if not output_dir:
            QMessageBox.warning(None, tr("err.title"), tr("merge.err.no_output"))
            return

        # Validate versions match
        versions = set()
        for f in self._files:
            v = f["version"]
            if v and v != "?":
                versions.add(v)
        if len(versions) > 1:
            QMessageBox.warning(
                None, tr("err.title"),
                tr("merge.err.version_mismatch",
                   versions=", ".join(sorted(versions))))
            return

        exe = settings.spine_executable() or ""

        # Step 1: Load all JSONs (export .spine files first)
        self._append(tr("merge.log.start"), bold=True)
        json_datas: list[dict] = []
        labels: list[str] = []
        images_dirs: list[str] = []
        temp_dirs: list[str] = []

        for f in self._files:
            label = f["label"]
            labels.append(label)

            if f["type"] == "spine":
                if not exe:
                    QMessageBox.warning(
                        None, tr("err.title"), tr("merge.err.no_exe"))
                    self._cleanup_temps(temp_dirs)
                    return

                self._append(tr("merge.log.exporting", name=Path(f["path"]).name))
                QApplication.processEvents()

                temp_dir = str(Path(output_dir) / f"_ssk_merge_temp_{label}")
                temp_dirs.append(temp_dir)

                result = export_spine_project(
                    exe, f["path"], temp_dir, pack=False,
                )
                if not result.success:
                    error = result.stderr or result.stdout or "Unknown error"
                    self._append(f"ERROR: {error}", color="#f38ba8")
                    QMessageBox.warning(
                        None, tr("err.title"),
                        tr("merge.err.export_failed",
                           name=Path(f["path"]).name, error=error))
                    self._cleanup_temps(temp_dirs)
                    return

                try:
                    data = load_spine_json(result.output_json)
                except Exception as e:
                    self._append(f"ERROR: {e}", color="#f38ba8")
                    self._cleanup_temps(temp_dirs)
                    return
                json_datas.append(data)

                # Use source .spine file's images dir (not the export)
                images_dirs.append(f["images_dir"])
            else:
                self._append(tr("merge.log.loading", name=Path(f["path"]).name))
                QApplication.processEvents()
                try:
                    data = load_spine_json(f["path"])
                except Exception as e:
                    self._append(f"ERROR: {e}", color="#f38ba8")
                    QMessageBox.warning(
                        None, tr("err.title"),
                        tr("merge.err.load_failed",
                           name=Path(f["path"]).name, error=str(e)))
                    self._cleanup_temps(temp_dirs)
                    return
                json_datas.append(data)
                images_dirs.append(f["images_dir"])

        # Step 2: Merge images
        output_images = str(Path(output_dir) / "images")
        self._append(tr("merge.log.merging_images"))
        QApplication.processEvents()

        image_rename_maps, img_warnings = merge_image_dirs(
            images_dirs, labels, output_images,
        )
        for w in img_warnings:
            self._append(f"  {w}", color="#f9e2af")

        # Step 3: Merge JSON
        self._append(tr("merge.log.merging_json"))
        QApplication.processEvents()

        merged, merge_warnings = merge_spine_data(
            json_datas, labels, output_name, image_rename_maps,
        )
        for w in merge_warnings:
            self._append(f"  {w}", color="#f9e2af")

        # Step 4: Save
        output_json = str(Path(output_dir) / f"{output_name}.json")
        try:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            save_spine_json(output_json, merged)
        except Exception as e:
            self._append(f"ERROR: {e}", color="#f38ba8")
            QMessageBox.warning(None, tr("err.title"), str(e))
            self._cleanup_temps(temp_dirs)
            return

        # Cleanup temp dirs
        self._cleanup_temps(temp_dirs)

        total_warnings = len(img_warnings) + len(merge_warnings)
        self._append(
            tr("merge.log.done",
               output=output_json,
               warnings=total_warnings),
            color="#a6e3a1", bold=True)

        self._stats.setText(
            tr("merge.stats.done",
               sources=len(self._files),
               bones=len(merged.get("bones", [])),
               slots=len(merged.get("slots", [])),
               anims=len(merged.get("animations", {})),
               warnings=total_warnings))

        QMessageBox.information(
            None, tr("done.title"),
            tr("merge.done",
               output=output_json,
               warnings=total_warnings))

    def _cleanup_temps(self, temp_dirs: list[str]):
        for d in temp_dirs:
            shutil.rmtree(d, ignore_errors=True)
