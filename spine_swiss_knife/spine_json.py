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


def mesh_is_weighted(vertices: list, vertex_count: int) -> bool:
    """Return True when a VertexAttachment uses Spine's weighted vertex format.

    A non-weighted mesh stores a flat list of (x, y) pairs, so its vertices
    array has exactly ``vertex_count * 2`` values. Weighted (bone-rigged)
    attachments store, per vertex, ``boneCount`` followed by
    ``(boneIndex, x, y, weight)`` for each influencing bone, so the array is a
    different (and usually longer, sometimes odd) length.

    *vertex_count* is ``len(uvs) // 2`` for meshes or the ``vertexCount`` field
    for clipping / path / bounding-box attachments.
    """
    return vertex_count > 0 and len(vertices) != vertex_count * 2


def resolve_weighted_vertices(vertices: list, bone_transforms: list) -> list[float]:
    """Decode weighted-mesh *vertices* into a flat list of world-space pairs.

    Weighted format, per vertex::

        boneCount, (boneIndex, x, y, weight) * boneCount

    where (x, y) are coordinates in each bone's local space. The world position
    is the weighted sum of each bone's current world transform applied to its
    local coordinate.

    *bone_transforms* is indexed by global bone index; each element exposes the
    attributes ``a, b, c, d, worldX, worldY`` (a ``BoneTransform``) or is
    ``None`` when the bone is missing. Returns ``[wx0, wy0, wx1, wy1, ...]``.
    """
    out: list[float] = []
    i = 0
    n = len(vertices)
    n_bones = len(bone_transforms)
    while i < n:
        bone_count = int(vertices[i])
        i += 1
        wx = 0.0
        wy = 0.0
        for _ in range(bone_count):
            if i + 3 >= n:  # malformed / truncated data — stop safely
                i = n
                break
            bone_index = int(vertices[i])
            vx = vertices[i + 1]
            vy = vertices[i + 2]
            weight = vertices[i + 3]
            i += 4
            bt = bone_transforms[bone_index] if 0 <= bone_index < n_bones else None
            if bt is None:
                continue
            wx += (bt.a * vx + bt.b * vy + bt.worldX) * weight
            wy += (bt.c * vx + bt.d * vy + bt.worldY) * weight
        out.append(wx)
        out.append(wy)
    return out


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
