"""Image downscaler planning / processing (pure logic, no Qt)."""

from __future__ import annotations

import os
import shutil

from .spine_json import (
    attachment_image_key, create_backup, image_key_from_path, iter_image_attachments,
    load_spine_json, normalize_skins, save_spine_json,
)

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tga", ".webp"}
ORIGINALS_DIR_NAME = "_ORIGINALS"


def resize_image(src_path: str, dst_path: str, scale_percent: int):
    if Image is None:
        raise RuntimeError("Pillow (PIL) is not installed. Run: pip3 install Pillow")
    with Image.open(src_path) as img:
        factor = scale_percent / 100.0
        new_w = max(1, round(img.width * factor))
        new_h = max(1, round(img.height * factor))
        if img.mode in ("P", "PA", "1"):
            img = img.convert("RGBA")  # Pillow would silently use NEAREST for these
        resized = img.resize((new_w, new_h), Image.LANCZOS)
        ext = os.path.splitext(dst_path)[1].lower()
        if ext in (".jpg", ".jpeg") and resized.mode not in ("RGB", "L"):
            resized = resized.convert("RGB")
        dst_dir = os.path.dirname(dst_path)
        if dst_dir:
            os.makedirs(dst_dir, exist_ok=True)
        resized.save(dst_path)
    return new_w, new_h


def scan_downscale_folder(downscale_dir: str) -> dict:
    result = {}
    if not os.path.isdir(downscale_dir):
        return result
    for entry in sorted(os.listdir(downscale_dir)):
        folder_path = os.path.join(downscale_dir, entry)
        if not os.path.isdir(folder_path):
            continue
        try:
            percent = int(entry)
        except ValueError:
            continue
        if percent <= 0 or percent >= 100:
            continue
        images = []
        for root, _dirs, files in os.walk(folder_path):
            for fname in files:
                if os.path.splitext(fname)[1].lower() in IMAGE_EXTS:
                    images.append(os.path.join(root, fname))
        if images:
            result[percent] = sorted(images)
    return result


def build_attachment_lookup(spine_data: dict) -> dict:
    """image key -> [(skin, slot, att_name, att_data)] for region / mesh / linkedmesh."""
    lookup: dict = {}
    skins = normalize_skins(spine_data.get("skins", {}))
    for skin_name, slot_name, att_name, att_data in iter_image_attachments(skins):
        key = attachment_image_key(att_name, att_data)
        lookup.setdefault(key, []).append((skin_name, slot_name, att_name, att_data))
    return lookup


def match_attachments(lookup: dict, image_key: str) -> list:
    matches = lookup.get(image_key, [])
    if not matches:
        low = image_key.lower()
        for k, v in lookup.items():
            if k.lower() == low:
                return v
    return matches


def is_region(att_data: dict) -> bool:
    return att_data.get("type") in (None, "region")


def apply_downscale_to_attachment(att_data: dict, inv_factor: float, new_w: int, new_h: int) -> bool:
    """Region: scale *= inv, width/height set. Mesh/linkedmesh: width/height only if present."""
    if is_region(att_data):
        att_data["scaleX"] = round(att_data.get("scaleX", 1) * inv_factor, 6)
        att_data["scaleY"] = round(att_data.get("scaleY", 1) * inv_factor, 6)
        att_data["width"] = new_w
        att_data["height"] = new_h
        return True
    changed = False
    if "width" in att_data:
        att_data["width"] = new_w
        changed = True
    if "height" in att_data:
        att_data["height"] = new_h
        changed = True
    return changed


def build_plan(downscale_dir: str, images_dir: str, spine_data: dict) -> list:
    """Plan items for every image under _DOWNSCALE/<percent>/."""
    lookup = build_attachment_lookup(spine_data)
    plan = []
    for percent, file_list in sorted(scan_downscale_folder(downscale_dir).items()):
        inv_factor = 100.0 / percent
        pct_dir = os.path.join(downscale_dir, str(percent))
        for src_path in file_list:
            rel = os.path.relpath(src_path, pct_dir)
            image_key = image_key_from_path(rel)
            orig_w = orig_h = None
            if Image is not None:
                try:
                    with Image.open(src_path) as img:
                        orig_w, orig_h = img.width, img.height
                except Exception:
                    pass
            plan.append({
                "src_path": src_path,
                "dst_path": os.path.join(images_dir, rel),
                "rel_path": rel,
                "image_key": image_key,
                "percent": percent,
                "inv_factor": inv_factor,
                "matches": match_attachments(lookup, image_key),
                "orig_w": orig_w, "orig_h": orig_h,
                "new_w": max(1, round(orig_w * percent / 100)) if orig_w else None,
                "new_h": max(1, round(orig_h * percent / 100)) if orig_h else None,
            })
    return plan


def unique_destination(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists("%s_%d%s" % (base, n, ext)):
        n += 1
    return "%s_%d%s" % (base, n, ext)


def move_to_originals(src_path: str, rel_path: str, images_dir: str) -> str:
    """Move *src_path* to <images>/_ORIGINALS/<rel_path> (never overwrite); returns dest."""
    dest = unique_destination(os.path.join(images_dir, ORIGINALS_DIR_NAME, rel_path))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.move(src_path, dest)
    return dest


def process_plan(json_path: str, images_dir: str, plan: list) -> dict:
    """Resize -> update JSON -> backup + save -> only then move sources to _ORIGINALS.

    Returns dict(resized, json_updated, moved, errors, json_saved, save_error).
    If saving the JSON fails the sources stay where they are.
    """
    result = {"resized": 0, "json_updated": 0, "moved": 0, "errors": [],
              "json_saved": False, "save_error": None}
    spine_data = load_spine_json(json_path)
    lookup = build_attachment_lookup(spine_data)

    done = []
    for item in plan:
        try:
            new_w, new_h = resize_image(item["src_path"], item["dst_path"], item["percent"])
        except Exception as e:
            result["errors"].append("Resize %s: %s" % (item["image_key"], e))
            continue
        result["resized"] += 1
        done.append(item)
        for _s, _sl, _n, att in match_attachments(lookup, item["image_key"]):
            if apply_downscale_to_attachment(att, item["inv_factor"], new_w, new_h):
                result["json_updated"] += 1

    try:
        create_backup(json_path)
        save_spine_json(json_path, spine_data)
    except Exception as e:
        result["save_error"] = str(e)
        return result
    result["json_saved"] = True

    for item in done:
        try:
            move_to_originals(item["src_path"], item["rel_path"], images_dir)
            result["moved"] += 1
        except OSError as e:
            result["errors"].append("Move %s: %s" % (item["image_key"], e))
    return result
