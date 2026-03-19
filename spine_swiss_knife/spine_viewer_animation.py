"""Spine animation evaluation, bone transforms, and draw-list building.

Pure computational logic extracted from the viewer.  Only uses Qt types
as data values (QPixmap, QTransform, QPainter) — no widget dependency.
"""

import math
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap, QPainter, QTransform, QPolygonF, QPainterPath

from .spine_json import normalize_skins, attachment_image_key, clip_end_marker_after_slot
from .texture_unpacker import parse_spine_atlas


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

    def restore_trimmed_frame(frame: dict, cropped: QPixmap) -> QPixmap:
        """Restore a trimmed atlas region onto its original transparent canvas."""
        orig_w = max(1, int(frame.get("orig_w", cropped.width())))
        orig_h = max(1, int(frame.get("orig_h", cropped.height())))
        offset_x = int(frame.get("offset_x", 0))
        offset_y = int(frame.get("offset_y", 0))
        paste_y = max(0, orig_h - cropped.height() - offset_y)

        if (
            cropped.width() == orig_w
            and cropped.height() == orig_h
            and offset_x == 0
            and paste_y == 0
        ):
            return cropped

        restored = QPixmap(orig_w, orig_h)
        restored.fill(Qt.transparent)
        painter = QPainter(restored)
        # libGDX/Spine atlas offsetY is measured from the bottom edge.
        painter.drawPixmap(offset_x, paste_y, cropped)
        painter.end()
        return restored

    for image_name, frames, _page_scale in pages:
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

            cropped = restore_trimmed_frame(frame, cropped)

            key = os.path.splitext(fname)[0] if "." in fname else fname
            textures[key] = cropped

    return textures


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

        state = slot_states.get(slot_name) if slot_states else None

        if state and "attachment" in state:
            att_name = state["attachment"]
            if att_name is None:
                if slot_name in pending_clip_ends:
                    draw_list.append({"type": "clip_end_marker",
                                      "slot_name": slot_name})
                    pending_clip_ends.discard(slot_name)
                continue  # Slot hidden by animation
        else:
            att_name = slot.get("attachment")
            if att_name is None:
                if slot_name in pending_clip_ends:
                    draw_list.append({"type": "clip_end_marker",
                                      "slot_name": slot_name})
                    pending_clip_ends.discard(slot_name)
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
            if slot_name in pending_clip_ends:
                draw_list.append({"type": "clip_end_marker",
                                  "slot_name": slot_name})
                pending_clip_ends.discard(slot_name)
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
            if slot_name in pending_clip_ends:
                draw_list.append({"type": "clip_end_marker",
                                  "slot_name": slot_name})
                pending_clip_ends.discard(slot_name)
            continue

        if att_type is not None and att_type != "region":
            if slot_name in pending_clip_ends:
                draw_list.append({"type": "clip_end_marker",
                                  "slot_name": slot_name})
                pending_clip_ends.discard(slot_name)
            continue

        image_key = attachment_image_key(att_name, att_data)
        pixmap = textures.get(image_key)
        if pixmap is None:
            if slot_name in pending_clip_ends:
                draw_list.append({"type": "clip_end_marker",
                                  "slot_name": slot_name})
                pending_clip_ends.discard(slot_name)
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
        if slot_name in pending_clip_ends and clip_end_marker_after_slot(
            att_name, att_data,
        ):
            draw_list.append({"type": "clip_end_marker",
                              "slot_name": slot_name})
            pending_clip_ends.discard(slot_name)

    return draw_list
