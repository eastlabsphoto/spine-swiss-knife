"""Spine Viewer tab — render skeleton with animation playback using QOpenGLWidget + QPainter."""

import math
import os
import platform
import subprocess

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QTabWidget, QMessageBox, QComboBox, QSlider,
    QScrollArea, QFrame, QListWidget,
)
from PySide6.QtCore import Qt, QPointF, QTimer, QElapsedTimer, Signal
from PySide6.QtGui import (
    QPixmap, QPainter, QPainterPath, QTransform, QColor,
    QWheelEvent, QMouseEvent, QPolygonF,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from .spine_json import load_spine_json, normalize_skins, attachment_image_key
from .texture_unpacker import parse_spine_atlas
from .style import CANVAS_BG, SUBTEXT
from .i18n import tr, language_changed


# ==========================================================================
# Helpers
# ==========================================================================

def _color_to_floats(color_hex: str) -> tuple[float, float, float, float]:
    """Parse "RRGGBBAA" hex string to (r, g, b, a) floats in 0..1."""
    color_hex = color_hex.lower()
    r = int(color_hex[0:2], 16) / 255.0
    g = int(color_hex[2:4], 16) / 255.0
    b = int(color_hex[4:6], 16) / 255.0
    a = int(color_hex[6:8], 16) / 255.0 if len(color_hex) >= 8 else 1.0
    return (r, g, b, a)


# ==========================================================================
# Timeline Interpolation Engine
# ==========================================================================

def _bezier_interpolate(curve: list, alpha: float) -> float:
    """Evaluate cubic bezier curve weight for a given time alpha.

    curve = [cx1, cy1, cx2, cy2] — control points for cubic bezier.
    Binary search for t where bezier_x(t) ~ alpha, returns bezier_y(t).
    """
    cx1, cy1, cx2, cy2 = curve[0], curve[1], curve[2], curve[3]
    lo, hi = 0.0, 1.0
    for _ in range(20):
        t = (lo + hi) * 0.5
        u = 1.0 - t
        x = 3.0 * u * u * t * cx1 + 3.0 * u * t * t * cx2 + t * t * t
        if x < alpha:
            lo = t
        else:
            hi = t
    t = (lo + hi) * 0.5
    u = 1.0 - t
    return 3.0 * u * u * t * cy1 + 3.0 * u * t * t * cy2 + t * t * t


def _get_bezier_alpha(curve, alpha: float) -> float:
    """Get interpolation weight from curve specification."""
    if curve == "stepped":
        return -1.0  # sentinel for stepped
    if isinstance(curve, list) and len(curve) >= 4:
        return _bezier_interpolate(curve, alpha)
    return alpha  # linear


def _interpolate_timeline(keys: list, time: float, fields: tuple, defaults: dict) -> dict:
    """Interpolate numeric fields across a keyframe timeline at given time.

    Supports linear (default), stepped, and cubic bezier interpolation.
    """
    n = len(keys)
    if n == 0:
        return dict(defaults)

    if time <= keys[0].get("time", 0):
        return {f: keys[0].get(f, defaults[f]) for f in fields}

    if time >= keys[-1].get("time", 0):
        return {f: keys[-1].get(f, defaults[f]) for f in fields}

    # Binary search: find lo where keys[lo].time <= time < keys[lo+1].time
    lo, hi = 0, n - 2
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if keys[mid].get("time", 0) <= time:
            lo = mid
        else:
            hi = mid - 1

    left = keys[lo]
    right = keys[lo + 1]
    dt = right.get("time", 0) - left.get("time", 0)
    alpha = (time - left.get("time", 0)) / dt if dt > 0 else 0.0

    curve = left.get("curve")
    if curve == "stepped":
        return {f: left.get(f, defaults[f]) for f in fields}
    if isinstance(curve, list) and len(curve) >= 4:
        alpha = _bezier_interpolate(curve, alpha)

    result = {}
    for f in fields:
        v0 = float(left.get(f, defaults[f]))
        v1 = float(right.get(f, defaults[f]))
        result[f] = v0 + (v1 - v0) * alpha
    return result


def _interpolate_color(keys: list, time: float) -> tuple[float, float, float, float]:
    """Interpolate color timeline at given time. Returns (r, g, b, a) floats."""
    if not keys:
        return (1.0, 1.0, 1.0, 1.0)

    if time <= keys[0].get("time", 0):
        return _color_to_floats(keys[0].get("color", "ffffffff"))

    if time >= keys[-1].get("time", 0):
        return _color_to_floats(keys[-1].get("color", "ffffffff"))

    lo, hi = 0, len(keys) - 2
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if keys[mid].get("time", 0) <= time:
            lo = mid
        else:
            hi = mid - 1

    left = keys[lo]
    right = keys[lo + 1]
    dt = right.get("time", 0) - left.get("time", 0)
    alpha = (time - left.get("time", 0)) / dt if dt > 0 else 0.0

    curve = left.get("curve")
    if curve == "stepped":
        return _color_to_floats(left.get("color", "ffffffff"))
    if isinstance(curve, list) and len(curve) >= 4:
        alpha = _bezier_interpolate(curve, alpha)

    lc = _color_to_floats(left.get("color", "ffffffff"))
    rc = _color_to_floats(right.get("color", "ffffffff"))
    return tuple(l + (r - l) * alpha for l, r in zip(lc, rc))


def _interpolate_deform(keys: list, time: float) -> list[float]:
    """Interpolate deform timeline — element-wise lerp of vertex delta arrays.

    Each key has {"time": t, "vertices": [full expanded deltas], "curve": ...}.
    Returns interpolated delta array.
    """
    n = len(keys)
    if n == 0:
        return []

    if time <= keys[0]["time"]:
        return keys[0]["vertices"]

    if time >= keys[-1]["time"]:
        return keys[-1]["vertices"]

    # Binary search for surrounding keyframes
    lo, hi = 0, n - 2
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if keys[mid]["time"] <= time:
            lo = mid
        else:
            hi = mid - 1

    left = keys[lo]
    right = keys[lo + 1]
    dt = right["time"] - left["time"]
    alpha = (time - left["time"]) / dt if dt > 0 else 0.0

    curve = left.get("curve")
    if curve == "stepped":
        return left["vertices"]
    if isinstance(curve, list) and len(curve) >= 4:
        alpha = _bezier_interpolate(curve, alpha)

    lv = left["vertices"]
    rv = right["vertices"]
    return [a + (b - a) * alpha for a, b in zip(lv, rv)]


def _affine_from_triangles(src, dst) -> QTransform | None:
    """Compute affine QTransform mapping 3 src points to 3 dst points.

    src/dst: [(x0,y0), (x1,y1), (x2,y2)]
    Returns None if the source triangle is degenerate.
    """
    (sx0, sy0), (sx1, sy1), (sx2, sy2) = src
    (dx0, dy0), (dx1, dy1), (dx2, dy2) = dst
    det = sx0 * (sy1 - sy2) + sx1 * (sy2 - sy0) + sx2 * (sy0 - sy1)
    if abs(det) < 1e-10:
        return None
    inv = 1.0 / det
    # Solve for coefficients mapping src → dst_x
    m11 = (dx0 * (sy1 - sy2) + dx1 * (sy2 - sy0) + dx2 * (sy0 - sy1)) * inv
    m12 = (dx0 * (sx2 - sx1) + dx1 * (sx0 - sx2) + dx2 * (sx1 - sx0)) * inv
    tdx = (dx0 * (sx1 * sy2 - sx2 * sy1) + dx1 * (sx2 * sy0 - sx0 * sy2)
           + dx2 * (sx0 * sy1 - sx1 * sy0)) * inv
    # Solve for coefficients mapping src → dst_y
    m21 = (dy0 * (sy1 - sy2) + dy1 * (sy2 - sy0) + dy2 * (sy0 - sy1)) * inv
    m22 = (dy0 * (sx2 - sx1) + dy1 * (sx0 - sx2) + dy2 * (sx1 - sx0)) * inv
    tdy = (dy0 * (sx1 * sy2 - sx2 * sy1) + dy1 * (sx2 * sy0 - sx0 * sy2)
           + dy2 * (sx0 * sy1 - sx1 * sy0)) * inv
    return QTransform(m11, m21, m12, m22, tdx, tdy)


# ==========================================================================
# Draw Order Evaluation
# ==========================================================================

def _evaluate_draw_order(base_slots: list, draw_order_keys: list, time: float) -> list:
    """Evaluate draw order timeline, returning reordered slot list."""
    if not draw_order_keys:
        return base_slots

    active_key = None
    for key in draw_order_keys:
        if key.get("time", 0) <= time:
            active_key = key
        else:
            break

    if active_key is None:
        return base_slots

    offsets_list = active_key.get("offsets")
    if not offsets_list:
        return base_slots

    n = len(base_slots)
    slot_index = {slot["name"]: i for i, slot in enumerate(base_slots)}

    offset_map = {}
    for entry in offsets_list:
        sname = entry.get("slot", "")
        off = entry.get("offset", 0)
        if sname in slot_index:
            orig = slot_index[sname]
            offset_map[orig] = max(0, min(n - 1, orig + off))

    result = [None] * n
    used = set()
    for orig, target in offset_map.items():
        result[target] = base_slots[orig]
        used.add(target)

    unchanged = [base_slots[i] for i in range(n) if i not in offset_map]
    j = 0
    for i in range(n):
        if i not in used:
            if j < len(unchanged):
                result[i] = unchanged[j]
                j += 1

    return [s for s in result if s is not None]


# ==========================================================================
# Animation State Evaluator
# ==========================================================================

class AnimationState:
    """Evaluates animation timelines at a given time."""

    def __init__(self, spine_data: dict, anim_data: dict):
        self._bone_timelines = anim_data.get("bones", {})
        self._slot_timelines = anim_data.get("slots", {})
        self._draw_order_keys = anim_data.get("drawOrder",
                                               anim_data.get("draworder", []))

        # Pre-process deform timelines
        self._deform_timelines: dict[tuple[str, str], list] = {}
        skins = normalize_skins(spine_data.get("skins", {}))
        for skin_name, skin_slots in anim_data.get("deform", {}).items():
            for slot_name, slot_atts in skin_slots.items():
                for att_name, keys in slot_atts.items():
                    att = skins.get(skin_name, {}).get(slot_name, {}).get(att_name, {})
                    vc = att.get("vertexCount", 0)
                    if vc == 0:
                        vc = len(att.get("vertices", [])) // 2
                    n_floats = vc * 2

                    expanded = []
                    for key in keys:
                        full = [0.0] * n_floats
                        offset = key.get("offset", 0)
                        deltas = key.get("vertices", [])
                        for i, v in enumerate(deltas):
                            idx = offset + i
                            if idx < n_floats:
                                full[idx] = v
                        expanded.append({
                            "time": key.get("time", 0),
                            "vertices": full,
                            "curve": key.get("curve"),
                        })
                    self._deform_timelines[(slot_name, att_name)] = expanded

        self.duration = 0.0
        for section in (self._bone_timelines, self._slot_timelines):
            for _name, channels in section.items():
                for _ch, keys in channels.items():
                    if isinstance(keys, list):
                        for key in keys:
                            t = key.get("time", 0)
                            if t > self.duration:
                                self.duration = t
        for key in self._draw_order_keys:
            t = key.get("time", 0)
            if t > self.duration:
                self.duration = t
        for keys in self._deform_timelines.values():
            for key in keys:
                t = key.get("time", 0)
                if t > self.duration:
                    self.duration = t

        if self.duration <= 0:
            self.duration = 0.001

    def evaluate(self, time: float) -> tuple[dict, dict, dict]:
        """Returns (bone_overrides, slot_states, deform_states).

        bone_overrides: {bone_name: {x, y, rotation, scaleX, scaleY, shearX, shearY}}
        slot_states: {slot_name: {attachment: name|None, color: (r,g,b,a)}}
        deform_states: {(slot_name, att_name): [deformed_vertex_deltas]}
        """
        bone_overrides: dict[str, dict] = {}
        slot_states: dict[str, dict] = {}

        for bone_name, timelines in self._bone_timelines.items():
            ov = {}
            if "translate" in timelines:
                vals = _interpolate_timeline(
                    timelines["translate"], time, ("x", "y"), {"x": 0, "y": 0})
                ov["x"] = vals["x"]
                ov["y"] = vals["y"]
            if "rotate" in timelines:
                vals = _interpolate_timeline(
                    timelines["rotate"], time, ("angle",), {"angle": 0})
                ov["rotation"] = vals["angle"]
            if "scale" in timelines:
                vals = _interpolate_timeline(
                    timelines["scale"], time, ("x", "y"), {"x": 1, "y": 1})
                ov["scaleX"] = vals["x"]
                ov["scaleY"] = vals["y"]
            if "shear" in timelines:
                vals = _interpolate_timeline(
                    timelines["shear"], time, ("x", "y"), {"x": 0, "y": 0})
                ov["shearX"] = vals["x"]
                ov["shearY"] = vals["y"]
            if ov:
                bone_overrides[bone_name] = ov

        for slot_name, timelines in self._slot_timelines.items():
            state = {}
            if "attachment" in timelines:
                keys = timelines["attachment"]
                att_key = None
                for key in keys:
                    if key.get("time", 0) <= time:
                        att_key = key
                    else:
                        break
                if att_key is not None:
                    state["attachment"] = att_key.get("name")
            if "color" in timelines:
                state["color"] = _interpolate_color(timelines["color"], time)
            if state:
                slot_states[slot_name] = state

        deform_states: dict[tuple[str, str], list] = {}
        for key, keys in self._deform_timelines.items():
            deform_states[key] = _interpolate_deform(keys, time)

        return bone_overrides, slot_states, deform_states


# ==========================================================================
# Bone World Transform Solver
# ==========================================================================

class BoneTransform:
    """World transform for a single bone: position + 2x2 affine matrix."""
    __slots__ = ("worldX", "worldY", "a", "b", "c", "d")

    def __init__(self):
        self.worldX = 0.0
        self.worldY = 0.0
        self.a = 1.0
        self.b = 0.0
        self.c = 0.0
        self.d = 1.0


def solve_world_transforms(
    bones: list[dict],
    bone_overrides: dict[str, dict] | None = None,
) -> dict[str, BoneTransform]:
    """Compute world transforms for all bones.

    Animation overrides are ABSOLUTE values applied to setup pose:
      translate: setup + override (additive)
      rotate:    setup + override (additive)
      scale:     setup * override (multiplicative)
      shear:     setup + override (additive)
    """
    world: dict[str, BoneTransform] = {}

    for bone in bones:
        name = bone["name"]
        lx = bone.get("x", 0.0)
        ly = bone.get("y", 0.0)
        rotation = bone.get("rotation", 0.0)
        sx = bone.get("scaleX", 1.0)
        sy = bone.get("scaleY", 1.0)
        shear_x = bone.get("shearX", 0.0)
        shear_y = bone.get("shearY", 0.0)

        if bone_overrides and name in bone_overrides:
            ov = bone_overrides[name]
            lx += ov.get("x", 0)
            ly += ov.get("y", 0)
            rotation += ov.get("rotation", 0)
            sx *= ov.get("scaleX", 1)
            sy *= ov.get("scaleY", 1)
            shear_x += ov.get("shearX", 0)
            shear_y += ov.get("shearY", 0)

        rot_rad = math.radians(rotation + 90 + shear_y)
        cos_r = math.cos(math.radians(rotation))
        sin_r = math.sin(math.radians(rotation))
        cos_ry = math.cos(rot_rad)
        sin_ry = math.sin(rot_rad)

        la = cos_r * sx
        lb = cos_ry * sy
        lc = sin_r * sx
        ld = sin_ry * sy

        if shear_x != 0.0:
            shx_rad = math.radians(shear_x)
            cos_shx = math.cos(shx_rad)
            sin_shx = math.sin(shx_rad)
            la, lb = la + lb * sin_shx, lb * cos_shx
            lc, ld = lc + ld * sin_shx, ld * cos_shx

        bt = BoneTransform()
        parent_name = bone.get("parent")

        if parent_name and parent_name in world:
            p = world[parent_name]
            bt.worldX = p.a * lx + p.b * ly + p.worldX
            bt.worldY = p.c * lx + p.d * ly + p.worldY
            bt.a = p.a * la + p.b * lc
            bt.b = p.a * lb + p.b * ld
            bt.c = p.c * la + p.d * lc
            bt.d = p.c * lb + p.d * ld
        else:
            bt.worldX = lx
            bt.worldY = ly
            bt.a = la
            bt.b = lb
            bt.c = lc
            bt.d = ld

        world[name] = bt

    return world


# ==========================================================================
# IK Constraint Solver
# ==========================================================================

def solve_ik_constraints(
    bones: list[dict],
    world: dict[str, BoneTransform],
    constraints: list[dict],
):
    """Apply IK constraints to world transforms in-place.

    Supports 1-bone and 2-bone IK. Constraints are processed in 'order' order.
    """
    bone_map = {b["name"]: b for b in bones}
    sorted_constraints = sorted(constraints, key=lambda c: c.get("order", 0))

    for constraint in sorted_constraints:
        ik_bones = constraint.get("bones", [])
        target_name = constraint.get("target", "")
        mix = constraint.get("mix", 1.0)
        bend_positive = constraint.get("bendPositive", True)

        target_bt = world.get(target_name)
        if target_bt is None:
            continue

        tx, ty = target_bt.worldX, target_bt.worldY

        if len(ik_bones) == 1:
            _apply_ik_1bone(bone_map, world, ik_bones[0], tx, ty, mix)
        elif len(ik_bones) == 2:
            _apply_ik_2bone(bone_map, world, ik_bones[0], ik_bones[1],
                            tx, ty, mix, bend_positive)


def _apply_ik_1bone(bone_map, world, bone_name, tx, ty, mix):
    """Single-bone IK: rotate bone to point at target."""
    bt = world.get(bone_name)
    if bt is None:
        return

    # Get parent inverse to find target in bone's local space
    bone_data = bone_map.get(bone_name, {})
    parent_name = bone_data.get("parent")

    if parent_name and parent_name in world:
        p = world[parent_name]
        # Inverse of parent world transform to get local target
        det = p.a * p.d - p.b * p.c
        if abs(det) < 1e-10:
            return
        inv_det = 1.0 / det
        dx = tx - p.worldX
        dy = ty - p.worldY
        local_tx = (dx * p.d - dy * p.b) * inv_det
        local_ty = (dy * p.a - dx * p.c) * inv_det
    else:
        local_tx = tx
        local_ty = ty

    # Current bone local position
    bx = bone_data.get("x", 0.0)
    by = bone_data.get("y", 0.0)

    # Angle from bone to target in local space
    target_angle = math.degrees(math.atan2(local_ty - by, local_tx - bx))
    setup_rotation = bone_data.get("rotation", 0.0)
    rotation_delta = target_angle - setup_rotation

    # Normalize to -180..180
    rotation_delta = ((rotation_delta + 180) % 360) - 180

    # Apply mix
    final_rotation = setup_rotation + rotation_delta * mix

    # Recompute world transform with new rotation
    _recompute_bone_world(bone_map, world, bone_name, rotation_override=final_rotation)


def _apply_ik_2bone(bone_map, world, parent_name, child_name,
                    tx, ty, mix, bend_positive):
    """Two-bone IK using law of cosines."""
    parent_bt = world.get(parent_name)
    child_bt = world.get(child_name)
    if parent_bt is None or child_bt is None:
        return

    parent_data = bone_map.get(parent_name, {})
    child_data = bone_map.get(child_name, {})

    # Bone lengths from setup pose
    cx_local = child_data.get("x", 0.0)
    cy_local = child_data.get("y", 0.0)
    child_len = math.sqrt(cx_local * cx_local + cy_local * cy_local)
    if child_len < 1e-6:
        return

    # Parent bone length: distance from parent to child in world space
    parent_len = child_len  # Use child's local offset as parent length

    # Target distance from parent
    dx = tx - parent_bt.worldX
    dy = ty - parent_bt.worldY
    target_dist = math.sqrt(dx * dx + dy * dy)

    if target_dist < 1e-6:
        return

    # Clamp target distance to reachable range
    l1 = parent_len
    l2 = child_len  # grandchild or end effector — approximate

    # For 2-bone IK, l1 = parent bone length, l2 = child bone length
    # We need the child's effective length — use the distance from child to its end
    # For simplicity, use local x offset of each bone
    l1 = math.sqrt(cx_local * cx_local + cy_local * cy_local)

    # Get grandchild data for l2 (length of child bone)
    # Look for bones whose parent is child_name
    l2 = 0.0
    for b in bone_map.values():
        if isinstance(b, dict) and b.get("parent") == child_name:
            bx = b.get("x", 0.0)
            by = b.get("y", 0.0)
            d = math.sqrt(bx * bx + by * by)
            if d > l2:
                l2 = d
    if l2 < 1e-6:
        l2 = l1  # Fallback: assume equal length

    # Get parent's parent for coordinate space
    pp_name = parent_data.get("parent")

    # Target angle from parent
    if pp_name and pp_name in world:
        pp = world[pp_name]
        det = pp.a * pp.d - pp.b * pp.c
        if abs(det) < 1e-10:
            return
        inv_det = 1.0 / det
        ddx = tx - pp.worldX
        ddy = ty - pp.worldY
        local_tx = (ddx * pp.d - ddy * pp.b) * inv_det
        local_ty = (ddy * pp.a - ddx * pp.c) * inv_det
    else:
        local_tx = tx
        local_ty = ty

    pbx = parent_data.get("x", 0.0)
    pby = parent_data.get("y", 0.0)
    ddx = local_tx - pbx
    ddy = local_ty - pby
    target_dist_local = math.sqrt(ddx * ddx + ddy * ddy)
    target_angle = math.atan2(ddy, ddx)

    # Law of cosines for parent angle
    cos_angle = (l1 * l1 + target_dist_local * target_dist_local - l2 * l2) / (2 * l1 * target_dist_local) if target_dist_local > 1e-6 else 1.0
    cos_angle = max(-1.0, min(1.0, cos_angle))
    parent_ik_angle = target_angle + ((-1 if bend_positive else 1) * math.acos(cos_angle))

    parent_setup_rot = parent_data.get("rotation", 0.0)
    parent_new_rot = math.degrees(parent_ik_angle)
    parent_delta = ((parent_new_rot - parent_setup_rot + 180) % 360) - 180
    final_parent_rot = parent_setup_rot + parent_delta * mix

    _recompute_bone_world(bone_map, world, parent_name,
                          rotation_override=final_parent_rot)

    # Now solve child rotation to point at target
    _apply_ik_1bone(bone_map, world, child_name, tx, ty, mix)


def _recompute_bone_world(bone_map, world, bone_name, rotation_override=None):
    """Recompute a single bone's world transform with an optional rotation override."""
    bone = bone_map.get(bone_name)
    if bone is None:
        return

    lx = bone.get("x", 0.0)
    ly = bone.get("y", 0.0)
    rotation = rotation_override if rotation_override is not None else bone.get("rotation", 0.0)
    sx = bone.get("scaleX", 1.0)
    sy = bone.get("scaleY", 1.0)
    shear_x = bone.get("shearX", 0.0)
    shear_y = bone.get("shearY", 0.0)

    rot_rad = math.radians(rotation + 90 + shear_y)
    cos_r = math.cos(math.radians(rotation))
    sin_r = math.sin(math.radians(rotation))
    cos_ry = math.cos(rot_rad)
    sin_ry = math.sin(rot_rad)

    la = cos_r * sx
    lb = cos_ry * sy
    lc = sin_r * sx
    ld = sin_ry * sy

    if shear_x != 0.0:
        shx_rad = math.radians(shear_x)
        cos_shx = math.cos(shx_rad)
        sin_shx = math.sin(shx_rad)
        la, lb = la + lb * sin_shx, lb * cos_shx
        lc, ld = lc + ld * sin_shx, ld * cos_shx

    bt = world.get(bone_name)
    if bt is None:
        bt = BoneTransform()
        world[bone_name] = bt

    parent_name = bone.get("parent")
    if parent_name and parent_name in world:
        p = world[parent_name]
        bt.worldX = p.a * lx + p.b * ly + p.worldX
        bt.worldY = p.c * lx + p.d * ly + p.worldY
        bt.a = p.a * la + p.b * lc
        bt.b = p.a * lb + p.b * ld
        bt.c = p.c * la + p.d * lc
        bt.d = p.c * lb + p.d * ld
    else:
        bt.worldX = lx
        bt.worldY = ly
        bt.a = la
        bt.b = lb
        bt.c = lc
        bt.d = ld

    # Recompute children
    for child_bone in bone_map.values():
        if isinstance(child_bone, dict) and child_bone.get("parent") == bone.get("name"):
            _recompute_bone_world(bone_map, world, child_bone["name"])


# ==========================================================================
# Atlas Texture Loader
# ==========================================================================

def load_atlas_textures(atlas_path: str) -> dict[str, QPixmap]:
    """Load all frames from a Spine atlas as individual QPixmaps."""
    pages = parse_spine_atlas(atlas_path)
    atlas_dir = os.path.dirname(atlas_path)
    textures: dict[str, QPixmap] = {}

    for image_name, frames in pages:
        image_path = os.path.join(atlas_dir, image_name)
        if not os.path.isfile(image_path):
            image_path = os.path.join(atlas_dir, os.path.basename(image_name))
        page_pixmap = QPixmap(image_path)
        if page_pixmap.isNull():
            continue

        for frame in frames:
            fname = frame.get("filename", "")
            x = frame.get("x", 0)
            y = frame.get("y", 0)
            w = frame.get("w", 0)
            h = frame.get("h", 0)
            rotated = frame.get("rotate", False)

            if w == 0 or h == 0:
                continue

            if rotated:
                cropped = page_pixmap.copy(x, y, h, w)
                xform = QTransform()
                xform.rotate(90)
                cropped = cropped.transformed(xform)
            else:
                cropped = page_pixmap.copy(x, y, w, h)

            key = os.path.splitext(fname)[0] if "." in fname else fname
            textures[key] = cropped

    return textures



# ==========================================================================
# OpenGL Canvas — renders skeleton with animation playback
# ==========================================================================

class SpineGLCanvas(QOpenGLWidget):
    """GPU-accelerated canvas that draws Spine skeleton with animation."""

    time_updated = Signal(float, float)  # current_time, duration
    zoom_changed = Signal(float)  # emitted when zoom changes (scroll wheel)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._zoom = 1.0
        self._pan = QPointF(0, 0)
        self._last_mouse = QPointF()
        self._panning = False

        self._spine_data: dict | None = None
        self._world_transforms: dict[str, BoneTransform] = {}
        self._textures: dict[str, QPixmap] = {}
        self._draw_list: list[dict] = []

        # Single-track animation state
        self._track = {
            "state": None, "name": "", "time": 0.0,
            "playing": False, "speed": 1.0, "loop": True,
        }
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self._elapsed = QElapsedTimer()

        self._skeleton_scale = 1.0
        self._active_skin: str | None = None

        self.setMinimumSize(200, 200)

    def set_base_data(self, spine_data: dict, textures: dict[str, QPixmap]):
        """Store spine data and textures; reset camera."""
        self.stop_animation()
        self._spine_data = spine_data
        self._textures = textures
        self._zoom = 1.0
        self._pan = QPointF(0, 0)
        self._skeleton_scale = 1.0
        self._active_skin = None

    def show_setup_pose(self):
        """Stop animation and display the setup pose."""
        self.stop_animation()
        if self._spine_data:
            bones = self._spine_data.get("bones", [])
            wt = solve_world_transforms(bones)
            ik_constraints = self._spine_data.get("ik", [])
            if ik_constraints:
                solve_ik_constraints(bones, wt, ik_constraints)
            dl = build_draw_list(self._spine_data, wt, self._textures,
                                 active_skin=self._active_skin)
            self._world_transforms = wt
            self._draw_list = dl
        self.update()

    def play_animation(self, spine_data: dict, anim_data: dict, **_kw):
        """Start playing an animation."""
        self._spine_data = spine_data
        t = self._track
        t["state"] = AnimationState(spine_data, anim_data)
        t["time"] = 0.0
        t["playing"] = True
        if not self._timer.isActive():
            self._elapsed.start()
            self._timer.start()
        self._update_frame()
        self.update()

    def stop_animation(self):
        """Stop playback."""
        t = self._track
        t["playing"] = False
        t["state"] = None
        t["time"] = 0.0
        t["name"] = ""
        self._timer.stop()

    def stop_track(self, _track: int = 0):
        """Stop playback (single-track)."""
        self.stop_animation()

    def pause(self):
        self._track["playing"] = False
        self._timer.stop()

    def resume(self):
        t = self._track
        if t["state"] is None:
            return
        t["playing"] = True
        if not self._timer.isActive():
            self._elapsed.start()
            self._timer.start()

    def seek(self, time: float):
        t = self._track
        if t["state"]:
            t["time"] = max(0.0, min(time, t["state"].duration))
            self._update_frame()
            self.update()
            self.time_updated.emit(t["time"], t["state"].duration)

    def set_speed(self, speed: float):
        self._track["speed"] = speed

    def set_loop(self, loop: bool):
        self._track["loop"] = loop

    def has_any_track(self):
        """True if there is an active AnimationState."""
        return self._track["state"] is not None

    def clear_data(self):
        self.stop_animation()
        self._spine_data = None
        self._world_transforms = {}
        self._textures = {}
        self._draw_list = []
        self.update()

    # ── Animation tick ──

    def _tick(self):
        dt = self._elapsed.restart() / 1000.0
        t = self._track
        if not t["playing"] or t["state"] is None:
            self._timer.stop()
            return
        t["time"] += dt * t["speed"]
        duration = t["state"].duration
        if t["time"] > duration:
            if t["loop"]:
                t["time"] %= duration
            else:
                t["time"] = duration
                t["playing"] = False
        if not t["playing"]:
            self._timer.stop()
        self._update_frame()
        self.update()
        if t["state"]:
            self.time_updated.emit(t["time"], t["state"].duration)

    def _update_frame(self):
        if not self._spine_data:
            return
        t = self._track
        if t["state"] is None:
            return

        bone_ov, slot_st, deform_st = t["state"].evaluate(t["time"])
        base_slots = self._spine_data.get("slots", [])
        slot_order = _evaluate_draw_order(
            base_slots, t["state"]._draw_order_keys, t["time"])

        bones = self._spine_data.get("bones", [])
        wt = solve_world_transforms(
            bones,
            bone_overrides=bone_ov if bone_ov else None)
        ik_constraints = self._spine_data.get("ik", [])
        if ik_constraints:
            solve_ik_constraints(bones, wt, ik_constraints)
        dl = build_draw_list(
            self._spine_data, wt, self._textures,
            slot_states=slot_st if slot_st else None,
            slot_order=slot_order,
            deform_states=deform_st if deform_st else None,
            active_skin=self._active_skin)
        self._world_transforms = wt
        self._draw_list = dl

    # ── Rendering ──

    def initializeGL(self):
        pass

    def paintGL(self):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        bg = QColor(CANVAS_BG)
        painter.fillRect(self.rect(), bg)

        if not self._draw_list:
            painter.setPen(QColor(SUBTEXT))
            painter.drawText(self.rect(), Qt.AlignCenter, "No skeleton loaded")
            painter.end()
            return

        cx = self.width() / 2.0 + self._pan.x()
        cy = self.height() / 2.0 + self._pan.y()
        effective_zoom = self._zoom * self._skeleton_scale
        flip_sx = 1.0
        flip_sy = 1.0

        clip_end = ""
        active_clip_path = None

        _BLEND_MODES = {
            "additive": QPainter.CompositionMode_Plus,
            "multiply": QPainter.CompositionMode_Multiply,
            "screen": QPainter.CompositionMode_Screen,
        }

        for item in self._draw_list:
            item_type = item.get("type")

            # Clip-end marker: stop clipping (emitted for slots with no attachment)
            if item_type == "clip_end_marker":
                if clip_end:
                    painter.setClipping(False)
                    clip_end = ""
                    active_clip_path = None
                continue

            # Start clipping mask
            if item_type == "clip":
                bt = self._world_transforms.get(item["bone"])
                if bt is None:
                    continue
                verts = item["vertices"]
                points = []
                for i in range(0, len(verts), 2):
                    lx, ly = verts[i], verts[i + 1]
                    wx = bt.a * lx + bt.b * ly + bt.worldX
                    wy = bt.c * lx + bt.d * ly + bt.worldY
                    sx = cx + wx * flip_sx * effective_zoom
                    sy = cy - wy * flip_sy * effective_zoom
                    points.append(QPointF(sx, sy))
                if points:
                    path = QPainterPath()
                    path.addPolygon(QPolygonF(points))
                    active_clip_path = path
                    painter.resetTransform()
                    painter.setClipPath(path)
                clip_end = item.get("clip_end", "")
                continue

            # Render mesh attachment
            if item_type == "mesh":
                bt = self._world_transforms.get(item["bone"])
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

                blend_mode = _BLEND_MODES.get(item.get("blend"))
                if blend_mode is not None:
                    painter.setCompositionMode(blend_mode)

                # Pre-compute screen-space vertex positions
                screen_pts = []
                for i in range(0, len(verts), 2):
                    lx, ly = verts[i], verts[i + 1]
                    wx = bt.a * lx + bt.b * ly + bt.worldX
                    wy = bt.c * lx + bt.d * ly + bt.worldY
                    screen_pts.append((cx + wx * flip_sx * effective_zoom,
                                       cy - wy * flip_sy * effective_zoom))

                # Pre-compute UV pixel coordinates
                uv_pts = [(uvs[i] * pw, uvs[i + 1] * ph) for i in range(0, len(uvs), 2)]

                # Render each triangle
                for t in range(0, len(tris), 3):
                    i0, i1, i2 = tris[t], tris[t + 1], tris[t + 2]
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
                        combined = active_clip_path.intersected(tri_path)
                        painter.setClipPath(combined)
                    else:
                        painter.setClipPath(tri_path)
                    painter.setTransform(xform)
                    painter.drawPixmap(0, 0, pixmap)

                # Restore clip state after mesh
                painter.setClipping(False)
                if clip_end and active_clip_path is not None:
                    painter.setClipPath(active_clip_path)

                if blend_mode is not None:
                    painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
                if color:
                    painter.setOpacity(1.0)
                continue

            # Render region attachment
            bone_name = item["bone"]
            pixmap = item["pixmap"]
            att = item["att_data"]
            bt = self._world_transforms.get(bone_name)
            if bt is None:
                continue

            ax = att.get("x", 0.0)
            ay = att.get("y", 0.0)
            a_rot = att.get("rotation", 0.0)
            a_sx = att.get("scaleX", 1.0)
            a_sy = att.get("scaleY", 1.0)

            pw = pixmap.width()
            ph = pixmap.height()

            bone_t = QTransform(
                bt.a, -bt.c,
                -bt.b, bt.d,
                bt.worldX, -bt.worldY,
            )

            att_t = QTransform()
            att_t.translate(ax, -ay)
            att_t.rotate(-a_rot)
            att_t.scale(a_sx, a_sy)
            att_t.translate(-pw / 2.0, -ph / 2.0)

            cam_t = QTransform()
            cam_t.translate(cx, cy)
            cam_t.scale(effective_zoom * flip_sx, effective_zoom * flip_sy)

            t = att_t * bone_t * cam_t

            painter.setTransform(t)

            color = item.get("color")
            if color:
                painter.setOpacity(color[3])

            blend_mode = _BLEND_MODES.get(item.get("blend"))
            if blend_mode is not None:
                painter.setCompositionMode(blend_mode)

            painter.drawPixmap(0, 0, pixmap)

            if blend_mode is not None:
                painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            if color:
                painter.setOpacity(1.0)

        painter.resetTransform()
        painter.setClipping(False)
        painter.setOpacity(1.0)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)

        painter.end()

    # ── Camera controls ──

    def wheelEvent(self, event: QWheelEvent):
        delta = event.angleDelta().y()
        factor = 1.1 if delta > 0 else 1 / 1.1
        self._zoom = max(0.05, min(self._zoom * factor, 20.0))
        self.zoom_changed.emit(self._zoom)
        self.update()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MiddleButton or (
            event.button() == Qt.LeftButton and event.modifiers() & Qt.ControlModifier
        ):
            self._panning = True
            self._last_mouse = event.position()
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._panning:
            delta = event.position() - self._last_mouse
            self._pan += delta
            self._last_mouse = event.position()
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self._panning:
            self._panning = False
            self.unsetCursor()

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        self._zoom = 1.0
        self._pan = QPointF(0, 0)
        self.update()


# ==========================================================================
# Build draw list from spine data
# ==========================================================================

def build_draw_list(
    spine_data: dict,
    world_transforms: dict[str, BoneTransform],
    textures: dict[str, QPixmap],
    slot_states: dict | None = None,
    slot_order: list[dict] | None = None,
    deform_states: dict | None = None,
    active_skin: str | None = None,
) -> list[dict]:
    """Build ordered list of drawable items from slots.

    slot_states: per-slot animation overrides (attachment name, color).
    slot_order: reordered slot list from draw order timeline.
    deform_states: {(slot_name, att_name): [vertex_deltas]} from deform timelines.
    active_skin: if set, search only this skin + "default" for attachments.
    """
    slots = slot_order if slot_order is not None else spine_data.get("slots", [])
    skins = normalize_skins(spine_data.get("skins", {}))
    draw_list = []
    pending_clip_ends: set[str] = set()

    for slot in slots:
        slot_name = slot["name"]
        bone_name = slot["bone"]

        # Spine checks clip-end BEFORE rendering — emit marker even for empty slots
        if slot_name in pending_clip_ends:
            draw_list.append({"type": "clip_end_marker", "slot_name": slot_name})
            pending_clip_ends.discard(slot_name)

        state = slot_states.get(slot_name) if slot_states else None

        if state and "attachment" in state:
            att_name = state["attachment"]
            if att_name is None:
                continue  # Slot hidden by animation
        else:
            att_name = slot.get("attachment")
            if att_name is None:
                continue  # No attachment in setup pose — slot is empty

        # Resolve slot color: animation overrides setup pose
        if state and "color" in state:
            color = state["color"]
        elif "color" in slot:
            color = _color_to_floats(slot["color"])
        else:
            color = None

        blend = slot.get("blend", "normal")

        # Search skins for this attachment
        att_data = None
        if active_skin:
            # Active skin first, then default as fallback
            search_order = [active_skin, "default"] if active_skin != "default" else ["default"]
            for skin_name in search_order:
                skin = skins.get(skin_name, {})
                slot_atts = skin.get(slot_name, {})
                if att_name in slot_atts:
                    att_data = slot_atts[att_name]
                    break
        else:
            # Original behavior: default first, then all skins
            for skin_name in ("default",):
                skin = skins.get(skin_name, {})
                slot_atts = skin.get(slot_name, {})
                if att_name in slot_atts:
                    att_data = slot_atts[att_name]
                    break

            if att_data is None:
                for skin_name, skin in skins.items():
                    slot_atts = skin.get(slot_name, {})
                    if att_name in slot_atts:
                        att_data = slot_atts[att_name]
                        break

        if att_data is None:
            att_data = {}

        att_type = att_data.get("type")

        # Handle clipping attachments
        if att_type == "clipping":
            setup_verts = att_data.get("vertices", [])
            deform_key = (slot_name, att_name)
            if deform_states and deform_key in deform_states:
                deltas = deform_states[deform_key]
                final_verts = [sv + dv for sv, dv in zip(setup_verts, deltas)]
            else:
                final_verts = list(setup_verts)
            clip_end_slot = att_data.get("end", "")
            if clip_end_slot:
                pending_clip_ends.add(clip_end_slot)
            draw_list.append({
                "type": "clip",
                "bone": bone_name,
                "slot_name": slot_name,
                "vertices": final_verts,
                "clip_end": clip_end_slot,
            })
            continue

        if att_type == "mesh":
            setup_verts = att_data.get("vertices", [])
            deform_key = (slot_name, att_name)
            if deform_states and deform_key in deform_states:
                deltas = deform_states[deform_key]
                final_verts = [sv + dv for sv, dv in zip(setup_verts, deltas)]
            else:
                final_verts = list(setup_verts)

            image_key = attachment_image_key(att_name, att_data)
            pixmap = textures.get(image_key)
            if pixmap is None:
                continue

            draw_list.append({
                "type": "mesh",
                "bone": bone_name,
                "slot_name": slot_name,
                "vertices": final_verts,
                "uvs": att_data.get("uvs", []),
                "triangles": att_data.get("triangles", []),
                "pixmap": pixmap,
                "color": color,
                "blend": blend,
            })
            continue

        if att_type is not None and att_type != "region":
            continue

        image_key = attachment_image_key(att_name, att_data)
        pixmap = textures.get(image_key)
        if pixmap is None:
            continue

        draw_list.append({
            "bone": bone_name,
            "pixmap": pixmap,
            "att_data": att_data,
            "slot_name": slot_name,
            "att_name": att_name,
            "color": color,
            "blend": blend,
        })

    return draw_list


# ==========================================================================
# UI Tab
# ==========================================================================

class SpineViewerTab:
    def __init__(self, tabs: QTabWidget, get_config):
        self._get_config = get_config
        self._tabs = tabs
        self._spine_data: dict | None = None
        self._textures: dict[str, QPixmap] = {}

        self._page = QWidget()
        tabs.addTab(self._page, tr("viewer.tab"))
        outer = QHBoxLayout(self._page)
        outer.setContentsMargins(5, 5, 5, 5)
        outer.setSpacing(5)

        # ══════════════════════════════════════════════════════════════
        # Left sidebar (scrollable)
        # ══════════════════════════════════════════════════════════════
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(265)
        scroll.setFrameShape(QFrame.NoFrame)
        sidebar_widget = QWidget()
        sb = QVBoxLayout(sidebar_widget)
        sb.setContentsMargins(2, 2, 2, 2)
        sb.setSpacing(4)
        scroll.setWidget(sidebar_widget)
        outer.addWidget(scroll)

        # ── Open Full Version ──
        self._btn_full = QPushButton(tr("viewer.open_full"))
        self._btn_full.clicked.connect(self._open_full_version)
        sb.addWidget(self._btn_full)

        self._add_separator(sb)

        # ── Stats ──
        self._stats = QLabel(tr("viewer.default_stats"))
        self._stats.setWordWrap(True)
        self._stats.setStyleSheet("font-weight: bold; color: #6ec072;")
        sb.addWidget(self._stats)

        self._add_separator(sb)

        # ── Skeleton Scale ──
        self._scale_title = QLabel(tr("viewer.scale_label"))
        sb.addWidget(self._scale_title)
        scale_row = QHBoxLayout()
        self._scale_slider = QSlider(Qt.Horizontal)
        self._scale_slider.setRange(10, 300)
        self._scale_slider.setValue(100)
        self._scale_slider.valueChanged.connect(self._on_scale_changed)
        scale_row.addWidget(self._scale_slider, 1)
        self._scale_label = QLabel("1.0")
        self._scale_label.setFixedWidth(30)
        scale_row.addWidget(self._scale_label)
        self._btn_scale_reset = QPushButton("R")
        self._btn_scale_reset.setFixedWidth(24)
        self._btn_scale_reset.setToolTip(tr("viewer.reset_tip"))
        self._btn_scale_reset.clicked.connect(lambda: self._scale_slider.setValue(100))
        scale_row.addWidget(self._btn_scale_reset)
        sb.addLayout(scale_row)

        # ── Zoom ──
        self._zoom_title = QLabel(tr("viewer.zoom_label"))
        sb.addWidget(self._zoom_title)
        zoom_row = QHBoxLayout()
        self._zoom_slider = QSlider(Qt.Horizontal)
        self._zoom_slider.setRange(1, 1000)
        self._zoom_slider.setValue(100)
        self._zoom_slider.valueChanged.connect(self._on_zoom_slider_changed)
        zoom_row.addWidget(self._zoom_slider, 1)
        self._zoom_label = QLabel("1.0")
        self._zoom_label.setFixedWidth(30)
        zoom_row.addWidget(self._zoom_label)
        self._btn_zoom_reset = QPushButton("R")
        self._btn_zoom_reset.setFixedWidth(24)
        self._btn_zoom_reset.setToolTip(tr("viewer.reset_zoom_tip"))
        self._btn_zoom_reset.clicked.connect(lambda: self._zoom_slider.setValue(100))
        zoom_row.addWidget(self._btn_zoom_reset)
        sb.addLayout(zoom_row)

        # ── Speed ──
        self._speed_title = QLabel(tr("viewer.speed_label"))
        sb.addWidget(self._speed_title)
        speed_row = QHBoxLayout()
        self._speed_slider = QSlider(Qt.Horizontal)
        self._speed_slider.setRange(1, 30)
        self._speed_slider.setValue(10)
        self._speed_slider.valueChanged.connect(self._on_speed_changed)
        speed_row.addWidget(self._speed_slider, 1)
        self._speed_label = QLabel("1.0x")
        self._speed_label.setFixedWidth(35)
        speed_row.addWidget(self._speed_label)
        self._btn_speed_reset = QPushButton("R")
        self._btn_speed_reset.setFixedWidth(24)
        self._btn_speed_reset.setToolTip(tr("viewer.reset_tip"))
        self._btn_speed_reset.clicked.connect(lambda: self._speed_slider.setValue(10))
        speed_row.addWidget(self._btn_speed_reset)
        sb.addLayout(speed_row)

        self._add_separator(sb)

        # ── Skin dropdown ──
        self._skin_title = QLabel(tr("viewer.skin_label"))
        sb.addWidget(self._skin_title)
        self._skin_combo = QComboBox()
        self._skin_combo.currentIndexChanged.connect(self._on_skin_changed)
        sb.addWidget(self._skin_combo)

        self._add_separator(sb)

        # ── Animation list (scrollable) ──
        self._anim_title = QLabel(tr("viewer.anim_label"))
        sb.addWidget(self._anim_title)
        self._anim_list = QListWidget()
        self._anim_list.currentRowChanged.connect(self._on_anim_changed)
        sb.addWidget(self._anim_list, 1)

        # ══════════════════════════════════════════════════════════════
        # Right panel: canvas + scrubber
        # ══════════════════════════════════════════════════════════════
        right = QVBoxLayout()
        right.setSpacing(3)

        self._canvas = SpineGLCanvas()
        self._canvas.time_updated.connect(self._on_time_updated)
        self._canvas.zoom_changed.connect(self._on_zoom_from_wheel)
        right.addWidget(self._canvas, 1)

        # ── Scrubber row below canvas ──
        scrub_row = QHBoxLayout()
        self._play_btn = QPushButton("\u25b6")
        self._play_btn.setFixedWidth(36)
        self._play_btn.clicked.connect(self._toggle_play)
        scrub_row.addWidget(self._play_btn)
        self._scrubber = QSlider(Qt.Horizontal)
        self._scrubber.setRange(0, 1000)
        self._scrubber.sliderMoved.connect(self._on_scrub)
        scrub_row.addWidget(self._scrubber, 1)
        self._time_label = QLabel("0.00s / 0.00s")
        self._time_label.setFixedWidth(120)
        scrub_row.addWidget(self._time_label)
        right.addLayout(scrub_row)

        outer.addLayout(right, 1)

        language_changed.connect(self._retranslate)

    def _retranslate(self):
        idx = self._tabs.indexOf(self._page)
        if idx >= 0:
            self._tabs.setTabText(idx, tr("viewer.tab"))
        self._btn_full.setText(tr("viewer.open_full"))
        if self._spine_data is None:
            self._stats.setText(tr("viewer.default_stats"))
        self._scale_title.setText(tr("viewer.scale_label"))
        self._btn_scale_reset.setToolTip(tr("viewer.reset_tip"))
        self._zoom_title.setText(tr("viewer.zoom_label"))
        self._btn_zoom_reset.setToolTip(tr("viewer.reset_zoom_tip"))
        self._speed_title.setText(tr("viewer.speed_label"))
        self._btn_speed_reset.setToolTip(tr("viewer.reset_tip"))
        self._skin_title.setText(tr("viewer.skin_label"))
        self._anim_title.setText(tr("viewer.anim_label"))

    # ── Helpers ──

    @staticmethod
    def _add_separator(layout):
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        layout.addWidget(line)

    # ── Callbacks ──

    def _on_scale_changed(self, value):
        scale = value / 100.0
        self._canvas._skeleton_scale = scale
        self._scale_label.setText(f"{scale:.1f}")
        self._canvas.update()

    def _on_zoom_slider_changed(self, value):
        zoom = value / 100.0
        self._canvas._zoom = zoom
        self._zoom_label.setText(f"{zoom:.1f}" if zoom < 10 else f"{zoom:.0f}")
        self._canvas.update()

    def _on_zoom_from_wheel(self, zoom):
        self._zoom_slider.blockSignals(True)
        self._zoom_slider.setValue(int(zoom * 100))
        self._zoom_slider.blockSignals(False)
        self._zoom_label.setText(f"{zoom:.1f}" if zoom < 10 else f"{zoom:.0f}")

    def _on_skin_changed(self, index):
        if self._spine_data is None:
            return
        skin_name = self._skin_combo.currentText()
        if skin_name == tr("viewer.all_skins"):
            self._canvas._active_skin = None
        else:
            self._canvas._active_skin = skin_name
        # Refresh display
        if self._canvas.has_any_track():
            self._canvas._update_frame()
        else:
            self._canvas.show_setup_pose()
        self._canvas.update()

    def _on_anim_changed(self, row):
        if self._spine_data is None:
            return

        if row <= 0:
            self._canvas.stop_animation()
            self._play_btn.setText("\u25b6")
            self._scrubber.setValue(0)
            self._scrubber.setRange(0, 1000)
            self._time_label.setText("0.00s / 0.00s")
            self._canvas.show_setup_pose()
            return

        item = self._anim_list.currentItem()
        if item is None:
            return
        anim_name = item.text()
        animations = self._spine_data.get("animations", {})
        anim_data = animations.get(anim_name)
        if anim_data is None:
            return

        self._canvas._track["name"] = anim_name
        self._canvas.play_animation(self._spine_data, anim_data)
        self._play_btn.setText("\u23f8")
        t = self._canvas._track
        if t["state"]:
            dur_ms = int(t["state"].duration * 1000)
            self._scrubber.setRange(0, max(dur_ms, 1))

    def _toggle_play(self):
        t = self._canvas._track
        if t["state"] is None:
            return
        if t["playing"]:
            self._canvas.pause()
            self._play_btn.setText("\u25b6")
        else:
            self._canvas.resume()
            self._play_btn.setText("\u23f8")

    def _on_speed_changed(self, value):
        speed = value / 10.0
        self._canvas.set_speed(speed)
        self._speed_label.setText(f"{speed:.1f}x")

    def _on_scrub(self, value):
        self._canvas.seek(value / 1000.0)

    def _on_time_updated(self, current_time, duration):
        self._scrubber.blockSignals(True)
        self._scrubber.setValue(int(current_time * 1000))
        self._scrubber.blockSignals(False)
        self._time_label.setText(f"{current_time:.2f}s / {duration:.2f}s")
        if not self._canvas._track["playing"]:
            self._play_btn.setText("\u25b6")

    # ── Open Full Version ──

    def _open_full_version(self):
        json_path = self._get_config("json")
        atlas_path = self._get_config("atlas")
        if not json_path or not os.path.isfile(json_path):
            QMessageBox.critical(None, tr("err.title"), tr("viewer.err.no_json"))
            return
        if not atlas_path or not os.path.isfile(atlas_path):
            QMessageBox.critical(None, tr("err.title"), tr("viewer.err.no_atlas"))
            return

        # Write JSON path into viewer preferences so it auto-loads
        prefs_path = os.path.expanduser("~/.prefs/spine-skeletonviewer")
        try:
            import re as _re
            if os.path.isfile(prefs_path):
                text = open(prefs_path, "r", encoding="utf-8").read()
                # Replace existing lastFile entry
                if '<entry key="lastFile">' in text:
                    text = _re.sub(
                        r'<entry key="lastFile">[^<]*</entry>',
                        f'<entry key="lastFile">{json_path}</entry>',
                        text,
                    )
                else:
                    # Insert before </properties>
                    text = text.replace(
                        "</properties>",
                        f'<entry key="lastFile">{json_path}</entry>\n</properties>',
                    )
                open(prefs_path, "w", encoding="utf-8").write(text)
            else:
                os.makedirs(os.path.dirname(prefs_path), exist_ok=True)
                with open(prefs_path, "w", encoding="utf-8") as f:
                    f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
                    f.write('<!DOCTYPE properties SYSTEM "http://java.sun.com/dtd/properties.dtd">\n')
                    f.write("<properties>\n")
                    f.write(f'<entry key="lastFile">{json_path}</entry>\n')
                    f.write("</properties>\n")
        except Exception:
            pass  # Non-critical — viewer will just open without a file

        viewer_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "GTSpineViewer_3653", "bin")
        if platform.system() == "Windows":
            script = os.path.join(viewer_dir, "GTSpineViewer_3653.bat")
        else:
            script = os.path.join(viewer_dir, "GTSpineViewer_3653")
        if not os.path.isfile(script):
            QMessageBox.critical(None, tr("err.title"), tr("viewer.err.viewer_not_found", path=script))
            return
        try:
            subprocess.Popen([script, json_path], cwd=viewer_dir)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("viewer.err.launch_failed", error=e))

    # ── Load ──

    def _load(self):
        json_path = self._get_config("json")
        atlas_path = self._get_config("atlas")

        if not json_path or not os.path.isfile(json_path):
            QMessageBox.critical(None, tr("err.title"), tr("err.no_json"))
            return
        if not atlas_path or not os.path.isfile(atlas_path):
            QMessageBox.critical(None, tr("err.title"), tr("err.no_atlas"))
            return

        try:
            spine_data = load_spine_json(json_path)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("viewer.err.load_json", error=e))
            return

        try:
            textures = load_atlas_textures(atlas_path)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("viewer.err.load_atlas", error=e))
            return

        self._spine_data = spine_data
        self._textures = textures
        self._canvas.set_base_data(spine_data, textures)

        # Populate animation list
        animations = spine_data.get("animations", {})
        self._anim_list.blockSignals(True)
        self._anim_list.clear()
        self._anim_list.addItem(tr("viewer.setup_pose"))
        for name in sorted(animations.keys()):
            self._anim_list.addItem(name)
        self._anim_list.blockSignals(False)

        # Populate skin dropdown
        skins = normalize_skins(spine_data.get("skins", {}))
        self._skin_combo.blockSignals(True)
        self._skin_combo.clear()
        self._skin_combo.addItem(tr("viewer.all_skins"))
        for name in sorted(skins.keys()):
            self._skin_combo.addItem(name)
        self._skin_combo.blockSignals(False)

        # Show setup pose first
        self._canvas.show_setup_pose()

        bones = spine_data.get("bones", [])
        slots = spine_data.get("slots", [])
        n_skins = len(skins)
        n_anims = len(animations)
        self._stats.setText(
            tr("viewer.stats", bones=len(bones), slots=len(slots),
               skins=n_skins, textures=len(textures), anims=n_anims)
        )

        # Reset sliders
        self._scale_slider.setValue(100)
        self._zoom_slider.setValue(100)

        # Auto-play first animation if available
        if n_anims > 0:
            self._anim_list.setCurrentRow(1)
