"""Spine atlas parsing and image file collection."""

from __future__ import annotations

import os

from .spine_json import IMAGE_EXTENSIONS, image_key_from_path


def _ints(value: str) -> list:
    out = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(float(part)))
        except ValueError:
            out.append(0)
    return out


def _new_region(name: str) -> dict:
    return {"name": name, "x": 0, "y": 0, "w": 0, "h": 0, "rotate": False,
            "degrees": 0, "orig_w": None, "orig_h": None,
            "offset_x": 0, "offset_y": 0, "index": -1}


def _finish_region(region: dict) -> dict:
    if region["orig_w"] is None:
        region["orig_w"] = region["w"]
    if region["orig_h"] is None:
        region["orig_h"] = region["h"]
    return region


def _apply_region_field(region: dict, key: str, value: str) -> None:
    v = _ints(value) if key in ("xy", "size", "bounds", "orig", "offset",
                                "offsets", "index") else None
    if key == "xy" and len(v) >= 2:
        region["x"], region["y"] = v[0], v[1]
    elif key == "size" and len(v) >= 2:
        region["w"], region["h"] = v[0], v[1]
    elif key == "bounds" and len(v) >= 4:
        region["x"], region["y"], region["w"], region["h"] = v[:4]
    elif key == "orig" and len(v) >= 2:
        region["orig_w"], region["orig_h"] = v[0], v[1]
    elif key == "offset" and len(v) >= 2:
        region["offset_x"], region["offset_y"] = v[0], v[1]
    elif key == "offsets" and len(v) >= 4:
        region["offset_x"], region["offset_y"] = v[0], v[1]
        region["orig_w"], region["orig_h"] = v[2], v[3]
    elif key == "index" and v:
        region["index"] = v[0]
    elif key == "rotate":
        val = value.strip().lower()
        if val == "true":
            region["rotate"], region["degrees"] = True, 90
        elif val == "false" or val == "":
            region["rotate"], region["degrees"] = False, 0
        else:
            try:
                deg = int(float(val))
            except ValueError:
                deg = 0
            region["degrees"] = deg
            region["rotate"] = deg != 0


def parse_atlas_pages(atlas_path: str) -> list:
    """Parse a Spine atlas (3.x and 4.x) into a list of page dicts.

    Page: {"name", "size": (w, h) | None, "scale", "pma", "regions": [...]}.
    Region: {"name", "x", "y", "w", "h" (unrotated), "rotate", "degrees",
    "orig_w", "orig_h", "offset_x", "offset_y", "index"}.
    """
    with open(atlas_path, "r", encoding="utf-8-sig") as f:
        text = f.read()
    lines = text.splitlines()

    pages: list = []
    page = None
    region = None
    new_page = True  # next non-blank line is a page name

    for raw in lines:
        line = raw.strip()
        if not line:
            if region is not None:
                _finish_region(region)
            page = None
            region = None
            new_page = True
            continue

        if new_page:
            page = {"name": line, "size": None, "scale": 1.0, "pma": False,
                    "regions": []}
            pages.append(page)
            region = None
            new_page = False
            continue

        if page is None:
            continue

        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if region is None:
                if key == "size":
                    v = _ints(value)
                    if len(v) >= 2:
                        page["size"] = (v[0], v[1])
                elif key == "scale":
                    try:
                        page["scale"] = float(value)
                    except ValueError:
                        pass
                elif key == "pma":
                    page["pma"] = value.lower() == "true"
            else:
                _apply_region_field(region, key, value)
        else:
            if region is not None:
                _finish_region(region)
            region = _new_region(line)
            page["regions"].append(region)

    if region is not None:
        _finish_region(region)
    return pages


def parse_atlas(atlas_path: str) -> set:
    """Return the set of region names of a Spine atlas file."""
    names = set()
    for page in parse_atlas_pages(atlas_path):
        for region in page["regions"]:
            names.add(region["name"])
    return names


def collect_images(images_dir: str) -> dict:
    """Map image key (relative path, "/" separators, no extension) -> full path.

    Skips every directory whose name starts with "_" (_UNUSED, _DOWNSCALE, _ssk_backups...).
    """
    images = {}
    for root, dirs, files in os.walk(images_dir):
        dirs[:] = [d for d in dirs if not d.startswith("_")]
        for fname in files:
            if os.path.splitext(fname)[1].lower() in IMAGE_EXTENSIONS:
                full_path = os.path.join(root, fname)
                rel = os.path.relpath(full_path, images_dir)
                images[image_key_from_path(rel)] = full_path
    return images
