"""Image Flattener core — pure logic (no Qt, no i18n)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .spine_json import (
    IMAGE_ATTACHMENT_TYPES, IMAGE_EXTENSIONS, attachment_image_key,
    create_backup, image_key_from_path, iter_all_attachments,
    load_spine_json, save_spine_json,
)

_IMAGE_ATT_TYPES = set(IMAGE_ATTACHMENT_TYPES)


def scan_subfolder_images(images_dir: str) -> list:
    """Images located in subdirectories of *images_dir* (directories starting
    with "_" are skipped).

    Items: {"rel_path" ("/"-separated), "abs_path", "stem", "folder", "ext"}.
    """
    root = Path(images_dir)
    results = []
    for dirpath, dirnames, filenames in os.walk(images_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith("_")]
        rel_dir = Path(dirpath).relative_to(root)
        if rel_dir == Path("."):
            continue
        for fname in sorted(filenames):
            fp = Path(dirpath) / fname
            if fp.suffix.lower() in IMAGE_EXTENSIONS:
                results.append({
                    "rel_path": str(fp.relative_to(root)).replace("\\", "/"),
                    "abs_path": str(fp),
                    "stem": fp.stem,
                    "folder": rel_dir.parts[0],
                    "ext": fp.suffix,
                })
    return results


def detect_conflicts(images_dir: str, subfolder_files: list) -> dict:
    """stem -> list of rel_paths sharing that stem (only stems seen more than once)."""
    root = Path(images_dir)
    stem_sources: dict = {}
    for f in sorted(root.iterdir()):
        if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS:
            stem_sources.setdefault(f.stem, []).append(f.name)
    for info in subfolder_files:
        stem_sources.setdefault(info["stem"], []).append(info["rel_path"])
    return {stem: src for stem, src in stem_sources.items() if len(src) > 1}


def build_flatten_plan(images_dir: str, subfolder_files: list, conflicts: dict,
                       add_suffix: bool) -> list:
    """Plan items: {"rel_path", "abs_path", "old_key", "new_key", "dest_path",
    "conflict", "skipped"}."""
    root = Path(images_dir)
    used_stems: set = set()
    for f in root.iterdir():
        if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS:
            used_stems.add(f.stem)

    plan = []
    for info in subfolder_files:
        stem = info["stem"]
        ext = info["ext"]
        old_key = image_key_from_path(info["rel_path"])
        is_conflict = stem in conflicts

        if is_conflict and stem in used_stems:
            if add_suffix:
                new_stem = "%s_%s" % (stem, info["folder"])
                counter = 2
                candidate = new_stem
                while candidate in used_stems:
                    candidate = "%s_%d" % (new_stem, counter)
                    counter += 1
                new_stem = candidate
            else:
                plan.append({
                    "rel_path": info["rel_path"], "abs_path": info["abs_path"],
                    "old_key": old_key, "new_key": old_key,
                    "dest_path": info["abs_path"],
                    "conflict": True, "skipped": True,
                })
                continue
        else:
            new_stem = stem

        used_stems.add(new_stem)
        plan.append({
            "rel_path": info["rel_path"], "abs_path": info["abs_path"],
            "old_key": old_key, "new_key": new_stem,
            "dest_path": str(root / ("%s%s" % (new_stem, ext))),
            "conflict": is_conflict, "skipped": False,
        })
    return plan


def flatten_images(images_dir: str, json_path: str, plan: list) -> dict:
    """Execute a flatten plan safely.

    Order: pre-check every destination (nothing may be overwritten; blocked
    items are dropped from the rename map BEFORE the JSON is touched), back up
    and save the JSON, then move the files.

    Returns {"moved", "json_updated", "blocked": [msg], "failed": [msg],
    "warnings": [msg] (blocked + failed), "backup": path or None,
    "needs_repack": bool}.
    """
    blocked: list = []
    failed: list = []
    seen_dest: set = set()
    movable: list = []
    for item in plan:
        if item.get("skipped"):
            continue
        src = item["abs_path"]
        dest = item["dest_path"]
        if not os.path.isfile(src):
            blocked.append("Source missing, skipping: %s" % item["rel_path"])
            continue
        norm = os.path.normcase(os.path.abspath(dest))
        if os.path.exists(dest) or norm in seen_dest:
            blocked.append("Destination exists, skipping: %s" % os.path.basename(dest))
            continue
        seen_dest.add(norm)
        movable.append(item)

    rename_map = {it["old_key"]: it["new_key"] for it in movable}

    json_updated = 0
    backup = None
    if rename_map:
        spine_data = load_spine_json(json_path)
        for _skin, _slot, att_name, att in iter_all_attachments(spine_data):
            if att.get("type") not in _IMAGE_ATT_TYPES:
                continue
            img_key = attachment_image_key(att_name, att)
            if img_key in rename_map:
                new_key = rename_map[img_key]
                if new_key == att_name:
                    att.pop("path", None)
                else:
                    att["path"] = new_key
                json_updated += 1
        if json_updated:
            backup = create_backup(json_path)
            save_spine_json(json_path, spine_data)

    moved = 0
    for item in movable:
        try:
            shutil.move(item["abs_path"], item["dest_path"])
            moved += 1
        except Exception as e:  # noqa: BLE001 - reported to the user
            failed.append("Failed to move %s: %s" % (item["rel_path"], e))

    for dirpath, dirnames, _files in os.walk(images_dir, topdown=False):
        dirnames[:] = [d for d in dirnames if not d.startswith("_")]
        if dirpath != images_dir:
            try:
                if not os.listdir(dirpath):
                    os.rmdir(dirpath)
            except OSError:
                pass

    return {
        "moved": moved, "json_updated": json_updated, "blocked": blocked,
        "failed": failed, "warnings": failed + blocked, "backup": backup,
        "needs_repack": moved > 0 or json_updated > 0,
    }


def execute_flatten(images_dir: str, json_path: str, plan: list) -> tuple:
    """Compatibility wrapper: (moved_count, json_updated_count, warnings)."""
    r = flatten_images(images_dir, json_path, plan)
    return r["moved"], r["json_updated"], r["warnings"]
