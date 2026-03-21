"""Image Flattener — move images from subdirectories to root images folder and update JSON paths."""

import os
import shutil
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QHeaderView, QMessageBox,
)

from .i18n import tr, language_changed
from .spine_json import (
    load_spine_json, save_spine_json, normalize_skins, denormalize_skins,
    is_skins_list, attachment_image_key,
)


# ── Image types that reference image files ──
_IMAGE_ATT_TYPES = {None, "region", "mesh", "linkedmesh"}


# ==========================================================================
# Analysis logic
# ==========================================================================

def scan_subfolder_images(images_dir: str) -> list[dict]:
    """Scan images directory for files in subdirectories.

    Returns list of dicts:
        {"rel_path": "folder/file.png", "abs_path": "/full/path", "stem": "file",
         "folder": "folder", "ext": ".png"}
    """
    image_exts = {".png", ".jpg", ".jpeg", ".bmp", ".tga", ".webp"}
    root = Path(images_dir)
    results = []
    for dirpath, dirnames, filenames in os.walk(images_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith("_")]
        rel_dir = Path(dirpath).relative_to(root)
        if rel_dir == Path("."):
            continue  # skip root-level files
        for fname in sorted(filenames):
            fp = Path(dirpath) / fname
            if fp.suffix.lower() in image_exts:
                results.append({
                    "rel_path": str(fp.relative_to(root)).replace("\\", "/"),
                    "abs_path": str(fp),
                    "stem": fp.stem,
                    "folder": rel_dir.parts[0],  # immediate subfolder under root
                    "ext": fp.suffix,
                })
    return results


def detect_conflicts(images_dir: str, subfolder_files: list[dict]) -> dict[str, list[str]]:
    """Detect naming conflicts between subfolder files and root, and between subfolders.

    Returns dict mapping stem -> list of rel_paths that share that stem.
    Only stems that appear more than once (conflict) are included.
    Root files that match are represented as just the filename.
    """
    image_exts = {".png", ".jpg", ".jpeg", ".bmp", ".tga", ".webp"}
    root = Path(images_dir)

    # Collect root-level stems
    stem_sources: dict[str, list[str]] = {}
    for f in sorted(root.iterdir()):
        if f.is_file() and f.suffix.lower() in image_exts:
            stem_sources.setdefault(f.stem, []).append(f.name)

    # Add subfolder files
    for info in subfolder_files:
        stem_sources.setdefault(info["stem"], []).append(info["rel_path"])

    # Filter to only conflicts (stem appears more than once)
    return {stem: sources for stem, sources in stem_sources.items() if len(sources) > 1}


def build_flatten_plan(
    images_dir: str,
    subfolder_files: list[dict],
    conflicts: dict[str, list[str]],
    add_suffix: bool,
) -> list[dict]:
    """Build a plan for flattening: list of moves with old/new keys.

    Each item: {"rel_path", "abs_path", "old_key", "new_key", "dest_path",
                "conflict": bool, "skipped": bool}
    """
    root = Path(images_dir)
    used_stems: set[str] = set()

    # Reserve root-level stems
    image_exts = {".png", ".jpg", ".jpeg", ".bmp", ".tga", ".webp"}
    for f in root.iterdir():
        if f.is_file() and f.suffix.lower() in image_exts:
            used_stems.add(f.stem)

    plan = []
    for info in subfolder_files:
        stem = info["stem"]
        ext = info["ext"]
        old_key = str(Path(info["rel_path"]).with_suffix("")).replace("\\", "/")
        is_conflict = stem in conflicts

        if is_conflict and stem in used_stems:
            if add_suffix:
                new_stem = f"{stem}_{info['folder']}"
                # Handle edge case: suffixed name also conflicts
                counter = 2
                candidate = new_stem
                while candidate in used_stems:
                    candidate = f"{new_stem}_{counter}"
                    counter += 1
                new_stem = candidate
            else:
                plan.append({
                    "rel_path": info["rel_path"],
                    "abs_path": info["abs_path"],
                    "old_key": old_key,
                    "new_key": old_key,
                    "dest_path": info["abs_path"],
                    "conflict": True,
                    "skipped": True,
                })
                continue
        else:
            new_stem = stem

        used_stems.add(new_stem)
        new_key = new_stem
        dest_path = str(root / f"{new_stem}{ext}")
        plan.append({
            "rel_path": info["rel_path"],
            "abs_path": info["abs_path"],
            "old_key": old_key,
            "new_key": new_key,
            "dest_path": dest_path,
            "conflict": is_conflict,
            "skipped": False,
        })

    return plan


def execute_flatten(
    images_dir: str,
    json_path: str,
    plan: list[dict],
) -> tuple[int, int, list[str]]:
    """Execute the flatten plan: update JSON and move files.

    Returns (moved_count, json_updated_count, warnings).
    """
    root = Path(images_dir)
    warnings: list[str] = []

    # Build rename map: old_key -> new_key (only for non-skipped items)
    rename_map = {}
    for item in plan:
        if not item["skipped"] and item["old_key"] != item["new_key"]:
            rename_map[item["old_key"]] = item["new_key"]
        elif not item["skipped"] and item["old_key"] == item["new_key"]:
            # Even if key doesn't change, the file is moving from subfolder to root
            # The key DOES change: "folder/name" -> "name"
            rename_map[item["old_key"]] = item["new_key"]

    # Update JSON
    spine_data = load_spine_json(json_path)
    skins_raw = spine_data.get("skins", {})
    was_list = is_skins_list(spine_data)
    skins = normalize_skins(skins_raw)

    json_updated = 0
    for skin_name, skin_data in skins.items():
        for slot_name, attachments in skin_data.items():
            for att_name, att_data in attachments.items():
                att_type = att_data.get("type")
                if att_type not in _IMAGE_ATT_TYPES:
                    continue
                img_key = attachment_image_key(att_name, att_data)
                if img_key in rename_map:
                    new_key = rename_map[img_key]
                    if new_key == att_name:
                        att_data.pop("path", None)
                    else:
                        att_data["path"] = new_key
                    json_updated += 1

    if was_list:
        spine_data["skins"] = denormalize_skins(skins)
    else:
        spine_data["skins"] = skins

    # Backup and save JSON
    shutil.copy2(json_path, json_path + ".backup")
    save_spine_json(json_path, spine_data)

    # Move files
    moved = 0
    for item in plan:
        if item["skipped"]:
            continue
        try:
            dest = Path(item["dest_path"])
            if dest.exists():
                warnings.append(f"Destination exists, skipping: {dest.name}")
                continue
            shutil.move(item["abs_path"], item["dest_path"])
            moved += 1
        except Exception as e:
            warnings.append(f"Failed to move {item['rel_path']}: {e}")

    # Clean up empty directories
    for dirpath, dirnames, filenames in os.walk(images_dir, topdown=False):
        dirnames[:] = [d for d in dirnames if not d.startswith("_")]
        if dirpath != images_dir:
            try:
                if not os.listdir(dirpath):
                    os.rmdir(dirpath)
            except OSError:
                pass

    return moved, json_updated, warnings


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
            moved, json_updated, warnings = execute_flatten(
                images_dir, json_path, self._plan,
            )
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), str(e))
            return

        msg = tr("flattener.done",
                 moved=moved,
                 json_updated=json_updated,
                 backup=json_path + ".backup")
        if warnings:
            msg += "\n\n" + tr("flattener.warnings_header") + "\n" + "\n".join(warnings)

        QMessageBox.information(None, tr("done.title"), msg)

        if self._on_modified:
            self._on_modified()
        else:
            self._analyze()
