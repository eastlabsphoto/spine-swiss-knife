"""Headless API for Spine Swiss Knife (no Qt, no GUI).

Every public function takes plain arguments and returns a JSON-serialisable
dict, so the same functions back both the CLI (``cli.py``) and the MCP
server (``mcp_server.py``).

Functions that modify files default to ``apply=False`` (dry-run) and write a
``.backup`` copy before changing anything.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from . import spine_cli
from .atlas_parser import parse_atlas
from .spine_json import load_spine_json, normalize_skins

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
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
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
    skins = normalize_skins(spine_data.get("skins", []))
    for skin_name, slots in skins.items():
        for slot_name, atts in slots.items():
            for att_key, att in atts.items():
                if not isinstance(att, dict):
                    continue
                if att.get("type", "region") not in ("region", "mesh",
                                                     "linkedmesh"):
                    continue
                region = att.get("path") or att.get("name") or att_key
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
    unused = sorted(r for r in regions - set(needed)
                    if not r.lower().endswith((".png", ".jpg", ".jpeg")))
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

def fix_attachment_name(json_path: str, old: str, new: str,
                        apply: bool = False) -> dict:
    """Rename an attachment/region string everywhere in an exported JSON.

    Works on the *exported JSON only* (the .spine project is not touched).
    Dry-run unless ``apply`` is True; writes ``<file>.backup`` first.
    """
    _require_file(json_path, "Skeleton JSON")
    text = Path(json_path).read_text(encoding="utf-8")
    needle = json.dumps(old)
    count = text.count(needle)
    result = {"json": json_path, "old": old, "new": new,
              "occurrences": count, "applied": False}
    if apply and count:
        shutil.copyfile(json_path, json_path + ".backup")
        Path(json_path).write_text(text.replace(needle, json.dumps(new)),
                                   encoding="utf-8")
        json.loads(Path(json_path).read_text(encoding="utf-8"))  # sanity
        result["applied"] = True
        result["backup"] = json_path + ".backup"
    return result


# ---------------------------------------------------------------------------
# export_static (first frame + optional blur)
# ---------------------------------------------------------------------------

def _spine_png(spine_file: str, settings: dict, spine_exe: Optional[str]):
    """Run one Spine PNG export, return the first frame as a PIL image."""
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
    from .static_exporter_core import _create_blur_variants
    _require_file(spine_file, "Spine project")
    if mode not in ("trim", "center", "fixed"):
        raise SpineError("mode must be trim, center or fixed")
    if mode == "fixed" and not (canvas_width and canvas_height):
        raise SpineError("mode=fixed needs canvas_width and canvas_height")

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = name or (f"{skeleton or Path(spine_file).stem}_{animation}")

    base = {"class": "export-png", "exportType": "animation",
            "animation": animation, "fps": 1}
    if skeleton:
        base["skeleton"] = skeleton
    img = _spine_png(spine_file, base, spine_exe)
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
                         spine_exe)
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
# registry used by cli.py and mcp_server.py
# ---------------------------------------------------------------------------

TOOLS = {
    "project_info": project_info,
    "export_json": export_json,
    "check_regions": check_regions,
    "fix_attachment_name": fix_attachment_name,
    "export_static": export_static,
}
