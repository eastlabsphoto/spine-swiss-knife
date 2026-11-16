"""Merge Skeletons core — pure logic (no Qt, no i18n).

Warnings are returned as ``(code, params)`` tuples; the UI layer translates them
with the key ``"merge.warn." + code``.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from copy import deepcopy
from pathlib import Path

from .spine_json import (
    IMAGE_ATTACHMENT_TYPES, attachment_image_key, image_key_from_path,
    IMAGE_EXTENSIONS, is_skins_list, skin_entries,
)
from .spine_refs import bone_index_map, remap_bone_indices, rename_slots

_IMAGE_ATT_TYPES = set(IMAGE_ATTACHMENT_TYPES)

CONSTRAINT_KINDS = ("ik", "transform", "path", "physics")

_BONE_DEFAULTS = {
    "length": 0, "x": 0, "y": 0, "rotation": 0, "scaleX": 1, "scaleY": 1,
    "shearX": 0, "shearY": 0,
}


# ==========================================================================
# Image merge
# ==========================================================================

def _file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def merge_image_dirs(source_dirs: list, source_labels: list, output_dir: str):
    """Merge image directories into *output_dir*.

    Returns (image_rename_maps, warnings): one dict per source mapping
    old_image_key -> new_image_key (changed entries only), and a list of
    ``(code, params)`` warning tuples.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    warnings: list = []
    image_rename_maps: list = [{} for _ in source_dirs]

    all_images: list = []  # (source_idx, rel_path, abs_path, image_key)
    for idx, sdir in enumerate(source_dirs):
        if not sdir or not Path(sdir).is_dir():
            continue
        root = Path(sdir)
        for dirpath, dirnames, filenames in os.walk(sdir):
            dirnames[:] = [d for d in dirnames if not d.startswith("_")]
            for fname in filenames:
                fp = Path(dirpath) / fname
                if fp.suffix.lower() in IMAGE_EXTENSIONS:
                    rel = fp.relative_to(root)
                    key = image_key_from_path(str(rel))
                    all_images.append((idx, str(rel), str(fp), key))

    by_key: dict = {}
    for idx, rel, abspath, key in all_images:
        by_key.setdefault(key, []).append((idx, rel, abspath))

    used_output_keys: set = set()

    for key, entries in sorted(by_key.items()):
        if len(entries) == 1:
            idx, rel, abspath = entries[0]
            dst = out / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(abspath, str(dst))
            used_output_keys.add(key)
            continue

        hashes = [(idx, rel, abspath, _file_hash(abspath)) for idx, rel, abspath in entries]
        unique_hashes: dict = {}
        for idx, rel, abspath, h in hashes:
            unique_hashes.setdefault(h, []).append((idx, rel, abspath))

        if len(unique_hashes) == 1:
            _, rel, abspath = entries[0]
            dst = out / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(abspath, str(dst))
            used_output_keys.add(key)
            continue

        first = True
        for _h, group in unique_hashes.items():
            if first:
                _, rel, abspath = group[0]
                dst = out / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(abspath, str(dst))
                used_output_keys.add(key)
                first = False
                continue

            for idx, rel, abspath in group:
                rel_p = Path(rel)
                label = source_labels[idx]
                new_stem = "%s_%s" % (rel_p.stem, label)
                new_rel = str(rel_p.with_name(new_stem + rel_p.suffix)).replace("\\", "/")
                new_key = image_key_from_path(new_rel)

                candidate = new_key
                counter = 2
                while candidate in used_output_keys:
                    candidate = "%s_%d" % (new_key, counter)
                    new_rel = str(rel_p.with_name(
                        "%s_%d%s" % (new_stem, counter, rel_p.suffix))).replace("\\", "/")
                    counter += 1
                new_key = candidate

                dst = out / new_rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(abspath, str(dst))
                used_output_keys.add(new_key)
                image_rename_maps[idx][key] = new_key
                warnings.append(("image_renamed", {
                    "old": key, "new": new_key, "source": source_labels[idx]}))

    return image_rename_maps, warnings


# ==========================================================================
# Rename helpers
# ==========================================================================

def _rename_slots_in_animation(anim_data: dict, renames: dict):
    """Rename slot references inside a single animation (all timeline kinds)."""
    def rekey(d):
        return {renames.get(k, k): v for k, v in d.items()}

    if isinstance(anim_data.get("slots"), dict):
        anim_data["slots"] = rekey(anim_data["slots"])
    for key in ("drawOrder", "draworder"):
        if isinstance(anim_data.get(key), list):
            for entry in anim_data[key]:
                for offset in (entry.get("offsets") or []) if isinstance(entry, dict) else []:
                    if offset.get("slot") in renames:
                        offset["slot"] = renames[offset["slot"]]
    for field in ("ffd", "deform", "attachments"):
        block = anim_data.get(field)
        if isinstance(block, dict):
            for skin_name, slots in list(block.items()):
                if isinstance(slots, dict):
                    block[skin_name] = rekey(slots)


def _rename_events_in_animation(anim_data: dict, renames: dict):
    if isinstance(anim_data.get("events"), list):
        for ev in anim_data["events"]:
            if isinstance(ev, dict) and ev.get("name") in renames:
                ev["name"] = renames[ev["name"]]


def _apply_image_renames_to_skins(skins: dict, image_renames: dict):
    """Update image path references in normalized (dict) skins, in place."""
    for _skin_name, skin_data in skins.items():
        for _slot, attachments in skin_data.items():
            for att_name, att_data in attachments.items():
                if att_data.get("type") not in _IMAGE_ATT_TYPES:
                    continue
                img_key = attachment_image_key(att_name, att_data)
                if img_key in image_renames:
                    new_key = image_renames[img_key]
                    if new_key == att_name:
                        att_data.pop("path", None)
                    else:
                        att_data["path"] = new_key


def _unique_name(base: str, existing) -> str:
    if base not in existing:
        return base
    counter = 2
    while "%s_%d" % (base, counter) in existing:
        counter += 1
    return "%s_%d" % (base, counter)


def _bone_diff_fields(a: dict, b: dict) -> list:
    fields = []
    for k in sorted(set(a) | set(b)):
        if k in ("name", "parent"):
            continue
        d = _BONE_DEFAULTS.get(k)
        if a.get(k, d) != b.get(k, d):
            fields.append(k)
    return fields


# ==========================================================================
# Core merge
# ==========================================================================

def merge_spine_data(sources: list, source_labels: list, output_name: str,
                     image_rename_maps=None):
    """Merge multiple Spine JSON dicts into one. Sources are never modified.

    Returns (merged_data, warnings) where warnings are ``(code, params)``.
    """
    if not sources:
        return {}, []

    warnings: list = []
    base_version = (sources[0].get("skeleton") or {}).get("spine", "")
    merged: dict = {"skeleton": {"spine": base_version, "images": "./images/"}}

    bone_parents: dict = {}
    merged_bones: list = []
    bone_by_name: dict = {}
    slot_set: set = set()
    merged_slots: list = []
    # skin name -> {"attachments": {slot: {att: data}}, "extras": {key: value}}
    merged_skins: dict = {}
    event_set: set = set()
    merged_events: dict = {}
    anim_set: set = set()
    merged_anims: dict = {}
    c_sets = {k: set() for k in CONSTRAINT_KINDS}
    c_lists = {k: [] for k in CONSTRAINT_KINDS}

    first_was_skins_list = is_skins_list(sources[0])

    for src_idx, (orig, label) in enumerate(zip(sources, source_labels)):
        src = deepcopy(orig)
        event_renames: dict = {}
        img_renames = image_rename_maps[src_idx] if image_rename_maps else {}

        # ── Image renames (in place on the copy) ──
        if img_renames:
            for _sn, atts in skin_entries(src):
                _apply_image_renames_to_skins({"": atts}, img_renames)

        # ── Slot renames on the copy, BEFORE merging ──
        src_slot_names = [s.get("name") for s in src.get("slots", []) or []]
        taken = set(slot_set) | set(src_slot_names)
        slot_renames: dict = {}
        for name in src_slot_names:
            if name in slot_set:
                new_name = _unique_name("%s_%s" % (name, label), taken)
                taken.add(new_name)
                slot_renames[name] = new_name
                warnings.append(("slot_renamed", {
                    "old": name, "new": new_name, "source": label}))
        if slot_renames:
            rename_slots(src, slot_renames)

        # ── Constraint renames on the copy (incl. animation timelines + skin extras) ──
        constraint_renames: dict = {k: {} for k in CONSTRAINT_KINDS}
        for kind in CONSTRAINT_KINDS:
            own = {c.get("name") for c in src.get(kind) or [] if isinstance(c, dict)}
            taken_c = set(c_sets[kind]) | own
            for c in src.get(kind) or []:
                if not isinstance(c, dict):
                    continue
                name = c.get("name")
                if name in c_sets[kind]:
                    new_name = _unique_name("%s_%s" % (name, label), taken_c)
                    taken_c.add(new_name)
                    constraint_renames[kind][name] = new_name
                    warnings.append(("constraint_renamed", {
                        "kind": kind, "old": name, "new": new_name, "source": label}))
        for kind in CONSTRAINT_KINDS:
            m = constraint_renames[kind]
            if not m:
                continue
            for c in src.get(kind) or []:
                if isinstance(c, dict) and c.get("name") in m:
                    c["name"] = m[c["name"]]
            for anim in (src.get("animations") or {}).values():
                block = anim.get(kind) if isinstance(anim, dict) else None
                if isinstance(block, dict):
                    anim[kind] = {m.get(k, k): v for k, v in block.items()}
            if isinstance(src.get("skins"), list):
                for sk in src["skins"]:
                    if isinstance(sk, dict) and isinstance(sk.get(kind), list):
                        sk[kind] = [m.get(n, n) for n in sk[kind]]

        # ── Bones ──
        src_bones = [b for b in src.get("bones", []) or []]
        for bone in src_bones:
            name = bone["name"]
            parent = bone.get("parent", "")
            if name in bone_by_name:
                if parent != bone_parents.get(name, ""):
                    warnings.append(("bone_parent_mismatch", {
                        "bone": name, "source": label,
                        "expected": bone_parents.get(name, "(root)") or "(root)",
                        "got": parent or "(root)"}))
                diff = _bone_diff_fields(bone_by_name[name], bone)
                if diff:
                    warnings.append(("bone_differs", {
                        "bone": name, "source": label, "fields": ", ".join(diff)}))
                continue
            bone_parents[name] = parent
            bone_by_name[name] = bone
            merged_bones.append(bone)

        # Bones are only ever appended, so indices of the merged list are final.
        index_map = bone_index_map(src_bones, merged_bones)
        remap_bone_indices(src, index_map)

        # ── Slots ──
        for slot in src.get("slots", []) or []:
            slot_set.add(slot["name"])
            merged_slots.append(slot)

        # ── Skins ──
        skin_raw = src.get("skins")
        extras_by_skin: dict = {}
        if isinstance(skin_raw, list):
            for sk in skin_raw:
                if isinstance(sk, dict):
                    extras_by_skin[sk.get("name", "default")] = {
                        k: v for k, v in sk.items() if k not in ("name", "attachments")}
        for skin_name, atts in skin_entries(src):
            dest = merged_skins.setdefault(skin_name, {"attachments": {}, "extras": {}})
            for slot, slot_atts in atts.items():
                dest["attachments"].setdefault(slot, {}).update(slot_atts)
            for k, v in extras_by_skin.get(skin_name, {}).items():
                cur = dest["extras"].get(k)
                if isinstance(v, list):
                    if not isinstance(cur, list):
                        cur = []
                        dest["extras"][k] = cur
                    for item in v:
                        if item not in cur:
                            cur.append(item)
                elif k not in dest["extras"]:
                    dest["extras"][k] = v

        # ── Events ──
        for event_name, event_data in (src.get("events") or {}).items():
            if event_name in merged_events:
                if merged_events[event_name] == event_data:
                    continue  # identical -> shared
                new_name = _unique_name("%s_%s" % (event_name, label), event_set)
                event_renames[event_name] = new_name
                warnings.append(("event_renamed", {
                    "old": event_name, "new": new_name, "source": label}))
                event_name = new_name
            event_set.add(event_name)
            merged_events[event_name] = event_data

        # ── Animations ──
        for anim_name, anim_data in (src.get("animations") or {}).items():
            if event_renames and isinstance(anim_data, dict):
                _rename_events_in_animation(anim_data, event_renames)
            final_name = anim_name
            if anim_name in anim_set:
                final_name = _unique_name("%s_%s" % (anim_name, label), anim_set)
                warnings.append(("anim_renamed", {
                    "old": anim_name, "new": final_name, "source": label}))
            anim_set.add(final_name)
            merged_anims[final_name] = anim_data

        # ── Constraints ──
        for kind in CONSTRAINT_KINDS:
            for c in src.get(kind) or []:
                if isinstance(c, dict):
                    c_sets[kind].add(c.get("name"))
                    c_lists[kind].append(c)

    # --- Assemble output ---
    merged["bones"] = merged_bones
    merged["slots"] = merged_slots
    for kind in CONSTRAINT_KINDS:
        if c_lists[kind]:
            merged[kind] = c_lists[kind]

    if first_was_skins_list:
        skins_out = []
        for name, info in merged_skins.items():
            entry = {"name": name}
            entry.update(info["extras"])
            if info["attachments"]:
                entry["attachments"] = info["attachments"]
            skins_out.append(entry)
        merged["skins"] = skins_out
    else:
        merged["skins"] = {n: i["attachments"] for n, i in merged_skins.items()}

    if merged_events:
        merged["events"] = merged_events
    merged["animations"] = merged_anims
    return merged, warnings
