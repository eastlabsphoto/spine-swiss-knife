"""Texture Unpacker tab — extract individual sprites from texture atlases.

Supports Spine .atlas files and TexturePacker JSON format.
Requires Pillow (PIL) for image processing.
"""

import json
import os
import sys

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QLineEdit,
    QTabWidget, QTextEdit, QMessageBox, QFileDialog, QScrollArea, QSplitter,
)
from PySide6.QtGui import QTextCharFormat, QColor, QFont, QPixmap
from PySide6.QtCore import Qt

from .i18n import tr, language_changed

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


# ==========================================================================
# Spine Atlas Parser
# ==========================================================================

def parse_spine_atlas(atlas_path: str) -> list[tuple[str, list[dict]]]:
    with open(atlas_path, "r") as f:
        lines = f.readlines()

    pages: list[tuple[str, list[dict]]] = []
    current_image = None
    current_frames: list[dict] = []
    current_frame: dict | None = None

    i = 0
    while i < len(lines):
        raw = lines[i].rstrip("\n\r")
        stripped = raw.strip()
        i += 1

        if not stripped:
            if current_frame:
                current_frames.append(current_frame)
                current_frame = None
            if current_image and current_frames:
                pages.append((current_image, current_frames))
            current_image = None
            current_frames = []
            continue

        if current_image is None:
            if ":" not in stripped:
                current_image = stripped
            continue

        if raw[0] in (' ', '\t'):
            if current_frame and ":" in stripped:
                key, value = [x.strip() for x in stripped.split(":", 1)]
                if key == "xy":
                    x, y = map(int, value.split(","))
                    current_frame["x"] = x
                    current_frame["y"] = y
                elif key == "size":
                    w, h = map(int, value.split(","))
                    current_frame["w"] = w
                    current_frame["h"] = h
                elif key == "orig":
                    w, h = map(int, value.split(","))
                    current_frame["orig_w"] = w
                    current_frame["orig_h"] = h
                elif key == "offset":
                    x, y = map(int, value.split(","))
                    current_frame["offset_x"] = x
                    current_frame["offset_y"] = y
                elif key == "rotate":
                    current_frame["rotate"] = value.lower() in ("true", "90")
                elif key == "index":
                    try:
                        current_frame["index"] = int(value)
                    except ValueError:
                        pass
            elif ":" in stripped:
                pass
        else:
            if ":" in stripped and not current_frames and current_frame is None:
                continue
            if current_frame:
                current_frames.append(current_frame)
            current_frame = {"filename": stripped}

    if current_frame:
        current_frames.append(current_frame)
    if current_image and current_frames:
        pages.append((current_image, current_frames))

    return pages


# ==========================================================================
# Extraction Functions
# ==========================================================================

def extract_spine_frames(atlas_path: str, output_dir: str) -> int:
    pages = parse_spine_atlas(atlas_path)
    atlas_dir = os.path.dirname(atlas_path)
    count = 0

    for image_name, frames in pages:
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
                frame_image = atlas_image.crop((x, y, x + h, y + w))
                frame_image = frame_image.rotate(-90, expand=True)
            else:
                frame_image = atlas_image.crop((x, y, x + w, y + h))
            restored = Image.new("RGBA", (orig_w, orig_h), (0, 0, 0, 0))
            restored.paste(frame_image, (offset_x, offset_y))
            base = os.path.splitext(frame["filename"])[0]
            out_path = os.path.join(output_dir, f"{base}.png")
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            restored.save(out_path)
            count += 1

    return count


def extract_json_frames(data: dict, output_dir: str, json_path: str) -> int:
    count = 0
    if "textures" in data:
        for texture in data["textures"]:
            image_path = os.path.join(os.path.dirname(json_path), texture["image"])
            atlas_image = Image.open(image_path)
            for frame in texture["frames"]:
                fd = frame["frame"]
                ss = frame["sourceSize"]
                sss = frame["spriteSourceSize"]
                frame_img = atlas_image.crop(
                    (fd["x"], fd["y"], fd["x"] + fd["w"], fd["y"] + fd["h"])
                )
                restored = Image.new("RGBA", (ss["w"], ss["h"]), (0, 0, 0, 0))
                restored.paste(frame_img, (sss["x"], sss["y"]))
                base = os.path.splitext(frame["filename"])[0]
                out_path = os.path.join(output_dir, f"{base}.png")
                os.makedirs(os.path.dirname(out_path), exist_ok=True)
                restored.save(out_path)
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
        for filename, frame in data["frames"].items():
            fd = frame["frame"]
            ss = frame["sourceSize"]
            sss = frame["spriteSourceSize"]
            frame_img = atlas_image.crop(
                (fd["x"], fd["y"], fd["x"] + fd["w"], fd["y"] + fd["h"])
            )
            restored = Image.new("RGBA", (ss["w"], ss["h"]), (0, 0, 0, 0))
            restored.paste(frame_img, (sss["x"], sss["y"]))
            base = os.path.splitext(filename)[0]
            out_path = os.path.join(output_dir, f"{base}.png")
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            restored.save(out_path)
            count += 1
    return count


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
            self._preview_atlas(atlas)

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
            fmt.setFontWeight(QFont.Bold)
        cursor = self._log.textCursor()
        cursor.movePosition(cursor.End)
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
            self._preview_atlas(path)

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
                with open(file_path, "r") as f:
                    data = json.load(f)
                if "textures" in data:
                    self._append(tr("unpacker.log.format_tp_textures"))
                elif "frames" in data:
                    self._append(tr("unpacker.log.format_tp_frames"))
                else:
                    self._append(tr("unpacker.log.unknown_json"), "#f38ba8", bold=True)
                    return
                count = extract_json_frames(data, output_dir, file_path)
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
            pages = parse_spine_atlas(file_path)
            atlas_dir = os.path.dirname(file_path)
            for img_name, frames in pages:
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
                with open(file_path, "r") as f:
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
            pages = parse_spine_atlas(atlas_path)
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
