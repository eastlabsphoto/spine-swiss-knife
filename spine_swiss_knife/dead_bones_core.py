"""Dead bone analysis (pure logic, no Qt); built on spine_refs."""

from __future__ import annotations

from .spine_refs import find_dead_bones, collect_bone_references

_REASON_LABELS = {
    "slot": "has_slots",
    "animation": "has_animation",
    "constraint": "has_constraint",
    "skin": "has_skin",
    "weights": "has_weights",
}


def analyze_dead_bones(spine_data):
    """Return (dead_list, alive_list) of dicts (name, parent, reason / reasons)."""
    bones = [b for b in (spine_data.get("bones") or []) if isinstance(b, dict)]
    if not bones:
        return [], []
    dead, alive = find_dead_bones(spine_data)
    dead_set = set(dead)
    dead_list = []
    alive_list = []
    for bone in bones:
        name = bone.get("name")
        parent = bone.get("parent") or ""
        if name in dead_set:
            dead_list.append({"name": name, "parent": parent,
                              "reason": "No slots, animations, constraints or mesh weights"})
        else:
            reasons = [_REASON_LABELS.get(r, r) for r in alive.get(name, [])]
            alive_list.append({"name": name, "parent": parent,
                               "reasons": ", ".join(reasons)})
    return dead_list, alive_list


def filter_removable_selection(spine_data, selected):
    """Drop selected bones that still have an unselected child (they cannot be removed).

    Returns (kept_names_set, skipped_names_list).
    """
    keep = set(selected)
    children = {}
    for b in spine_data.get("bones") or []:
        if isinstance(b, dict):
            children.setdefault(b.get("parent"), []).append(b.get("name"))
    skipped = []
    changed = True
    while changed:
        changed = False
        for name in sorted(keep):
            if any(c not in keep for c in children.get(name, ())):
                keep.discard(name)
                skipped.append(name)
                changed = True
                break
    return keep, skipped
