"""Spine JSON load/save and attachment iteration helpers.

Supports both Spine 3.7 (skins as dict) and 3.8+ (skins as list) formats.
"""

import json


def load_spine_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_spine_json(path: str, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))


# ---------------------------------------------------------------
# Skins format helpers (3.7 dict  <->  3.8 list)
# ---------------------------------------------------------------

def is_skins_list(spine_data: dict) -> bool:
    """True if skins are in 3.8+ list format."""
    return isinstance(spine_data.get("skins"), list)


def normalize_skins(skins_raw) -> dict:
    """Convert skins to a unified dict format: {skin_name: {slot: {att: data}}}.

    3.7 format (dict):  {"default": {"slot": {"att": {...}}}}
    3.8 format (list):  [{"name": "default", "attachments": {"slot": {"att": {...}}}}]
    """
    if isinstance(skins_raw, dict):
        return skins_raw
    # 3.8 list format
    result = {}
    for skin in skins_raw:
        name = skin.get("name", "default")
        result[name] = skin.get("attachments", {})
    return result


def denormalize_skins(skins_dict: dict) -> list:
    """Convert unified dict format back to 3.8 list format."""
    result = []
    for name, attachments in skins_dict.items():
        entry = {"name": name}
        if attachments:
            entry["attachments"] = attachments
        result.append(entry)
    return result


# ---------------------------------------------------------------
# Attachment iterators (work with normalized dict skins)
# ---------------------------------------------------------------

def iter_region_attachments(skins: dict):
    """
    Yield (skin_name, slot_name, att_name, att_data) for every region attachment.
    Region = default type (no "type" field) or type == "region".
    Accepts normalized (dict) skins.
    """
    for skin_name, skin_data in skins.items():
        for slot_name, attachments in skin_data.items():
            for att_name, att_data in attachments.items():
                att_type = att_data.get("type")
                if att_type is None or att_type == "region":
                    yield skin_name, slot_name, att_name, att_data


def iter_clipping_attachments(skins: dict):
    """Yield (skin_name, slot_name, att_name, att_data) for every clipping attachment.
    Accepts normalized (dict) skins.
    """
    for skin_name, skin_data in skins.items():
        for slot_name, attachments in skin_data.items():
            for att_name, att_data in attachments.items():
                if att_data.get("type") == "clipping":
                    yield skin_name, slot_name, att_name, att_data


def attachment_image_key(att_name: str, att_data: dict) -> str:
    """Return the image lookup key for an attachment (path field or att_name)."""
    return att_data.get("path", att_name)


def clip_end_marker_after_slot(att_name: str | None, att_data: dict | None) -> bool:
    """Return True when a clip end marker must be emitted after this slot.

    Spine clipping ends after the end slot is processed. That only matters for
    slots that actually render a drawable attachment. Empty slots, clipping
    attachments, and unsupported attachment types can end clipping immediately.
    """
    if att_name is None:
        return False

    att_type = (att_data or {}).get("type")
    return att_type is None or att_type in ("region", "mesh")
