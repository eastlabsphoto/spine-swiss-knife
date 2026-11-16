"""Keyframe redundancy detection (pure logic, no Qt).

Guarantee: after removing the reported keys, for every removed key the value
linearly interpolated between the surviving neighbours differs from the removed
key's value by less than the tolerance in every channel.
"""

from __future__ import annotations

from .spine_json import is_spine4

# timeline -> (fields, defaults, tolerance factor)
_BONE_TIMELINES = {
    "rotate": (("angle",), (0.0,), 1.0),  # field is "value" on 4.x, see _key_values
    "translate": (("x", "y"), (0.0, 0.0), 1.0),
    "scale": (("x", "y"), (1.0, 1.0), 0.01),
    "shear": (("x", "y"), (0.0, 0.0), 1.0),
    "translatex": (("value",), (0.0,), 1.0),
    "translatey": (("value",), (0.0,), 1.0),
    "scalex": (("value",), (1.0,), 0.01),
    "scaley": (("value",), (1.0,), 0.01),
    "shearx": (("value",), (0.0,), 1.0),
    "sheary": (("value",), (0.0,), 1.0),
}
# slot timelines: kind "hex" (colour string field) or "num"
_SLOT_TIMELINES = {
    "color": ("hex", "color", "ffffffff"),
    "rgba": ("hex", "color", "ffffffff"),
    "rgb": ("hex", "color", "ffffff"),
    "alpha": ("num", "value", 0.0),
}
_COLOR_FACTOR = 0.01


def _hex_to_floats(raw, default):
    s = str(raw if raw is not None else default).lower()
    if len(s) % 2 or not s:
        raise ValueError("bad colour")
    return [int(s[i:i + 2], 16) / 255.0 for i in range(0, len(s), 2)]


def _num(v, default):
    if v is None:
        return float(default)
    if isinstance(v, bool):
        raise ValueError("bad number")
    return float(v)


def _key_values(key, tl_type, is_bone, is4):
    """List of channel values for a key, or raises (ValueError/TypeError)."""
    if is_bone:
        fields, defaults, _f = _BONE_TIMELINES[tl_type]
        if tl_type == "rotate":
            field = "value" if (is4 or "value" in key) else "angle"
            return [_num(key.get(field), 0.0)]
        return [_num(key.get(f), d) for f, d in zip(fields, defaults)]
    kind, field, default = _SLOT_TIMELINES[tl_type]
    if kind == "hex":
        return _hex_to_floats(key.get(field), default)
    return [_num(key.get(field), default)]


def _time(key):
    t = key.get("time", 0)
    return 0.0 if t is None else float(t)


def _simplify_run(times, vals, lo, hi, tols, limit180, out_removed):
    """Ramer-Douglas-Peucker on keys lo..hi (inclusive ends are kept)."""
    if hi - lo < 2:
        return
    t0, t1 = times[lo], times[hi]
    dt = t1 - t0
    nch = len(tols)
    best_i = -1
    best_ratio = -1.0
    ok = dt > 0
    if ok:
        for i in range(lo + 1, hi):
            r = (times[i] - t0) / dt
            worst = 0.0
            for c in range(nch):
                exp = vals[lo][c] + r * (vals[hi][c] - vals[lo][c])
                ratio = abs(exp - vals[i][c]) / tols[c] if tols[c] > 0 else (
                    0.0 if exp == vals[i][c] else float("inf"))
                if ratio > worst:
                    worst = ratio
            if worst > best_ratio:
                best_ratio = worst
                best_i = i
        if best_ratio >= 1.0:
            ok = False
        elif limit180 and abs(vals[hi][0] - vals[lo][0]) >= 180.0:
            ok = False
            best_i = (lo + hi) // 2
    else:
        best_i = (lo + hi) // 2
    if ok:
        out_removed.extend(range(lo + 1, hi))
        return
    _simplify_run(times, vals, lo, best_i, tols, limit180, out_removed)
    _simplify_run(times, vals, best_i, hi, tols, limit180, out_removed)


def redundant_indices(keys, tl_type, is_bone, tolerance, is4):
    """Sorted indices of removable keys of one timeline (guaranteed within tolerance)."""
    n = len(keys)
    if n < 3 or not all(isinstance(k, dict) for k in keys):
        return []
    try:
        vals = [_key_values(k, tl_type, is_bone, is4) for k in keys]
    except (ValueError, TypeError):
        return []
    nch = len(vals[0])
    if any(len(v) != nch for v in vals):
        return []
    try:
        times = [_time(k) for k in keys]
    except (TypeError, ValueError):
        return []
    if is_bone:
        factor = _BONE_TIMELINES[tl_type][2]
    else:
        factor = _COLOR_FACTOR
    tol = float(tolerance) * factor
    tols = [tol] * nch
    limit180 = is_bone and tl_type == "rotate" and not is4

    boundary = [False] * n
    boundary[0] = boundary[-1] = True
    for i in range(n):
        if "curve" in keys[i]:
            boundary[i] = True
            if i + 1 < n:
                boundary[i + 1] = True
    if limit180:
        for i in range(n - 1):
            if abs(vals[i + 1][0] - vals[i][0]) >= 180.0:
                boundary[i] = boundary[i + 1] = True
    removed: list = []
    start = 0
    for i in range(1, n):
        if boundary[i]:
            _simplify_run(times, vals, start, i, tols, limit180, removed)
            start = i
    removed.sort()
    return removed


def _count_keys(obj) -> int:
    if isinstance(obj, list):
        return len(obj)
    if isinstance(obj, dict):
        return sum(_count_keys(v) for v in obj.values())
    return 0


def find_redundant_keyframes(animations, tolerance, spine_data=None):
    is4 = bool(spine_data) and is_spine4(spine_data)
    results = {}
    for anim_name, anim_data in animations.items():
        if not isinstance(anim_data, dict):
            continue
        res = {"bones": {}, "slots": {}, "total_keys": 0, "redundant_keys": 0}

        for bone_name, timelines in (anim_data.get("bones") or {}).items():
            if not isinstance(timelines, dict):
                continue
            found = {}
            for tl_type, keys in timelines.items():
                if not isinstance(keys, list):
                    continue
                res["total_keys"] += len(keys)
                if tl_type in _BONE_TIMELINES:
                    idx = redundant_indices(keys, tl_type, True, tolerance, is4)
                    if idx:
                        found[tl_type] = idx
                        res["redundant_keys"] += len(idx)
            if found:
                res["bones"][bone_name] = found

        for slot_name, timelines in (anim_data.get("slots") or {}).items():
            if not isinstance(timelines, dict):
                continue
            found = {}
            for tl_type, keys in timelines.items():
                if not isinstance(keys, list):
                    continue
                res["total_keys"] += len(keys)
                if tl_type in _SLOT_TIMELINES:
                    idx = redundant_indices(keys, tl_type, False, tolerance, is4)
                    if idx:
                        found[tl_type] = idx
                        res["redundant_keys"] += len(idx)
            if found:
                res["slots"][slot_name] = found

        for field, val in anim_data.items():
            if field in ("bones", "slots"):
                continue
            res["total_keys"] += _count_keys(val)

        if res["total_keys"] > 0:
            results[anim_name] = res
    return results


def remove_redundant_keyframes(spine_data, analysis):
    animations = spine_data.get("animations", {})
    removed = 0
    for anim_name, info in analysis.items():
        anim_data = animations.get(anim_name)
        if anim_data is None:
            continue
        for section in ("bones", "slots"):
            owners = anim_data.get(section, {})
            for owner_name, timelines in info[section].items():
                owner = owners.get(owner_name)
                if owner is None:
                    continue
                for tl_type, indices in timelines.items():
                    keys = owner.get(tl_type)
                    if keys is None:
                        continue
                    for idx in sorted(set(indices), reverse=True):
                        if idx < len(keys):
                            keys.pop(idx)
                            removed += 1
    return removed
