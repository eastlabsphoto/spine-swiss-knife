"""Spine animation evaluation, bone transforms, and draw-list building.

Pure computational logic extracted from the viewer.  Only uses Qt types
as data values (QPixmap, QTransform, QPainter) — no widget dependency.
"""

from __future__ import annotations

import logging
import math
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap, QPainter, QTransform, QPolygonF, QPainterPath

from .spine_json import (
    normalize_skins, attachment_image_key, clip_end_marker_after_slot,
    mesh_is_weighted, resolve_weighted_vertices, is_spine4, is_weighted,
    iter_deform_timelines, get_draw_order_timeline,
)
from .spine_refs import apply_draw_order_offsets
from .texture_unpacker import parse_spine_atlas

_log = logging.getLogger(__name__)
_warned: set = set()


def _warn_once(msg: str) -> None:
    if msg not in _warned:
        _warned.add(msg)
        _log.warning(msg)


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
    """Evaluate a NORMALISED cubic bezier (3.x) for a given time ratio.

    curve = [cx1, cy1, cx2, cy2] — control points of a bezier from (0,0) to (1,1).
    Binary search for t where bezier_x(t) ~ alpha, returns bezier_y(t).
    """
    if alpha <= 0.0:
        return 0.0
    if alpha >= 1.0:
        return 1.0
    cx1, cy1, cx2, cy2 = curve[0], curve[1], curve[2], curve[3]
    lo, hi = 0.0, 1.0
    for _ in range(24):
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


def _bezier_absolute(t1: float, v1: float, cx1: float, cy1: float,
                     cx2: float, cy2: float, t2: float, v2: float,
                     time: float) -> float:
    """Value at ``time`` of a 4.x bezier segment with ABSOLUTE control points.

    Control points are (t1, v1), (cx1, cy1), (cx2, cy2), (t2, v2).
    """
    if time <= t1:
        return v1
    if time >= t2:
        return v2
    lo, hi = 0.0, 1.0
    for _ in range(30):
        s = (lo + hi) * 0.5
        u = 1.0 - s
        x = (u * u * u * t1 + 3.0 * u * u * s * cx1
             + 3.0 * u * s * s * cx2 + s * s * s * t2)
        if x < time:
            lo = s
        else:
            hi = s
    s = (lo + hi) * 0.5
    u = 1.0 - s
    return (u * u * u * v1 + 3.0 * u * u * s * cy1
            + 3.0 * u * s * s * cy2 + s * s * s * v2)


def _get_bezier_alpha(curve, alpha: float) -> float:
    """Get interpolation weight from a (normalised) curve specification."""
    if curve == "stepped":
        return -1.0  # sentinel for stepped
    if isinstance(curve, list) and len(curve) >= 4:
        return _bezier_interpolate(curve, alpha)
    return alpha  # linear


def _segment_alpha(left: dict, t1: float, t2: float, time: float,
                   v1: float, v2: float, v4: bool, channel: int = 0):
    """Interpolation weight within the segment that starts at key ``left``.

    Returns None for a stepped segment.  Handles the three JSON curve formats:
    3.7- (list of 4 normalised numbers), 3.8 (``curve``/``c2``/``c3``/``c4``
    scalars, normalised) and 4.x (4 absolute numbers per value channel).
    """
    curve = left.get("curve")
    if curve == "stepped":
        return None
    dt = t2 - t1
    lin = (time - t1) / dt if dt > 0 else 0.0
    if isinstance(curve, (int, float)) and not isinstance(curve, bool):
        return _bezier_interpolate(
            [curve, left.get("c2", 0.0), left.get("c3", 1.0), left.get("c4", 1.0)], lin)
    if isinstance(curve, list) and len(curve) >= 4:
        if not v4:
            return _bezier_interpolate(curve, lin)
        base = min(4 * channel, len(curve) - 4)
        cx1, cy1, cx2, cy2 = curve[base:base + 4]
        if abs(v2 - v1) < 1e-12:
            return lin
        y = _bezier_absolute(t1, v1, cx1, cy1, cx2, cy2, t2, v2, time)
        return (y - v1) / (v2 - v1)
    return lin


def _wrap_angle_delta(delta: float) -> float:
    """Wrap an angle delta into [-180, 180] (3.x runtime behaviour)."""
    return delta - (16384 - int(16384.5 - delta / 360.0)) * 360.0


def _interpolate_vector(keys: list, time: float, extract, v4: bool = False,
                        wrap_channels=()) -> list:
    """Interpolate a timeline whose keys convert to float vectors by ``extract``."""
    n = len(keys)
    if n == 0:
        return []
    if time <= keys[0].get("time", 0):
        return list(extract(keys[0]))
    if time >= keys[-1].get("time", 0):
        return list(extract(keys[-1]))

    lo, hi = 0, n - 2
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if keys[mid].get("time", 0) <= time:
            lo = mid
        else:
            hi = mid - 1

    left = keys[lo]
    right = keys[lo + 1]
    t1 = left.get("time", 0)
    t2 = right.get("time", 0)
    lv = extract(left)
    rv = extract(right)
    out = []
    for k in range(min(len(lv), len(rv))):
        v1 = float(lv[k])
        v2 = float(rv[k])
        if not v4 and k in wrap_channels:
            v2 = v1 + _wrap_angle_delta(v2 - v1)
        alpha = _segment_alpha(left, t1, t2, time, v1, v2, v4, k)
        if alpha is None:
            out.append(v1)
        else:
            out.append(v1 + (v2 - v1) * alpha)
    return out


def _interpolate_timeline(keys: list, time: float, fields: tuple, defaults: dict,
                          v4: bool = False, wrap_fields: tuple = ()) -> dict:
    """Interpolate numeric fields across a keyframe timeline at given time.

    Supports linear (default), stepped, and cubic bezier interpolation in the
    3.7-, 3.8 and 4.x curve formats.  ``wrap_fields`` (3.x rotation) take the
    shortest way around between consecutive keys; ignored for 4.x.
    """
    if not keys:
        return dict(defaults)

    def extract(key):
        return [float(key.get(f, defaults[f])) for f in fields]

    wrap = tuple(i for i, f in enumerate(fields) if f in wrap_fields)
    vals = _interpolate_vector(keys, time, extract, v4, wrap)
    return {f: vals[i] for i, f in enumerate(fields)}


def _hex_channels(color_hex, count: int, default: str) -> list:
    """Parse "rrggbb[aa]" into the first ``count`` float channels."""
    if not isinstance(color_hex, str) or len(color_hex) < 6:
        color_hex = default
    try:
        vals = _color_to_floats(color_hex)
    except ValueError:
        vals = _color_to_floats(default)
    return list(vals[:count])


def _interpolate_color(keys: list, time: float, v4: bool = False,
                       field: str = "color", count: int = 4
                       ) -> tuple[float, ...]:
    """Interpolate a colour timeline at given time. Returns ``count`` floats
    (r, g, b, a by default)."""
    if not keys:
        return (1.0,) * count

    def extract(key):
        return _hex_channels(key.get(field), count, "ffffffff")

    return tuple(_interpolate_vector(keys, time, extract, v4))


def _interpolate_deform(keys: list, time: float, v4: bool = False) -> list[float]:
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
    # One curve channel drives all values; 4.x control points are in 0..1 value units.
    alpha = _segment_alpha(left, left["time"], right["time"], time, 0.0, 1.0, v4, 0)
    if alpha is None:
        return left["vertices"]

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
    """Evaluate draw order timeline, returning reordered slot list.

    Decoded exactly like the runtimes (``apply_draw_order_offsets``).  Invalid
    offsets fall back to the setup order (logged once).
    """
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

    names = [slot["name"] for slot in base_slots]
    try:
        order = apply_draw_order_offsets(names, offsets_list)
    except ValueError as exc:
        _warn_once("Invalid draw order key, using setup order: %s" % exc)
        return base_slots

    by_name: dict = {}
    for slot in base_slots:
        by_name.setdefault(slot["name"], slot)
    return [by_name[name] for name in order]


# ==========================================================================
# Animation State Evaluator
# ==========================================================================

def _max_key_time(node) -> float:
    """Largest ``time`` of any keyframe dict found anywhere inside ``node``."""
    best = 0.0
    if isinstance(node, list):
        for item in node:
            if isinstance(item, dict):
                t = item.get("time", 0)
                if isinstance(t, (int, float)) and t > best:
                    best = t
                best = max(best, _max_key_time(item))
    elif isinstance(node, dict):
        for value in node.values():
            if isinstance(value, (list, dict)):
                best = max(best, _max_key_time(value))
    return best


def _resolve_linked_mesh(skins: dict, skin_name, slot_name: str, att_data: dict,
                         fallback_skins=("default",)):
    """Follow a linkedmesh chain to its parent mesh.

    Returns (parent_skin, parent_name, parent_data) or None when unresolved.
    ``skin_name`` is the skin the linked mesh itself was found in.
    """
    seen = 0
    cur = att_data
    cur_skin = skin_name
    while isinstance(cur, dict) and cur.get("type") == "linkedmesh" and seen < 16:
        seen += 1
        parent = cur.get("parent")
        if not parent:
            return None
        order = []
        if cur.get("skin"):
            order.append(cur["skin"])
        if cur_skin is not None:
            order.append(cur_skin)
        order.extend(fallback_skins)
        found = None
        for sk in order:
            cand = skins.get(sk, {}).get(slot_name, {}).get(parent)
            if cand is not None:
                found = (sk, parent, cand)
                break
        if found is None:
            return None
        cur_skin, _pname, cur = found
        if cur.get("type") != "linkedmesh":
            return found
        att_data = cur
    return None


def _rotate_field(keys: list, v4: bool) -> str:
    """Rotate keys use ``angle`` (3.x) or ``value`` (4.x); be lenient about both."""
    primary, other = ("value", "angle") if v4 else ("angle", "value")
    if any(primary in k for k in keys):
        return primary
    if any(other in k for k in keys):
        return other
    return primary


class AnimationState:
    """Evaluates animation timelines at a given time."""

    def __init__(self, spine_data: dict, anim_data: dict):
        self._v4 = is_spine4(spine_data or {})
        self._bone_timelines = anim_data.get("bones", {})
        self._slot_timelines = anim_data.get("slots", {})
        self._draw_order_keys = get_draw_order_timeline(anim_data)

        # Pre-process deform timelines (ffd / deform / 4.1+ attachments)
        self._deform_timelines: dict[tuple, list] = {}
        skins = normalize_skins((spine_data or {}).get("skins", {}))
        for skin_name, slot_name, att_name, keys in iter_deform_timelines(anim_data):
            att = skins.get(skin_name, {}).get(slot_name, {}).get(att_name)
            if att is None:
                for sk in skins.values():
                    if att_name in sk.get(slot_name, {}):
                        att = sk[slot_name][att_name]
                        break
            att = att or {}
            if att.get("type") == "linkedmesh":
                res = _resolve_linked_mesh(skins, skin_name, slot_name, att)
                if res is not None:
                    att = res[2]
            verts = att.get("vertices", [])
            n_floats = None
            if att.get("type") in ("mesh", "linkedmesh") and not is_weighted(att):
                n_floats = len(verts)
            elif att.get("type") in ("clipping", "boundingbox", "path") \
                    and not is_weighted(att):
                n_floats = len(verts)

            expanded = []
            for key in keys:
                deltas = key.get("vertices", [])
                offset = key.get("offset", 0)
                size = n_floats if n_floats is not None else offset + len(deltas)
                full = [0.0] * size
                for i, v in enumerate(deltas):
                    idx = offset + i
                    if idx < size:
                        full[idx] = v
                expanded.append({
                    "time": key.get("time", 0),
                    "vertices": full,
                    "curve": key.get("curve"),
                })
            self._deform_timelines[(skin_name, slot_name, att_name)] = expanded

        # Duration: the last keyframe of ANY timeline (bones, slots, constraints,
        # deform in all formats, draw order, events).
        self.duration = float(_max_key_time(anim_data))
        if self.duration <= 0:
            self.duration = 0.001

    def evaluate(self, time: float) -> tuple[dict, dict, dict]:
        """Returns (bone_overrides, slot_states, deform_states).

        bone_overrides: {bone_name: {x, y, rotation, scaleX, scaleY, shearX, shearY}}
        slot_states: {slot_name: {attachment: name|None, color: (r,g,b,a),
                                  rgb: (r,g,b), alpha: a}}
        deform_states: {(slot_name, att_name): [deltas],
                        (skin_name, slot_name, att_name): [deltas]}
        """
        v4 = self._v4
        bone_overrides: dict[str, dict] = {}
        slot_states: dict[str, dict] = {}

        for bone_name, timelines in self._bone_timelines.items():
            if not isinstance(timelines, dict):
                continue
            ov = {}
            if "translate" in timelines:
                vals = _interpolate_timeline(
                    timelines["translate"], time, ("x", "y"), {"x": 0, "y": 0}, v4)
                ov["x"] = vals["x"]
                ov["y"] = vals["y"]
            if "translatex" in timelines:
                ov["x"] = _interpolate_timeline(
                    timelines["translatex"], time, ("value",), {"value": 0}, v4)["value"]
            if "translatey" in timelines:
                ov["y"] = _interpolate_timeline(
                    timelines["translatey"], time, ("value",), {"value": 0}, v4)["value"]
            if "rotate" in timelines:
                keys = timelines["rotate"]
                field = _rotate_field(keys, v4)
                vals = _interpolate_timeline(
                    keys, time, (field,), {field: 0}, v4, wrap_fields=(field,))
                ov["rotation"] = vals[field]
            if "scale" in timelines:
                vals = _interpolate_timeline(
                    timelines["scale"], time, ("x", "y"), {"x": 1, "y": 1}, v4)
                ov["scaleX"] = vals["x"]
                ov["scaleY"] = vals["y"]
            if "scalex" in timelines:
                ov["scaleX"] = _interpolate_timeline(
                    timelines["scalex"], time, ("value",), {"value": 1}, v4)["value"]
            if "scaley" in timelines:
                ov["scaleY"] = _interpolate_timeline(
                    timelines["scaley"], time, ("value",), {"value": 1}, v4)["value"]
            if "shear" in timelines:
                vals = _interpolate_timeline(
                    timelines["shear"], time, ("x", "y"), {"x": 0, "y": 0}, v4)
                ov["shearX"] = vals["x"]
                ov["shearY"] = vals["y"]
            if "shearx" in timelines:
                ov["shearX"] = _interpolate_timeline(
                    timelines["shearx"], time, ("value",), {"value": 0}, v4)["value"]
            if "sheary" in timelines:
                ov["shearY"] = _interpolate_timeline(
                    timelines["sheary"], time, ("value",), {"value": 0}, v4)["value"]
            if ov:
                bone_overrides[bone_name] = ov

        for slot_name, timelines in self._slot_timelines.items():
            if not isinstance(timelines, dict):
                continue
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
            # 3.x colour / twoColor, 4.x rgba / rgba2 (light part) -> full colour
            if "color" in timelines:
                state["color"] = _interpolate_color(timelines["color"], time, v4)
            if "twoColor" in timelines:
                state["color"] = _interpolate_color(
                    timelines["twoColor"], time, v4, field="light")
            if "rgba" in timelines:
                state["color"] = _interpolate_color(timelines["rgba"], time, v4)
            if "rgba2" in timelines:
                state["color"] = _interpolate_color(
                    timelines["rgba2"], time, v4, field="light")
            if "rgb" in timelines:
                state["rgb"] = _interpolate_color(
                    timelines["rgb"], time, v4, count=3)
            if "rgb2" in timelines:
                state["rgb"] = _interpolate_color(
                    timelines["rgb2"], time, v4, field="light", count=3)
            if "alpha" in timelines:
                state["alpha"] = _interpolate_timeline(
                    timelines["alpha"], time, ("value",), {"value": 0}, v4)["value"]
            if state:
                slot_states[slot_name] = state

        deform_states: dict[tuple, list] = {}
        # "default"-skin timelines win the plain (slot, attachment) key
        ordered = sorted(self._deform_timelines.items(),
                         key=lambda kv: 0 if kv[0][0] == "default" else 1)
        for (skin_name, slot_name, att_name), keys in ordered:
            vals = _interpolate_deform(keys, time, v4)
            deform_states[(skin_name, slot_name, att_name)] = vals
            deform_states.setdefault((slot_name, att_name), vals)

        return bone_overrides, slot_states, deform_states


# ==========================================================================
# Bone World Transform Solver
# ==========================================================================

class BoneTransform:
    """World transform for a single bone: position + 2x2 affine matrix.

    ``pose`` holds the local pose (x, y, rotation, scaleX, scaleY, shearX,
    shearY) the transform was computed from, ``mode`` its inherit mode, so
    constraints can re-solve from the animated pose instead of the setup pose.
    """
    __slots__ = ("worldX", "worldY", "a", "b", "c", "d", "pose", "mode")

    def __init__(self):
        self.worldX = 0.0
        self.worldY = 0.0
        self.a = 1.0
        self.b = 0.0
        self.c = 0.0
        self.d = 1.0
        self.pose = None
        self.mode = "normal"


_INHERIT_MODES = ("normal", "onlyTranslation", "noRotationOrReflection",
                  "noScale", "noScaleOrReflection")


def _inherit_mode(bone: dict) -> str:
    """Inherit mode of a bone: 4.2 ``inherit``, 3.x/4.0/4.1 ``transform`` or the
    very old ``inheritRotation`` / ``inheritScale`` flags."""
    for field in ("inherit", "transform"):
        val = bone.get(field)
        if isinstance(val, str) and val in _INHERIT_MODES:
            return val
    rot = bone.get("inheritRotation", True)
    scale = bone.get("inheritScale", True)
    if rot is False and scale is False:
        return "onlyTranslation"
    if rot is False:
        return "noRotationOrReflection"
    if scale is False:
        return "noScale"
    return "normal"


def _local_matrix(rotation: float, sx: float, sy: float,
                  shear_x: float, shear_y: float):
    """Local 2x2 matrix (la, lb, lc, ld) of a bone, angles in degrees."""
    rx = math.radians(rotation + shear_x)
    ry = math.radians(rotation + 90.0 + shear_y)
    return (math.cos(rx) * sx, math.cos(ry) * sy,
            math.sin(rx) * sx, math.sin(ry) * sy)


def _apply_local_pose(bt: BoneTransform, parent: BoneTransform | None,
                      pose: tuple, mode: str = "normal") -> None:
    """Compute the world transform of a bone from its local pose and parent."""
    lx, ly, rotation, sx, sy, shear_x, shear_y = pose
    bt.pose = pose
    bt.mode = mode

    if parent is None:
        bt.worldX = lx
        bt.worldY = ly
        bt.a, bt.b, bt.c, bt.d = _local_matrix(rotation, sx, sy, shear_x, shear_y)
        return

    pa, pb, pc, pd = parent.a, parent.b, parent.c, parent.d
    bt.worldX = pa * lx + pb * ly + parent.worldX
    bt.worldY = pc * lx + pd * ly + parent.worldY

    if mode == "onlyTranslation":
        bt.a, bt.b, bt.c, bt.d = _local_matrix(rotation, sx, sy, shear_x, shear_y)
    elif mode == "noRotationOrReflection":
        s = pa * pa + pc * pc
        if s > 1e-4:
            s = abs(pa * pd - pb * pc) / s
            pb = pc * s
            pd = pa * s
            prx = math.degrees(math.atan2(pc, pa))
        else:
            pa = 0.0
            pc = 0.0
            prx = 90.0 - math.degrees(math.atan2(pd, pb))
        rx = math.radians(rotation + shear_x - prx)
        ry = math.radians(rotation + shear_y - prx + 90.0)
        la = math.cos(rx) * sx
        lb = math.cos(ry) * sy
        lc = math.sin(rx) * sx
        ld = math.sin(ry) * sy
        bt.a = pa * la - pb * lc
        bt.b = pa * lb - pb * ld
        bt.c = pc * la + pd * lc
        bt.d = pc * lb + pd * ld
    elif mode in ("noScale", "noScaleOrReflection"):
        cos_r = math.cos(math.radians(rotation))
        sin_r = math.sin(math.radians(rotation))
        za = pa * cos_r + pb * sin_r
        zc = pc * cos_r + pd * sin_r
        s = math.sqrt(za * za + zc * zc)
        if s > 1e-5:
            s = 1.0 / s
        za *= s
        zc *= s
        s = math.sqrt(za * za + zc * zc)
        if mode == "noScale" and (pa * pd - pb * pc < 0):
            s = -s
        r = math.pi / 2.0 + math.atan2(zc, za)
        zb = math.cos(r) * s
        zd = math.sin(r) * s
        sx_r = math.radians(shear_x)
        sy_r = math.radians(90.0 + shear_y)
        la = math.cos(sx_r) * sx
        lb = math.cos(sy_r) * sy
        lc = math.sin(sx_r) * sx
        ld = math.sin(sy_r) * sy
        bt.a = za * la + zb * lc
        bt.b = za * lb + zb * ld
        bt.c = zc * la + zd * lc
        bt.d = zc * lb + zd * ld
    else:
        la, lb, lc, ld = _local_matrix(rotation, sx, sy, shear_x, shear_y)
        bt.a = pa * la + pb * lc
        bt.b = pa * lb + pb * ld
        bt.c = pc * la + pd * lc
        bt.d = pc * lb + pd * ld


def _setup_pose(bone: dict) -> tuple:
    return (bone.get("x", 0.0), bone.get("y", 0.0), bone.get("rotation", 0.0),
            bone.get("scaleX", 1.0), bone.get("scaleY", 1.0),
            bone.get("shearX", 0.0), bone.get("shearY", 0.0))


def _animated_pose(bone: dict, ov: dict | None) -> tuple:
    """Setup pose combined with an animation override (additive / multiplicative)."""
    lx, ly, rotation, sx, sy, shx, shy = _setup_pose(bone)
    if ov:
        lx += ov.get("x", 0)
        ly += ov.get("y", 0)
        rotation += ov.get("rotation", 0)
        sx *= ov.get("scaleX", 1)
        sy *= ov.get("scaleY", 1)
        shx += ov.get("shearX", 0)
        shy += ov.get("shearY", 0)
    return (lx, ly, rotation, sx, sy, shx, shy)


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
        ov = bone_overrides.get(name) if bone_overrides else None
        pose = _animated_pose(bone, ov)
        parent_name = bone.get("parent")
        parent = world.get(parent_name) if parent_name else None
        bt = BoneTransform()
        _apply_local_pose(bt, parent, pose, _inherit_mode(bone))
        world[name] = bt

    return world


# ==========================================================================
# IK Constraint Solver
# ==========================================================================

def solve_ik_constraints(
    bones: list[dict],
    world: dict[str, BoneTransform],
    constraints: list[dict],
    bone_overrides: dict[str, dict] | None = None,
):
    """Apply IK constraints to world transforms in-place.

    Supports 1-bone and 2-bone IK. Constraints are processed in 'order' order.
    The animated local pose is taken from ``world`` (stored by
    ``solve_world_transforms``); ``bone_overrides`` is only needed for
    transforms built by other means.
    """
    bone_map = {b["name"]: b for b in bones}
    if bone_overrides:
        for name, ov in bone_overrides.items():
            bt = world.get(name)
            if bt is not None and bt.pose is None and name in bone_map:
                bt.pose = _animated_pose(bone_map[name], ov)
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


def _current_pose(bone_map, world, name) -> tuple:
    """Current animated local pose of a bone (falls back to the setup pose)."""
    bt = world.get(name)
    if bt is not None and bt.pose is not None:
        return bt.pose
    return _setup_pose(bone_map.get(name, {}))


def _to_parent_space(world, parent_name, tx, ty):
    """Target (world) -> local space of the parent bone; None if singular."""
    if parent_name and parent_name in world:
        p = world[parent_name]
        det = p.a * p.d - p.b * p.c
        if abs(det) < 1e-10:
            return None
        inv_det = 1.0 / det
        dx = tx - p.worldX
        dy = ty - p.worldY
        return ((dx * p.d - dy * p.b) * inv_det,
                (dy * p.a - dx * p.c) * inv_det)
    return tx, ty


def _wrap180(angle: float) -> float:
    return ((angle + 180.0) % 360.0) - 180.0


def _apply_ik_1bone(bone_map, world, bone_name, tx, ty, mix):
    """Single-bone IK: rotate bone to point at target."""
    bt = world.get(bone_name)
    if bt is None:
        return

    bone_data = bone_map.get(bone_name, {})
    local = _to_parent_space(world, bone_data.get("parent"), tx, ty)
    if local is None:
        return
    local_tx, local_ty = local

    bx, by, rotation, _sx, _sy, shear_x, _shy = _current_pose(bone_map, world, bone_name)

    target_angle = math.degrees(math.atan2(local_ty - by, local_tx - bx))
    delta = _wrap180(target_angle - shear_x - rotation)
    final_rotation = rotation + delta * mix

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

    px, py, p_rot, p_sx, p_sy, p_shx, _p_shy = _current_pose(bone_map, world, parent_name)
    cx_local, cy_local = _current_pose(bone_map, world, child_name)[:2]

    # l1: distance parent origin -> child origin (child offset scaled by the parent)
    cxs = cx_local * p_sx
    cys = cy_local * p_sy
    l1 = math.sqrt(cxs * cxs + cys * cys)
    if l1 < 1e-6:
        return
    child_dir = math.atan2(cys, cxs)

    pp_name = parent_data.get("parent")
    pp = world.get(pp_name) if pp_name else None

    # l2: length of the child bone in the same (grandparent) space
    length = child_data.get("length", 0.0) or 0.0
    l2 = 0.0
    if length > 0:
        pp_scale = math.hypot(pp.a, pp.c) if pp is not None else 1.0
        if pp_scale < 1e-9:
            pp_scale = 1.0
        l2 = length * math.hypot(child_bt.a, child_bt.c) / pp_scale
    if l2 < 1e-6:
        # No usable length: longest offset of a grandchild, else equal lengths.
        for b in bone_map.values():
            if isinstance(b, dict) and b.get("parent") == child_name:
                bx, by = b.get("x", 0.0), b.get("y", 0.0)
                l2 = max(l2, math.sqrt(bx * bx + by * by))
        if l2 < 1e-6:
            l2 = l1

    local = _to_parent_space(world, pp_name, tx, ty)
    if local is None:
        return
    local_tx, local_ty = local

    ddx = local_tx - px
    ddy = local_ty - py
    dist = math.sqrt(ddx * ddx + ddy * ddy)
    if dist < 1e-6:
        return
    target_angle = math.atan2(ddy, ddx)

    cos_angle = (l1 * l1 + dist * dist - l2 * l2) / (2 * l1 * dist)
    cos_angle = max(-1.0, min(1.0, cos_angle))
    parent_ik_angle = target_angle + ((-1 if bend_positive else 1) * math.acos(cos_angle))

    parent_new_rot = math.degrees(parent_ik_angle - child_dir) - p_shx
    parent_delta = _wrap180(parent_new_rot - p_rot)
    final_parent_rot = p_rot + parent_delta * mix

    _recompute_bone_world(bone_map, world, parent_name,
                          rotation_override=final_parent_rot)

    # Now solve child rotation to point at target
    _apply_ik_1bone(bone_map, world, child_name, tx, ty, mix)


def _recompute_bone_world(bone_map, world, bone_name, rotation_override=None):
    """Recompute a bone's world transform (optionally with a new local rotation)
    from its current animated pose, then all descendants."""
    bone = bone_map.get(bone_name)
    if bone is None:
        return

    pose = _current_pose(bone_map, world, bone_name)
    if rotation_override is not None:
        pose = (pose[0], pose[1], rotation_override) + tuple(pose[3:])

    bt = world.get(bone_name)
    if bt is None:
        bt = BoneTransform()
        world[bone_name] = bt

    parent_name = bone.get("parent")
    parent = world.get(parent_name) if parent_name else None
    _apply_local_pose(bt, parent, pose, _inherit_mode(bone))

    # Recompute children from their own (animated) poses
    for child_bone in bone_map.values():
        if isinstance(child_bone, dict) and child_bone.get("parent") == bone_name:
            _recompute_bone_world(bone_map, world, child_bone["name"])


# ==========================================================================
# Atlas Texture Loader
# ==========================================================================

def assign_texture_keys(items) -> dict:
    """Map texture keys to values for atlas regions.

    ``items``: iterable of (region_name, index, value).  Region names are used
    verbatim.  When several regions share a name the plain name maps to the one
    with the LOWEST index (what ``findRegion(name)`` returns); every indexed
    region is additionally registered as ``name_<index>`` when that key is free.
    """
    groups: dict = {}
    order = []
    for name, index, value in items:
        if name not in groups:
            groups[name] = []
            order.append(name)
        groups[name].append((index, len(groups[name]), value))

    result: dict = {}
    for name in order:
        best = min(groups[name], key=lambda t: (t[0], t[1]))
        result[name] = best[2]
    for name in order:
        for index, _seq, value in groups[name]:
            if isinstance(index, int) and index >= 0:
                result.setdefault("%s_%d" % (name, index), value)
    return result


def load_atlas_textures(atlas_path: str) -> dict[str, QPixmap]:
    """Load all frames from a Spine atlas as individual QPixmaps."""
    pages = parse_spine_atlas(atlas_path)
    atlas_dir = os.path.dirname(atlas_path)
    items = []

    def restore_trimmed_frame(frame: dict, cropped: QPixmap) -> QPixmap:
        """Restore a trimmed atlas region onto its original transparent canvas."""
        orig_w = max(1, int(frame.get("orig_w") or cropped.width()))
        orig_h = max(1, int(frame.get("orig_h") or cropped.height()))
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
                # Stored rotated 90 deg CCW, occupying h x w: rotate back clockwise.
                cropped = page_pixmap.copy(x, y, h, w)
                xform = QTransform()
                xform.rotate(90)
                cropped = cropped.transformed(xform)
            else:
                cropped = page_pixmap.copy(x, y, w, h)

            cropped = restore_trimmed_frame(frame, cropped)

            # Atlas region names have no extension: never strip anything.
            items.append((fname, frame.get("index", -1), cropped))

    return assign_texture_keys(items)


# ==========================================================================
# Build draw list from spine data
# ==========================================================================

def _find_attachment(skins: dict, slot_name: str, att_name: str, active_skin):
    """(skin_name, attachment) of ``att_name`` in the slot, or (None, None).

    With an active skin: that skin, then "default".  Without: "default", then
    every skin.
    """
    if active_skin:
        order = [active_skin] if active_skin == "default" else [active_skin, "default"]
    else:
        order = ["default"] + [n for n in skins if n != "default"]
    for skin_name in order:
        att = skins.get(skin_name, {}).get(slot_name, {}).get(att_name)
        if att is not None:
            return skin_name, att
    return None, None


def _resolve_slot_color(slot: dict, state: dict | None):
    """Slot colour: setup colour overridden by animated colour / rgb / alpha."""
    base = _color_to_floats(slot["color"]) if "color" in slot else None
    if not state or not ("color" in state or "rgb" in state or "alpha" in state):
        return base
    r, g, b, a = state.get("color") or base or (1.0, 1.0, 1.0, 1.0)
    rgb = state.get("rgb")
    if rgb is not None:
        r, g, b = rgb[0], rgb[1], rgb[2]
    if state.get("alpha") is not None:
        a = state["alpha"]
    return (r, g, b, a)


def _apply_deltas(setup_verts: list, deltas: list) -> list:
    if len(deltas) >= len(setup_verts):
        return [sv + dv for sv, dv in zip(setup_verts, deltas)]
    out = list(setup_verts)
    for i, dv in enumerate(deltas):
        out[i] += dv
    return out


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
    deform_states: {(slot_name, att_name): [vertex_deltas]} from deform timelines
        (``(skin, slot, att)`` keys are preferred when present).
    active_skin: if set, search only this skin + "default" for attachments.
    """
    slots = slot_order if slot_order is not None else spine_data.get("slots", [])
    skins = normalize_skins(spine_data.get("skins", {}))
    draw_list = []
    pending_clip_ends: set[str] = set()

    # Bone transforms indexed by global bone index, for weighted attachments.
    bone_tx_list = [
        world_transforms.get(b.get("name")) for b in spine_data.get("bones", [])
    ]

    def end_clip_if_pending(slot_name):
        if slot_name in pending_clip_ends:
            draw_list.append({"type": "clip_end_marker", "slot_name": slot_name})
            pending_clip_ends.discard(slot_name)

    def lookup_deform(skin_name, slot_name, att_name):
        if not deform_states:
            return None
        for key in ((skin_name, slot_name, att_name), (slot_name, att_name)):
            if key in deform_states:
                return deform_states[key]
        return None

    for slot in slots:
        slot_name = slot["name"]
        bone_name = slot["bone"]

        state = slot_states.get(slot_name) if slot_states else None

        if state and "attachment" in state:
            att_name = state["attachment"]
        else:
            att_name = slot.get("attachment")
        if att_name is None:
            end_clip_if_pending(slot_name)
            continue  # Slot hidden / empty

        color = _resolve_slot_color(slot, state)
        blend = slot.get("blend", "normal")

        skin_found, att_data = _find_attachment(skins, slot_name, att_name, active_skin)
        if att_data is None:
            # Not in the active skin nor in default: nothing to draw.
            end_clip_if_pending(slot_name)
            continue

        att_type = att_data.get("type")

        # Handle clipping attachments
        if att_type == "clipping":
            end_clip_if_pending(slot_name)
            setup_verts = att_data.get("vertices", [])
            weighted = mesh_is_weighted(
                setup_verts, att_data.get("vertexCount", 0))
            if weighted:
                # Weighted clip: decode to world space (deform not applied).
                final_verts = resolve_weighted_vertices(setup_verts, bone_tx_list)
            else:
                deltas = lookup_deform(skin_found, slot_name, att_name)
                final_verts = (_apply_deltas(setup_verts, deltas)
                               if deltas is not None else list(setup_verts))
            clip_end_slot = att_data.get("end", "")
            if clip_end_slot:
                pending_clip_ends.add(clip_end_slot)
            draw_list.append({
                "type": "clip",
                "bone": bone_name,
                "slot_name": slot_name,
                "vertices": final_verts,
                "world_space": weighted,
                "clip_end": clip_end_slot,
            })
            continue

        if att_type in ("mesh", "linkedmesh"):
            geom = att_data
            geom_skin, geom_name = skin_found, att_name
            inherit_deform = False
            if att_type == "linkedmesh":
                res = _resolve_linked_mesh(skins, skin_found, slot_name, att_data)
                if res is None:
                    end_clip_if_pending(slot_name)
                    continue
                geom_skin, geom_name, geom = res
                inherit_deform = (att_data.get("timelines", att_data.get("deform", True))
                                  is not False)

            setup_verts = geom.get("vertices", [])
            uvs = geom.get("uvs", [])
            weighted = mesh_is_weighted(setup_verts, len(uvs) // 2)
            if weighted:
                # Weighted (bone-rigged) mesh: decode to world space. The
                # vertices array is not a flat list of (x, y) pairs, so fine
                # deform deltas are not applied here.
                final_verts = resolve_weighted_vertices(setup_verts, bone_tx_list)
            else:
                deltas = lookup_deform(skin_found, slot_name, att_name)
                if deltas is None and inherit_deform:
                    deltas = lookup_deform(geom_skin, slot_name, geom_name)
                final_verts = (_apply_deltas(setup_verts, deltas)
                               if deltas is not None else list(setup_verts))

            image_key = attachment_image_key(att_name, att_data)
            pixmap = textures.get(image_key)
            if pixmap is None:
                end_clip_if_pending(slot_name)
                continue

            draw_list.append({
                "type": "mesh",
                "bone": bone_name,
                "slot_name": slot_name,
                "vertices": final_verts,
                "world_space": weighted,
                "uvs": uvs,
                "triangles": geom.get("triangles", []),
                "pixmap": pixmap,
                "color": color,
                "blend": blend,
            })
            end_clip_if_pending(slot_name)
            continue

        if att_type is not None and att_type != "region":
            end_clip_if_pending(slot_name)
            continue

        image_key = attachment_image_key(att_name, att_data)
        pixmap = textures.get(image_key)
        if pixmap is None:
            end_clip_if_pending(slot_name)
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
            end_clip_if_pending(slot_name)

    return draw_list
