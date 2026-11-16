"""Spine JSON load/save and attachment iteration helpers.

Supports both Spine 3.7 (skins as dict) and 3.8+ (skins as list) formats.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import datetime


def load_spine_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_spine_json(path: str, data: dict):
    """Atomically write *data* as compact JSON (temp file + os.replace)."""
    path = os.fspath(path)
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(
        prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------

def spine_version(data) -> tuple:
    """Return (major, minor) of ``skeleton.spine``; (0, 0) when unknown.

    "3.8.99-from-4.2.43" -> (3, 8): only the part before "-from-" counts.
    """
    try:
        raw = (data.get("skeleton") or {}).get("spine")
        if not isinstance(raw, str):
            return (0, 0)
        raw = raw.split("-from-")[0].strip()
        m = re.match(r"^(\d+)\.(\d+)", raw)
        if not m:
            return (0, 0)
        return (int(m.group(1)), int(m.group(2)))
    except Exception:
        return (0, 0)


def is_spine4(data) -> bool:
    return spine_version(data)[0] >= 4


# ---------------------------------------------------------------
# Backups
# ---------------------------------------------------------------

BACKUP_DIR_NAME = "_ssk_backups"


def _history_prefix(json_path: str) -> str:
    return os.path.basename(json_path) + "."


def list_backups(json_path: str) -> list:
    """History backup files for *json_path*, newest first."""
    json_path = os.fspath(json_path)
    bdir = os.path.join(os.path.dirname(os.path.abspath(json_path)), BACKUP_DIR_NAME)
    if not os.path.isdir(bdir):
        return []
    prefix = _history_prefix(json_path)
    pat = re.compile(r"^" + re.escape(prefix) + r"\d{8}_\d{6}_\d{6}\.json$")
    names = [n for n in os.listdir(bdir) if pat.match(n)]
    names.sort(reverse=True)  # timestamp is fixed width -> lexical == chronological
    return [os.path.join(bdir, n) for n in names]


def create_backup(json_path: str, keep: int = 20) -> str:
    """Back up *json_path* before modifying it.

    Writes ``<json>.backup`` (state before the last change) and a timestamped
    history copy in ``_ssk_backups`` next to the file, pruned to *keep* entries.
    Returns the ``.backup`` path. Raises OSError on failure.
    """
    json_path = os.fspath(json_path)
    backup_path = json_path + ".backup"
    shutil.copy2(json_path, backup_path)

    bdir = os.path.join(os.path.dirname(os.path.abspath(json_path)), BACKUP_DIR_NAME)
    os.makedirs(bdir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    hist = os.path.join(bdir, "%s%s.json" % (_history_prefix(json_path), stamp))
    shutil.copy2(json_path, hist)

    if keep is not None and keep > 0:
        for old in list_backups(json_path)[keep:]:
            try:
                os.remove(old)
            except OSError:
                pass
    return backup_path


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


def skin_entries(spine_data: dict) -> list:
    """Return [(skin_name, attachments_dict)] for both skin formats.

    The attachments dicts are the ORIGINAL objects (mutate in place to keep
    extra 3.8+ skin keys such as bones / ik / transform / path / physics).
    A 3.8 list skin lacking "attachments" gets an empty dict attached to it.
    """
    skins = spine_data.get("skins")
    out = []
    if isinstance(skins, dict):
        for name, atts in skins.items():
            if isinstance(atts, dict):
                out.append((name, atts))
    elif isinstance(skins, list):
        for skin in skins:
            if not isinstance(skin, dict):
                continue
            atts = skin.get("attachments")
            if not isinstance(atts, dict):
                atts = {}
                skin["attachments"] = atts
            out.append((skin.get("name", "default"), atts))
    return out


def denormalize_skins(skins_dict: dict) -> list:
    """Convert unified dict format back to 3.8 list format.

    LOSSY: extra 3.8+ skin keys (bones, ik, transform, path, physics) are not
    preserved. Prefer ``skin_entries`` and mutate in place.
    """
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


IMAGE_ATTACHMENT_TYPES = (None, "region", "mesh", "linkedmesh")

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".tga", ".webp")


def attachment_image_key(att_name: str, att_data: dict) -> str:
    """Image lookup key for an attachment: path -> name -> att_name."""
    if isinstance(att_data, dict):
        for field in ("path", "name"):
            val = att_data.get(field)
            if isinstance(val, str) and val:
                return val
    return att_name


def iter_image_attachments(skins: dict):
    """Yield (skin, slot, att_name, att_data) for attachments that use an image
    (region, mesh, linkedmesh). Accepts normalized (dict) skins."""
    for skin_name, skin_data in skins.items():
        for slot_name, attachments in skin_data.items():
            for att_name, att_data in attachments.items():
                if att_data.get("type") in IMAGE_ATTACHMENT_TYPES:
                    yield skin_name, slot_name, att_name, att_data


def image_key_from_path(rel_path: str) -> str:
    """"sub\\dir\\img.png" -> "sub/dir/img" (strip one image extension only)."""
    key = rel_path.replace("\\", "/")
    base, ext = os.path.splitext(key)
    if ext.lower() in IMAGE_EXTENSIONS:
        return base
    return key


# ---------------------------------------------------------------
# Draw order / deform timeline accessors
# ---------------------------------------------------------------

def draw_order_key(anim: dict):
    """Which draw-order key the animation uses ("drawOrder" / "draworder") or None."""
    if "drawOrder" in anim:
        return "drawOrder"
    if "draworder" in anim:
        return "draworder"
    return None


def get_draw_order_timeline(anim: dict) -> list:
    """The actual draw-order key list of the animation, else []."""
    key = draw_order_key(anim)
    if key is None:
        return []
    val = anim.get(key)
    return val if isinstance(val, list) else []


def iter_deform_timelines(anim: dict):
    """Yield (skin, slot, attachment, keys_list) for ffd / deform / 4.1+ attachments."""
    for field in ("ffd", "deform"):
        block = anim.get(field)
        if not isinstance(block, dict):
            continue
        for skin, slots in block.items():
            if not isinstance(slots, dict):
                continue
            for slot, atts in slots.items():
                if not isinstance(atts, dict):
                    continue
                for att, keys in atts.items():
                    if isinstance(keys, list):
                        yield skin, slot, att, keys
    block = anim.get("attachments")
    if isinstance(block, dict):
        for skin, slots in block.items():
            if not isinstance(slots, dict):
                continue
            for slot, atts in slots.items():
                if not isinstance(atts, dict):
                    continue
                for att, tls in atts.items():
                    if isinstance(tls, dict) and isinstance(tls.get("deform"), list):
                        yield skin, slot, att, tls["deform"]


# ---------------------------------------------------------------
# Vertices / weights
# ---------------------------------------------------------------

def attachment_vertex_count(att_data):
    """Vertex count of an attachment, or None when not a vertex attachment."""
    if not isinstance(att_data, dict):
        return None
    t = att_data.get("type")
    if t in ("mesh", "linkedmesh"):
        uvs = att_data.get("uvs")
        if isinstance(uvs, list):
            return len(uvs) // 2
        return None
    if t in ("clipping", "boundingbox", "path"):
        vc = att_data.get("vertexCount")
        return vc if isinstance(vc, int) else None
    return None


def is_weighted(att_data) -> bool:
    """True when the attachment stores weighted vertices."""
    if not isinstance(att_data, dict):
        return False
    verts = att_data.get("vertices")
    if not isinstance(verts, list):
        return False
    t = att_data.get("type")
    if t in ("mesh", "linkedmesh"):
        uvs = att_data.get("uvs")
        return isinstance(uvs, list) and len(verts) > len(uvs)
    if t in ("clipping", "boundingbox", "path"):
        vc = att_data.get("vertexCount")
        return isinstance(vc, int) and len(verts) > vc * 2
    return False


def iter_all_attachments(spine_data: dict):
    """Yield (skin, slot, att_name, att_data) from raw spine_data (both formats)."""
    for skin_name, atts in skin_entries(spine_data):
        for slot_name, slot_atts in atts.items():
            if not isinstance(slot_atts, dict):
                continue
            for att_name, att_data in slot_atts.items():
                if isinstance(att_data, dict):
                    yield skin_name, slot_name, att_name, att_data


def iter_weighted_attachments(spine_data: dict):
    """Yield att_data of every weighted attachment."""
    for _s, _sl, _n, att in iter_all_attachments(spine_data):
        if is_weighted(att):
            yield att


def _walk_weighted(vertices):
    """Yield (position_of_bone_index, bone_index) per influence; ValueError if malformed."""
    i = 0
    n = len(vertices)
    while i < n:
        try:
            bc = vertices[i]
            if isinstance(bc, bool) or int(bc) != bc or bc < 0:
                raise ValueError
            bc = int(bc)
        except (TypeError, ValueError):
            raise ValueError("malformed weighted vertices: bad bone count at %d" % i)
        i += 1
        if i + bc * 4 > n:
            raise ValueError("malformed weighted vertices: truncated at %d" % i)
        for _ in range(bc):
            bi = vertices[i]
            try:
                if isinstance(bi, bool) or int(bi) != bi:
                    raise ValueError
            except (TypeError, ValueError):
                raise ValueError("malformed weighted vertices: bad bone index at %d" % i)
            yield i, int(bi)
            i += 4


def weighted_bone_indices(vertices) -> set:
    """Set of bone indices used by a weighted vertices array (ValueError if malformed)."""
    return {bi for _pos, bi in _walk_weighted(vertices)}


def remap_weighted_vertices(vertices, index_map: dict) -> list:
    """New vertices list with bone indices remapped through *index_map*.

    KeyError(old_index) if an index is not mapped; ValueError if malformed.
    """
    out = list(vertices)
    for pos, bi in _walk_weighted(vertices):
        if bi not in index_map:
            raise KeyError(bi)
        out[pos] = index_map[bi]
    return out


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
