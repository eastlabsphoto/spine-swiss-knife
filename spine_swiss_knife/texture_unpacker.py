"""Texture Unpacker tab — extract individual sprites from texture atlases.

Supports Spine .atlas files and TexturePacker JSON format.
Requires Pillow (PIL) for image processing.
"""

from __future__ import annotations

import json
import os
import sys

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QLineEdit,
    QTabWidget, QTextEdit, QMessageBox, QFileDialog, QScrollArea, QSplitter,
)
from PySide6.QtGui import QTextCharFormat, QTextCursor, QColor, QFont, QPixmap
from PySide6.QtCore import Qt

from .i18n import tr, language_changed
from .atlas_parser import parse_atlas_pages
from .spine_json import image_key_from_path

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


# ==========================================================================
# Spine Atlas Parser
# ==========================================================================

def parse_spine_atlas(atlas_path: str) -> list[tuple[str, list[dict], float]]:
    """Parse a Spine atlas (3.x and 4.x) into ``[(page_image, frames, page_scale)]``.

    Frame dict keys: filename (region name, never stripped), x, y, w, h (unrotated),
    orig_w, orig_h, offset_x, offset_y, rotate (bool), index (-1 = none).
    """
    pages: list[tuple[str, list[dict], float]] = []
    for page in parse_atlas_pages(atlas_path):
        frames = []
        for r in page["regions"]:
            frames.append({
                "filename": r["name"],
                "x": r["x"], "y": r["y"], "w": r["w"], "h": r["h"],
                "orig_w": r["orig_w"], "orig_h": r["orig_h"],
                "offset_x": r["offset_x"], "offset_y": r["offset_y"],
                "rotate": bool(r["rotate"]), "index": r["index"],
            })
        if frames:
            pages.append((page["name"], frames, page["scale"]))
    return pages


def frame_output_name(frame: dict) -> str:
    """Relative output name (without extension) of an atlas frame.

    Region names are kept verbatim (they have no extension and may contain dots);
    indexed regions get the original ``_<index>`` file suffix.
    """
    name = frame["filename"]
    index = frame.get("index", -1)
    if isinstance(index, int) and index >= 0:
        name = "%s_%d" % (name, index)
    return name


def _safe_output_path(output_dir: str, base: str) -> str | None:
    """output_dir/<base>.png, or None when ``base`` would escape output_dir."""
    out_path = os.path.normpath(os.path.join(output_dir, base + ".png"))
    root = os.path.normpath(os.path.abspath(output_dir))
    if not os.path.abspath(out_path).startswith(root + os.sep):
        return None
    return out_path


def _save_image(img, output_dir: str, base: str) -> bool:
    out_path = _safe_output_path(output_dir, base)
    if out_path is None:
        return False
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path)
    return True


def _strip_image_ext(filename: str) -> str:
    return image_key_from_path(filename)


# ==========================================================================
# Extraction Functions
# ==========================================================================

def extract_spine_frames(atlas_path: str, output_dir: str) -> int:
    pages = parse_spine_atlas(atlas_path)
    atlas_dir = os.path.dirname(atlas_path)
    count = 0

    for image_name, frames, _scale in pages:
        image_path = os.path.join(atlas_dir, image_name)
        try:
            atlas_image = Image.open(image_path)
        except FileNotFoundError:
            image_path = os.path.join(atlas_dir, os.path.basename(image_name))
            atlas_image = Image.open(image_path)

        for frame in frames:
            x = frame.get("x", 0)
            y = frame.get("y", 0)
            w = frame.get("w", 0)
            h = frame.get("h", 0)
            orig_w = frame.get("orig_w", w)
            orig_h = frame.get("orig_h", h)
            offset_x = frame.get("offset_x", 0)
            offset_y = frame.get("offset_y", 0)
            rotated = frame.get("rotate", False)
            if w == 0 or h == 0:
                continue
            if rotated:
                # Rotated regions occupy h x w on the page, stored 90 deg CCW:
                # rotate the piece 90 deg clockwise to restore it.
                frame_image = atlas_image.crop((x, y, x + h, y + w))
                frame_image = frame_image.rotate(-90, expand=True)
            else:
                frame_image = atlas_image.crop((x, y, x + w, y + h))
            restored = Image.new("RGBA", (orig_w, orig_h), (0, 0, 0, 0))
            paste_y = max(0, orig_h - frame_image.height - offset_y)
            # libGDX/Spine atlas offsetY is measured from the bottom edge.
            restored.paste(frame_image, (offset_x, paste_y))
            if _save_image(restored, output_dir, frame_output_name(frame)):
                count += 1

    return count


def _iter_tp_frames(frames):
    """Yield (filename, frame) for TexturePacker "frames" given as dict or list."""
    if isinstance(frames, dict):
        for filename, frame in frames.items():
            yield filename, frame
    elif isinstance(frames, list):
        for frame in frames:
            if isinstance(frame, dict) and "filename" in frame:
                yield frame["filename"], frame


def _tp_crop_size(frame: dict) -> tuple[int, int]:
    """(width, height) of the rectangle a TexturePacker frame occupies on the sheet.

    Rotated frames are stored turned by 90 degrees, so the sheet rectangle is the
    sprite size with width and height swapped.  Some exporters already list the
    swapped size in ``frame``; ``spriteSourceSize`` tells the two apart.
    """
    fd = frame["frame"]
    w, h = fd["w"], fd["h"]
    if not frame.get("rotated"):
        return w, h
    sss = frame.get("spriteSourceSize") or {}
    if (sss.get("w"), sss.get("h")) == (h, w) and w != h:
        return w, h  # already the swapped (sheet) size
    return h, w


def _tp_extract_one(atlas_image, frame: dict):
    """Return (restored_image, skip_reason) for one TexturePacker frame."""
    fd = frame["frame"]
    ss = frame.get("sourceSize") or {"w": fd["w"], "h": fd["h"]}
    sss = frame.get("spriteSourceSize") or {"x": 0, "y": 0}
    rotated = bool(frame.get("rotated"))
    aw, ah = atlas_image.size
    cw, ch = _tp_crop_size(frame)
    x0, y0 = fd["x"], fd["y"]
    x1, y1 = x0 + cw, y0 + ch
    x0c = min(max(x0, 0), aw)
    y0c = min(max(y0, 0), ah)
    x1c = min(max(x1, 0), aw)
    y1c = min(max(y1, 0), ah)
    if x1c <= x0c or y1c <= y0c:
        return None, "region outside image bounds"
    if rotated and (x0c, y0c, x1c, y1c) == (x0, y0, x1, y1):
        piece = atlas_image.crop((x0, y0, x1, y1))
        # TexturePacker rotates 90 deg clockwise: undo counter-clockwise.
        piece = piece.rotate(90, expand=True)
        pasted_x, pasted_y = sss.get("x", 0), sss.get("y", 0)
    elif rotated:
        return None, "rotated region partly outside image bounds"
    else:
        piece = atlas_image.crop((x0c, y0c, x1c, y1c))
        pasted_x = sss.get("x", 0) + (x0c - x0)
        pasted_y = sss.get("y", 0) + (y0c - y0)
    restored = Image.new("RGBA", (ss["w"], ss["h"]), (0, 0, 0, 0))
    restored.paste(piece, (pasted_x, pasted_y))
    return restored, None


def extract_json_frames(data: dict, output_dir: str, json_path: str):
    count = 0
    warnings = []
    if "textures" in data:
        for texture in data["textures"]:
            image_path = os.path.join(os.path.dirname(json_path), texture["image"])
            atlas_image = Image.open(image_path).convert("RGBA")
            for filename, frame in _iter_tp_frames(texture["frames"]):
                restored, reason = _tp_extract_one(atlas_image, frame)
                if restored is None:
                    warnings.append(f"Skipped '{filename}': {reason}.")
                    continue
                if _save_image(restored, output_dir, _strip_image_ext(filename)):
                    count += 1
    elif "frames" in data:
        meta = data["meta"]
        json_dir = os.path.dirname(json_path)
        image_path = os.path.join(json_dir, meta["image"])
        try:
            atlas_image = Image.open(image_path)
        except FileNotFoundError:
            image_path = os.path.join(json_dir, os.path.basename(meta["image"]))
            atlas_image = Image.open(image_path)
        atlas_image = atlas_image.convert("RGBA")
        aw, ah = atlas_image.size
        meta_size = meta.get("size", {})
        mw, mh = meta_size.get("w", aw), meta_size.get("h", ah)
        size_match = (aw, ah) == (mw, mh)
        items = list(_iter_tp_frames(data["frames"]))

        if not size_match:
            # Derive grid layout from JSON frame coordinates, then
            # re-map to actual image dimensions.
            xs = sorted({f["frame"]["x"] for _n, f in items})
            ys = sorted({f["frame"]["y"] for _n, f in items})
            cols = len(xs)
            rows = len(ys)
            cell_w = aw / cols
            cell_h = ah / rows
            x_to_col = {x: i for i, x in enumerate(xs)}
            y_to_row = {y: i for i, y in enumerate(ys)}
            warnings.append(
                f"Atlas size mismatch ({aw}x{ah} vs JSON {mw}x{mh}). "
                f"Using grid extraction: {rows} rows x {cols} cols, "
                f"cell ~{cell_w:.0f}x{cell_h:.0f}px."
            )
            for filename, frame in items:
                fd = frame["frame"]
                col = x_to_col[fd["x"]]
                row = y_to_row[fd["y"]]
                x0 = round(col * cell_w)
                y0 = round(row * cell_h)
                x1 = round((col + 1) * cell_w)
                y1 = round((row + 1) * cell_h)
                frame_img = atlas_image.crop((x0, y0, x1, y1))
                if _save_image(frame_img, output_dir, _strip_image_ext(filename)):
                    count += 1
        else:
            for filename, frame in items:
                restored, reason = _tp_extract_one(atlas_image, frame)
                if restored is None:
                    warnings.append(f"Skipped '{filename}': {reason}.")
                    continue
                if _save_image(restored, output_dir, _strip_image_ext(filename)):
                    count += 1
    return count, warnings


# ==========================================================================
# UI Tab
# ==========================================================================

class TextureUnpackerTab:
    def __init__(self, tabs: QTabWidget, get_config):
        self._get_config = get_config
        self._tabs = tabs

        self._page = QWidget()
        tabs.addTab(self._page, tr("unpacker.tab"))
        layout = QVBoxLayout(self._page)
        layout.setContentsMargins(5, 5, 5, 5)

        if HAS_PIL:
            desc = tr("unpacker.info")
        else:
            desc = tr("unpacker.info_no_pil")
        self._info = QLabel(desc)
        self._info.setWordWrap(True)
        layout.addWidget(self._info)

        file_row = QHBoxLayout()
        self._file_label = QLabel(tr("unpacker.file_label"))
        file_row.addWidget(self._file_label)
        self._file_edit = QLineEdit()
        file_row.addWidget(self._file_edit, 1)
        self._browse_file_btn = QPushButton(tr("unpacker.browse"))
        self._browse_file_btn.clicked.connect(self._browse_file)
        file_row.addWidget(self._browse_file_btn)
        layout.addLayout(file_row)

        out_row = QHBoxLayout()
        self._output_label = QLabel(tr("unpacker.output_label"))
        out_row.addWidget(self._output_label)
        self._output_edit = QLineEdit()
        out_row.addWidget(self._output_edit, 1)
        self._browse_out_btn = QPushButton(tr("unpacker.browse"))
        self._browse_out_btn.clicked.connect(self._browse_output)
        out_row.addWidget(self._browse_out_btn)
        layout.addLayout(out_row)

        btn_row = QHBoxLayout()
        self._extract_btn = QPushButton(tr("unpacker.extract_btn"))
        self._extract_btn.clicked.connect(self._extract)
        btn_row.addWidget(self._extract_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._stats = QLabel(tr("unpacker.default_stats"))
        self._stats.setStyleSheet("font-weight: bold; color: #6ec072;")
        layout.addWidget(self._stats)

        splitter = QSplitter(Qt.Vertical)
        layout.addWidget(splitter, 1)

        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setFont(QFont("monospace", 10))
        splitter.addWidget(self._log)

        # Preview area
        self._preview_scroll = QScrollArea()
        self._preview_scroll.setWidgetResizable(True)
        self._preview_inner = QWidget()
        self._preview_layout = QHBoxLayout(self._preview_inner)
        self._preview_layout.setContentsMargins(4, 4, 4, 4)
        self._preview_layout.setSpacing(8)
        self._preview_layout.addStretch()
        self._preview_scroll.setWidget(self._preview_inner)
        self._preview_scroll.setMinimumHeight(120)
        splitter.addWidget(self._preview_scroll)
        splitter.setSizes([300, 150])

        atlas = get_config("atlas")
        if atlas and os.path.isfile(atlas):
            self._file_edit.setText(atlas)
            self._auto_output(atlas)
            try:
                self._preview_atlas(atlas)
            except Exception:
                pass

        language_changed.connect(self._retranslate)

    def _retranslate(self):
        idx = self._tabs.indexOf(self._page)
        if idx >= 0:
            self._tabs.setTabText(idx, tr("unpacker.tab"))
        if HAS_PIL:
            self._info.setText(tr("unpacker.info"))
        else:
            self._info.setText(tr("unpacker.info_no_pil"))
        self._file_label.setText(tr("unpacker.file_label"))
        self._browse_file_btn.setText(tr("unpacker.browse"))
        self._output_label.setText(tr("unpacker.output_label"))
        self._browse_out_btn.setText(tr("unpacker.browse"))
        self._extract_btn.setText(tr("unpacker.extract_btn"))
        self._stats.setText(tr("unpacker.default_stats"))

    def _append(self, text: str, color: str = "#cdd6f4", bold: bool = False):
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        if bold:
            fmt.setFontWeight(QFont.Weight.Bold)
        cursor = self._log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text + "\n", fmt)
        self._log.setTextCursor(cursor)
        self._log.ensureCursorVisible()

    def _clear_log(self):
        self._log.clear()

    def _auto_output(self, file_path: str):
        if not self._output_edit.text():
            file_dir = os.path.dirname(os.path.abspath(file_path))
            base = os.path.splitext(os.path.basename(file_path))[0]
            self._output_edit.setText(os.path.join(file_dir, f"_unpacked_{base}"))

    def _browse_file(self):
        if sys.platform == "darwin":
            path, _ = QFileDialog.getOpenFileName(None, tr("unpacker.dialog.select_file"))
        else:
            path, _ = QFileDialog.getOpenFileName(
                None, tr("unpacker.dialog.select_file"), "",
                "Atlas Files (*.atlas *.atlas.txt *.json);;All files (*.*)",
            )
        if path:
            self._file_edit.setText(path)
            self._auto_output(path)
            try:
                self._preview_atlas(path)
            except Exception:
                pass

    def _browse_output(self):
        path = QFileDialog.getExistingDirectory(None, tr("unpacker.dialog.select_output"))
        if path:
            self._output_edit.setText(path)

    def _extract(self):
        if not HAS_PIL:
            QMessageBox.critical(None, tr("err.title"), tr("unpacker.err.no_pil"))
            return

        file_path = self._file_edit.text().strip()
        if not file_path or not os.path.isfile(file_path):
            QMessageBox.critical(None, tr("err.title"), tr("unpacker.err.no_file"))
            return

        output_dir = self._output_edit.text().strip()
        if not output_dir:
            QMessageBox.critical(None, tr("err.title"), tr("unpacker.err.no_output"))
            return

        self._clear_log()
        self._append(tr("unpacker.log.input", path=file_path))
        self._append(tr("unpacker.log.output", path=output_dir))

        try:
            os.makedirs(output_dir, exist_ok=True)
        except Exception as e:
            self._append(tr("unpacker.log.cannot_create_dir", error=e), "#f38ba8", bold=True)
            return

        ext = os.path.splitext(file_path)[1].lower()
        if file_path.lower().endswith(".atlas.txt"):
            ext = ".atlas"

        try:
            if ext == ".atlas":
                self._append(tr("unpacker.log.format_spine"))
                count = extract_spine_frames(file_path, output_dir)
            elif ext == ".json":
                with open(file_path, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                if "textures" in data:
                    self._append(tr("unpacker.log.format_tp_textures"))
                elif "frames" in data:
                    self._append(tr("unpacker.log.format_tp_frames"))
                else:
                    self._append(tr("unpacker.log.unknown_json"), "#f38ba8", bold=True)
                    return
                count, warnings = extract_json_frames(data, output_dir, file_path)
                for w in warnings:
                    self._append(f"⚠ {w}", "#fab387")
            else:
                self._append(tr("unpacker.log.unsupported_ext", ext=ext), "#f38ba8", bold=True)
                return

            self._append("")
            self._append(tr("unpacker.log.extracted", count=count), "#a6e3a1", bold=True)
            self._stats.setText(tr("unpacker.stats.done", count=count, dir=output_dir))
            self._show_preview(file_path, output_dir)

        except Exception as e:
            self._append(tr("unpacker.log.error", error=e), "#f38ba8", bold=True)
            self._stats.setText(tr("unpacker.stats.failed"))

    def _preview_atlas(self, file_path):
        """Show atlas image preview when a file is selected (before extraction)."""
        self._clear_preview()
        if not file_path or not os.path.isfile(file_path):
            return
        max_h = 140
        ext = os.path.splitext(file_path)[1].lower()
        if file_path.lower().endswith(".atlas.txt"):
            ext = ".atlas"
        if ext == ".atlas":
            try:
                pages = parse_spine_atlas(file_path)
            except Exception:
                pages = []
            atlas_dir = os.path.dirname(file_path)
            for img_name, frames, _scale in pages:
                img_path = os.path.join(atlas_dir, img_name)
                if not os.path.isfile(img_path):
                    continue
                pix = QPixmap(img_path)
                if pix.isNull():
                    continue
                lbl = QLabel()
                lbl.setPixmap(pix.scaledToHeight(max_h, Qt.SmoothTransformation))
                lbl.setToolTip(f"Atlas: {img_name} ({pix.width()}x{pix.height()}, {len(frames)} sprites)")
                lbl.setStyleSheet("border: 2px solid #6ec072; border-radius: 4px;")
                self._preview_layout.addWidget(lbl)
        elif ext == ".json":
            try:
                with open(file_path, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                json_dir = os.path.dirname(file_path)
                images = []
                if "textures" in data:
                    images = [t["image"] for t in data["textures"]]
                elif "frames" in data and "meta" in data:
                    images = [data["meta"]["image"]]
                for img_name in images:
                    img_path = os.path.join(json_dir, img_name)
                    if not os.path.isfile(img_path):
                        continue
                    pix = QPixmap(img_path)
                    if pix.isNull():
                        continue
                    lbl = QLabel()
                    lbl.setPixmap(pix.scaledToHeight(max_h, Qt.SmoothTransformation))
                    lbl.setToolTip(f"Atlas: {img_name} ({pix.width()}x{pix.height()})")
                    lbl.setStyleSheet("border: 2px solid #6ec072; border-radius: 4px;")
                    self._preview_layout.addWidget(lbl)
            except Exception:
                pass
        self._preview_layout.addStretch()

    def _clear_preview(self):
        while self._preview_layout.count():
            item = self._preview_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _show_preview(self, atlas_path, output_dir):
        self._clear_preview()
        max_h = 100

        # Show atlas image
        ext = os.path.splitext(atlas_path)[1].lower()
        if atlas_path.lower().endswith(".atlas.txt"):
            ext = ".atlas"
        if ext == ".atlas":
            try:
                pages = parse_spine_atlas(atlas_path)
            except Exception:
                pages = []
            if pages:
                img_name = pages[0][0]
                img_path = os.path.join(os.path.dirname(atlas_path), img_name)
                if os.path.isfile(img_path):
                    pix = QPixmap(img_path)
                    if not pix.isNull():
                        lbl = QLabel()
                        lbl.setPixmap(pix.scaledToHeight(max_h, Qt.SmoothTransformation))
                        lbl.setToolTip(f"Atlas: {img_name}")
                        lbl.setStyleSheet("border: 2px solid #6ec072; border-radius: 4px;")
                        self._preview_layout.addWidget(lbl)

        # Show extracted sprites (up to 8)
        if os.path.isdir(output_dir):
            sprites = sorted(
                f for f in os.listdir(output_dir)
                if f.lower().endswith((".png", ".jpg", ".jpeg"))
            )[:8]
            for fname in sprites:
                fpath = os.path.join(output_dir, fname)
                pix = QPixmap(fpath)
                if pix.isNull():
                    continue
                lbl = QLabel()
                lbl.setPixmap(pix.scaledToHeight(max_h, Qt.SmoothTransformation))
                lbl.setToolTip(fname)
                lbl.setStyleSheet("border: 1px solid #45475a; border-radius: 4px;")
                self._preview_layout.addWidget(lbl)

        self._preview_layout.addStretch()
