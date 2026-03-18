"""Draw Order Optimizer — reduce draw calls by grouping blend modes.

Splits the slot list into zones (bounded by clipping masks), optimises
each zone independently via stable partition, then verifies the result
by rendering full animation sequences with QPainter and comparing
frames pixel-by-pixel.
"""

import copy
import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QSpinBox,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QHeaderView, QMessageBox,
    QTextEdit, QApplication,
)
from PySide6.QtGui import (
    QFont, QTextCharFormat, QColor, QTextCursor,
    QImage, QPainter, QTransform, QPainterPath, QPolygonF,
)
from PySide6.QtCore import QPointF, Qt

from .i18n import tr, language_changed
from .spine_json import load_spine_json, save_spine_json, normalize_skins
from .spine_viewer import (
    BoneTransform, solve_world_transforms, AnimationState,
    _evaluate_draw_order, solve_ik_constraints,
    build_draw_list, _affine_from_triangles, load_atlas_textures,
)

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


# ==========================================================================
# Analysis helpers
# ==========================================================================

def count_blend_groups(slots: list[dict]) -> int:
    """Count contiguous blend-mode groups (~ draw calls)."""
    if not slots:
        return 0
    groups = 1
    prev = slots[0].get("blend", "normal")
    for slot in slots[1:]:
        cur = slot.get("blend", "normal")
        if cur != prev:
            groups += 1
            prev = cur
    return groups


def compute_optimal_order(slots: list[dict]) -> list[dict]:
    """Stable-partition: normal slots first, then non-normal.

    Preserves relative order within each group so that z-order among
    same-blend slots is unchanged.  Non-normal slots are further grouped
    by their specific blend mode (additive, multiply, screen).
    """
    groups: dict[str, list[dict]] = {}
    for s in slots:
        blend = s.get("blend", "normal")
        groups.setdefault(blend, []).append(s)
    # Normal first, then each non-normal blend grouped together
    result = groups.pop("normal", [])
    for blend in sorted(groups):
        result.extend(groups[blend])
    return result


def has_draw_order_timelines(spine_data: dict) -> bool:
    """Check whether any animation has drawOrder keyframes."""
    for anim in spine_data.get("animations", {}).values():
        if anim.get("drawOrder"):
            return True
    return False


def analyze_draw_order(spine_data: dict) -> dict:
    """Full draw-order analysis for the UI."""
    slots = spine_data.get("slots", [])
    current_groups = count_blend_groups(slots)
    optimal = compute_optimal_order(slots)
    optimal_groups = count_blend_groups(optimal)

    # Build per-group breakdown
    groups = []
    if slots:
        cur_blend = slots[0].get("blend", "normal")
        cur_count = 1
        for slot in slots[1:]:
            blend = slot.get("blend", "normal")
            if blend == cur_blend:
                cur_count += 1
            else:
                groups.append({"blend": cur_blend, "count": cur_count})
                cur_blend = blend
                cur_count = 1
        groups.append({"blend": cur_blend, "count": cur_count})

    return {
        "slots": slots,
        "groups": groups,
        "current_groups": current_groups,
        "optimal_order": optimal,
        "optimal_groups": optimal_groups,
        "has_draw_order_timelines": has_draw_order_timelines(spine_data),
        "can_optimize": current_groups > optimal_groups,
    }


# ==========================================================================
# Zone splitting
# ==========================================================================

@dataclass
class Zone:
    """A contiguous range of slots that can be optimised independently."""
    slots: list  # list of slot dicts
    is_boundary: bool = False  # True for clip start/end slots (never moved)


def _get_clip_regions(
    slots: list[dict], skins: dict,
) -> list[tuple[int, int]]:
    """Return list of (clip_start_idx, clip_end_idx) pairs.

    A clipping attachment on slot *i* with ``end`` naming slot *j*
    means slots between *i* and *j* are clipped.
    """
    slot_index = {s["name"]: i for i, s in enumerate(slots)}
    regions = []
    for skin in skins.values():
        for slot_name, atts in skin.items():
            for att_name, att_data in atts.items():
                if att_data.get("type") != "clipping":
                    continue
                end_name = att_data.get("end", "")
                if not end_name:
                    continue
                start_idx = slot_index.get(slot_name)
                end_idx = slot_index.get(end_name)
                if start_idx is not None and end_idx is not None:
                    lo, hi = min(start_idx, end_idx), max(start_idx, end_idx)
                    regions.append((lo, hi))
    return regions


def _split_into_zones(slots, skins):
    """Split slot list into zones separated by clip boundaries.

    Returns list of Zone objects. Clip start/end slots are single-slot
    boundary zones. Slots between clip boundaries form clipped zones.
    Slots outside form unclipped zones.
    """
    regions = _get_clip_regions(slots, skins)
    if not regions:
        return [Zone(slots=list(slots))]

    # Collect all boundary indices (both start and end of each clip region)
    boundaries = set()
    for lo, hi in regions:
        boundaries.add(lo)
        boundaries.add(hi)

    # Also track which indices are INSIDE a clip region (exclusive of boundaries)
    inside_clip = set()
    for lo, hi in regions:
        for i in range(lo + 1, hi):
            inside_clip.add(i)

    zones = []
    current_slots = []

    for i, slot in enumerate(slots):
        if i in boundaries:
            # Flush any accumulated slots as a zone
            if current_slots:
                zones.append(Zone(slots=current_slots))
                current_slots = []
            # Boundary slot gets its own zone
            zones.append(Zone(slots=[slot], is_boundary=True))
        else:
            # If transitioning between inside/outside clip, flush
            if current_slots:
                prev_inside = (i - 1) in inside_clip
                curr_inside = i in inside_clip
                if prev_inside != curr_inside:
                    zones.append(Zone(slots=current_slots))
                    current_slots = []
            current_slots.append(slot)

    if current_slots:
        zones.append(Zone(slots=current_slots))

    return zones


def _optimize_zone(zone):
    """Optimise a single zone: stable partition by blend mode."""
    if zone.is_boundary or len(zone.slots) <= 1:
        return list(zone.slots)
    return compute_optimal_order(zone.slots)


def _optimize_zones(zones):
    """Optimise all zones and reassemble into a flat slot list."""
    result = []
    for zone in zones:
        result.extend(_optimize_zone(zone))
    return result


# ==========================================================================
# Bounding box calculation
# ==========================================================================

def _slot_aabb(
    att_data: dict, bone_tx: BoneTransform,
) -> tuple[float, float, float, float] | None:
    """Compute axis-aligned bounding box for a region attachment.

    Returns ``(min_x, min_y, max_x, max_y)`` in world space,
    or *None* if the attachment has no area.
    """
    w = att_data.get("width", 0)
    h = att_data.get("height", 0)
    if w <= 0 or h <= 0:
        return None

    # Attachment local transform
    ax = att_data.get("x", 0.0)
    ay = att_data.get("y", 0.0)
    a_rot = math.radians(att_data.get("rotation", 0.0))
    a_sx = att_data.get("scaleX", 1.0)
    a_sy = att_data.get("scaleY", 1.0)

    cos_r = math.cos(a_rot)
    sin_r = math.sin(a_rot)

    # Four corners in attachment-local space
    hw, hh = w / 2, h / 2
    corners = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]

    world_xs = []
    world_ys = []
    for cx, cy in corners:
        # Scale
        lx = cx * a_sx
        ly = cy * a_sy
        # Rotate
        rx = lx * cos_r - ly * sin_r
        ry = lx * sin_r + ly * cos_r
        # Offset
        bx = rx + ax
        by = ry + ay
        # Bone world transform
        wx = bone_tx.a * bx + bone_tx.b * by + bone_tx.worldX
        wy = bone_tx.c * bx + bone_tx.d * by + bone_tx.worldY
        world_xs.append(wx)
        world_ys.append(wy)

    return (min(world_xs), min(world_ys), max(world_xs), max(world_ys))


def _mesh_aabb(
    att_data: dict, bone_tx: BoneTransform,
    deform_deltas: list[float] | None = None,
) -> tuple[float, float, float, float] | None:
    """Compute AABB for a mesh attachment from its vertices."""
    verts = att_data.get("vertices", [])
    if len(verts) < 2:
        return None

    # Weighted meshes have a different vertex format
    if att_data.get("hull", 0) > 0 and len(verts) > att_data["hull"] * 2:
        # Weighted mesh — extract rest positions (simplified: skip weights)
        return None  # Conservative: can't easily compute AABB

    # Simple mesh: pairs of (x, y)
    if deform_deltas and len(deform_deltas) == len(verts):
        verts = [v + d for v, d in zip(verts, deform_deltas)]

    world_xs = []
    world_ys = []
    for i in range(0, len(verts) - 1, 2):
        bx, by = verts[i], verts[i + 1]
        wx = bone_tx.a * bx + bone_tx.b * by + bone_tx.worldX
        wy = bone_tx.c * bx + bone_tx.d * by + bone_tx.worldY
        world_xs.append(wx)
        world_ys.append(wy)

    if not world_xs:
        return None
    return (min(world_xs), min(world_ys), max(world_xs), max(world_ys))


def _get_slot_attachment(
    slot_name: str, slot_data: dict, skins: dict,
    slot_states: dict | None = None, active_skin: str = "default",
) -> dict | None:
    """Resolve the active attachment for a slot."""
    att_name = slot_data.get("attachment")
    # Animation override
    if slot_states and slot_name in slot_states:
        att_name = slot_states[slot_name].get("attachment", att_name)
    if att_name is None:
        return None

    # Search in active skin first, then default
    for skin_name in (active_skin, "default"):
        skin = skins.get(skin_name, {})
        slot_atts = skin.get(slot_name, {})
        if att_name in slot_atts:
            return slot_atts[att_name]
    return None


def _compute_skeleton_bbox(spine_data, textures=None, padding=0.2):
    """Compute skeleton bounding box from setup pose.
    Returns (x, y, width, height) in Spine world coordinates.
    """
    bones = spine_data.get("bones", [])
    ik = spine_data.get("ik", [])
    skins = normalize_skins(spine_data.get("skins", {}))
    slots = spine_data.get("slots", [])

    world = solve_world_transforms(bones)
    if ik:
        solve_ik_constraints(bones, world, ik)

    min_x, min_y = float("inf"), float("inf")
    max_x, max_y = float("-inf"), float("-inf")

    for slot in slots:
        att = _get_slot_attachment(slot["name"], slot, skins)
        if att is None:
            continue
        bone = world.get(slot.get("bone", ""))
        if bone is None:
            continue

        att_type = att.get("type", "region")
        if att_type in ("region", None):
            aabb = _slot_aabb(att, bone)
        elif att_type == "mesh":
            aabb = _mesh_aabb(att, bone)
        else:
            continue

        if aabb is None:
            continue
        min_x = min(min_x, aabb[0])
        min_y = min(min_y, aabb[1])
        max_x = max(max_x, aabb[2])
        max_y = max(max_y, aabb[3])

    # Fallback if no valid AABBs
    if min_x == float("inf"):
        return (-512, -512, 1024, 1024)

    w = max_x - min_x
    h = max_y - min_y
    pad_x = w * padding
    pad_y = h * padding

    return (min_x - pad_x, min_y - pad_y, w + 2 * pad_x, h + 2 * pad_y)


# ==========================================================================
# QPainter off-screen rendering
# ==========================================================================

_BLEND_MODES = {
    "additive": QPainter.CompositionMode_Plus,
    "multiply": QPainter.CompositionMode_Multiply,
    "screen": QPainter.CompositionMode_Screen,
}


def render_frame(
    spine_data: dict,
    textures: dict,
    world_transforms: dict,
    slot_states: dict | None = None,
    deform_states: dict | None = None,
    slot_order: list[dict] | None = None,
    canvas_size: int = 2048,
    active_skin: str = "default",
    bbox=None,
) -> QImage:
    """Render a single skeleton frame to a QImage.

    When *bbox* is (x, y, w, h) in Spine world coords, uses that as
    the viewport. Otherwise uses a square canvas centered at origin.
    """
    draw_list = build_draw_list(
        spine_data, world_transforms, textures,
        slot_states=slot_states,
        slot_order=slot_order,
        deform_states=deform_states,
        active_skin=active_skin,
    )

    if bbox:
        bx, by, bw, bh = bbox
        img_w = max(1, int(math.ceil(bw)))
        img_h = max(1, int(math.ceil(bh)))
        # Camera: maps world (bx, by) to pixel (0, img_h)
        # Y is flipped: Spine Y-up, QPainter Y-down
        cx = -bx
        cy = by + bh
        zoom = 1.0
    else:
        img_w = img_h = canvas_size
        cx = canvas_size / 2.0
        cy = canvas_size / 2.0
        zoom = 1.0

    image = QImage(img_w, img_h, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)

    active_clip_path = None
    clip_end = ""

    for item in draw_list:
        item_type = item.get("type")

        # -- clip end marker --
        if item_type == "clip_end_marker":
            if clip_end:
                painter.setClipping(False)
                clip_end = ""
                active_clip_path = None
            continue

        # -- clipping attachment --
        if item_type == "clip":
            bt = world_transforms.get(item["bone"])
            if bt is None:
                continue
            verts = item["vertices"]
            points = []
            for vi in range(0, len(verts), 2):
                lx, ly = verts[vi], verts[vi + 1]
                wx = bt.a * lx + bt.b * ly + bt.worldX
                wy = bt.c * lx + bt.d * ly + bt.worldY
                sx = cx + wx * zoom
                sy = cy - wy * zoom
                points.append(QPointF(sx, sy))
            if points:
                path = QPainterPath()
                path.addPolygon(QPolygonF(points))
                active_clip_path = path
                painter.resetTransform()
                painter.setClipPath(path)
            clip_end = item.get("clip_end", "")
            continue

        # -- mesh attachment --
        if item_type == "mesh":
            bt = world_transforms.get(item["bone"])
            if bt is None:
                continue
            verts = item["vertices"]
            uvs = item["uvs"]
            tris = item["triangles"]
            pixmap = item["pixmap"]
            pw, ph = pixmap.width(), pixmap.height()
            color = item.get("color")

            if color:
                painter.setOpacity(color[3])
            blend = _BLEND_MODES.get(item.get("blend"))
            if blend is not None:
                painter.setCompositionMode(blend)

            screen_pts = []
            for vi in range(0, len(verts), 2):
                lx, ly = verts[vi], verts[vi + 1]
                wx = bt.a * lx + bt.b * ly + bt.worldX
                wy = bt.c * lx + bt.d * ly + bt.worldY
                screen_pts.append((
                    cx + wx * zoom, cy - wy * zoom))

            uv_pts = [(uvs[ui] * pw, uvs[ui + 1] * ph)
                       for ui in range(0, len(uvs), 2)]

            for ti in range(0, len(tris), 3):
                i0, i1, i2 = tris[ti], tris[ti + 1], tris[ti + 2]
                if max(i0, i1, i2) >= len(screen_pts):
                    continue
                if max(i0, i1, i2) >= len(uv_pts):
                    continue
                src = [uv_pts[i0], uv_pts[i1], uv_pts[i2]]
                dst = [screen_pts[i0], screen_pts[i1], screen_pts[i2]]
                xform = _affine_from_triangles(src, dst)
                if xform is None:
                    continue

                tri_path = QPainterPath()
                tri_path.moveTo(*dst[0])
                tri_path.lineTo(*dst[1])
                tri_path.lineTo(*dst[2])
                tri_path.closeSubpath()

                painter.resetTransform()
                if clip_end and active_clip_path is not None:
                    painter.setClipPath(
                        active_clip_path.intersected(tri_path))
                else:
                    painter.setClipPath(tri_path)
                painter.setTransform(xform)
                painter.drawPixmap(0, 0, pixmap)

            painter.setClipping(False)
            painter.resetTransform()
            if clip_end and active_clip_path is not None:
                painter.setClipPath(active_clip_path)
            if blend is not None:
                painter.setCompositionMode(
                    QPainter.CompositionMode_SourceOver)
            if color:
                painter.setOpacity(1.0)
            continue

        # -- region attachment (default) --
        pixmap = item.get("pixmap")
        if pixmap is None or pixmap.isNull():
            continue
        bt = world_transforms.get(item["bone"])
        if bt is None:
            continue
        att = item["att_data"]
        color = item.get("color")

        ax = att.get("x", 0.0)
        ay = att.get("y", 0.0)
        a_rot = att.get("rotation", 0.0)
        a_sx = att.get("scaleX", 1.0)
        a_sy = att.get("scaleY", 1.0)
        pw, ph = pixmap.width(), pixmap.height()
        att_w = att.get("width", pw)
        att_h = att.get("height", ph)

        bone_t = QTransform(
            bt.a, -bt.c, -bt.b, bt.d,
            bt.worldX, -bt.worldY)
        att_t = QTransform()
        att_t.translate(ax, -ay)
        att_t.rotate(-a_rot)
        att_t.scale(a_sx * att_w / pw if pw else 1,
                    a_sy * att_h / ph if ph else 1)
        att_t.translate(-pw / 2.0, -ph / 2.0)

        cam_t = QTransform()
        cam_t.translate(cx, cy)
        cam_t.scale(zoom, zoom)

        t = att_t * bone_t * cam_t
        painter.setTransform(t)

        if color:
            painter.setOpacity(color[3])
        blend = _BLEND_MODES.get(item.get("blend"))
        if blend is not None:
            painter.setCompositionMode(blend)

        painter.drawPixmap(0, 0, pixmap)

        if blend is not None:
            painter.setCompositionMode(
                QPainter.CompositionMode_SourceOver)
        if color:
            painter.setOpacity(1.0)

    painter.end()
    return image


def render_animation_sequence(spine_data, textures, slot_order, anim_name,
                               fps, bbox, output_dir=None):
    """Render an animation as a list of QImages, optionally saving PNGs.

    If anim_name is None, renders one setup-pose frame.
    Processes frames one at a time to keep memory usage low.
    Returns list of QImages.
    """
    bones = spine_data.get("bones", [])
    ik = spine_data.get("ik", [])

    if anim_name is None:
        # Setup pose
        world = solve_world_transforms(bones)
        if ik:
            solve_ik_constraints(bones, world, ik)
        img = render_frame(spine_data, textures, world,
                          slot_order=slot_order, bbox=bbox)
        if output_dir:
            d = os.path.join(output_dir, "setup_pose")
            os.makedirs(d, exist_ok=True)
            img.save(os.path.join(d, "0000.png"))
        return [img]

    anim_data = spine_data.get("animations", {}).get(anim_name)
    if not anim_data:
        return []

    anim_state = AnimationState(spine_data, anim_data)
    duration = anim_state.duration
    if duration <= 0:
        return []

    frame_count = int(duration * fps) + 1
    images = []

    # Create output dir for this animation if saving
    if output_dir:
        anim_dir = os.path.join(output_dir, anim_name)
        os.makedirs(anim_dir, exist_ok=True)

    for fi in range(frame_count):
        t = fi / fps
        if t > duration:
            t = duration

        bone_overrides, slot_states, deform_states = anim_state.evaluate(t)
        world = solve_world_transforms(bones, bone_overrides)
        if ik:
            solve_ik_constraints(bones, world, ik)

        # Handle animated draw order
        do_keys = anim_data.get("drawOrder", anim_data.get("draworder"))
        if do_keys:
            effective_order = _evaluate_draw_order(slot_order, do_keys, t)
        else:
            effective_order = slot_order

        img = render_frame(spine_data, textures, world,
                          slot_states=slot_states,
                          deform_states=deform_states,
                          slot_order=effective_order,
                          bbox=bbox)

        if output_dir:
            img.save(os.path.join(anim_dir, f"{fi:04d}.png"))

        images.append(img)

    return images


# ==========================================================================
# QImage comparison
# ==========================================================================

def _qimages_match(img_a, img_b, tolerance=5):
    """Compare two QImages. Uses numpy if available for speed."""
    if img_a.size() != img_b.size():
        return False

    if tolerance == 0:
        return img_a == img_b

    if HAS_NUMPY:
        w, h = img_a.width(), img_a.height()
        # Get raw bytes
        ptr_a = img_a.constBits()
        ptr_b = img_b.constBits()
        if ptr_a is None or ptr_b is None:
            return img_a == img_b
        arr_a = np.frombuffer(bytes(ptr_a), dtype=np.uint8).reshape(h, w, 4)
        arr_b = np.frombuffer(bytes(ptr_b), dtype=np.uint8).reshape(h, w, 4)
        diff = np.abs(arr_a.astype(np.int16) - arr_b.astype(np.int16))
        pixel_max = diff.max(axis=2)
        bad = np.count_nonzero(pixel_max > tolerance)
        total = w * h
        return bad / total < 0.001

    # Fallback: exact comparison only
    return img_a == img_b


def compare_sequences(images_a, images_b, tolerance=5):
    """Compare two lists of QImages frame by frame.
    Returns (all_match, list_of_differing_frame_indices).
    """
    if len(images_a) != len(images_b):
        return False, list(range(max(len(images_a), len(images_b))))

    diffs = []
    for i, (a, b) in enumerate(zip(images_a, images_b)):
        if not _qimages_match(a, b, tolerance):
            diffs.append(i)
            if len(diffs) >= 10:  # Early exit
                break
    return len(diffs) == 0, diffs


# ==========================================================================
# Optimisation engine
# ==========================================================================

def _moved_names(
    original: list[dict], reordered: list[dict],
) -> list[str]:
    """Return names of slots whose index changed."""
    orig_pos = {s["name"]: i for i, s in enumerate(original)}
    return [
        s["name"] for i, s in enumerate(reordered)
        if orig_pos.get(s["name"]) != i
    ]


def _render_all_animations(spine_data, textures, slot_order,
                            anim_names, fps, bbox, output_dir=None):
    """Render all animations and return dict of {anim_name: [QImage]}."""
    result = {}
    for anim_name in anim_names:
        imgs = render_animation_sequence(
            spine_data, textures, slot_order, anim_name,
            fps, bbox, output_dir)
        result[anim_name] = imgs
    return result


def _compare_with_original(spine_data, textures, original_images,
                            candidate_slots, anim_names, fps, bbox,
                            tolerance, on_log=None, early_exit=True):
    """Render candidate and compare against cached original images.

    Returns ``(all_match, list_of_diff_anim_names)``.
    """
    diffs = []
    for anim_name in anim_names:
        QApplication.processEvents()
        imgs_orig = original_images.get(anim_name, [])
        imgs_cand = render_animation_sequence(
            spine_data, textures, candidate_slots, anim_name,
            fps, bbox)
        match, _ = compare_sequences(imgs_orig, imgs_cand, tolerance)
        if not match:
            diffs.append(anim_name or "setup_pose")
            if early_exit:
                return False, diffs
    return len(diffs) == 0, diffs


def _bisect_zone(
    spine_data, textures, original_images, full_slots,
    zone_start, zone_end, anim_names, fps, bbox, tolerance,
    on_log=None, _depth=0,
):
    """Binary search on a sub-range of slots within a zone.

    Optimises slots[zone_start:zone_end] via stable partition and
    tests visually.  If it fails, splits the range in half and
    recurses on each half independently.

    *full_slots* is the complete slot list (mutable — updated in place
    when a sub-range is successfully optimised).

    Returns the number of blend groups saved.
    """
    length = zone_end - zone_start
    if length <= 1:
        return 0

    indent = "    " + "  " * _depth
    sub_slots = full_slots[zone_start:zone_end]
    optimized_sub = compute_optimal_order(sub_slots)

    # Check if optimization changes anything
    if optimized_sub == sub_slots:
        return 0

    old_groups = count_blend_groups(full_slots)

    # Build candidate: replace sub-range with optimized version
    candidate = (full_slots[:zone_start] +
                 optimized_sub +
                 full_slots[zone_end:])
    new_groups = count_blend_groups(candidate)

    if new_groups >= old_groups:
        return 0

    if on_log:
        on_log(f"{indent}Testing slots [{zone_start}:{zone_end}] "
               f"({length} slots, {old_groups} \u2192 {new_groups})...")

    ok, _ = _compare_with_original(
        spine_data, textures, original_images, candidate,
        anim_names, fps, bbox, tolerance)

    if ok:
        if on_log:
            on_log(f"{indent}\u2714 safe ({old_groups} \u2192 "
                   f"{new_groups})")
        # Apply in place
        full_slots[zone_start:zone_end] = optimized_sub
        return old_groups - new_groups

    if length <= 2:
        if on_log:
            on_log(f"{indent}\u2718 visual change, skipping")
        return 0

    # Split in half
    mid = zone_start + length // 2
    if on_log:
        on_log(f"{indent}Visual change, splitting "
               f"[{zone_start}:{mid}] + [{mid}:{zone_end}]")

    saved = 0
    saved += _bisect_zone(
        spine_data, textures, original_images, full_slots,
        zone_start, mid, anim_names, fps, bbox, tolerance,
        on_log, _depth + 1)
    saved += _bisect_zone(
        spine_data, textures, original_images, full_slots,
        mid, zone_end, anim_names, fps, bbox, tolerance,
        on_log, _depth + 1)
    return saved


def optimize_draw_order(spine_data, fps=30, tolerance=5,
                         textures=None, on_log=None):
    """Zone-based draw-order optimisation with QPainter verification.

    1. Render original once (cached).
    2. Compute optimal slot order (zone-aware stable partition).
    3. Render optimized, compare with original.
    4. If mismatch, bisect on moved slots to find safe subset.

    Returns dict with: success, modified_data, original_groups,
    optimized_groups, moved_slots, unmovable_slots, message
    """
    def log(msg):
        if on_log:
            on_log(msg)

    original_slots = spine_data.get("slots", [])
    initial_groups = count_blend_groups(original_slots)

    if not textures:
        log("Atlas required for visual verification. "
            "Please load a project with an atlas file.")
        return {"success": False,
                "message": "Atlas required for visual verification"}

    skins = normalize_skins(spine_data.get("skins", {}))

    # Compute optimal order (zone-aware)
    zones = _split_into_zones(original_slots, skins)
    optimized = _optimize_zones(zones)
    optimal_groups = count_blend_groups(optimized)

    if optimal_groups >= initial_groups:
        log("Draw order is already optimal.")
        return {"success": True, "message": "Already optimal",
                "original_groups": initial_groups,
                "optimized_groups": initial_groups,
                "modified_data": spine_data,
                "moved_slots": [], "unmovable_slots": []}

    log(f"Current draw-call groups: {initial_groups}")
    log(f"Optimal (zone-constrained): {optimal_groups}")
    log(f"Zones: {len(zones)} "
        f"({sum(1 for z in zones if not z.is_boundary)} optimisable)")

    # Compute bounding box
    bbox = _compute_skeleton_bbox(spine_data, textures)
    bw, bh = int(bbox[2]), int(bbox[3])
    log(f"Render canvas: {bw}\u00d7{bh}px")

    animations = spine_data.get("animations", {})
    anim_names = [None] + list(animations.keys())

    # -- Step 1: Render original ONCE (in-memory only) --
    log("Rendering original...")
    original_images = _render_all_animations(
        spine_data, textures, original_slots,
        anim_names, fps, bbox)
    total_frames = sum(len(v) for v in original_images.values())
    log(f"  {total_frames} frames rendered.")

    # -- Step 2: Test full optimization --
    log("Testing full optimisation...")
    ok, diffs = _compare_with_original(
        spine_data, textures, original_images, optimized,
        anim_names, fps, bbox, tolerance, on_log=on_log,
        early_exit=False)

    if ok:
        log(f"\u2714 Full optimisation safe! "
            f"{initial_groups} \u2192 {optimal_groups}")
        test_data = copy.deepcopy(spine_data)
        test_data["slots"] = optimized
        return {"success": True, "modified_data": test_data,
                "original_groups": initial_groups,
                "optimized_groups": optimal_groups,
                "moved_slots": _moved_names(original_slots, optimized),
                "unmovable_slots": [],
                "message": f"Reduced {initial_groups} \u2192 {optimal_groups}"}

    # -- Step 3: Bisect per zone, loop until stable --
    log(f"Full optimisation has visual diffs in "
        f"{len(diffs)} animation(s). "
        f"Bisecting zones to find safe sub-ranges...")

    working_slots = list(original_slots)
    total_saved = 0
    pass_num = 0
    max_passes = 20

    while pass_num < max_passes:
        pass_num += 1
        current_groups = count_blend_groups(working_slots)

        # Recompute zones from current state
        cur_skins = normalize_skins(spine_data.get("skins", {}))
        cur_zones = _split_into_zones(working_slots, cur_skins)

        pass_saved = 0
        zone_offset = 0
        for zi, zone in enumerate(cur_zones):
            zone_len = len(zone.slots)
            if not zone.is_boundary and zone_len > 1:
                saved = _bisect_zone(
                    spine_data, textures, original_images,
                    working_slots, zone_offset,
                    zone_offset + zone_len,
                    anim_names, fps, bbox, tolerance,
                    on_log=on_log)
                pass_saved += saved
            zone_offset += zone_len

        total_saved += pass_saved
        new_groups = count_blend_groups(working_slots)
        log(f"  Pass {pass_num}: {current_groups} \u2192 "
            f"{new_groups} ({pass_saved} saved)")

        if pass_saved == 0:
            break

    final_groups = count_blend_groups(working_slots)
    log(f"Done! {initial_groups} \u2192 {final_groups} "
        f"({total_saved} groups saved)")

    test_data = copy.deepcopy(spine_data)
    test_data["slots"] = working_slots
    return {"success": True, "modified_data": test_data,
            "original_groups": initial_groups,
            "optimized_groups": final_groups,
            "moved_slots": _moved_names(original_slots, working_slots),
            "unmovable_slots": [],
            "message": f"Reduced {initial_groups} \u2192 {final_groups}"}


# ---------------------------------------------------------------------------
# UI Tab
# ---------------------------------------------------------------------------

class DrawOrderOptimizerTab:
    def __init__(self, tabs: QTabWidget, get_config, on_modified=None):
        self._get_config = get_config
        self._on_modified = on_modified
        self._tabs = tabs

        self._page = QWidget()
        tabs.addTab(self._page, tr("draw_order.tab"))
        layout = QVBoxLayout(self._page)
        layout.setContentsMargins(5, 5, 5, 5)

        # Info
        self._info = QLabel(tr("draw_order.info"))
        self._info.setWordWrap(True)
        layout.addWidget(self._info)

        # Stats
        self._stats = QLabel(tr("draw_order.default_stats"))
        self._stats.setStyleSheet("font-weight: bold; color: #6ec072;")
        layout.addWidget(self._stats)

        # Slot order tree
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels([
            tr("draw_order.tree.index"),
            tr("draw_order.tree.slot"),
            tr("draw_order.tree.blend"),
            tr("draw_order.tree.group"),
        ])
        self._tree.header().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self._tree, 1)

        # Controls
        btn_row = QHBoxLayout()

        self._optimize_btn = QPushButton(tr("draw_order.optimize_btn"))
        self._optimize_btn.setProperty("role", "primary")
        self._optimize_btn.setEnabled(False)
        self._optimize_btn.clicked.connect(self._optimize)
        btn_row.addWidget(self._optimize_btn)


        btn_row.addWidget(QLabel(tr("draw_order.tolerance_label")))
        self._tolerance_spin = QSpinBox()
        self._tolerance_spin.setRange(0, 20)
        self._tolerance_spin.setValue(5)
        self._tolerance_spin.setToolTip(tr("draw_order.tolerance_tip"))
        btn_row.addWidget(self._tolerance_spin)

        btn_row.addStretch()
        layout.addLayout(btn_row)

        # Log output
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(200)
        font = QFont("Menlo, Consolas, monospace", 11)
        self._log.setFont(font)
        layout.addWidget(self._log)

        language_changed.connect(self._retranslate)

    def _retranslate(self):
        idx = self._tabs.indexOf(self._page)
        if idx >= 0:
            self._tabs.setTabText(idx, tr("draw_order.tab"))
        self._info.setText(tr("draw_order.info"))
        self._stats.setText(tr("draw_order.default_stats"))
        self._optimize_btn.setText(tr("draw_order.optimize_btn"))
        self._tree.setHeaderLabels([
            tr("draw_order.tree.index"),
            tr("draw_order.tree.slot"),
            tr("draw_order.tree.blend"),
            tr("draw_order.tree.group"),
        ])

    def _log_line(self, text: str, color: str = "#cccccc"):
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cursor = self._log.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(text + "\n", fmt)
        self._log.setTextCursor(cursor)
        self._log.ensureCursorVisible()
        QApplication.processEvents()

    def _analyze(self):
        json_path = self._get_config("json")
        if not json_path or not os.path.isfile(json_path):
            return

        try:
            spine_data = load_spine_json(json_path)
        except Exception:
            return

        analysis = analyze_draw_order(spine_data)
        self._tree.clear()

        # Populate tree -- show current slot order with blend groups
        group_idx = 0
        prev_blend = None
        for i, slot in enumerate(analysis["slots"]):
            blend = slot.get("blend", "normal")
            if blend != prev_blend:
                group_idx += 1
                prev_blend = blend
            item = QTreeWidgetItem(self._tree, [
                str(i), slot.get("name", ""), blend, str(group_idx),
            ])
            # Color non-normal blends
            if blend != "normal":
                for col in range(4):
                    item.setForeground(col, QColor("#e8a838"))

        cur = analysis["current_groups"]
        opt = analysis["optimal_groups"]
        has_do = analysis["has_draw_order_timelines"]

        self._stats.setText(tr("draw_order.stats",
                               total=len(analysis["slots"]),
                               current=cur,
                               optimal=opt))

        if has_do:
            self._log_line(tr("draw_order.warn_timelines"), "#e8a838")

        self._optimize_btn.setEnabled(analysis["can_optimize"])

    def _optimize(self):
        json_path = self._get_config("json")
        if not json_path or not os.path.isfile(json_path):
            QMessageBox.critical(None, tr("err.title"), tr("err.no_json"))
            return

        try:
            spine_data = load_spine_json(json_path)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"),
                                 tr("err.parse_json", error=e))
            return

        analysis = analyze_draw_order(spine_data)
        if not analysis["can_optimize"]:
            return

        if QMessageBox.question(
            None, tr("confirm.title"),
            tr("draw_order.confirm",
               current=analysis["current_groups"],
               optimal=analysis["optimal_groups"]),
        ) != QMessageBox.Yes:
            return

        self._log.clear()
        fps = 30
        tolerance = self._tolerance_spin.value()

        # Load textures
        atlas_path = self._get_config("atlas")
        textures = None
        if atlas_path and os.path.isfile(atlas_path):
            try:
                textures = load_atlas_textures(atlas_path)
                self._log_line(f"Loaded {len(textures)} textures.", "#6ec072")
            except Exception:
                pass
        if not textures:
            self._log_line("No atlas \u2014 optimising without visual check.", "#e8a838")

        self._optimize_btn.setEnabled(False)
        QApplication.processEvents()

        try:
            result = optimize_draw_order(
                spine_data,
                fps=fps,
                tolerance=tolerance,
                textures=textures,
                on_log=lambda msg: (
                    self._log_line(msg, "#6ec072"),
                    QApplication.processEvents(),
                ),
            )
        except Exception as e:
            self._log_line(f"Error: {e}", "#ff6666")
            import traceback
            self._log_line(traceback.format_exc(), "#ff6666")
            self._optimize_btn.setEnabled(True)
            return

        if result["success"] and "modified_data" in result:
            modified = result["modified_data"]
            orig_groups = result["original_groups"]
            final_groups = result["optimized_groups"]

            if final_groups < orig_groups:
                # Save with backup
                import shutil
                backup = json_path + ".backup"
                try:
                    shutil.copy2(json_path, backup)
                    save_spine_json(json_path, modified)
                except Exception as e:
                    QMessageBox.critical(None, tr("err.title"),
                                         tr("err.save_json", error=e))
                    self._optimize_btn.setEnabled(True)
                    return

                self._log_line(
                    tr("draw_order.done",
                       orig=orig_groups,
                       final=final_groups,
                       moved=len(result.get("moved_slots", [])),
                       skipped=len(result.get("unmovable_slots", [])),
                       backup=backup),
                    "#6ec072",
                )

                QMessageBox.information(
                    None, tr("done.title"),
                    tr("draw_order.done",
                       orig=orig_groups,
                       final=final_groups,
                       moved=len(result.get("moved_slots", [])),
                       skipped=len(result.get("unmovable_slots", [])),
                       backup=backup),
                )

                if self._on_modified:
                    self._on_modified()
                else:
                    self._analyze()
            else:
                self._log_line("No improvement possible.", "#e8a838")
        else:
            self._log_line(
                result.get("message", "Optimisation failed"), "#ff6666",
            )

        self._optimize_btn.setEnabled(True)
