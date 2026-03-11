"""Spine atlas parsing and image file collection."""

import os


def parse_atlas(atlas_path: str) -> set[str]:
    """Parse a Spine atlas file and return set of sprite names (without extension)."""
    sprites = set()
    with open(atlas_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    page_header_keys = {"size:", "format:", "filter:", "repeat:"}
    skip_next_as_page = False
    for line in lines:
        stripped = line.rstrip("\n\r").strip()
        raw = line.rstrip("\n\r")

        if stripped == "":
            skip_next_as_page = True
            continue

        if skip_next_as_page:
            skip_next_as_page = False
            continue

        if any(stripped.startswith(k) for k in page_header_keys):
            continue

        if raw.startswith(" ") or raw.startswith("\t"):
            continue

        sprites.add(stripped)

    return sprites


def collect_images(images_dir: str) -> dict[str, str]:
    """
    Walk images directory and return dict mapping relative_name (without ext) -> full_path.
    Skips _UNUSED and _DOWNSCALE folders.
    """
    images = {}
    image_exts = {".png", ".jpg", ".jpeg", ".bmp", ".tga", ".webp"}
    for root, dirs, files in os.walk(images_dir):
        dirs[:] = [d for d in dirs if d not in ("_UNUSED", "_DOWNSCALE")]
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext in image_exts:
                full_path = os.path.join(root, fname)
                rel = os.path.relpath(full_path, images_dir)
                name_no_ext = os.path.splitext(rel)[0]
                images[name_no_ext] = full_path
    return images
