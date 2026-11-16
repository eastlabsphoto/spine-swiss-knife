"""QPainter off-screen rendering and frame comparison for draw-order verification."""

from __future__ import annotations

import math
import os
import shutil
import tempfile

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
from .spine_json import (
    normalize_skins, is_weighted, resolve_weighted_vertices,
    get_draw_order_timeline,
)

np = draw_order_core.np
HAS_NUMPY = draw_order_core.HAS_NUMPY

MAX_CANVAS_SIDE = 8192
BBOX_SAMPLE_FPS = 10


class RenderError(RuntimeError):
    """Rendering for verification failed (e.g. image allocation)."""


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
    bone_tx_list: list | None = None,
) -> tuple[float, float, float, float] | None:
    """Compute AABB for a mesh attachment from its vertices.

    Weighted meshes (``len(vertices) > len(uvs)``) are resolved through
    *bone_tx_list* (world transforms indexed by global bone index).
    """
    verts = att_data.get("vertices", [])
    if len(verts) < 2:
        return None

    if is_weighted(att_data):
        if not bone_tx_list:
            return None
        world_pts = resolve_weighted_vertices(verts, bone_tx_list)
        return _points_aabb(world_pts)

    # Simple mesh: pairs of (x, y)
    if deform_deltas and len(deform_deltas) == len(verts):
        verts = [v + d for v, d in zip(verts, deform_deltas)]

    world = []
    for i in range(0, len(verts) - 1, 2):
        bx, by = verts[i], verts[i + 1]
        world.append(bone_tx.a * bx + bone_tx.b * by + bone_tx.worldX)
        world.append(bone_tx.c * bx + bone_tx.d * by + bone_tx.worldY)
    return _points_aabb(world)


def _points_aabb(flat: list) -> tuple[float, float, float, float] | None:
    xs = flat[0::2]
    ys = flat[1::2]
    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


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


def render_skin_names(spine_data) -> list:
    """Skins to render/verify: every skin present (``default`` first).

    Falls back to ``["default"]`` when the skeleton has no skins at all.
    """
    skins = normalize_skins(spine_data.get("skins", {}))
    names = list(skins.keys())
    if "default" in names:
        names.remove("default")
        names.insert(0, "default")
    return names or ["default"]


def _pose_at(bones, ik, anim_state, t):
    """(world transforms, slot_states, deform_states) of *anim_state* at *t*."""
    bone_overrides, slot_states, deform_states = anim_state.evaluate(t)
    world = solve_world_transforms(bones, bone_overrides)
    if ik:
        solve_ik_constraints(bones, world, ik)
    return world, slot_states, deform_states


def _item_aabb(item, world):
    """AABB (world space) of a region/mesh draw-list item, else None."""
    t = item.get("type")
    if t in ("clip", "clip_end_marker"):
        return None
    if t == "mesh":
        verts = item.get("vertices") or []
        if item.get("world_space"):
            return _points_aabb(list(verts))
        bt = world.get(item.get("bone"))
        if bt is None:
            return None
        pts = []
        for i in range(0, len(verts) - 1, 2):
            pts.append(bt.a * verts[i] + bt.b * verts[i + 1] + bt.worldX)
            pts.append(bt.c * verts[i] + bt.d * verts[i + 1] + bt.worldY)
        return _points_aabb(pts)
    bt = world.get(item.get("bone"))
    if bt is None:
        return None
    return _slot_aabb(item.get("att_data") or {}, bt)


def _compute_skeleton_bbox(spine_data, textures=None, padding=0.2,
                           sample_fps=BBOX_SAMPLE_FPS,
                           max_side=MAX_CANVAS_SIDE, on_log=None):
    """Bounding box over the setup pose and every animation, for every skin.

    Animations are sampled at *sample_fps*. Returns (x, y, width, height) in
    Spine world coordinates including *padding* on every side, clamped to
    *max_side* pixels per side (a log line is emitted when clamped).
    """
    bones = spine_data.get("bones", [])
    ik = spine_data.get("ik", [])
    skins = normalize_skins(spine_data.get("skins", {}))
    slots = spine_data.get("slots", [])
    skin_names = render_skin_names(spine_data)

    acc = [float("inf"), float("inf"), float("-inf"), float("-inf")]

    def add(aabb):
        if aabb is None:
            return
        acc[0] = min(acc[0], aabb[0])
        acc[1] = min(acc[1], aabb[1])
        acc[2] = max(acc[2], aabb[2])
        acc[3] = max(acc[3], aabb[3])

    # Setup pose (geometry only, no textures needed)
    world = solve_world_transforms(bones)
    if ik:
        solve_ik_constraints(bones, world, ik)
    bone_tx_list = [world.get(b.get("name")) for b in bones]
    for skin_name in skin_names:
        for slot in slots:
            att = _get_slot_attachment(slot["name"], slot, skins,
                                       active_skin=skin_name)
            bone = world.get(slot.get("bone", ""))
            if att is None or bone is None:
                continue
            att_type = att.get("type", "region")
            if att_type in ("region", None):
                add(_slot_aabb(att, bone))
            elif att_type == "mesh":
                add(_mesh_aabb(att, bone, None, bone_tx_list))

    # Sampled poses (draw lists) over every animation and skin
    if textures:
        def add_pose(w, ss, ds):
            for skin_name in skin_names:
                for item in build_draw_list(
                        spine_data, w, textures, slot_states=ss,
                        deform_states=ds, active_skin=skin_name):
                    add(_item_aabb(item, w))

        add_pose(world, None, None)
        for anim_data in (spine_data.get("animations") or {}).values():
            if not isinstance(anim_data, dict):
                continue
            state = AnimationState(spine_data, anim_data)
            for t in draw_order_core.sample_times(state.duration, sample_fps):
                w, ss, ds = _pose_at(bones, ik, state, t)
                add_pose(w, ss, ds)

    if acc[0] == float("inf"):
        return (-512, -512, 1024, 1024)

    w = acc[2] - acc[0]
    h = acc[3] - acc[1]
    pad_x = w * padding
    pad_y = h * padding
    x, y = acc[0] - pad_x, acc[1] - pad_y
    w, h = max(w + 2 * pad_x, 1.0), max(h + 2 * pad_y, 1.0)

    if w > max_side or h > max_side:
        cx, cy = x + w / 2.0, y + h / 2.0
        nw, nh = min(w, max_side), min(h, max_side)
        if on_log:
            on_log("Render canvas clamped from %dx%d to %dx%d px"
                   % (math.ceil(w), math.ceil(h), nw, nh))
        x, y, w, h = cx - nw / 2.0, cy - nh / 2.0, nw, nh
    return (x, y, w, h)


# ==========================================================================
# QPainter off-screen rendering
# ==========================================================================

_BLEND_MODES = {
    "additive": QPainter.CompositionMode_Plus,
    "multiply": QPainter.CompositionMode_Multiply,
    "screen": QPainter.CompositionMode_Screen,
}


_CLIP_TO_END = "\0end-of-draw-order"


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
        world_space = item.get("world_space", False)
        bt = world_transforms.get(item["bone"])
        if bt is None and not world_space:
            return clip_end, active_clip_path
        verts = item["vertices"]
        points = []
        for vi in range(0, len(verts) - 1, 2):
            lx, ly = verts[vi], verts[vi + 1]
            if world_space:
                wx, wy = lx, ly
            else:
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
        # No ``end`` clips to the end of the draw order: keep the clip
        # active (sentinel) until the end of the frame.
        clip_end = item.get("clip_end") or _CLIP_TO_END
        return clip_end, active_clip_path

    # -- mesh attachment --
    if item_type == "mesh":
        world_space = item.get("world_space", False)
        bt = world_transforms.get(item["bone"])
        if bt is None and not world_space:
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
        for vi in range(0, len(verts) - 1, 2):
            lx, ly = verts[vi], verts[vi + 1]
            if world_space:
                wx, wy = lx, ly
            else:
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
    Raises :class:`RenderError` when the image cannot be allocated.
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
    if image.isNull():
        raise RenderError(
            "Could not allocate a %dx%d render image (out of memory)"
            % (img_w, img_h))
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


def animation_duration(spine_data, anim_name) -> float:
    """Duration used for sampling (0 for the setup pose / unknown names)."""
    if anim_name is None:
        return 0.0
    anim_data = (spine_data.get("animations") or {}).get(anim_name)
    if not anim_data:
        return 0.0
    return AnimationState(spine_data, anim_data).duration


def iter_animation_frames(spine_data, textures, slot_order, anim_name,
                          fps, bbox, skins=None, output_dir=None,
                          on_log=None):
    """Yield ``(index, QImage)`` for an animation, one frame at a time.

    Frames are produced skin by skin (``skins`` defaults to every skin of
    *spine_data*), so the index is stable between two renders of equivalent
    data. The last sample is always exactly at the animation duration.
    ``anim_name`` None renders one setup-pose frame per skin.
    """
    bones = spine_data.get("bones", [])
    ik = spine_data.get("ik", [])
    skin_list = list(skins) if skins else render_skin_names(spine_data)

    def save(img, directory, name):
        if not output_dir:
            return
        try:
            os.makedirs(directory, exist_ok=True)
            if not img.save(os.path.join(directory, name)):
                raise OSError("QImage.save returned False")
        except Exception as exc:  # pragma: no cover - disk problems
            if on_log:
                on_log("Could not save debug image %s: %s" % (name, exc))

    index = 0
    if anim_name is None:
        world = solve_world_transforms(bones)
        if ik:
            solve_ik_constraints(bones, world, ik)
        for skin in skin_list:
            img = render_frame(spine_data, textures, world,
                               slot_order=slot_order, bbox=bbox,
                               active_skin=skin)
            if output_dir:
                save(img, os.path.join(output_dir, "setup_pose"),
                     "%s_%04d.png" % (draw_order_core.sanitize_dir_name(skin), index))
            yield index, img
            index += 1
        return

    anim_data = (spine_data.get("animations") or {}).get(anim_name)
    if not anim_data:
        return

    anim_state = AnimationState(spine_data, anim_data)
    duration = anim_state.duration
    if duration <= 0:
        return
    times = draw_order_core.sample_times(duration, fps)
    do_keys = get_draw_order_timeline(anim_data)
    anim_dir = None
    if output_dir:
        anim_dir = os.path.join(output_dir,
                                draw_order_core.sanitize_dir_name(anim_name))

    for skin in skin_list:
        for fi, t in enumerate(times):
            if fi % 5 == 0:
                QApplication.processEvents()
            world, slot_states, deform_states = _pose_at(
                bones, ik, anim_state, t)
            if do_keys:
                effective_order = _evaluate_draw_order(slot_order, do_keys, t)
            else:
                effective_order = slot_order
            img = render_frame(spine_data, textures, world,
                               slot_states=slot_states,
                               deform_states=deform_states,
                               slot_order=effective_order,
                               bbox=bbox, active_skin=skin)
            if anim_dir:
                save(img, anim_dir, "%04d.png" % index)
            yield index, img
            index += 1


def render_animation_sequence(spine_data, textures, slot_order, anim_name,
                               fps, bbox, output_dir=None,
                               active_skin=None, on_log=None):
    """Render an animation as a list of QImages, optionally saving PNGs.

    If anim_name is None, renders one setup-pose frame. With ``active_skin``
    None every skin is rendered (skin after skin); otherwise only that skin.
    The whole sequence is kept in memory: prefer :func:`iter_animation_frames`
    for long animations. Returns list of QImages.
    """
    skins = [active_skin] if active_skin else None
    return [img for _i, img in iter_animation_frames(
        spine_data, textures, slot_order, anim_name, fps, bbox,
        skins=skins, output_dir=output_dir, on_log=on_log)]


# ==========================================================================
# QImage comparison
# ==========================================================================

def _image_array(img):
    """(h, w, 4) uint8 view of a QImage, or None without numpy / bits."""
    if not HAS_NUMPY or img is None or img.isNull():
        return None
    ptr = img.constBits()
    if ptr is None:
        return None
    w, h = img.width(), img.height()
    arr = np.frombuffer(ptr, dtype=np.uint8)
    if arr.size != h * w * 4:
        arr = arr[:h * img.bytesPerLine()].reshape(h, img.bytesPerLine())[:, :w * 4]
    return arr.reshape(h, w, 4)


def _is_blank(img) -> bool:
    """True when every pixel of *img* is fully transparent."""
    arr = _image_array(img)
    if arr is not None:
        return not bool(arr[:, :, 3].any())
    blank = QImage(img.size(), img.format())
    blank.fill(Qt.transparent)
    return img == blank


def _qimages_match(img_a, img_b, tolerance=5, threshold=0.02,
                   _log_diffs=None):
    """Compare two QImages. Uses numpy if available for speed.

    A null (unallocated) image never matches anything.
    """
    if img_a is None or img_b is None or img_a.isNull() or img_b.isNull():
        if _log_diffs is not None:
            _log_diffs.append("render image is null (allocation failed)")
        return False
    if img_a.size() != img_b.size():
        return False

    if tolerance == 0:
        return img_a == img_b

    if HAS_NUMPY:
        arr_a = _image_array(img_a)
        arr_b = _image_array(img_b)
        if arr_a is None or arr_b is None:
            return img_a == img_b
        match, stats = draw_order_core.compare_rgba_arrays(
            arr_a, arr_b, tolerance=tolerance, threshold=threshold)
        if not match and _log_diffs is not None:
            _log_diffs.append(
                f"max_diff={stats['max_diff']}, "
                f"bad_pixels={stats['bad_pixels']} of "
                f"{stats['visible_pixels']} visible "
                f"({stats['pct_bad']:.2f}%, worst 64px cell "
                f"{stats.get('worst_cell_pct', 0.0):.0f}%), "
                f"tolerance={tolerance}")
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
# Baseline frame storage (memory or temp-directory spill)
# ==========================================================================

class _DiskFrames:
    """Sequence of QImages stored as raw files in a directory."""

    def __init__(self, directory, label):
        self._dir = directory
        self._label = label
        self._meta = []  # (path, w, h, bytes_per_line, format)

    def append(self, img):
        path = os.path.join(self._dir, "%s_%05d.raw" % (self._label, len(self._meta)))
        with open(path, "wb") as f:
            f.write(bytes(img.constBits()))
        self._meta.append((path, img.width(), img.height(),
                           img.bytesPerLine(), img.format()))

    def __len__(self):
        return len(self._meta)

    def __getitem__(self, i):
        path, w, h, bpl, fmt = self._meta[i]
        with open(path, "rb") as f:
            data = f.read()
        return QImage(data, w, h, bpl, fmt).copy()

    def __iter__(self):
        for i in range(len(self._meta)):
            yield self[i]


class BaselineFrames(dict):
    """``{anim_name: frames}`` of the cached original renders.

    ``frames`` is a list of QImages (memory mode) or a disk-backed sequence
    (spill mode, ``spill_dir`` given). ``any_content`` tells whether any frame
    contains a visible pixel; call :meth:`cleanup` to remove spilled files.
    """

    def __init__(self, spill_dir=None):
        super().__init__()
        self.spill_dir = spill_dir
        self.any_content = False
        self.fps = None

    def ensure(self, anim_name):
        frames = self.get(anim_name)
        if frames is None:
            if self.spill_dir:
                frames = _DiskFrames(self.spill_dir, "f%d" % len(self))
            else:
                frames = []
            self[anim_name] = frames
        return frames

    def add(self, anim_name, img):
        frames = self.ensure(anim_name)
        if img.isNull():
            raise RenderError("Render image is null (allocation failed)")
        if not self.any_content and not _is_blank(img):
            self.any_content = True
        frames.append(img)

    def cleanup(self):
        if self.spill_dir:
            shutil.rmtree(self.spill_dir, ignore_errors=True)
            self.spill_dir = None


def make_spill_dir() -> str:
    return tempfile.mkdtemp(prefix="ssk_draworder_")


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
                            anim_names, fps, bbox, output_dir=None,
                            spill_dir=None, on_log=None):
    """Render all animations (all skins) into a :class:`BaselineFrames`.

    Frames are produced one at a time and stored in memory, or in
    *spill_dir* when given (the caller removes it via ``cleanup()``).
    """
    result = BaselineFrames(spill_dir=spill_dir)
    result.fps = fps
    for anim_name in anim_names:
        result.ensure(anim_name)  # entry exists even without frames
        for _i, img in iter_animation_frames(
                spine_data, textures, slot_order, anim_name,
                fps, bbox, output_dir=output_dir, on_log=on_log):
            result.add(anim_name, img)
    return result


def _compare_with_original(spine_data, textures, original_images,
                            candidate_slots, anim_names, fps, bbox,
                            tolerance, threshold=0.02, on_log=None,
                            early_exit=True, debug_dir=None,
                            moved_slots=None, slot_visibility=None):
    """Render candidate and compare against cached original images.

    The candidate is rendered frame by frame (streamed, never held in
    memory). Draw-order timeline offsets of *spine_data* are re-encoded for
    the candidate slot order, so animated draw orders keep their absolute
    order. Every skin is compared.

    When *debug_dir* is set, saves frame pairs (original + candidate)
    for frames that differ, so the user can inspect visually.

    When *moved_slots* (set of slot names) and *slot_visibility* (dict
    mapping anim name -> frozenset of visible slot names) are both
    provided, animations where none of the moved slots are visible are
    skipped — they cannot be affected by the reorder.

    Returns ``(all_match, list_of_diff_anim_names)``.
    """
    cand_data = draw_order_core.build_reordered_data(spine_data, candidate_slots)
    diffs = []
    for anim_name in anim_names:
        # Skip animations unaffected by this move
        if moved_slots and slot_visibility:
            visible = slot_visibility.get(anim_name, frozenset())
            if not moved_slots & visible:
                continue
        QApplication.processEvents()
        imgs_orig = original_images.get(anim_name, [])
        details = []
        diff_frames = []
        n_cand = 0
        limit = 5 if debug_dir else 1
        for idx, img in iter_animation_frames(
                cand_data, textures, candidate_slots, anim_name, fps, bbox,
                on_log=on_log):
            n_cand += 1
            if idx >= len(imgs_orig):
                diff_frames.append(idx)
                details.append("candidate has more frames than the baseline")
                break
            orig = imgs_orig[idx]
            if not _qimages_match(orig, img, tolerance, threshold,
                                  _log_diffs=details):
                diff_frames.append(idx)
                if debug_dir:
                    _save_debug_pair(debug_dir, anim_name, idx, orig, img,
                                     on_log)
                if len(diff_frames) >= limit:
                    break
        if not diff_frames and n_cand != len(imgs_orig):
            diff_frames.append(min(n_cand, len(imgs_orig)))
            details.append("frame count differs (%d vs %d)"
                           % (n_cand, len(imgs_orig)))
        if diff_frames:
            label = anim_name or "setup_pose"
            diffs.append(label)
            if on_log and details:
                on_log(f"      {label}: {details[0]}")
            if early_exit:
                return False, diffs
    return len(diffs) == 0, diffs


def _save_debug_pair(debug_dir, anim_name, fi, orig, cand, on_log=None):
    d = os.path.join(debug_dir, draw_order_core.sanitize_dir_name(anim_name))
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as exc:
        if on_log:
            on_log(f"Could not create debug directory {d}: {exc}")
        return
    for suffix, img in (("original", orig), ("candidate", cand)):
        path = os.path.join(d, f"{fi:04d}_{suffix}.png")
        if not img.save(path):
            if on_log:
                on_log(f"Could not save debug image {path}")
