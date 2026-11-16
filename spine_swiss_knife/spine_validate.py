"""Structural validation of a Spine skeleton JSON (pure logic, never raises)."""

from __future__ import annotations

from .spine_json import (
    IMAGE_ATTACHMENT_TYPES, attachment_image_key, attachment_vertex_count,
    get_draw_order_timeline, is_weighted, skin_entries, weighted_bone_indices,
)
from .spine_refs import apply_draw_order_offsets


def _dicts(value) -> list:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def validate_skeleton(spine_data, atlas_regions=None) -> list:
    """Return a list of {"severity", "code", "where", "message"} issues."""
    issues: list = []
    try:
        _validate(spine_data, atlas_regions, issues)
    except Exception as exc:  # defensive: never raise on malformed input
        issues.append({"severity": "error", "code": "validator_failure",
                       "where": "skeleton",
                       "message": "validation aborted: %s" % exc})
    return issues


def _validate(data, atlas_regions, issues: list) -> None:
    def err(code, where, msg):
        issues.append({"severity": "error", "code": code, "where": where,
                       "message": msg})

    def warn(code, where, msg):
        issues.append({"severity": "warning", "code": code, "where": where,
                       "message": msg})

    if not isinstance(data, dict):
        err("not_object", "skeleton", "skeleton JSON root is not an object")
        return

    bones = _dicts(data.get("bones"))
    slots = _dicts(data.get("slots"))

    # bones
    bone_names: list = []
    bone_pos: dict = {}
    for i, b in enumerate(bones):
        n = b.get("name")
        if n in bone_pos:
            err("duplicate_bone", "bones[%d]" % i, "duplicate bone name '%s'" % n)
        else:
            bone_pos[n] = i
        bone_names.append(n)
    for i, b in enumerate(bones):
        p = b.get("parent")
        if p is None:
            continue
        if p not in bone_pos:
            err("bone_parent_missing", "bone '%s'" % b.get("name"),
                "parent '%s' does not exist" % p)
        elif bone_pos[p] >= i:
            err("bone_parent_order", "bone '%s'" % b.get("name"),
                "parent '%s' is defined after the bone" % p)

    # slots
    slot_pos: dict = {}
    for i, s in enumerate(slots):
        n = s.get("name")
        if n in slot_pos:
            err("duplicate_slot", "slots[%d]" % i, "duplicate slot name '%s'" % n)
        else:
            slot_pos[n] = i
        if s.get("bone") not in bone_pos:
            err("slot_bone_missing", "slot '%s'" % n,
                "bone '%s' does not exist" % s.get("bone"))
    slot_names = [s.get("name") for s in slots]

    # skins
    atts_by_slot: dict = {}  # slot -> set of attachment names (any skin)
    for skin_name, atts in skin_entries(data):
        for slot_name, slot_atts in atts.items():
            if slot_name not in slot_pos:
                err("skin_slot_missing", "skin '%s'" % skin_name,
                    "slot '%s' does not exist" % slot_name)
            if not isinstance(slot_atts, dict):
                continue
            atts_by_slot.setdefault(slot_name, set()).update(slot_atts.keys())
            for att_name, att in slot_atts.items():
                where = "skin '%s' / %s / %s" % (skin_name, slot_name, att_name)
                if not isinstance(att, dict):
                    err("attachment_malformed", where, "attachment is not an object")
                    continue
                _check_attachment(att, att_name, where, slot_atts, bones, slot_pos,
                                  atlas_regions, err)

    # setup attachments
    for s in slots:
        a = s.get("attachment")
        if a is not None and a not in atts_by_slot.get(s.get("name"), ()):
            warn("setup_attachment_missing", "slot '%s'" % s.get("name"),
                 "setup attachment '%s' not found in any skin" % a)

    # constraints
    constraint_names: dict = {}
    for kind in ("ik", "transform", "path", "physics"):
        names = set()
        for c in _dicts(data.get(kind)):
            cn = c.get("name")
            names.add(cn)
            where = "%s constraint '%s'" % (kind, cn)
            if kind == "physics":
                if c.get("bone") not in bone_pos:
                    err("constraint_bone_missing", where,
                        "bone '%s' does not exist" % c.get("bone"))
                continue
            cb = c.get("bones")
            if isinstance(cb, str):
                cb = [cb]
            if not isinstance(cb, list):
                cb = []
            for bn in cb:
                if bn not in bone_pos:
                    err("constraint_bone_missing", where,
                        "bone '%s' does not exist" % bn)
            tgt = c.get("target")
            if kind == "path":
                if tgt not in slot_pos:
                    err("constraint_target_missing", where,
                        "target slot '%s' does not exist" % tgt)
            elif tgt not in bone_pos:
                err("constraint_target_missing", where,
                    "target bone '%s' does not exist" % tgt)
        constraint_names[kind] = names

    # animations
    events = data.get("events")
    declared_events = set(events.keys()) if isinstance(events, dict) else set()
    anims = data.get("animations")
    if isinstance(anims, dict):
        for an, anim in anims.items():
            if not isinstance(anim, dict):
                err("animation_malformed", "animation '%s'" % an,
                    "animation is not an object")
                continue
            w = "animation '%s'" % an
            if isinstance(anim.get("bones"), dict):
                for bn in anim["bones"]:
                    if bn not in bone_pos:
                        err("animation_bone_missing", w,
                            "timeline for missing bone '%s'" % bn)
            if isinstance(anim.get("slots"), dict):
                for sn, tls in anim["slots"].items():
                    if sn not in slot_pos:
                        err("animation_slot_missing", w,
                            "timeline for missing slot '%s'" % sn)
                    elif isinstance(tls, dict) and isinstance(tls.get("attachment"), list):
                        known = atts_by_slot.get(sn, set())
                        for k in tls["attachment"]:
                            nm = k.get("name") if isinstance(k, dict) else None
                            if nm is not None and nm not in known:
                                warn("timeline_attachment_missing", w,
                                     "attachment timeline of slot '%s' uses unknown "
                                     "attachment '%s'" % (sn, nm))
            for kind in ("ik", "transform", "path", "physics"):
                block = anim.get(kind)
                if isinstance(block, dict):
                    for cn in block:
                        if cn not in constraint_names.get(kind, ()):
                            err("animation_constraint_missing", w,
                                "%s timeline for missing constraint '%s'" % (kind, cn))
            for field in ("drawOrder", "draworder"):
                tl = anim.get(field)
                if isinstance(tl, list):
                    for k in tl:
                        if isinstance(k, dict) and k.get("offsets"):
                            try:
                                apply_draw_order_offsets(slot_names, k["offsets"])
                            except Exception as exc:
                                err("draw_order_invalid", w,
                                    "draw order key at time %s: %s"
                                    % (k.get("time", 0), exc))
            evs = anim.get("events")
            if isinstance(evs, list):
                for k in evs:
                    nm = k.get("name") if isinstance(k, dict) else None
                    if nm not in declared_events:
                        err("event_undeclared", w,
                            "event '%s' is not declared in top-level events" % nm)


def _check_attachment(att, att_name, where, slot_atts, bones, slot_pos,
                      atlas_regions, err) -> None:
    t = att.get("type")
    if t == "clipping":
        end = att.get("end")
        if end is not None and end not in slot_pos:
            err("clipping_end_missing", where, "end slot '%s' does not exist" % end)
    if t == "linkedmesh":
        parent = att.get("parent")
        if parent not in slot_atts:
            err("linkedmesh_parent_missing", where,
                "parent '%s' does not exist in this slot" % parent)

    verts = att.get("vertices")
    count = attachment_vertex_count(att)
    if isinstance(verts, list) and count is not None:
        if is_weighted(att):
            try:
                idxs = weighted_bone_indices(verts)
            except ValueError as exc:
                err("weights_malformed", where, str(exc))
            else:
                for i in sorted(idxs):
                    if not (0 <= i < len(bones)):
                        err("weights_bone_range", where,
                            "bone index %d out of range" % i)
        elif len(verts) != count * 2:
            err("vertices_length", where,
                "vertices has %d values, expected %d" % (len(verts), count * 2))

    if atlas_regions is not None and t in IMAGE_ATTACHMENT_TYPES:
        key = attachment_image_key(att_name, att)
        if key not in atlas_regions:
            err("missing_region", where, "image '%s' is not in the atlas" % key)
