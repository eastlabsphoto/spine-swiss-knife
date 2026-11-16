"""Pure JSON-level Spine version conversion (no Qt).

``convert_to_destination`` downgrades a skeleton JSON step by step
(4.x -> 3.8 -> 3.7 -> 3.6).  Every conversion returns ``(data, warnings)``;
each lossy step appends one aggregated, human-readable English warning
(counts, not one line per key).
"""

from __future__ import annotations

import re

_TIME_EPS = 1e-9


# ==========================================================================
# Version detection
# ==========================================================================

def _read_source_version(data: dict) -> str:
    skel = data.get("skeleton", {}) if isinstance(data, dict) else {}
    v = skel.get("spine", "") if isinstance(skel, dict) else ""
    return str(v).split("-from-")[0] if v else ""


def _normalize_version(tag: str) -> str:
    t = str(tag).strip()
    if re.match(r"^4(\.|$)", t):
        return "4.x"
    if re.match(r"^3\.8(\.|$)", t):
        return "3.8"
    if re.match(r"^3\.7(\.|$)", t):
        return "3.7"
    if re.match(r"^3\.6(\.|$)", t):
        return "3.6"
    return "unknown"


_RANK = {"4.x": 4, "3.8": 3, "3.7": 2, "3.6": 1, "unknown": 99}


# ==========================================================================
# Warning collection
# ==========================================================================

class _Warnings:
    """Aggregates warnings per kind: ``add("k", "{n} things changed")``."""

    def __init__(self):
        self._order: list = []
        self._items: dict = {}

    def add(self, kind: str, template: str, n: int = 1):
        if kind not in self._items:
            self._order.append(kind)
            self._items[kind] = [0, template]
        self._items[kind][0] += n

    def lines(self) -> list:
        return [self._items[k][1].format(n=self._items[k][0])
                for k in self._order if self._items[k][0] > 0]


# ==========================================================================
# Small helpers
# ==========================================================================

def _ensure_skeleton_tag(data: dict, version: str) -> dict:
    if "skeleton" not in data or not isinstance(data["skeleton"], dict):
        data["skeleton"] = {}
    old = data["skeleton"].get("spine", "")
    data["skeleton"]["spine"] = f"{version}-from-{old}" if old else version
    return data


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _ftime(frame) -> float:
    return _num(frame.get("time", 0.0)) if isinstance(frame, dict) else 0.0


def _frames(value) -> list:
    return [f for f in value if isinstance(f, dict)] if isinstance(value, list) else []


def _hex_channels(s, count: int, default: str) -> list:
    """First *count* bytes of a hex colour string as 0..1 floats."""
    if not isinstance(s, str) or len(s) < count * 2:
        s = default
    out = []
    for i in range(count):
        try:
            out.append(int(s[i * 2:i * 2 + 2], 16) / 255.0)
        except ValueError:
            out.append(int(default[i * 2:i * 2 + 2], 16) / 255.0)
    return out


def _to_hex(values) -> str:
    return "".join("%02x" % max(0, min(255, int(round(v * 255)))) for v in values)


def _sample(frames: list, t: float, getter, default: float) -> float:
    """Value of a timeline at time *t*: clamped at the ends, linear between
    keys (a ``stepped`` key holds its value)."""
    if not frames:
        return default
    frames = sorted(frames, key=_ftime)
    if t <= _ftime(frames[0]) + _TIME_EPS:
        return getter(frames[0])
    for a, b in zip(frames, frames[1:]):
        ta, tb = _ftime(a), _ftime(b)
        if t < tb - _TIME_EPS:
            va = getter(a)
            if a.get("curve") == "stepped" or tb - ta <= _TIME_EPS:
                return va
            return va + (getter(b) - va) * (t - ta) / (tb - ta)
        if abs(t - tb) <= _TIME_EPS:
            return getter(b)
    return getter(frames[-1])


def _union_times(*frame_lists) -> list:
    times = []
    for fl in frame_lists:
        for f in fl:
            t = _ftime(f)
            if not any(abs(t - u) <= _TIME_EPS for u in times):
                times.append(t)
    return sorted(times)


def _put_time(key: dict, t: float) -> dict:
    if t != 0:
        key["time"] = t
    return key


# ==========================================================================
# Curve conversion (4.x absolute curves -> 3.8 normalised scalars)
# ==========================================================================

_ONE6 = [("mixRotate", 1.0), ("mixX", 1.0), ("mixY", 1.0),
         ("mixScaleX", 1.0), ("mixScaleY", 1.0), ("mixShearY", 1.0)]

# timeline name -> channel layout.  ("n", ...) numeric fields with defaults,
# ("hex", field, byte_count, default, ...) colour channels.
_BONE_SPECS = {
    "rotate": [("n", [("value", 0.0)])],
    "translate": [("n", [("x", 0.0), ("y", 0.0)])],
    "scale": [("n", [("x", 1.0), ("y", 1.0)])],
    "shear": [("n", [("x", 0.0), ("y", 0.0)])],
}
_SLOT_SPECS = {
    "rgba": [("hex", "color", 4, "ffffffff")],
    "rgb": [("hex", "color", 3, "ffffff")],
    "alpha": [("n", [("value", 0.0)])],
    "rgba2": [("hex", "light", 4, "ffffffff"), ("hex", "dark", 3, "000000")],
    "rgb2": [("hex", "light", 3, "ffffff"), ("hex", "dark", 3, "000000")],
}
_IK_SPEC = [("n", [("mix", 1.0), ("softness", 0.0)])]
_TRANSFORM_SPEC = [("n", _ONE6)]
_PATH_SPECS = {
    "position": [("n", [("value", 0.0)])],
    "spacing": [("n", [("value", 0.0)])],
    "mix": [("n", [("mixRotate", 1.0), ("mixX", 1.0), ("mixY", 1.0)])],
}


def _channel_values(spec, frame: dict) -> list:
    out: list = []
    for part in spec:
        if part[0] == "n":
            for name, default in part[1]:
                if name == "value" and "value" not in frame and "angle" in frame:
                    out.append(_num(frame["angle"], default))
                else:
                    out.append(_num(frame.get(name, default), default))
        else:
            out.extend(_hex_channels(frame.get(part[1]), part[2], part[3]))
    return out


def _convert_frames_curves(frames, spec, warn: _Warnings, is_rotate=False):
    """Rewrite 4.x ``curve`` arrays of *frames* to the 3.8 scalar form."""
    fl = [f for f in frames if isinstance(f, dict)] if isinstance(frames, list) else []
    for i, f in enumerate(fl):
        curve = f.get("curve")
        if curve is None or isinstance(curve, str):
            if is_rotate and i + 1 < len(fl):
                v1 = _channel_values(spec, f)[0]
                v2 = _channel_values(spec, fl[i + 1])[0]
                if abs(v2 - v1) > 180:
                    warn.add("rot180", "{n} rotation segment(s) turn by more than 180 degrees; "
                                       "Spine 3.x takes the shortest way round, so they may rotate differently")
            continue
        if not isinstance(curve, list):
            continue  # already a 3.x scalar curve
        if i + 1 >= len(fl):
            del f["curve"]
            continue
        nxt = fl[i + 1]
        v1s = _channel_values(spec, f)
        v2s = _channel_values(spec, nxt)
        nch = len(v1s)
        if is_rotate and abs(v2s[0] - v1s[0]) > 180:
            warn.add("rot180", "{n} rotation segment(s) turn by more than 180 degrees; "
                               "Spine 3.x takes the shortest way round, so they may rotate differently")
        t1, t2 = _ftime(f), _ftime(nxt)
        dt = t2 - t1
        nums = [_num(c) for c in curve]
        changed = [k for k in range(nch) if abs(v2s[k] - v1s[k]) > _TIME_EPS]
        del f["curve"]
        for extra in ("c2", "c3", "c4"):
            f.pop(extra, None)
        if dt <= _TIME_EPS or not changed:
            warn.add("curve_flat", "{n} bezier curve(s) between equal values/times were dropped "
                                   "(linear, no visual change)")
            continue
        if len(nums) < 4 * nch:
            warn.add("curve_bad", "{n} malformed bezier curve(s) were replaced by linear interpolation")
            continue
        k = changed[0]
        c = nums[4 * k:4 * k + 4]
        dv = v2s[k] - v1s[k]
        f["curve"] = (c[0] - t1) / dt
        f["c2"] = (c[1] - v1s[k]) / dv
        f["c3"] = (c[2] - t1) / dt
        f["c4"] = (c[3] - v1s[k]) / dv
        if len(changed) > 1:
            warn.add("curve_multi", "{n} multi-channel bezier curve(s) were reduced to the curve "
                                    "of one channel (other channels approximated)")
        else:
            warn.add("curve_ok", "{n} bezier curve(s) converted to the Spine 3.8 curve format")


def _convert_deform_curves(frames, warn: _Warnings):
    fl = _frames(frames)
    for i, f in enumerate(fl):
        curve = f.get("curve")
        if not isinstance(curve, list):
            continue
        del f["curve"]
        if i + 1 >= len(fl) or len(curve) < 4:
            continue
        t1, t2 = _ftime(f), _ftime(fl[i + 1])
        dt = t2 - t1
        if dt <= _TIME_EPS:
            warn.add("curve_flat", "{n} bezier curve(s) between equal values/times were dropped "
                                   "(linear, no visual change)")
            continue
        c = [_num(x) for x in curve[:4]]
        # deform curves blend 0..1, already normalised on the value axis
        f["curve"] = (c[0] - t1) / dt
        f["c2"] = c[1]
        f["c3"] = (c[2] - t1) / dt
        f["c4"] = c[3]
        warn.add("curve_ok", "{n} bezier curve(s) converted to the Spine 3.8 curve format")


# ==========================================================================
# Constraint mix renaming
# ==========================================================================

def _rename_constraint_mixes(obj: dict, warn: _Warnings = None):
    if not isinstance(obj, dict):
        return
    if "mixRotate" in obj:
        obj["rotateMix"] = obj.pop("mixRotate")
    if "mixX" in obj or "mixY" in obj:
        x = obj.pop("mixX", None)
        y = obj.pop("mixY", None)
        if warn is not None and x is not None and y is not None and x != y:
            warn.add("mix_xy", "{n} constraint mix(es) have different X and Y values; "
                               "Spine 3.8 has one translate mix, X is used")
        val = x if x is not None else y
        if val is not None:
            obj["translateMix"] = val
    if "mixScaleX" in obj or "mixScaleY" in obj:
        x = obj.pop("mixScaleX", None)
        y = obj.pop("mixScaleY", None)
        if warn is not None and x is not None and y is not None and x != y:
            warn.add("mix_scale", "{n} constraint mix(es) have different scale X and Y values; "
                                  "Spine 3.8 has one scale mix, X is used")
        val = x if x is not None else y
        if val is not None:
            obj["scaleMix"] = val
    if "mixShearX" in obj or "mixShearY" in obj:
        x = obj.pop("mixShearX", None)
        y = obj.pop("mixShearY", None)
        val = x if x is not None else y
        if val is not None:
            obj["shearMix"] = val


def _linearize_curves(node):
    """Remove every non-string ``curve`` (legacy helper, no longer used by
    ``convert_to_38``)."""
    if node is None:
        return
    if isinstance(node, list):
        for item in node:
            _linearize_curves(item)
        return
    if not isinstance(node, dict):
        return
    if "curve" in node and not isinstance(node["curve"], str):
        del node["curve"]
    for k in node:
        _linearize_curves(node[k])


def _rollback_curves(node, parent_name=None, parent_is_array=False):
    if node is None:
        return
    if isinstance(node, list):
        for item in node:
            _rollback_curves(item, parent_name, True)
        return
    if not isinstance(node, dict):
        return
    if parent_is_array:
        if "time" not in node:
            node["time"] = 0.0
        if parent_name == "rotate" and "angle" not in node:
            node["angle"] = 0.0
        if parent_name == "scale":
            if "x" not in node:
                node["x"] = 1.0
            if "y" not in node:
                node["y"] = 1.0
    if "curve" in node:
        c = node["curve"]
        if isinstance(c, (int, float)):
            c2 = node.pop("c2", 0)
            c3 = node.pop("c3", 1)
            c4 = node.pop("c4", 1)
            node["curve"] = [c, c2, c3, c4]
        return
    for k, child in list(node.items()):
        _rollback_curves(child, k, isinstance(child, list))


# ==========================================================================
# 4.x -> 3.8 pieces
# ==========================================================================

_AXIS_PAIRS = (
    ("translatex", "translatey", "translate", 0.0),
    ("scalex", "scaley", "scale", 1.0),
    ("shearx", "sheary", "shear", 0.0),
)


def _merge_axis_timelines(timelines: dict, warn: _Warnings):
    for xname, yname, cname, default in _AXIS_PAIRS:
        has_x, has_y = xname in timelines, yname in timelines
        if not (has_x or has_y):
            continue
        xs = _frames(timelines.pop(xname, None))
        ys = _frames(timelines.pop(yname, None))
        if cname in timelines:
            warn.add("axis_dropped", "{n} separate-axis timeline(s) were dropped because a combined "
                                     "timeline already exists")
            continue
        times = _union_times(xs, ys)
        if not times:
            continue
        merged = []
        for t in times:
            x = _sample(xs, t, lambda f: _num(f.get("value", default), default), default)
            y = _sample(ys, t, lambda f: _num(f.get("value", default), default), default)
            merged.append(_put_time({"x": x, "y": y}, t))
        timelines[cname] = merged
        warn.add("axis_merged", "{n} separate-axis timeline(s) merged into combined timelines "
                                "(curves replaced by linear interpolation)")


def _slot_setup_colors(data: dict) -> dict:
    out = {}
    slots = data.get("slots", [])
    if isinstance(slots, list):
        for s in slots:
            if isinstance(s, dict) and "name" in s:
                out[s["name"]] = s
    return out


def _merge_color_timelines(stl: dict, setup_slot: dict, warn: _Warnings):
    """rgb + alpha -> color, rgb2 + alpha -> twoColor (3.8 has no split channels)."""
    setup_color = (setup_slot or {}).get("color", "ffffffff")
    setup_rgb = _hex_channels(setup_color, 3, "ffffff")
    setup_alpha = _hex_channels(setup_color[6:] if isinstance(setup_color, str)
                                and len(setup_color) >= 8 else "ff", 1, "ff")[0]
    setup_dark = _hex_channels((setup_slot or {}).get("dark"), 3, "000000")

    rgb = _frames(stl.get("rgb"))
    rgb2 = _frames(stl.get("rgb2"))
    alpha = _frames(stl.get("alpha"))

    if "rgb" in stl or ("alpha" in stl and "rgb2" not in stl):
        if "rgba" in stl or "color" in stl:
            warn.add("color_dropped", "{n} rgb/alpha timeline(s) were dropped because a full colour "
                                      "timeline already exists")
        else:
            times = _union_times(rgb, alpha)
            if times:
                keys = []
                for t in times:
                    comps = []
                    for ci in range(3):
                        comps.append(_sample(
                            rgb, t, lambda f, ci=ci: _hex_channels(f.get("color"), 3, "ffffff")[ci],
                            setup_rgb[ci]))
                    a = _sample(alpha, t, lambda f: _num(f.get("value", 0.0)), setup_alpha)
                    keys.append(_put_time({"color": _to_hex(comps + [a])}, t))
                stl["color"] = keys
                warn.add("color_merged", "{n} rgb/alpha timeline set(s) merged into colour timelines "
                                         "(curves replaced by linear interpolation)")
    if "rgb2" in stl:
        if "rgba2" in stl or "twoColor" in stl:
            warn.add("color_dropped", "{n} rgb/alpha timeline(s) were dropped because a full colour "
                                      "timeline already exists")
        else:
            times = _union_times(rgb2, alpha)
            if times:
                keys = []
                for t in times:
                    light = [_sample(
                        rgb2, t, lambda f, ci=ci: _hex_channels(f.get("light"), 3, "ffffff")[ci],
                        setup_rgb[ci]) for ci in range(3)]
                    dark = [_sample(
                        rgb2, t, lambda f, ci=ci: _hex_channels(f.get("dark"), 3, "000000")[ci],
                        setup_dark[ci]) for ci in range(3)]
                    a = _sample(alpha, t, lambda f: _num(f.get("value", 0.0)), setup_alpha)
                    keys.append(_put_time({"light": _to_hex(light + [a]),
                                           "dark": _to_hex(dark)}, t))
                stl["twoColor"] = keys
                warn.add("color_merged", "{n} rgb/alpha timeline set(s) merged into colour timelines "
                                         "(curves replaced by linear interpolation)")
    for k in ("rgb", "rgb2", "alpha"):
        stl.pop(k, None)


def _convert_skin_extras(data: dict, warn: _Warnings):
    """Drop 4.x-only attachment/skin data that 3.8 cannot read."""
    skins = data.get("skins")
    entries = skins if isinstance(skins, list) else []
    for skin in entries:
        if not isinstance(skin, dict):
            continue
        if skin.pop("physics", None):
            warn.add("physics", "{n} physics constraint item(s) were removed (Spine 3.8 has no physics)")
        atts = skin.get("attachments")
        if not isinstance(atts, dict):
            continue
        for slot_atts in atts.values():
            if not isinstance(slot_atts, dict):
                continue
            for att in slot_atts.values():
                if isinstance(att, dict) and "sequence" in att:
                    att.pop("sequence")
                    warn.add("sequence", "{n} image sequence(s) were removed (not supported by Spine 3.8)")


def _move_deform(anim: dict, warn: _Warnings):
    """4.1+ ``attachments.<skin>.<slot>.<att>.{deform,sequence}`` -> ``deform``."""
    att_tl = anim.pop("attachments", None)
    if isinstance(att_tl, dict):
        deform = anim.get("deform")
        if not isinstance(deform, dict):
            deform = {}
        for skin, slots in att_tl.items():
            if not isinstance(slots, dict):
                continue
            for slot, atts in slots.items():
                if not isinstance(atts, dict):
                    continue
                for att, tls in atts.items():
                    if not isinstance(tls, dict):
                        continue
                    if "sequence" in tls:
                        warn.add("seq_tl", "{n} sequence timeline(s) were removed "
                                           "(not supported by Spine 3.8)")
                    frames = tls.get("deform")
                    if frames:
                        deform.setdefault(skin, {}).setdefault(slot, {})[att] = frames
        if deform:
            anim["deform"] = deform
    deform = anim.get("deform")
    if isinstance(deform, dict):
        for slots in deform.values():
            if not isinstance(slots, dict):
                continue
            for atts in slots.values():
                if not isinstance(atts, dict):
                    continue
                for frames in atts.values():
                    _convert_deform_curves(frames, warn)


def _convert_animation_to_38(anim: dict, setup_slots: dict, warn: _Warnings):
    bones = anim.get("bones", {})
    if isinstance(bones, dict):
        for tls in bones.values():
            if not isinstance(tls, dict):
                continue
            if tls.pop("inherit", None) is not None:
                warn.add("inherit_tl", "{n} bone inherit timeline(s) were removed (not supported by Spine 3.8)")
            _merge_axis_timelines(tls, warn)
            for name, spec in _BONE_SPECS.items():
                if name in tls:
                    _convert_frames_curves(tls[name], spec, warn, is_rotate=(name == "rotate"))
            rot = tls.get("rotate")
            for f in _frames(rot):
                if "value" in f and "angle" not in f:
                    f["angle"] = f.pop("value")

    slots = anim.get("slots", {})
    if isinstance(slots, dict):
        for sname, stl in slots.items():
            if not isinstance(stl, dict):
                continue
            if stl.pop("sequence", None) is not None:
                warn.add("seq_tl", "{n} sequence timeline(s) were removed (not supported by Spine 3.8)")
            _merge_color_timelines(stl, setup_slots.get(sname), warn)
            for name, spec in _SLOT_SPECS.items():
                if name in stl:
                    _convert_frames_curves(stl[name], spec, warn)
            if "rgba" in stl:
                if "color" in stl:
                    stl.pop("rgba")
                else:
                    stl["color"] = stl.pop("rgba")
            if "rgba2" in stl:
                if "twoColor" in stl:
                    stl.pop("rgba2")
                else:
                    stl["twoColor"] = stl.pop("rgba2")

    ik = anim.get("ik")
    if isinstance(ik, dict):
        for frames in ik.values():
            _convert_frames_curves(frames, _IK_SPEC, warn)
    tc = anim.get("transform")
    if isinstance(tc, dict):
        for frames in tc.values():
            _convert_frames_curves(frames, _TRANSFORM_SPEC, warn)
            for f in _frames(frames):
                _rename_constraint_mixes(f, warn)
    path = anim.get("path")
    if isinstance(path, dict):
        for tls in path.values():
            if not isinstance(tls, dict):
                continue
            for name, spec in _PATH_SPECS.items():
                if name in tls:
                    _convert_frames_curves(tls[name], spec, warn)
            for name in ("position", "spacing"):
                for f in _frames(tls.get(name)):
                    if "value" in f and name not in f:
                        f[name] = f.pop("value")
            for f in _frames(tls.get("mix")):
                _rename_constraint_mixes(f, warn)

    if anim.pop("physics", None):
        warn.add("physics", "{n} physics constraint item(s) were removed (Spine 3.8 has no physics)")
    _move_deform(anim, warn)


# ==========================================================================
# Conversion functions
# ==========================================================================

def convert_to_38(data: dict) -> tuple:
    """Spine 4.x JSON -> 3.8 JSON.  Returns ``(data, warnings)``."""
    warn = _Warnings()
    data = _ensure_skeleton_tag(data, "3.8.99")

    bones = data.get("bones")
    if isinstance(bones, list):
        for b in bones:
            if isinstance(b, dict) and "inherit" in b:
                mode = b.pop("inherit")
                if mode != "normal":
                    b["transform"] = mode

    if data.pop("physics", None):
        warn.add("physics", "{n} physics constraint item(s) were removed (Spine 3.8 has no physics)")
    _convert_skin_extras(data, warn)

    for key in ("transform", "path"):
        group = data.get(key)
        if isinstance(group, (dict, list)):
            items = group.values() if isinstance(group, dict) else group
            for item in items:
                if isinstance(item, dict):
                    _rename_constraint_mixes(item, warn)

    setup_slots = _slot_setup_colors(data)
    animations = data.get("animations", {})
    if isinstance(animations, dict):
        for anim in animations.values():
            if isinstance(anim, dict):
                _convert_animation_to_38(anim, setup_slots, warn)
    return data, warn.lines()


def convert_to_37(data: dict) -> tuple:
    warn = _Warnings()
    data = _ensure_skeleton_tag(data, "3.7.94")
    if isinstance(data.get("skins"), list):
        new_skins = {}
        for s in data["skins"]:
            if not isinstance(s, dict) or not s.get("name"):
                continue
            extras = [k for k in ("bones", "ik", "transform", "path", "physics") if s.get(k)]
            if extras:
                warn.add("skin_extras", "{n} skin(s) lost their skin-specific constraint/bone lists "
                                        "(not supported by Spine 3.7)")
            new_skins[s["name"]] = s.get("attachments") or {}
        data["skins"] = new_skins
    _rollback_curves(data.get("animations"))
    return data, warn.lines()


def convert_to_3653(data: dict) -> tuple:
    warn = _Warnings()
    data = _ensure_skeleton_tag(data, "3.6.53")
    animations = data.get("animations", {})
    if isinstance(animations, dict):
        for anim in animations.values():
            if isinstance(anim, dict) and "deform" in anim:
                anim["ffd"] = anim.pop("deform")
    return data, warn.lines()


def convert_to_destination(data: dict, dest: str) -> tuple:
    src_raw = _read_source_version(data)
    src_norm = _normalize_version(src_raw)
    dest_norm = _normalize_version(dest)
    all_warnings: list = []
    src_rank = _RANK.get(src_norm, 99)
    dest_rank = _RANK.get(dest_norm, 1)
    if src_rank < dest_rank and src_norm != "unknown":
        all_warnings.append(f"Cannot upgrade from {src_raw or 'unknown'} to {dest}")
        return data, all_warnings

    if src_norm == "unknown":
        if dest_rank <= 3:
            all_warnings.append(
                f"Source version '{src_raw or 'unknown'}' is not recognised; "
                "the JSON was not patched and is imported as it is")
        return data, all_warnings

    def refresh():
        nonlocal src_raw, src_norm
        src_raw = _read_source_version(data)
        src_norm = _normalize_version(src_raw)

    if src_norm == "4.x" and dest_rank <= 3:
        data, w = convert_to_38(data)
        all_warnings.extend(w)
        refresh()
    if src_norm == "3.8" and dest_rank <= 2:
        data, w = convert_to_37(data)
        all_warnings.extend(w)
        refresh()
    if src_norm == "3.7" and dest_rank <= 1:
        data, w = convert_to_3653(data)
        all_warnings.extend(w)
        refresh()
    return data, all_warnings
