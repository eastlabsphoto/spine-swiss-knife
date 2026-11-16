"""Unused Finder core — pure comparison logic (no Qt, no i18n)."""

from __future__ import annotations

from .spine_json import (
    IMAGE_ATTACHMENT_TYPES, attachment_image_key, iter_all_attachments,
)


def json_image_keys(spine_data: dict) -> set:
    """Image keys referenced by region / mesh / linkedmesh attachments."""
    keys = set()
    for _skin, _slot, att_name, att in iter_all_attachments(spine_data):
        if att.get("type") in IMAGE_ATTACHMENT_TYPES:
            keys.add(attachment_image_key(att_name, att))
    return keys


def compare_images(disk_keys, atlas_regions=None, json_keys=None) -> dict:
    """Compare image keys on disk with the atlas and/or JSON references.

    An image is USED if its key is an atlas region OR is referenced by the JSON.
    "missing" = referenced (atlas or JSON) but not on disk.
    At least one of *atlas_regions* / *json_keys* must be given (not None).
    """
    if atlas_regions is None and json_keys is None:
        raise ValueError("need an atlas or a JSON to compare against")
    disk = set(disk_keys)
    referenced = set(atlas_regions or ()) | set(json_keys or ())
    return {
        "used": disk & referenced,
        "unused": disk - referenced,
        "missing": referenced - disk,
    }
