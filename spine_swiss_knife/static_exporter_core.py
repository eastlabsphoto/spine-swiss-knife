"""Static exporter image processing — canvas normalization and blur variants."""

from pathlib import Path

from PIL import Image


# ---------------------------------------------------------------------------
# Default Spine CLI export-png settings
# ---------------------------------------------------------------------------

DEFAULT_EXPORT_SETTINGS: dict = {
    "class": "export-png",
    "name": "PNG",
    "open": False,
    "exportType": "animation",
    "skeletonType": "single",
    "animationType": "all",
    "animation": None,
    "skinType": "current",
    "skinNone": False,
    "skin": None,
    "maxBounds": False,
    "renderImages": True,
    "renderBones": False,
    "renderOthers": False,
    "linearFiltering": True,
    "scale": 100,
    "fitWidth": 0,
    "fitHeight": 0,
    "enlarge": False,
    "background": None,
    "fps": 1,
    "lastFrame": False,
    "cropX": -1000,
    "cropY": -1000,
    "cropWidth": 2000,
    "cropHeight": 2000,
    "rangeStart": -1,
    "rangeEnd": -1,
    "pad": False,
    "msaa": 0,
    "packAtlas": None,
    "compression": 9,
}


# ---------------------------------------------------------------------------
# Image processing helpers
# ---------------------------------------------------------------------------

def _vertical_motion_blur(img: "Image.Image", radius: int) -> "Image.Image":
    """Apply vertical motion blur (90 degrees) using Pillow only.

    Uses progressive blending of vertically-shifted copies to produce
    a uniform box blur in the vertical direction.
    """
    if radius <= 0:
        return img.copy()

    w, h = img.size
    acc = None
    for i, dy in enumerate(range(-radius, radius + 1)):
        shifted = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        src_y1 = max(0, -dy)
        src_y2 = min(h, h - dy)
        dst_y1 = max(0, dy)
        if src_y2 > src_y1:
            strip = img.crop((0, src_y1, w, src_y2))
            shifted.paste(strip, (0, dst_y1))
        if acc is None:
            acc = shifted
        else:
            acc = Image.blend(acc, shifted, 1.0 / (i + 1))
    return acc


def _normalize_canvas(
    image_paths: dict[str, str],
    shape: str,
    padding: int,
    center_pivot: bool,
    fixed_size: tuple[int, int] | None = None,
) -> dict[str, str]:
    """Normalize all exported images to uniform canvas size.

    When *fixed_size* ``(w, h)`` is given the output canvas is exactly that
    size with the Spine origin mapped to its centre.  Content that overflows
    is cropped; smaller content gets transparent padding.

    When *center_pivot* is True the Spine origin (centre of the exported
    image) is kept at the centre of the output canvas so that pivots are
    preserved.  Otherwise content is trimmed and top-left aligned.

    Overwrites files in place.  Returns the same dict.
    """
    images: dict[str, "Image.Image"] = {}
    bboxes: dict[str, tuple] = {}

    for name, path in image_paths.items():
        img = Image.open(path).convert("RGBA")
        images[name] = img
        bbox = img.getbbox()
        if bbox is None:
            bbox = (0, 0, img.width, img.height)
        bboxes[name] = bbox

    if not bboxes:
        return image_paths

    if fixed_size:
        # User-defined canvas — origin stays at centre.
        target_w, target_h = fixed_size
        for name, img in images.items():
            ox = img.width / 2.0
            oy = img.height / 2.0
            canvas = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
            paste_x = int(target_w / 2.0 - ox)
            paste_y = int(target_h / 2.0 - oy)
            canvas.paste(img, (paste_x, paste_y))
            canvas.save(image_paths[name])
    elif center_pivot:
        # Keep Spine origin (center of source image) at canvas center.
        max_left = max_right = max_top = max_bottom = 0
        for name, img in images.items():
            bbox = bboxes[name]
            ox = img.width / 2.0
            oy = img.height / 2.0
            max_left = max(max_left, ox - bbox[0])
            max_right = max(max_right, bbox[2] - ox)
            max_top = max(max_top, oy - bbox[1])
            max_bottom = max(max_bottom, bbox[3] - oy)

        half_w = max(max_left, max_right) + padding
        half_h = max(max_top, max_bottom) + padding

        if shape == "square":
            half = max(half_w, half_h)
            half_w = half_h = half

        target_w = int(2 * half_w)
        target_h = int(2 * half_h)

        for name, img in images.items():
            bbox = bboxes[name]
            ox = img.width / 2.0
            oy = img.height / 2.0
            content = img.crop(bbox)
            canvas = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
            paste_x = int(target_w / 2.0 + (bbox[0] - ox))
            paste_y = int(target_h / 2.0 + (bbox[1] - oy))
            canvas.paste(content, (paste_x, paste_y))
            canvas.save(image_paths[name])
    else:
        # Trim to content, uniform size, top-left aligned.
        max_cw = max((b[2] - b[0]) for b in bboxes.values())
        max_ch = max((b[3] - b[1]) for b in bboxes.values())

        if shape == "square":
            side = max(max_cw, max_ch) + 2 * padding
            target_w, target_h = side, side
        else:
            target_w = max_cw + 2 * padding
            target_h = max_ch + 2 * padding

        for name, img in images.items():
            bbox = bboxes[name]
            content = img.crop(bbox)
            canvas = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
            canvas.paste(content, (padding, padding))
            canvas.save(image_paths[name])

    return image_paths


def _create_blur_variants(
    image_paths: dict[str, str],
    radius: int,
    extra_pad: int,
    blur_canvas_size: tuple[int, int] | None = None,
) -> dict[str, str]:
    """Create vertical motion blur variants for each image.

    When *blur_canvas_size* ``(w, h)`` is given the blur output is placed
    on a canvas of exactly that size, centred on the image centre.

    Returns dict mapping animation name -> blur PNG path.
    """
    blur_paths: dict[str, str] = {}
    for name, path in image_paths.items():
        img = Image.open(path).convert("RGBA")
        w, h = img.size

        # Enlarge canvas vertically
        if extra_pad > 0:
            new_h = h + 2 * extra_pad
            padded = Image.new("RGBA", (w, new_h), (0, 0, 0, 0))
            padded.paste(img, (0, extra_pad))
            img = padded

        blurred = _vertical_motion_blur(img, radius)

        # Resize to fixed blur canvas if requested
        if blur_canvas_size:
            bw, bh = blur_canvas_size
            src_w, src_h = blurred.size
            canvas = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
            paste_x = (bw - src_w) // 2
            paste_y = (bh - src_h) // 2
            canvas.paste(blurred, (paste_x, paste_y))
            blurred = canvas

        stem = Path(path).stem
        parent = Path(path).parent
        blur_path = str(parent / f"{stem}_blur.png")
        blurred.save(blur_path)
        blur_paths[name] = blur_path

    return blur_paths
