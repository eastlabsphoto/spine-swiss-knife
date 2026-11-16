"""Splitter core — pure logic (no Qt, no i18n) for splitting a Spine skeleton
into sub-skeletons by animation groups."""

from __future__ import annotations

import json as _json
import os
import re
import shutil
from copy import deepcopy

from .spine_json import (
    attachment_image_key, get_draw_order_timeline, draw_order_key,
    is_skins_list, is_weighted, iter_deform_timelines, skin_entries,
    weighted_bone_indices, IMAGE_ATTACHMENT_TYPES,
)
from .spine_refs import (
    apply_draw_order_offsets, bone_index_map, encode_draw_order_offsets,
    remap_bone_indices,
)
from .atlas_parser import collect_images

CONSTRAINT_KINDS = ("ik", "transform", "path", "physics")
SKIN_EXTRA_KEYS = ("bones", "ik", "transform", "path", "physics")


# ==========================================================================
# Saving in Spine's whitespace format
# ==========================================================================

def _dumps(value) -> str:
    return _json.dumps(value, ensure_ascii=False, separators=(", ", ": "))


def _brace(s: str) -> str:
    if s.startswith("{") and s.endswith("}") and len(s) > 2:
        return "{ " + s[1:-1] + " }"
    return s


def _join_blocks(blocks: list) -> list:
    """Flatten blocks (lists of lines) adding a comma after all but the last."""
    lines: list = []
    for i, block in enumerate(blocks):
        block = list(block)
        if i < len(blocks) - 1 and block:
            block[-1] += ","
        lines.extend(block)
    return lines


def _skin_block(skin: dict) -> list:
    blocks = []
    for k, v in skin.items():
        if k == "attachments" and isinstance(v, dict):
            sub = []
            for slot_name, slot_atts in v.items():
                sub.append(['%s: %s' % (_dumps(slot_name), _dumps(slot_atts))])
            blocks.append(['"attachments": {'] + _join_blocks(sub) + ["}"])
        else:
            blocks.append(["%s: %s" % (_dumps(k), _dumps(v))])
    return ["{"] + _join_blocks(blocks) + ["}"]


def format_spine_json(data: dict) -> str:
    """Serialise *data* in the whitespace format Spine itself exports.

    The result always ``json.loads`` back to *data*.
    """
    top = []
    for key, val in data.items():
        kj = _dumps(key)
        if key == "skeleton" and isinstance(val, dict):
            top.append(["%s: %s" % (kj, _brace(_dumps(val)))])
        elif key == "skins" and isinstance(val, list) and all(
                isinstance(s, dict) for s in val):
            sub = [_skin_block(s) for s in val]
            top.append(["%s: [" % kj] + _join_blocks(sub) + ["]"])
        elif isinstance(val, list):
            items = [["\t" + _brace(_dumps(item))] for item in val]
            top.append(["%s: [" % kj] + _join_blocks(items) + ["]"])
        elif isinstance(val, dict) and key in ("skins", "animations", "events"):
            entries = []
            for dk, dv in val.items():
                if isinstance(dv, dict):
                    inner = [["%s: %s" % (_dumps(ik), _dumps(iv))]
                             for ik, iv in dv.items()]
                    entries.append(["%s: {" % _dumps(dk)] + _join_blocks(inner) + ["}"])
                else:
                    entries.append(["%s: %s" % (_dumps(dk), _dumps(dv))])
            top.append(["%s: {" % kj] + _join_blocks(entries) + ["}"])
        else:
            top.append(["%s: %s" % (kj, _dumps(val))])
    return "\n".join(["{"] + _join_blocks(top) + ["}"])


def save_spine_format(path: str, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        f.write(format_spine_json(data))


# ==========================================================================
# Names
# ==========================================================================

_BAD_CHARS = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul"} | {"com%d" % i for i in range(1, 10)} \
    | {"lpt%d" % i for i in range(1, 10)}


def sanitize_name(name: str) -> str:
    """Make *name* usable as a single directory / file name."""
    s = _BAD_CHARS.sub("_", str(name))
    s = re.sub(r"^[. ]+|[. ]+$", lambda m: "_" * len(m.group(0)), s)
    if not s:
        s = "_"
    if s.split(".")[0].lower() in _RESERVED:
        s = "_" + s
    return s


def sanitize_group_names(names) -> dict:
    """group name -> unique filesystem-safe name (case-insensitive unique)."""
    out: dict = {}
    used: set = set()
    for name in names:
        base = sanitize_name(name)
        cand = base
        n = 2
        while cand.lower() in used:
            cand = "%s_%d" % (base, n)
            n += 1
        used.add(cand.lower())
        out[name] = cand
    return out


# ==========================================================================
# Dependencies
# ==========================================================================

def _build_bone_hierarchy(bones_list: list):
    parent_map = {}
    bone_data_map = {}
    for bone in bones_list:
        name = bone["name"]
        parent_map[name] = bone.get("parent")
        bone_data_map[name] = bone
    return parent_map, bone_data_map


def _ancestors(bone_name: str, parent_map: dict) -> set:
    result = set()
    current = bone_name
    while current and current not in result:
        result.add(current)
        current = parent_map.get(current)
    return result


def _get_slot_bone_map(slots_list: list) -> dict:
    return {s["name"]: s["bone"] for s in slots_list}


def _get_slot_default_attachment(slots_list: list) -> dict:
    return {s["name"]: s.get("attachment") for s in slots_list}


def _constraint_bones(c: dict) -> list:
    b = c.get("bones")
    if isinstance(b, str):
        return [b]
    if isinstance(b, list):
        return [x for x in b if isinstance(x, str)]
    return []


def _constraint_refs(kind: str, c: dict):
    """(bone names, target bone or None, target slot or None)."""
    bones = _constraint_bones(c)
    if kind == "physics":
        b = c.get("bone")
        return ([b] if isinstance(b, str) else []), None, None
    if kind == "path":
        return bones, None, c.get("target")
    return bones, c.get("target"), None


def _slot_alpha(slot: dict) -> float:
    color = slot.get("color")
    if isinstance(color, str) and len(color) >= 8:
        try:
            return int(color[6:8], 16) / 255.0
        except ValueError:
            return 1.0
    return 1.0


def _animated_slots_of(anim: dict):
    """Yield (slot name, attachment names referenced) for an animation."""
    slots_block = anim.get("slots")
    if isinstance(slots_block, dict):
        for slot_name, channels in slots_block.items():
            names = set()
            if isinstance(channels, dict) and isinstance(channels.get("attachment"), list):
                for key in channels["attachment"]:
                    if isinstance(key, dict) and key.get("name") is not None:
                        names.add(key["name"])
            yield slot_name, names
    for entry in get_draw_order_timeline(anim):
        if isinstance(entry, dict):
            for offset in entry.get("offsets") or []:
                if isinstance(offset, dict) and "slot" in offset:
                    yield offset["slot"], set()
    for _skin, slot, att, _keys in iter_deform_timelines(anim):
        yield slot, {att}


def compute_dependencies(spine_data: dict, anim_names: list,
                         include_static_visible: bool = True) -> dict:
    bones_list = spine_data.get("bones", []) or []
    slots_list = spine_data.get("slots", []) or []
    animations = spine_data.get("animations", {}) or {}

    parent_map, _bone_data = _build_bone_hierarchy(bones_list)
    slot_bone_map = _get_slot_bone_map(slots_list)
    slot_default_att = _get_slot_default_attachment(slots_list)
    slot_order = [s["name"] for s in slots_list]
    bone_names_by_index = [b["name"] for b in bones_list]
    entries = skin_entries(spine_data)

    animated_bones: set = set()
    extra_slots: set = set()
    slot_att_refs: dict = {}
    timeline_constraints = {k: set() for k in CONSTRAINT_KINDS}

    for anim_name in anim_names:
        anim = animations.get(anim_name)
        if not isinstance(anim, dict):
            continue
        if isinstance(anim.get("bones"), dict):
            animated_bones.update(anim["bones"].keys())
        for slot_name, atts in _animated_slots_of(anim):
            extra_slots.add(slot_name)
            if atts:
                slot_att_refs.setdefault(slot_name, set()).update(atts)
        for kind in CONSTRAINT_KINDS:
            block = anim.get(kind)
            if isinstance(block, dict):
                timeline_constraints[kind].update(block.keys())

    if include_static_visible:
        for s in slots_list:
            if s.get("attachment") and _slot_alpha(s) > 0:
                extra_slots.add(s["name"])

    bones_needed: set = set()
    for bone in animated_bones:
        if bone in parent_map:
            bones_needed |= _ancestors(bone, parent_map)
    for slot in extra_slots:
        b = slot_bone_map.get(slot)
        if b:
            bones_needed |= _ancestors(b, parent_map)

    involved = {k: set() for k in CONSTRAINT_KINDS}
    slots_needed: set = set()
    attachments_needed: dict = {}

    def compute_attachments():
        res: dict = {}
        for slot_name in slots_needed:
            atts = set()
            d = slot_default_att.get(slot_name)
            if d:
                atts.add(d)
            atts |= slot_att_refs.get(slot_name, set())
            if not atts:
                for _sn, sd in entries:
                    if slot_name in sd and isinstance(sd[slot_name], dict):
                        atts |= set(sd[slot_name].keys())
            res[slot_name] = atts
        # linked mesh parents
        changed = True
        while changed:
            changed = False
            for _sn, sd in entries:
                for slot_name, slot_atts in sd.items():
                    if slot_name not in res or not isinstance(slot_atts, dict):
                        continue
                    for att_name, att in slot_atts.items():
                        if (att_name in res[slot_name] and isinstance(att, dict)
                                and att.get("type") == "linkedmesh"):
                            parent = att.get("parent")
                            if parent and parent not in res[slot_name]:
                                res[slot_name].add(parent)
                                changed = True
        return {k: v for k, v in res.items() if v}

    guard = 0
    while True:
        guard += 1
        before = (len(bones_needed), len(slots_needed), len(extra_slots),
                  sum(len(v) for v in involved.values()))
        for s in slots_list:
            if s["bone"] in bones_needed or s["name"] in extra_slots:
                slots_needed.add(s["name"])
        for slot in list(slots_needed):
            b = slot_bone_map.get(slot)
            if b:
                bones_needed |= _ancestors(b, parent_map)
        attachments_needed = compute_attachments()

        # clipping end slots (and everything in between in draw order)
        for _sn, sd in entries:
            for slot_name, slot_atts in sd.items():
                if slot_name not in slots_needed or not isinstance(slot_atts, dict):
                    continue
                needed = attachments_needed.get(slot_name, set())
                for att_name, att in slot_atts.items():
                    if not isinstance(att, dict) or att.get("type") != "clipping":
                        continue
                    if needed and att_name not in needed:
                        continue
                    end_slot = att.get("end")
                    if not end_slot:
                        continue
                    if slot_name in slot_order and end_slot in slot_order:
                        lo = slot_order.index(slot_name)
                        hi = slot_order.index(end_slot)
                        rng = slot_order[lo:hi + 1]
                    else:
                        rng = [end_slot] if end_slot in slot_bone_map else []
                    for s in rng:
                        if s not in slots_needed:
                            extra_slots.add(s)
                            slots_needed.add(s)

        # weighted attachments pull in the bones they use
        for _sn, sd in entries:
            for slot_name, slot_atts in sd.items():
                if slot_name not in attachments_needed or not isinstance(slot_atts, dict):
                    continue
                for att_name, att in slot_atts.items():
                    if att_name not in attachments_needed[slot_name]:
                        continue
                    if is_weighted(att):
                        try:
                            idxs = weighted_bone_indices(att["vertices"])
                        except ValueError:
                            continue
                        for i in idxs:
                            if 0 <= i < len(bone_names_by_index):
                                bones_needed |= _ancestors(bone_names_by_index[i],
                                                           parent_map)

        # constraints
        for kind in CONSTRAINT_KINDS:
            for c in spine_data.get(kind) or []:
                if not isinstance(c, dict):
                    continue
                name = c.get("name")
                cbones, tbone, tslot = _constraint_refs(kind, c)
                is_inv = (name in involved[kind] or name in timeline_constraints[kind]
                          or any(b in bones_needed for b in cbones))
                if not is_inv:
                    continue
                involved[kind].add(name)
                for b in cbones + ([tbone] if tbone else []):
                    if b in parent_map:
                        bones_needed |= _ancestors(b, parent_map)
                if tslot and tslot in slot_bone_map:
                    extra_slots.add(tslot)
                    slots_needed.add(tslot)
                    bones_needed |= _ancestors(slot_bone_map[tslot], parent_map)

        after = (len(bones_needed), len(slots_needed), len(extra_slots),
                 sum(len(v) for v in involved.values()))
        if after == before or guard > 1000:
            break

    attachments_needed = compute_attachments()

    image_keys = set()
    for _sn, sd in entries:
        for slot_name, slot_atts in sd.items():
            if slot_name not in attachments_needed or not isinstance(slot_atts, dict):
                continue
            for att_name, att in slot_atts.items():
                if att_name in attachments_needed[slot_name] and isinstance(att, dict):
                    if att.get("type") in IMAGE_ATTACHMENT_TYPES:
                        image_keys.add(attachment_image_key(att_name, att))

    return {
        "bones_needed": bones_needed,
        "slots_needed": slots_needed,
        "attachments_needed": attachments_needed,
        "image_keys_needed": image_keys,
        "constraints_needed": involved,
    }


# ==========================================================================
# Build output
# ==========================================================================

def _rewrite_draw_order(anim: dict, src_slots: list, out_slots: list):
    key = draw_order_key(anim)
    if key is None or not isinstance(anim.get(key), list):
        return
    out_set = set(out_slots)
    for k in anim[key]:
        if not isinstance(k, dict) or not k.get("offsets"):
            continue
        try:
            absolute = apply_draw_order_offsets(src_slots, k["offsets"])
            restricted = [s for s in absolute if s in out_set]
            offsets = encode_draw_order_offsets(out_slots, restricted)
        except ValueError:
            offsets = [o for o in k["offsets"]
                       if isinstance(o, dict) and o.get("slot") in out_set]
        if offsets:
            k["offsets"] = offsets
        else:
            del k["offsets"]


def build_split_json(spine_data: dict, anim_names: list, deps: dict,
                     group_name: str = "", images_path: str = "") -> dict:
    """Build the split skeleton. *spine_data* is never modified."""
    bones_needed = deps["bones_needed"]
    slots_needed = deps["slots_needed"]
    attachments_needed = deps["attachments_needed"]
    constraints_needed = deps.get("constraints_needed")
    result: dict = {}

    if "skeleton" in spine_data:
        result["skeleton"] = dict(spine_data["skeleton"])
        if images_path:
            result["skeleton"]["images"] = images_path

    src_bones = spine_data.get("bones", []) or []
    src_slots = spine_data.get("slots", []) or []
    result["bones"] = [deepcopy(b) for b in src_bones if b["name"] in bones_needed]
    result["slots"] = [deepcopy(s) for s in src_slots if s["name"] in slots_needed]
    out_bone_names = {b["name"] for b in result["bones"]}
    out_slot_names = [s["name"] for s in result["slots"]]
    out_slot_set = set(out_slot_names)

    def filter_atts(atts: dict) -> dict:
        out = {}
        for slot_name, slot_atts in atts.items():
            if slot_name not in out_slot_set or not isinstance(slot_atts, dict):
                continue
            needed = attachments_needed.get(slot_name, set())
            kept = {n: deepcopy(a) for n, a in slot_atts.items()
                    if n in needed or not needed}
            if kept:
                out[slot_name] = kept
        return out

    # ---- constraints (computed first: skin extras are filtered by them) ----
    out_constraints: dict = {}
    for kind in CONSTRAINT_KINDS:
        if kind not in spine_data:
            continue
        kept = []
        for c in spine_data[kind] or []:
            if not isinstance(c, dict):
                continue
            if constraints_needed is not None and \
                    c.get("name") not in constraints_needed.get(kind, ()):
                continue
            cbones, tbone, tslot = _constraint_refs(kind, c)
            if not cbones and kind != "physics":
                continue
            if any(b not in out_bone_names for b in cbones):
                continue
            if kind == "physics" and not cbones:
                continue
            if tbone is not None and tbone not in out_bone_names:
                continue
            if kind == "path" and tslot not in out_slot_set:
                continue
            if kind in ("ik", "transform") and tbone is None:
                continue
            kept.append(deepcopy(c))
        if kept:
            out_constraints[kind] = kept
    constraint_names = {k: {c.get("name") for c in v}
                        for k, v in out_constraints.items()}

    # ---- skins ----
    skins = spine_data.get("skins", {})
    if isinstance(skins, list):
        out_skins: list = []
        for sk in skins:
            if not isinstance(sk, dict):
                continue
            new_sk: dict = {}
            has_extra = False
            for k, v in sk.items():
                if k == "attachments":
                    continue
                if k == "bones" and isinstance(v, list):
                    v = [b for b in v if b in out_bone_names]
                    has_extra = has_extra or bool(v)
                elif k in ("ik", "transform", "path", "physics") and isinstance(v, list):
                    v = [n for n in v if n in constraint_names.get(k, ())]
                    has_extra = has_extra or bool(v)
                new_sk[k] = deepcopy(v)
            atts = filter_atts(sk.get("attachments") or {})
            if atts:
                new_sk["attachments"] = atts
            if atts or has_extra or sk.get("name", "default") == "default":
                out_skins.append(new_sk)
        result["skins"] = out_skins
    else:
        out_skins_d: dict = {}
        for skin_name, sd in (skins or {}).items():
            atts = filter_atts(sd if isinstance(sd, dict) else {})
            if atts or skin_name == "default":
                out_skins_d[skin_name] = atts
        result["skins"] = out_skins_d

    # ---- events ----
    if "events" in spine_data:
        used_events = set()
        for anim_name in anim_names:
            anim = (spine_data.get("animations") or {}).get(anim_name, {})
            for ev in anim.get("events", []) or []:
                if isinstance(ev, dict) and "name" in ev:
                    used_events.add(ev["name"])
        if used_events:
            result["events"] = {k: deepcopy(v) for k, v in spine_data["events"].items()
                                if k in used_events}

    for kind, lst in out_constraints.items():
        result[kind] = lst

    # ---- bone indices of weighted attachments ----
    index_map = bone_index_map(src_bones, result["bones"])
    remap_bone_indices(result, index_map)

    # ---- animations ----
    anims = {}
    for name in anim_names:
        if name in (spine_data.get("animations") or {}):
            a = deepcopy(spine_data["animations"][name])
            if isinstance(a, dict):
                _rewrite_draw_order(a, [s["name"] for s in src_slots], out_slot_names)
            anims[name] = a
    result["animations"] = anims

    return result


# ==========================================================================
# Writing groups to disk
# ==========================================================================

def split_to_directory(spine_data: dict, groups: dict, output_dir: str,
                       images_dir: str = "", images_path: str = "",
                       include_static_visible: bool = True,
                       json_name_fallback: str = "") -> tuple:
    """Write one sub-project per group. Returns (created, errors, details)."""
    all_images = {}
    if images_dir and os.path.isdir(images_dir):
        all_images = collect_images(images_dir)

    safe = sanitize_group_names(sorted(groups.keys()))
    created = 0
    errors: list = []
    details: dict = {}
    for group_name, anim_names in sorted(groups.items()):
        fs_name = safe[group_name]
        group_dir = os.path.join(output_dir, fs_name)
        group_images_dir = os.path.join(group_dir, "images")
        try:
            os.makedirs(group_images_dir, exist_ok=True)
            deps = compute_dependencies(spine_data, anim_names,
                                        include_static_visible=include_static_visible)
            split_json = build_split_json(spine_data, anim_names, deps,
                                          group_name=group_name,
                                          images_path=images_path)
            save_spine_format(os.path.join(group_dir, fs_name + ".json"), split_json)
            copied = 0
            for image_key in sorted(deps["image_keys_needed"]):
                src = all_images.get(image_key)
                if src:
                    rel = os.path.relpath(src, images_dir)
                    dst = os.path.join(group_images_dir, rel)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
                    copied += 1
            details[group_name] = {"dir": group_dir, "images_copied": copied}
            created += 1
        except Exception as e:  # noqa: BLE001 - reported to the user
            errors.append("Group '%s': %s" % (group_name, e))
    return created, errors, details
