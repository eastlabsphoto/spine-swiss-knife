"""Reference-aware edits of Spine JSON: bones, slot renames, draw-order offsets.

Pure logic (no Qt, no i18n).
"""

from __future__ import annotations

from .spine_json import (
    is_weighted, iter_all_attachments, remap_weighted_vertices,
    weighted_bone_indices,
)


# ---------------------------------------------------------------
# Bones
# ---------------------------------------------------------------

def _bones(spine_data) -> list:
    b = spine_data.get("bones")
    return [x for x in b if isinstance(x, dict)] if isinstance(b, list) else []


def _constraint_bones(c: dict) -> list:
    b = c.get("bones")
    if isinstance(b, str):
        return [b]
    if isinstance(b, list):
        return [x for x in b if isinstance(x, str)]
    return []


def collect_bone_references(spine_data) -> dict:
    """bone name -> set of reasons ("slot", "animation", "constraint", "skin", "weights")."""
    refs: dict = {}

    def add(name, reason):
        if isinstance(name, str):
            refs.setdefault(name, set()).add(reason)

    bones = spine_data.get("bones") or []
    for s in spine_data.get("slots") or []:
        if isinstance(s, dict):
            add(s.get("bone"), "slot")
    for anim in (spine_data.get("animations") or {}).values():
        if isinstance(anim, dict) and isinstance(anim.get("bones"), dict):
            for bn in anim["bones"]:
                add(bn, "animation")

    slot_bone = {s.get("name"): s.get("bone") for s in spine_data.get("slots") or []
                 if isinstance(s, dict)}
    for kind in ("ik", "transform", "path", "physics"):
        for c in spine_data.get(kind) or []:
            if not isinstance(c, dict):
                continue
            for bn in _constraint_bones(c):
                add(bn, "constraint")
            if kind == "physics":
                add(c.get("bone"), "constraint")
            elif kind == "path":
                add(slot_bone.get(c.get("target")), "constraint")
            else:
                add(c.get("target"), "constraint")

    skins = spine_data.get("skins")
    if isinstance(skins, list):
        for sk in skins:
            if isinstance(sk, dict) and isinstance(sk.get("bones"), list):
                for bn in sk["bones"]:
                    add(bn, "skin")

    for att in (a for _s, _sl, _n, a in iter_all_attachments(spine_data)
                if is_weighted(a)):
        try:
            idxs = weighted_bone_indices(att["vertices"])
        except ValueError:
            continue
        for i in idxs:
            if isinstance(i, int) and 0 <= i < len(bones):
                add(bones[i].get("name"), "weights")
    return refs


def find_dead_bones(spine_data):
    """(dead bone names in skeleton order, {alive bone: sorted reasons})."""
    bones = _bones(spine_data)
    refs = collect_bone_references(spine_data)
    children: dict = {}
    for b in bones:
        children.setdefault(b.get("parent"), []).append(b.get("name"))
    alive: dict = {}
    for b in reversed(bones):  # children come after parents
        name = b.get("name")
        reasons = set(refs.get(name, ()))
        if any(c in alive for c in children.get(name, ())):
            reasons.add("has_alive_child")
        if reasons:
            alive[name] = sorted(reasons)
    dead = [b.get("name") for b in bones if b.get("name") not in alive]
    return dead, alive


def bone_index_map(old_bones: list, new_bones: list) -> dict:
    """old index -> new index by bone name (bones present in both)."""
    new_idx = {}
    for i, b in enumerate(new_bones):
        new_idx.setdefault(b.get("name"), i)
    out = {}
    for i, b in enumerate(old_bones):
        n = b.get("name")
        if n in new_idx:
            out[i] = new_idx[n]
    return out


def remap_bone_indices(spine_data, index_map) -> int:
    """Remap bone indices of every weighted attachment in place; returns count."""
    count = 0
    for att in iter_all_attachments(spine_data):
        att = att[3]
        if is_weighted(att):
            att["vertices"] = remap_weighted_vertices(att["vertices"], index_map)
            count += 1
    return count


def remove_bones(spine_data, names) -> int:
    """Remove bones in place (validated); returns number removed."""
    names = set(names)
    bones = spine_data.get("bones") or []
    existing = {b.get("name") for b in bones if isinstance(b, dict)}
    targets = names & existing
    if not targets:
        return 0

    refs = collect_bone_references(spine_data)
    blockers = []
    for n in sorted(targets):
        bad = sorted(refs.get(n, set()) & {"slot", "constraint", "skin", "weights"})
        if bad:
            blockers.append("bone '%s' is referenced (%s)" % (n, ", ".join(bad)))
    for b in bones:
        if b.get("parent") in targets and b.get("name") not in targets:
            blockers.append("bone '%s' is the parent of remaining bone '%s'"
                            % (b.get("parent"), b.get("name")))
    if blockers:
        raise ValueError("; ".join(blockers))

    new_bones = [b for b in bones if b.get("name") not in targets]
    index_map = bone_index_map(bones, new_bones)
    remap_bone_indices(spine_data, index_map)
    spine_data["bones"] = new_bones
    for anim in (spine_data.get("animations") or {}).values():
        if isinstance(anim, dict) and isinstance(anim.get("bones"), dict):
            for n in targets:
                anim["bones"].pop(n, None)
    return len(targets)


# ---------------------------------------------------------------
# Slot renames
# ---------------------------------------------------------------

def find_rename_collisions(spine_data, rename_map: dict) -> list:
    """Messages describing collisions of a simultaneous slot rename."""
    effective = {o: n for o, n in rename_map.items() if o != n}
    msgs = []
    by_new: dict = {}
    for old, new in effective.items():
        by_new.setdefault(new, []).append(old)
    for new, olds in by_new.items():
        if len(olds) > 1:
            msgs.append("slots %s would all be renamed to '%s'"
                        % (", ".join("'%s'" % o for o in sorted(olds)), new))
    existing = {s.get("name") for s in spine_data.get("slots") or []
                if isinstance(s, dict)}
    for new, olds in by_new.items():
        if new in existing and new not in effective:
            msgs.append("new slot name '%s' (from '%s') already exists"
                        % (new, "', '".join(sorted(olds))))
    return msgs


def _rekey(d: dict, m: dict) -> dict:
    """New dict with keys renamed simultaneously, preserving order."""
    return {m.get(k, k): v for k, v in d.items()}


def rename_slots(spine_data, rename_map) -> None:
    """Simultaneous in-place slot rename across the whole skeleton."""
    msgs = find_rename_collisions(spine_data, rename_map)
    if msgs:
        raise ValueError("; ".join(msgs))
    m = {o: n for o, n in rename_map.items() if o != n}
    if not m:
        return

    for s in spine_data.get("slots") or []:
        if isinstance(s, dict) and s.get("name") in m:
            s["name"] = m[s["name"]]

    skins = spine_data.get("skins")
    if isinstance(skins, dict):
        for sname, atts in list(skins.items()):
            if isinstance(atts, dict):
                skins[sname] = _rekey(atts, m)
    elif isinstance(skins, list):
        for sk in skins:
            if isinstance(sk, dict) and isinstance(sk.get("attachments"), dict):
                sk["attachments"] = _rekey(sk["attachments"], m)

    for _sk, _sl, _n, att in iter_all_attachments(spine_data):
        if att.get("type") == "clipping" and att.get("end") in m:
            att["end"] = m[att["end"]]

    for c in spine_data.get("path") or []:
        if isinstance(c, dict) and c.get("target") in m:
            c["target"] = m[c["target"]]

    for anim in (spine_data.get("animations") or {}).values():
        if not isinstance(anim, dict):
            continue
        if isinstance(anim.get("slots"), dict):
            anim["slots"] = _rekey(anim["slots"], m)
        for key in ("drawOrder", "draworder"):
            if isinstance(anim.get(key), list):
                for k in anim[key]:
                    if isinstance(k, dict) and isinstance(k.get("offsets"), list):
                        for o in k["offsets"]:
                            if isinstance(o, dict) and o.get("slot") in m:
                                o["slot"] = m[o["slot"]]
        for field in ("ffd", "deform", "attachments"):
            block = anim.get(field)
            if isinstance(block, dict):
                for skin, slots in list(block.items()):
                    if isinstance(slots, dict):
                        block[skin] = _rekey(slots, m)


# ---------------------------------------------------------------
# Draw-order offsets
# ---------------------------------------------------------------

def apply_draw_order_offsets(slot_names: list, offsets) -> list:
    """Decode draw-order offsets like the runtimes; returns slot names by draw position."""
    n = len(slot_names)
    if not offsets:
        return list(slot_names)
    index = {}
    for i, s in enumerate(slot_names):
        index.setdefault(s, i)
    draw = [-1] * n
    unchanged = [0] * max(n - len(offsets), 0)
    original = 0
    uidx = 0
    for o in offsets:
        if not isinstance(o, dict) or o.get("slot") not in index:
            raise ValueError("draw order offset references unknown slot %r"
                             % (o.get("slot") if isinstance(o, dict) else o))
        slot_index = index[o["slot"]]
        if slot_index < original:
            raise ValueError("draw order offsets are not ascending by setup index")
        if len(offsets) > n:
            raise ValueError("more draw order offsets than slots")
        while original != slot_index:
            unchanged[uidx] = original
            uidx += 1
            original += 1
        off = o.get("offset", 0)
        if not isinstance(off, int) or isinstance(off, bool):
            raise ValueError("draw order offset is not an integer")
        target = original + off
        if target < 0 or target >= n:
            raise ValueError("draw order offset out of range for slot '%s'" % o["slot"])
        if draw[target] != -1:
            raise ValueError("draw order offsets collide at position %d" % target)
        draw[target] = original
        original += 1
    while original < n:
        unchanged[uidx] = original
        uidx += 1
        original += 1
    for i in range(n - 1, -1, -1):
        if draw[i] == -1:
            uidx -= 1
            draw[i] = unchanged[uidx]
    return [slot_names[i] for i in draw]


def encode_draw_order_offsets(slot_names: list, target_order: list) -> list:
    """Minimal offsets such that apply_draw_order_offsets(slot_names, result) == target_order."""
    if list(target_order) == list(slot_names):
        return []
    n = len(slot_names)
    if sorted(target_order) != sorted(slot_names) or len(set(slot_names)) != n:
        raise ValueError("target order is not a permutation of the slots")
    index = {s: i for i, s in enumerate(slot_names)}
    seq = [index[s] for s in target_order]  # setup index per draw position

    # Longest increasing subsequence (patience, O(n log n)); these stay unlisted.
    import bisect
    tails: list = []
    tails_pos: list = []
    prev = [-1] * n
    for pos, v in enumerate(seq):
        k = bisect.bisect_left(tails, v)
        if k == len(tails):
            tails.append(v)
            tails_pos.append(pos)
        else:
            tails[k] = v
            tails_pos[k] = pos
        prev[pos] = tails_pos[k - 1] if k > 0 else -1
    keep = set()
    p = tails_pos[-1] if tails_pos else -1
    while p != -1:
        keep.add(p)
        p = prev[p]

    offsets = []
    position_of = {v: pos for pos, v in enumerate(seq)}
    for i in range(n):  # ascending setup index
        pos = position_of[i]
        if pos not in keep:
            offsets.append({"slot": slot_names[i], "offset": pos - i})
    return offsets


def rewrite_draw_order_offsets(spine_data, old_slot_order: list, new_slot_order: list) -> int:
    """Re-encode every offsets list so absolute draw orders stay unchanged."""
    count = 0
    for anim in (spine_data.get("animations") or {}).values():
        if not isinstance(anim, dict):
            continue
        for field in ("drawOrder", "draworder"):
            timeline = anim.get(field)
            if not isinstance(timeline, list):
                continue
            for key in timeline:
                if not isinstance(key, dict) or not key.get("offsets"):
                    continue
                absolute = apply_draw_order_offsets(old_slot_order, key["offsets"])
                new_offsets = encode_draw_order_offsets(new_slot_order, absolute)
                if new_offsets:
                    key["offsets"] = new_offsets
                else:
                    del key["offsets"]
                count += 1
    return count
