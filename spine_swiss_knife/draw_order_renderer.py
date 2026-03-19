"""QPainter off-screen rendering and frame comparison for draw-order verification."""

import math
import os

from PySide6.QtGui import (
    QImage, QPainter, QTransform, QPainterPath, QPolygonF, QColor,
)
from PySide6.QtCore import QPointF, Qt
from PySide6.QtWidgets import QApplication

from . import draw_order_core
from .draw_order_core import count_blend_groups
from .spine_viewer_animation import (
    BoneTransform, solve_world_transforms, AnimationState,
    _evaluate_draw_order, solve_ik_constraints,
    build_draw_list, _affine_from_triangles, load_atlas_textures,
)
from .spine_json import normalize_skins

np = draw_order_core.np
HAS_NUMPY = draw_order_core.HAS_NUMPY


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


def _render_item(painter, item, world_transforms, cx, cy, zoom,
                 clip_end, active_clip_path):
    """Render a single draw-list item with *painter* using normal compositing.

    Handles clip, clip_end_marker, mesh, and region item types.
    Returns updated ``(clip_end, active_clip_path)``.
    """
    item_type = item.get("type")

    # -- clip end marker --
    if item_type == "clip_end_marker":
        if clip_end:
            painter.setClipping(False)
            clip_end = ""
            active_clip_path = None
        return clip_end, active_clip_path

    # -- clipping attachment --
    if item_type == "clip":
        bt = world_transforms.get(item["bone"])
        if bt is None:
            return clip_end, active_clip_path
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
        return clip_end, active_clip_path

    # -- mesh attachment --
    if item_type == "mesh":
        bt = world_transforms.get(item["bone"])
        if bt is None:
            return clip_end, active_clip_path

        verts = item["vertices"]
        uvs = item["uvs"]
        tris = item["triangles"]
        pixmap = item["pixmap"]
        pw, ph = pixmap.width(), pixmap.height()
        color = item.get("color")

        if color:
            painter.setOpacity(color[3])

        screen_pts = []
        for vi in range(0, len(verts), 2):
            lx, ly = verts[vi], verts[vi + 1]
            wx = bt.a * lx + bt.b * ly + bt.worldX
            wy = bt.c * lx + bt.d * ly + bt.worldY
            screen_pts.append((cx + wx * zoom, cy - wy * zoom))

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

        if color:
            painter.setOpacity(1.0)
        return clip_end, active_clip_path

    # -- region attachment (default) --
    pixmap = item.get("pixmap")
    if pixmap is None or pixmap.isNull():
        return clip_end, active_clip_path
    bt = world_transforms.get(item["bone"])
    if bt is None:
        return clip_end, active_clip_path
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

    painter.drawPixmap(0, 0, pixmap)

    painter.resetTransform()
    if color:
        painter.setOpacity(1.0)

    return clip_end, active_clip_path


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
    clip_end = ""
    active_clip_path = None

    for item in draw_list:
        blend_name = item.get("blend", "normal")
        qp_blend = _BLEND_MODES.get(blend_name)
        if qp_blend is not None:
            painter.setCompositionMode(qp_blend)
        clip_end, active_clip_path = _render_item(
            painter, item, world_transforms, cx, cy, zoom,
            clip_end, active_clip_path)
        if qp_blend is not None:
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)

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
        if fi % 5 == 0:
            QApplication.processEvents()

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

def _qimages_match(img_a, img_b, tolerance=5, threshold=0.02,
                   _log_diffs=None):
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
        match, stats = draw_order_core.compare_rgba_arrays(
            arr_a, arr_b, tolerance=tolerance, threshold=threshold)
        if not match and _log_diffs is not None:
            _log_diffs.append(
                f"max_diff={stats['max_diff']}, "
                f"bad_pixels={stats['bad_pixels']} of "
                f"{stats['visible_pixels']} visible "
                f"({stats['pct_bad']:.2f}%), tolerance={tolerance}")
        return match

    # Fallback: exact comparison only
    return img_a == img_b


def compare_sequences(images_a, images_b, tolerance=5,
                      threshold=0.02, diff_details=None):
    """Compare two lists of QImages frame by frame.
    Returns (all_match, list_of_differing_frame_indices).
    When *diff_details* is a list, appends diff stats strings.
    """
    if len(images_a) != len(images_b):
        return False, list(range(max(len(images_a), len(images_b))))

    diffs = []
    for i, (a, b) in enumerate(zip(images_a, images_b)):
        if not _qimages_match(a, b, tolerance, threshold,
                              _log_diffs=diff_details):
            diffs.append(i)
            if len(diffs) >= 10:  # Early exit
                break
    return len(diffs) == 0, diffs


# ==========================================================================
# Optimisation engine helpers
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
                            tolerance, threshold=0.02, on_log=None,
                            early_exit=True, debug_dir=None):
    """Render candidate and compare against cached original images.

    When *debug_dir* is set, saves frame pairs (original + candidate)
    for frames that differ, so the user can inspect visually.

    Returns ``(all_match, list_of_diff_anim_names)``.
    """
    diffs = []
    for anim_name in anim_names:
        QApplication.processEvents()
        imgs_orig = original_images.get(anim_name, [])
        imgs_cand = render_animation_sequence(
            spine_data, textures, candidate_slots, anim_name,
            fps, bbox)
        details = []
        match, diff_frames = compare_sequences(
            imgs_orig, imgs_cand, tolerance, threshold,
            diff_details=details)
        if not match:
            label = anim_name or "setup_pose"
            diffs.append(label)
            if on_log and details:
                on_log(f"      {label}: {details[0]}")
            # Save debug PNGs for differing frames
            if debug_dir and diff_frames:
                anim_label = anim_name or "setup_pose"
                for fi in diff_frames[:5]:  # Max 5 per animation
                    if fi < len(imgs_orig) and fi < len(imgs_cand):
                        d = os.path.join(debug_dir, anim_label)
                        os.makedirs(d, exist_ok=True)
                        imgs_orig[fi].save(
                            os.path.join(d, f"{fi:04d}_original.png"))
                        imgs_cand[fi].save(
                            os.path.join(d, f"{fi:04d}_candidate.png"))
            if early_exit:
                return False, diffs
    return len(diffs) == 0, diffs
