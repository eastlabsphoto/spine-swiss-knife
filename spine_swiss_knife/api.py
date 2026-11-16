"""Headless API for Spine Swiss Knife (no Qt, no GUI).

Every public function takes plain arguments and returns a JSON-serialisable
dict, so the same functions back both the CLI (``cli.py``) and the MCP
server (``mcp_server.py``).

Functions that modify files default to ``apply=False`` (dry-run) and write a
``.backup`` copy before changing anything.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Optional

from . import spine_cli
from .atlas_parser import collect_images, parse_atlas
from .dead_bones_core import analyze_dead_bones, filter_removable_selection
from .hidden_attachments_core import (
    analyze_hidden_attachments, fix_hidden_attachments as _core_fix_hidden)
from .keyframe_core import find_redundant_keyframes, remove_redundant_keyframes
from .mask_core import analyze_clipping
from .spine_refs import remove_bones
from .unused_core import compare_images, json_image_keys
from .spine_json import (
    attachment_image_key, create_backup, iter_all_attachments,
    load_spine_json, save_spine_json, skin_entries,
)
from .spine_validate import validate_skeleton

# Export settings that reproduce the Greentube/Mynt export (compact JSON,
# animation clean-up, no whitespace stripping, RGBA8888 atlas).
MYNT_EXPORT_SETTINGS: dict = {
    "class": "export-json",
    "extension": ".json",
    "format": "JSON",
    "prettyPrint": False,
    "nonessential": False,
    "cleanUp": True,
    "packAtlas": {
        "stripWhitespaceX": False, "stripWhitespaceY": False,
        "rotation": False, "alias": True, "ignoreBlankImages": True,
        "alphaThreshold": 9, "minWidth": 16, "minHeight": 16,
        "maxWidth": 2048, "maxHeight": 2048, "pot": False, "square": False,
        "fast": False, "paddingX": 2, "paddingY": 2, "edgePadding": True,
        "duplicatePadding": False, "bleed": True, "bleedIterations": 2,
        "filterMin": "Linear", "filterMag": "Linear",
        "atlasExtension": ".atlas", "format": "RGBA8888",
        "outputFormat": "png", "jpegQuality": 0.9, "premultiplyAlpha": False,
        "scale": [1], "scaleSuffix": [""], "scaleResampling": ["bicubic"],
        "packSource": "attachments", "packTarget": "perskeleton",
        "warnings": True, "prettyPrint": False, "legacyOutput": False,
        "flattenPaths": False, "useIndexes": False, "debug": False,
        "combineSubdirectories": False, "ignore": False,
    },
}


class SpineError(RuntimeError):
    """Raised for expected, user-facing failures (missing Spine, bad input)."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _exe(spine_exe: Optional[str] = None) -> str:
    exe = spine_exe or spine_cli.detect_spine_executable()
    if not exe:
        raise SpineError("Spine executable not found; pass spine_exe.")
    return exe


def _run_spine(args: list[str], spine_file: str, spine_exe: Optional[str],
               timeout: int = 600) -> tuple[int, str, str]:
    cmd = [_exe(spine_exe)]
    version = spine_cli.read_spine_file_version(spine_file)
    # read_spine_file_version only understands old (3.x) project headers;
    # for anything else let Spine pick the version stored in the project.
    if not re.match(r"^\d+\.\d+(\.\d+)?$", version or ""):
        version = "project"
    cmd += ["-u", version]
    cmd += args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise SpineError(f"Spine timed out after {timeout}s: {' '.join(cmd)}")
    except OSError as e:
        raise SpineError(f"Could not run Spine ({cmd[0]}): {e}")
    return p.returncode, p.stdout, p.stderr


def _write_json_tmp(data: dict) -> str:
    fd, path = tempfile.mkstemp(suffix=".json", prefix="ssk_api_")
    with open(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


def _require_file(path: str, what: str) -> None:
    if not Path(path).is_file():
        raise SpineError(f"{what} not found: {path}")


# ---------------------------------------------------------------------------
# project_info
# ---------------------------------------------------------------------------

def project_info(spine_file: str, spine_exe: Optional[str] = None) -> dict:
    """List skeletons, bones/slots counts, skins and animations of a .spine."""
    _require_file(spine_file, "Spine project")
    rc, out, err = _run_spine(["-i", spine_file], spine_file, spine_exe)
    if rc != 0:
        raise SpineError(f"Spine exited with {rc}: {err.strip()[:500]}")
    info: dict = {"spine_file": spine_file, "skeletons": []}
    cur: Optional[dict] = None
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("Spine version:"):
            info["spine_version"] = s.split(":", 1)[1].strip()
        elif s.startswith("Skeleton:"):
            cur = {"name": s.split(":", 1)[1].strip(), "animations": [],
                   "skins": []}
            info["skeletons"].append(cur)
        elif cur is not None:
            m = re.match(r"(Bones|Slots|Skins|Animations)\s*\((\d+)\):\s*(.*)", s)
            if m:
                key, count, names = m.group(1), int(m.group(2)), m.group(3)
                if key == "Animations":
                    cur["animations"] = [n.strip() for n in names.split(",") if n.strip()]
                elif key == "Skins":
                    cur["skins"] = [n.strip() for n in names.split(",") if n.strip()]
                else:
                    cur[key.lower() + "_count"] = count
    return info


# ---------------------------------------------------------------------------
# export_json
# ---------------------------------------------------------------------------

def export_json(spine_file: str, out_dir: str,
                spine_exe: Optional[str] = None,
                settings: Optional[dict] = None) -> dict:
    """Export a .spine to JSON + packed atlas (default: Mynt settings).

    Exports every skeleton of the project (e.g. joker_5, joker_5_left, ...).
    """
    _require_file(spine_file, "Spine project")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tmp = _write_json_tmp(settings or MYNT_EXPORT_SETTINGS)
    try:
        rc, stdout, stderr = _run_spine(
            ["-i", spine_file, "-o", str(out.resolve()), "-e", tmp],
            spine_file, spine_exe)
    finally:
        Path(tmp).unlink(missing_ok=True)
    if rc != 0:
        raise SpineError(f"Export failed ({rc}): {(stderr or stdout).strip()[:800]}")
    files = sorted(p.name for p in out.iterdir() if p.is_file())
    return {"out_dir": str(out), "files": files,
            "warnings": [l for l in stdout.splitlines() if "WARN" in l.upper()]}


# ---------------------------------------------------------------------------
# check_regions
# ---------------------------------------------------------------------------

def _attachment_region_names(spine_data: dict) -> dict[str, list[str]]:
    """Map region name -> ['skin/slot/attachment', ...] for region/mesh."""
    needed: dict[str, list[str]] = {}
    for skin_name, slot_name, att_key, att in iter_all_attachments(spine_data):
        if att.get("type", "region") not in ("region", "mesh", "linkedmesh"):
            continue
        region = attachment_image_key(att_key, att)
        needed.setdefault(region, []).append(
            f"{skin_name}/{slot_name}/{att_key}")
    return needed


def check_regions(json_path: str, atlas_path: str) -> dict:
    """Check that every region/mesh attachment has a region in the atlas.

    Catches errors like 'Region not found in atlas: sj_skirt.png' before the
    editor does.
    """
    _require_file(json_path, "Skeleton JSON")
    _require_file(atlas_path, "Atlas")
    data = load_spine_json(json_path)
    regions = parse_atlas(atlas_path)
    needed = _attachment_region_names(data)
    missing = {r: where[:3] for r, where in needed.items() if r not in regions}
    unused = sorted(regions - set(needed))
    hints = {}
    for r in missing:
        stem = r.rsplit(".", 1)[0]
        if stem in regions:
            hints[r] = f"atlas has '{stem}' (attachment name has an extension)"
    return {"ok": not missing, "json": json_path, "atlas": atlas_path,
            "attachments_checked": len(needed), "missing": missing,
            "hints": hints, "unused_atlas_regions": unused}


# ---------------------------------------------------------------------------
# fix_attachment_name
# ---------------------------------------------------------------------------

def _rename_dict_key(d: dict, old: str, new: str) -> None:
    """Rename a key of *d* in place, keeping the key order."""
    items = list(d.items())
    d.clear()
    for k, v in items:
        d[new if k == old else k] = v


def _rename_attachment_refs(data: dict, old: str, new: str) -> int:
    """Rename attachment references structurally; return how many changed."""
    count = 0

    # skin attachment keys + name/path/parent fields
    for skin_name, atts in skin_entries(data):
        for slot_name, slot_atts in atts.items():
            if not isinstance(slot_atts, dict):
                continue
            if old in slot_atts:
                if new in slot_atts:
                    raise SpineError(
                        f"Attachment '{new}' already exists in skin "
                        f"'{skin_name}', slot '{slot_name}'.")
                _rename_dict_key(slot_atts, old, new)
                count += 1
    for _skin, _slot, _key, att in iter_all_attachments(data):
        for field in ("name", "path", "parent"):
            if att.get(field) == old and (field != "parent"
                                          or att.get("type") == "linkedmesh"):
                att[field] = new
                count += 1

    # slot setup attachment
    for slot in data.get("slots", []) or []:
        if isinstance(slot, dict) and slot.get("attachment") == old:
            slot["attachment"] = new
            count += 1

    # animations
    anims = data.get("animations")
    if isinstance(anims, dict):
        for anim in anims.values():
            if not isinstance(anim, dict):
                continue
            slots = anim.get("slots")
            if isinstance(slots, dict):
                for tls in slots.values():
                    keys = tls.get("attachment") if isinstance(tls, dict) else None
                    if isinstance(keys, list):
                        for k in keys:
                            if isinstance(k, dict) and k.get("name") == old:
                                k["name"] = new
                                count += 1
            for fld in ("ffd", "deform", "attachments"):
                block = anim.get(fld)
                if not isinstance(block, dict):
                    continue
                for slots_d in block.values():
                    if not isinstance(slots_d, dict):
                        continue
                    for atts_d in slots_d.values():
                        if isinstance(atts_d, dict) and old in atts_d:
                            if new not in atts_d:
                                _rename_dict_key(atts_d, old, new)
                                count += 1
    return count


def fix_attachment_name(json_path: str, old: str, new: str,
                        apply: bool = False) -> dict:
    """Rename an attachment/region name in an exported JSON.

    Only attachment references are renamed (skin attachment keys, attachment
    name/path, slot setup attachment, attachment timelines, linked-mesh
    parents, deform timelines); bones, slots etc. with the same name are not
    touched. Works on the *exported JSON only* (the .spine project is not
    touched). Dry-run unless ``apply`` is True; writes a backup first.
    """
    _require_file(json_path, "Skeleton JSON")
    data = load_spine_json(json_path)
    count = 0 if old == new else _rename_attachment_refs(data, old, new)
    result = {"json": json_path, "old": old, "new": new,
              "occurrences": count, "applied": False}
    if apply and count:
        result["backup"] = create_backup(json_path)
        save_spine_json(json_path, data)
        result["applied"] = True
    return result


# ---------------------------------------------------------------------------
# export_static (first frame + optional blur)
# ---------------------------------------------------------------------------

def _frame_skeletons(tmp_dir: Path, frames: list) -> list[str]:
    """Distinct top-level folder names of the frames (skeletons), if nested."""
    names = []
    for f in frames:
        parts = f.relative_to(tmp_dir).parts
        if len(parts) > 1 and parts[0] not in names:
            names.append(parts[0])
    return names


def _safe_stem(text: str) -> str:
    """Make *text* usable as a file name (no path separators etc.)."""
    out = re.sub(r'[/\\:*?"<>|]', "_", text).strip()
    return out or "_"


def _spine_png(spine_file: str, settings: dict, spine_exe: Optional[str],
               single_skeleton: bool = False):
    """Run one Spine PNG export, return the first frame as a PIL image.

    With ``single_skeleton`` a SpineError is raised when the export produced
    frames for several skeletons (the caller did not say which one to use).
    """
    from PIL import Image
    tmp_dir = Path(tempfile.mkdtemp(prefix="ssk_static_"))
    settings = dict(settings)
    settings["output"] = str(tmp_dir.resolve())
    settings["open"] = False
    tmp = _write_json_tmp(settings)
    try:
        rc, stdout, stderr = _run_spine(
            ["-i", spine_file, "-o", str(tmp_dir.resolve() / "f"), "-e", tmp],
            spine_file, spine_exe)
        if rc != 0:
            raise SpineError(f"PNG export failed ({rc}): "
                             f"{(stderr or stdout).strip()[:800]}")
        frames = sorted(tmp_dir.rglob("*.png"))
        if not frames:
            raise SpineError("Spine produced no PNG (check skeleton/animation).")
        if single_skeleton:
            skels = _frame_skeletons(tmp_dir, frames)
            if len(skels) > 1:
                raise SpineError(
                    "The project has several skeletons (" + ", ".join(skels)
                    + "); pass skeleton=<name>.")
        img = Image.open(frames[0]).convert("RGBA")
        img.load()
        return img
    finally:
        Path(tmp).unlink(missing_ok=True)
        shutil.rmtree(tmp_dir, ignore_errors=True)


def export_static(spine_file: str, animation: str, out_dir: str,
                  skeleton: Optional[str] = None, name: Optional[str] = None,
                  mode: str = "trim", canvas_width: int = 0,
                  canvas_height: int = 0, padding: int = 0,
                  blur_radius: int = 0, blur_extra_pad: int = 0,
                  blur_canvas_width: int = 0, blur_canvas_height: int = 0,
                  spine_exe: Optional[str] = None) -> dict:
    """Export frame 0 of an animation as PNG, optionally plus a blur variant.

    mode: ``trim``   - crop to content (alpha bbox);
          ``center`` - symmetric canvas around the Spine origin, so a symbol
                       placed at 0,0 lines up with the spine entity;
          ``fixed``  - exact canvas_width x canvas_height, origin centred.
    blur_radius > 0 also writes ``<name>_blur.png`` (vertical motion blur).

    Pixels come from Spine's automatic-bounds export. For center/fixed a
    second export with a crop box centred on the origin is used only to
    measure where the origin is (the crop-box render is not used for pixels).
    """
    from PIL import Image
    from .static_exporter_core import (
        DEFAULT_EXPORT_SETTINGS, _create_blur_variants)
    _require_file(spine_file, "Spine project")
    if mode not in ("trim", "center", "fixed"):
        raise SpineError("mode must be trim, center or fixed")
    if mode == "fixed" and not (canvas_width and canvas_height):
        raise SpineError("mode=fixed needs canvas_width and canvas_height")

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = _safe_stem(name or f"{skeleton or Path(spine_file).stem}_{animation}")

    base = dict(DEFAULT_EXPORT_SETTINGS)
    base.update({"class": "export-png", "exportType": "animation",
                 "animation": animation, "fps": 1})
    if skeleton:
        base["skeleton"] = skeleton
    img = _spine_png(spine_file, base, spine_exe,
                     single_skeleton=not skeleton)
    bbox = img.getbbox()
    if bbox is None:
        raise SpineError("Exported frame is fully transparent.")
    content = img.crop(bbox)

    if mode == "trim":
        canvas = Image.new("RGBA", (content.width + 2 * padding,
                                    content.height + 2 * padding), (0, 0, 0, 0))
        canvas.paste(content, (padding, padding))
    else:
        ref = _spine_png(spine_file, dict(base, cropX=-1000, cropY=-1000,
                                          cropWidth=2000, cropHeight=2000),
                         spine_exe, single_skeleton=not skeleton)
        rb = ref.getbbox()
        if rb is None:
            raise SpineError("Origin reference export is fully transparent.")
        # content edges relative to the Spine origin (image centre)
        left, top = rb[0] - ref.width // 2, rb[1] - ref.height // 2
        right, bottom = rb[2] - ref.width // 2, rb[3] - ref.height // 2
        if mode == "center":
            half_w = max(-left, right) + padding
            half_h = max(-top, bottom) + padding
            cw, ch = 2 * half_w, 2 * half_h
        else:
            cw, ch = canvas_width, canvas_height
        canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        canvas.paste(content, (cw // 2 + left, ch // 2 + top))

    dst = out / f"{stem}.png"
    canvas.save(dst)
    result = {"static": str(dst), "size": list(canvas.size), "mode": mode}
    if mode != "trim":
        result["origin_offsets"] = {"left": left, "top": top,
                                    "right": right, "bottom": bottom}
        if (rb[2] - rb[0], rb[3] - rb[1]) != content.size:
            result["warning"] = ("origin reference bbox size differs from the "
                                 "exported frame; alignment may be off")
    if blur_radius > 0:
        bsize = ((blur_canvas_width, blur_canvas_height)
                 if blur_canvas_width and blur_canvas_height else None)
        bp = _create_blur_variants({stem: str(dst)}, blur_radius,
                                   blur_extra_pad, bsize)
        result["blur"] = bp[stem]
        result["blur_size"] = list(Image.open(bp[stem]).size)
    return result


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

def validate(json_path: str, atlas_path: Optional[str] = None) -> dict:
    """Validate an exported skeleton JSON (optionally against an atlas).

    Returns ok (no errors), the list of errors and the list of warnings.
    """
    _require_file(json_path, "Skeleton JSON")
    data = load_spine_json(json_path)
    regions = None
    if atlas_path:
        _require_file(atlas_path, "Atlas")
        regions = parse_atlas(atlas_path)
    issues = validate_skeleton(data, regions)
    errors = [i for i in issues if i.get("severity") == "error"]
    warnings = [i for i in issues if i.get("severity") != "error"]
    return {"ok": not errors, "json": json_path, "errors": errors,
            "warnings": warnings}


# ---------------------------------------------------------------------------
# optimizers (dry-run unless apply=True)
# ---------------------------------------------------------------------------

_MAX_LISTED = 50  # cap on the "would change" lists of a dry-run


def _error_counter(data) -> Counter:
    return Counter((i.get("code"), i.get("message"))
                   for i in validate_skeleton(data)
                   if i.get("severity") == "error")


def _run_optimizer(json_path: str, apply: bool, analyze, mutate) -> dict:
    """Shared dry-run / apply flow of the JSON optimizers.

    ``analyze(data)`` returns ``(summary_dict, changes_list, work)`` where
    ``work`` is passed to ``mutate(data, work)`` (in place) when applying.
    After applying the file is validated; if the change introduced new
    validation errors the file is restored from the backup and SpineError
    is raised.
    """
    _require_file(json_path, "Skeleton JSON")
    data = load_spine_json(json_path)
    summary, changes, work = analyze(data)
    result = {"json": json_path, "applied": False}
    result.update(summary)
    result["changes_total"] = len(changes)
    result["changes"] = changes[:_MAX_LISTED]
    if changes and len(changes) > _MAX_LISTED:
        result["changes_truncated"] = True
    if not apply or not changes:
        return result
    before = _error_counter(data)
    backup = create_backup(json_path)
    try:
        mutate(data, work)
        after = _error_counter(data)
        introduced = [k for k, n in after.items() if n > before.get(k, 0)]
        if introduced:
            raise SpineError(
                "Applying the change would introduce validation errors "
                "(%s); the file was left unchanged."
                % "; ".join("%s: %s" % k for k in introduced[:3]))
        save_spine_json(json_path, data)
    except SpineError:
        shutil.copy2(backup, json_path)
        raise
    except Exception as e:
        shutil.copy2(backup, json_path)
        raise SpineError("%s: %s" % (type(e).__name__, e))
    result["applied"] = True
    result["backup"] = backup
    result["validation_errors"] = [
        {"code": c, "message": m} for (c, m) in sorted(
            after.elements(), key=lambda k: (str(k[0]), str(k[1])))
    ][:_MAX_LISTED]
    return result


def optimize_keyframes(json_path: str, tolerance: float = 0.1,
                       apply: bool = False) -> dict:
    """Find (and with apply remove) redundant animation keyframes.

    tolerance: Largest allowed value deviation of a removed key from the
    interpolation of its neighbours.
    """
    if tolerance < 0:
        raise SpineError("tolerance must be >= 0")

    def analyze(data):
        analysis = find_redundant_keyframes(
            data.get("animations") or {}, tolerance, data)
        total = sum(i["total_keys"] for i in analysis.values())
        redundant = sum(i["redundant_keys"] for i in analysis.values())
        changes = []
        for anim, info in analysis.items():
            for section in ("bones", "slots"):
                for owner, tls in info[section].items():
                    for tl, idx in tls.items():
                        changes.append({"animation": anim, section[:-1]: owner,
                                        "timeline": tl, "keys_removed": len(idx)})
        summary = {"tolerance": tolerance, "total_keys": total,
                   "redundant_keys": redundant}
        return summary, (changes if redundant else []), analysis

    def mutate(data, analysis):
        remove_redundant_keyframes(data, analysis)

    return _run_optimizer(json_path, apply, analyze, mutate)


def remove_dead_bones(json_path: str, apply: bool = False) -> dict:
    """Find (and with apply remove) bones nothing uses.

    A bone is dead when no slot, constraint, animation, skin or mesh weight
    refers to it and none of its descendants is alive.
    """
    def analyze(data):
        dead, _alive = analyze_dead_bones(data)
        names = [d["name"] for d in dead]
        keep, skipped = filter_removable_selection(data, names)
        changes = [{"bone": n, "parent": d["parent"]}
                   for n, d in ((d["name"], d) for d in dead) if n in keep]
        summary = {"dead_bones": len(changes), "skipped": sorted(skipped)}
        return summary, changes, sorted(keep)

    def mutate(data, keep):
        remove_bones(data, keep)

    return _run_optimizer(json_path, apply, analyze, mutate)


def fix_hidden_attachments(json_path: str, apply: bool = False) -> dict:
    """Find (and with apply fix) slots whose attachment is never visible.

    Fix: clear the setup-pose attachment and insert an attachment key at t=0
    in the animations where the slot can become visible.
    """
    def analyze(data):
        cands, details, warnings = analyze_hidden_attachments(data)
        changes = [{"slot": c["slot_name"], "attachment": c["original_attachment"],
                    "reasons": c.get("causes", [])} for c in cands]
        summary = {"hidden_slots": len(cands),
                   "keyframes_to_insert": sum(
                       1 for d in details if d["action"] == "insert_kf"),
                   "warnings": len(warnings)}
        return summary, changes, (cands, details)

    def mutate(data, work):
        _core_fix_hidden(data, work[0], work[1])

    return _run_optimizer(json_path, apply, analyze, mutate)


def snap_rect_masks(json_path: str, tolerance: float = 10.0,
                    apply: bool = False) -> dict:
    """Find (and with apply snap) near-rectangular clipping masks to exact rectangles.

    tolerance: Largest allowed corner deviation in skeleton units.
    """
    if tolerance < 0:
        raise SpineError("tolerance must be >= 0")

    def analyze(data):
        work, changes, total = [], [], 0
        for skin, slot, name, att in iter_all_attachments(data):
            if att.get("type") != "clipping":
                continue
            total += 1
            info = analyze_clipping(att, tolerance)
            if (info["is_rect_candidate"]
                    and list(att.get("vertices")) != info["snapped_vertices"]):
                work.append((att, info["snapped_vertices"]))
                changes.append({"skin": skin, "slot": slot, "attachment": name,
                                "max_deviation": info["max_deviation"]})
        return ({"tolerance": tolerance, "clipping_masks": total,
                 "snappable": len(changes)}, changes, work)

    def mutate(data, work):
        for att, verts in work:
            att["vertices"] = verts

    return _run_optimizer(json_path, apply, analyze, mutate)


def unused_images(json_path: str, images_dir: str,
                  atlas_path: Optional[str] = None) -> dict:
    """Report images in a folder that the skeleton/atlas never reference (read-only).

    images_dir: Folder with the source images (sub-folders starting with _ are skipped).
    """
    _require_file(json_path, "Skeleton JSON")
    if not Path(images_dir).is_dir():
        raise SpineError(f"Images folder not found: {images_dir}")
    data = load_spine_json(json_path)
    regions = None
    if atlas_path:
        _require_file(atlas_path, "Atlas")
        regions = parse_atlas(atlas_path)
    disk = collect_images(images_dir)
    cmp = compare_images(disk.keys(), regions, json_image_keys(data))
    return {"json": json_path, "images_dir": images_dir,
            "images_on_disk": len(disk),
            "unused": sorted(cmp["unused"]),
            "unused_count": len(cmp["unused"]),
            "missing": sorted(cmp["missing"]),
            "missing_count": len(cmp["missing"]),
            "used_count": len(cmp["used"])}


def report(json_path: str, atlas_path: Optional[str] = None,
           images_dir: Optional[str] = None) -> dict:
    """One read-only overview: counts, validation and what each optimizer would save."""
    _require_file(json_path, "Skeleton JSON")
    data = load_spine_json(json_path)
    skins = [n for n, _ in skin_entries(data)]
    anims = data.get("animations") or {}
    analysis = find_redundant_keyframes(anims, 0.1, data)
    dead, _alive = analyze_dead_bones(data)
    keep, _skipped = filter_removable_selection(data, [d["name"] for d in dead])
    hidden, _details, _warn = analyze_hidden_attachments(data)
    masks = sum(1 for _s, _sl, _n, att in iter_all_attachments(data)
                if att.get("type") == "clipping"
                and analyze_clipping(att, 10.0)["is_rect_candidate"])
    regions = None
    if atlas_path:
        _require_file(atlas_path, "Atlas")
        regions = parse_atlas(atlas_path)
    issues = validate_skeleton(data, regions)
    errors = [i for i in issues if i.get("severity") == "error"]
    skel = data.get("skeleton") if isinstance(data.get("skeleton"), dict) else {}
    out = {
        "json": json_path,
        "file_size": Path(json_path).stat().st_size,
        "spine_version": skel.get("spine"),
        "counts": {
            "bones": len(data.get("bones") or []),
            "slots": len(data.get("slots") or []),
            "skins": len(skins),
            "animations": len(anims),
            "total_keys": sum(i["total_keys"] for i in analysis.values()),
        },
        "validation": {"ok": not errors, "errors": len(errors),
                       "warnings": len(issues) - len(errors)},
        "savings": {
            "redundant_keys": sum(i["redundant_keys"] for i in analysis.values()),
            "dead_bones": len(keep),
            "hidden_slots": len(hidden),
            "snappable_masks": masks,
        },
    }
    if images_dir:
        out["unused_images"] = unused_images(
            json_path, images_dir, atlas_path)["unused_count"]
    return out


_BATCH_TOOLS = {
    "report": report,
    "validate": validate,
    "optimize_keyframes": optimize_keyframes,
    "remove_dead_bones": remove_dead_bones,
    "fix_hidden_attachments": fix_hidden_attachments,
    "snap_rect_masks": snap_rect_masks,
}


def _find_skeleton_jsons(folder: Path, recursive: bool) -> list:
    found = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs
                         if not d.startswith("_") and recursive)
        for fname in sorted(files):
            if not fname.lower().endswith(".json"):
                continue
            p = os.path.join(root, fname)
            try:
                d = load_spine_json(p)
            except Exception:
                continue
            if isinstance(d, dict) and "bones" in d and "skeleton" in d:
                found.append(p)
    return found


def batch(folder: str, tool: str = "report", recursive: bool = False,
          apply: bool = False) -> dict:
    """Run one JSON tool on every skeleton JSON in a folder.

    tool: report, validate, optimize_keyframes, remove_dead_bones, fix_hidden_attachments or snap_rect_masks.
    recursive: Also descend into sub-folders (folders starting with _ are always skipped).
    """
    if tool not in _BATCH_TOOLS:
        raise SpineError("tool must be one of: " + ", ".join(_BATCH_TOOLS))
    root = Path(folder)
    if not root.is_dir():
        raise SpineError(f"Folder not found: {folder}")
    fn = _BATCH_TOOLS[tool]
    takes_apply = "apply" in inspect.signature(fn).parameters
    results, failed = [], 0
    for p in _find_skeleton_jsons(root, recursive):
        try:
            r = fn(p, apply=apply) if takes_apply else fn(p)
            results.append({"file": p, "ok": True, "result": r})
        except Exception as e:
            failed += 1
            results.append({"file": p, "ok": False,
                            "error": f"{type(e).__name__}: {e}"})
    totals = {"files": len(results), "failed": failed}
    if tool != "report":
        for key in ("redundant_keys", "dead_bones", "hidden_slots", "snappable"):
            vals = [r["result"][key] for r in results
                    if r["ok"] and key in r["result"]]
            if vals:
                totals[key] = sum(vals)
        totals["applied"] = sum(1 for r in results
                                if r["ok"] and r["result"].get("applied"))
    return {"folder": folder, "tool": tool, "apply": apply,
            "results": results, "totals": totals}


# ---------------------------------------------------------------------------
# registry used by cli.py and mcp_server.py
# ---------------------------------------------------------------------------

TOOLS = {
    "project_info": project_info,
    "export_json": export_json,
    "check_regions": check_regions,
    "fix_attachment_name": fix_attachment_name,
    "export_static": export_static,
    "validate": validate,
    "optimize_keyframes": optimize_keyframes,
    "remove_dead_bones": remove_dead_bones,
    "fix_hidden_attachments": fix_hidden_attachments,
    "snap_rect_masks": snap_rect_masks,
    "unused_images": unused_images,
    "report": report,
    "batch": batch,
}
