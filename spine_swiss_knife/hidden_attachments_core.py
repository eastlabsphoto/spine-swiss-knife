"""Hidden attachments analysis / fix (pure logic, no Qt)."""

from __future__ import annotations

_NO_SCALE_PROPAGATION = ("onlyTranslation", "noScale", "noScaleOrReflection")
_SCALE_TIMELINES = ("scale", "scalex", "scaley")
_EPS = 1e-6


def _build_bone_maps(spine_data):
    """Return (children_map, bone_dict_map)."""
    children = {}
    bone_map = {}
    for bone in spine_data.get("bones", []):
        name = bone["name"]
        bone_map[name] = bone
        children.setdefault(name, [])
        parent = bone.get("parent")
        if parent is not None:
            children.setdefault(parent, [])
            children[parent].append(name)
    return children, bone_map


def _find_zero_scale_bones(bone_map):
    """Return set of bone names where scaleX == 0 or scaleY == 0."""
    result = set()
    for name, bone in bone_map.items():
        if bone.get("scaleX", 1) == 0 or bone.get("scaleY", 1) == 0:
            result.add(name)
    return result


def _collect_descendants(bone_name, children_map):
    """Return set of bone_name and all its descendants."""
    result = {bone_name}
    for child in children_map.get(bone_name, []):
        result |= _collect_descendants(child, children_map)
    return result


def _alpha_from_hex(color_hex):
    """Extract alpha (0-255) from an 8-char hex color string."""
    color_hex = str(color_hex)
    if len(color_hex) >= 8:
        return int(color_hex[6:8], 16)
    return 255


def _blocks_scale(bone) -> bool:
    mode = bone.get("inherit", bone.get("transform"))
    if mode in _NO_SCALE_PROPAGATION:
        return True
    return bone.get("inheritScale") is False


def _zero_scale_causes(bone_name, bone_map):
    """Zero-scale bones on the chain of *bone_name* that really zero its scale."""
    causes = []
    seen = set()
    cur = bone_name
    while cur and cur in bone_map and cur not in seen:
        seen.add(cur)
        bone = bone_map[cur]
        if bone.get("scaleX", 1) == 0 or bone.get("scaleY", 1) == 0:
            causes.append(cur)
        if _blocks_scale(bone):
            break
        cur = bone.get("parent")
    return causes


def _key_alpha_positive(tl_type, kf) -> bool:
    try:
        if tl_type == "alpha":
            return float(kf.get("value", 0) or 0) > 0
        if tl_type in ("twoColor", "rgba2"):
            return _alpha_from_hex(kf.get("light", "ffffffff")) > 0
        c = kf.get("color", kf.get("rgba", "ffffffff"))
        return _alpha_from_hex(c) > 0
    except (TypeError, ValueError, AttributeError):
        return True  # in doubt: may become visible


def _slot_can_gain_alpha(slot_tl) -> bool:
    for tl_type in ("color", "rgba", "twoColor", "rgba2", "alpha"):
        for kf in slot_tl.get(tl_type) or []:
            if isinstance(kf, dict) and _key_alpha_positive(tl_type, kf):
                return True
    return False


def _bone_has_scale_timeline(anim_data, bone_name) -> bool:
    tls = (anim_data.get("bones") or {}).get(bone_name)
    if not isinstance(tls, dict):
        return False
    return any(tls.get(t) for t in _SCALE_TIMELINES)


def analyze_hidden_attachments(spine_data):
    """Detect slots with active attachments that are invisible.

    Returns (candidates, anim_details, warnings).

    candidates - list of {slot_name, original_attachment, bone_name, reason, ...}
    anim_details - list of {animation, slot_name, action}
    warnings - list of mid-animation alpha-to-zero without attachment null
    """
    _children_map, bone_map = _build_bone_maps(spine_data)

    # ── Phase 1: find candidates ──
    candidates = []
    for slot in spine_data.get("slots", []):
        slot_name = slot["name"]
        attachment = slot.get("attachment")
        if attachment is None:
            continue

        bone_name = slot.get("bone", "")
        alpha = _alpha_from_hex(slot.get("color", "ffffffff"))
        zero_bones = _zero_scale_causes(bone_name, bone_map)

        causes = []
        if alpha == 0:
            causes.append("alpha_zero")
        if zero_bones:
            causes.append("bone_scale_zero")
        if not causes:
            continue

        cand = {
            "slot_name": slot_name,
            "original_attachment": attachment,
            "bone_name": bone_name,
            "reason": causes[0],
            "causes": causes,
        }
        if zero_bones:
            cand["zero_scale_bone"] = zero_bones[0]
            cand["zero_scale_bones"] = zero_bones
        candidates.append(cand)

    # ── Phase 2: classify candidate x animation ──
    animations = spine_data.get("animations", {})
    anim_details = []

    for cand in candidates:
        slot_name = cand["slot_name"]
        anims_affected = []

        for anim_name, anim_data in animations.items():
            slot_tl = (anim_data.get("slots") or {}).get(slot_name) or {}

            visible = True
            if "alpha_zero" in cand["causes"] and not _slot_can_gain_alpha(slot_tl):
                visible = False
            if visible and "bone_scale_zero" in cand["causes"]:
                if not any(_bone_has_scale_timeline(anim_data, b)
                           for b in cand["zero_scale_bones"]):
                    visible = False
            if not visible:
                continue  # always hidden in this animation

            att_keys = slot_tl.get("attachment") or []
            has_zero = any(isinstance(k, dict) and (k.get("time", 0) or 0) <= _EPS
                           for k in att_keys)
            action = "already_ok" if has_zero else "insert_kf"

            anim_details.append({
                "animation": anim_name,
                "slot_name": slot_name,
                "action": action,
            })
            anims_affected.append(anim_name)

        cand["anims_affected"] = anims_affected

    # ── Phase 3: mid-animation warnings (all slots) ──
    warnings = []
    for anim_name, anim_data in animations.items():
        for slot_name, timelines in (anim_data.get("slots") or {}).items():
            color_keys = timelines.get("color") or timelines.get("rgba") or []
            att_keys = timelines.get("attachment") or []

            for kf in color_keys:
                t = kf.get("time", 0)
                if t == 0:
                    continue
                c = kf.get("color", kf.get("rgba", "ffffffff"))
                if _alpha_from_hex(c) > 0:
                    continue
                has_null = any(
                    ak.get("name") is None and abs(ak.get("time", 0) - t) < 0.001
                    for ak in att_keys
                )
                if not has_null:
                    warnings.append({
                        "animation": anim_name,
                        "slot_name": slot_name,
                        "time": t,
                    })

    return candidates, anim_details, warnings


def fix_hidden_attachments(spine_data, candidates, anim_details):
    """Apply fixes: null setup-pose attachments, insert animation keyframes.

    Returns (slots_fixed, keyframes_inserted).
    """
    candidate_names = {c["slot_name"] for c in candidates}
    candidate_att = {c["slot_name"]: c["original_attachment"] for c in candidates}

    slots_fixed = 0
    for slot in spine_data.get("slots", []):
        if slot["name"] in candidate_names and slot.get("attachment") is not None:
            slot["attachment"] = None
            slots_fixed += 1

    keyframes_inserted = 0
    animations = spine_data.get("animations", {})

    for detail in anim_details:
        if detail["action"] != "insert_kf":
            continue

        anim_name = detail["animation"]
        slot_name = detail["slot_name"]
        original = candidate_att.get(slot_name)
        if original is None:
            continue

        anim_data = animations.get(anim_name)
        if anim_data is None:
            continue

        slot_tl = anim_data.setdefault("slots", {}).setdefault(slot_name, {})
        keys = slot_tl.setdefault("attachment", [])
        if any(isinstance(k, dict) and (k.get("time", 0) or 0) <= _EPS for k in keys):
            continue
        keys.insert(0, {"time": 0, "name": original})
        keyframes_inserted += 1

    return slots_fixed, keyframes_inserted
