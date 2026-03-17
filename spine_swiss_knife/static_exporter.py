"""Static Image Exporter tab — export first frame of each animation as PNG.

Provides canvas normalization (uniform size, centered pivot) and optional
vertical motion blur variants for slot game symbols.

Uses hardcoded export-png settings (discovered from Spine CLI).  The user
can tweak parameters via an Export Settings dialog.
"""

import json as _json
import os
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QComboBox,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QHeaderView, QMessageBox,
    QGroupBox, QCheckBox, QSlider, QSpinBox, QLineEdit, QFileDialog,
    QTextEdit, QApplication, QDialog, QFormLayout, QDialogButtonBox,
)
from PySide6.QtGui import QFont, QTextCharFormat, QColor, QTextCursor
from PySide6.QtCore import Qt

from .i18n import tr, language_changed
from .spine_json import load_spine_json
from .settings import settings

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


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


# ---------------------------------------------------------------------------
# Export Settings Dialog
# ---------------------------------------------------------------------------

class _ExportSettingsDialog(QDialog):
    """Dialog to edit Spine CLI export-png parameters."""

    def __init__(self, current: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("static.settings_title"))
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)

        # --- Render group ---
        render_grp = QGroupBox(tr("static.settings.render"))
        render_form = QFormLayout(render_grp)

        self._scale = QSpinBox()
        self._scale.setRange(1, 1000)
        self._scale.setSuffix(" %")
        self._scale.setValue(current.get("scale", 100))
        render_form.addRow(tr("static.settings.scale"), self._scale)

        self._linear = QCheckBox()
        self._linear.setChecked(current.get("linearFiltering", True))
        render_form.addRow(tr("static.settings.linear"), self._linear)

        self._msaa = QComboBox()
        self._msaa.addItem("Off", 0)
        self._msaa.addItem("2x", 2)
        self._msaa.addItem("4x", 4)
        self._msaa.addItem("8x", 8)
        msaa_val = current.get("msaa", 0)
        idx = self._msaa.findData(msaa_val)
        if idx >= 0:
            self._msaa.setCurrentIndex(idx)
        render_form.addRow("MSAA:", self._msaa)

        self._render_images = QCheckBox()
        self._render_images.setChecked(current.get("renderImages", True))
        render_form.addRow(tr("static.settings.render_images"), self._render_images)

        self._render_bones = QCheckBox()
        self._render_bones.setChecked(current.get("renderBones", False))
        render_form.addRow(tr("static.settings.render_bones"), self._render_bones)

        self._render_others = QCheckBox()
        self._render_others.setChecked(current.get("renderOthers", False))
        render_form.addRow(tr("static.settings.render_others"), self._render_others)

        layout.addWidget(render_grp)

        # --- Animation group ---
        anim_grp = QGroupBox(tr("static.settings.animation"))
        anim_form = QFormLayout(anim_grp)

        self._fps = QSpinBox()
        self._fps.setRange(1, 60)
        self._fps.setValue(current.get("fps", 1))
        anim_form.addRow("FPS:", self._fps)

        self._last_frame = QCheckBox()
        self._last_frame.setChecked(current.get("lastFrame", False))
        anim_form.addRow(tr("static.settings.last_frame"), self._last_frame)

        layout.addWidget(anim_grp)

        # --- Crop group ---
        crop_grp = QGroupBox(tr("static.settings.crop"))
        crop_form = QFormLayout(crop_grp)

        self._max_bounds = QCheckBox()
        self._max_bounds.setChecked(current.get("maxBounds", False))
        crop_form.addRow(tr("static.settings.max_bounds"), self._max_bounds)

        self._crop_x = QSpinBox()
        self._crop_x.setRange(-10000, 10000)
        self._crop_x.setValue(current.get("cropX", -1000))
        crop_form.addRow("Crop X:", self._crop_x)

        self._crop_y = QSpinBox()
        self._crop_y.setRange(-10000, 10000)
        self._crop_y.setValue(current.get("cropY", -1000))
        crop_form.addRow("Crop Y:", self._crop_y)

        self._crop_w = QSpinBox()
        self._crop_w.setRange(0, 20000)
        self._crop_w.setValue(current.get("cropWidth", 2000))
        crop_form.addRow(tr("static.settings.crop_w"), self._crop_w)

        self._crop_h = QSpinBox()
        self._crop_h.setRange(0, 20000)
        self._crop_h.setValue(current.get("cropHeight", 2000))
        crop_form.addRow(tr("static.settings.crop_h"), self._crop_h)

        self._pad_cb = QCheckBox()
        self._pad_cb.setChecked(current.get("pad", False))
        crop_form.addRow(tr("static.settings.pad"), self._pad_cb)

        layout.addWidget(crop_grp)

        # --- Output group ---
        out_grp = QGroupBox(tr("static.settings.output"))
        out_form = QFormLayout(out_grp)

        self._compression = QSpinBox()
        self._compression.setRange(0, 9)
        self._compression.setValue(current.get("compression", 9))
        out_form.addRow(tr("static.settings.compression"), self._compression)

        layout.addWidget(out_grp)

        # --- Reset + OK/Cancel ---
        btn_row = QHBoxLayout()
        self._reset_btn = QPushButton(tr("static.settings.reset"))
        self._reset_btn.clicked.connect(self._reset_defaults)
        btn_row.addWidget(self._reset_btn)
        btn_row.addStretch()

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        btn_row.addWidget(buttons)
        layout.addLayout(btn_row)

    def _reset_defaults(self):
        d = DEFAULT_EXPORT_SETTINGS
        self._scale.setValue(d["scale"])
        self._linear.setChecked(d["linearFiltering"])
        idx = self._msaa.findData(d["msaa"])
        if idx >= 0:
            self._msaa.setCurrentIndex(idx)
        self._render_images.setChecked(d["renderImages"])
        self._render_bones.setChecked(d["renderBones"])
        self._render_others.setChecked(d["renderOthers"])
        self._fps.setValue(d["fps"])
        self._last_frame.setChecked(d["lastFrame"])
        self._max_bounds.setChecked(d["maxBounds"])
        self._crop_x.setValue(d["cropX"])
        self._crop_y.setValue(d["cropY"])
        self._crop_w.setValue(d["cropWidth"])
        self._crop_h.setValue(d["cropHeight"])
        self._pad_cb.setChecked(d["pad"])
        self._compression.setValue(d["compression"])

    def get_settings(self) -> dict:
        """Return the full settings dict with user-modified values."""
        s = dict(DEFAULT_EXPORT_SETTINGS)
        s["scale"] = self._scale.value()
        s["linearFiltering"] = self._linear.isChecked()
        s["msaa"] = self._msaa.currentData()
        s["renderImages"] = self._render_images.isChecked()
        s["renderBones"] = self._render_bones.isChecked()
        s["renderOthers"] = self._render_others.isChecked()
        s["fps"] = self._fps.value()
        s["lastFrame"] = self._last_frame.isChecked()
        s["maxBounds"] = self._max_bounds.isChecked()
        s["cropX"] = self._crop_x.value()
        s["cropY"] = self._crop_y.value()
        s["cropWidth"] = self._crop_w.value()
        s["cropHeight"] = self._crop_h.value()
        s["pad"] = self._pad_cb.isChecked()
        s["compression"] = self._compression.value()
        return s


# ---------------------------------------------------------------------------
# UI Tab
# ---------------------------------------------------------------------------

class StaticExporterTab:

    def __init__(self, tabs: QTabWidget, get_config):
        self._get_config = get_config
        self._tabs = tabs
        self._anim_data: dict = {}  # {name: duration}
        self._custom_settings: dict = dict(DEFAULT_EXPORT_SETTINGS)

        # Restore persisted custom settings
        saved = settings.static_export_settings()
        if saved:
            try:
                self._custom_settings = _json.loads(saved)
            except Exception:
                pass

        self._page = QWidget()
        tabs.addTab(self._page, tr("static.tab"))
        layout = QVBoxLayout(self._page)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(4)

        # Info
        self._info = QLabel(tr("static.info"))
        self._info.setWordWrap(True)
        layout.addWidget(self._info)

        # Toolbar row: Select All, Unselect All, stretch, Export Settings
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        self._select_all_btn = QPushButton(tr("static.select_all"))
        self._select_all_btn.clicked.connect(self._select_all)
        btn_row.addWidget(self._select_all_btn)
        self._unselect_all_btn = QPushButton(tr("static.unselect_all"))
        self._unselect_all_btn.clicked.connect(self._unselect_all)
        btn_row.addWidget(self._unselect_all_btn)
        btn_row.addStretch()
        self._settings_btn = QPushButton(tr("static.export_settings_btn"))
        self._settings_btn.clicked.connect(self._open_export_settings)
        btn_row.addWidget(self._settings_btn)
        layout.addLayout(btn_row)

        # Stats
        self._stats = QLabel(tr("static.default_stats"))
        self._stats.setStyleSheet("font-weight: bold; color: #6ec072;")
        layout.addWidget(self._stats)

        # Animation tree — gets all remaining vertical space
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels([
            tr("static.tree.animation"),
            tr("static.tree.duration"),
        ])
        self._tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._tree.setRootIsDecorated(False)
        layout.addWidget(self._tree, 1)

        # --- Compact canvas row ---
        canvas_row = QHBoxLayout()
        canvas_row.setSpacing(6)
        self._shape_label = QLabel(tr("static.shape_label"))
        canvas_row.addWidget(self._shape_label)
        self._shape_combo = QComboBox()
        self._shape_combo.addItem(tr("static.shape_square"), "square")
        self._shape_combo.addItem(tr("static.shape_original"), "original")
        self._shape_combo.addItem(tr("static.shape_custom"), "custom")
        self._shape_combo.currentIndexChanged.connect(self._on_shape_changed)
        canvas_row.addWidget(self._shape_combo)
        canvas_row.addSpacing(10)
        self._pad_label = QLabel(tr("static.padding_label"))
        canvas_row.addWidget(self._pad_label)
        self._pad_spin = QSpinBox()
        self._pad_spin.setRange(0, 200)
        self._pad_spin.setValue(0)
        self._pad_spin.setFixedWidth(60)
        canvas_row.addWidget(self._pad_spin)
        canvas_row.addSpacing(10)
        self._center_cb = QCheckBox(tr("static.center_pivot"))
        self._center_cb.setChecked(True)
        canvas_row.addWidget(self._center_cb)
        canvas_row.addStretch()
        layout.addLayout(canvas_row)

        # --- Custom canvas size row (hidden by default) ---
        custom_row = QHBoxLayout()
        custom_row.setSpacing(6)
        self._static_size_label = QLabel(tr("static.custom_static_label"))
        custom_row.addWidget(self._static_size_label)
        self._static_w = QSpinBox()
        self._static_w.setRange(1, 10000)
        self._static_w.setValue(400)
        self._static_w.setFixedWidth(70)
        custom_row.addWidget(self._static_w)
        self._static_x_label = QLabel("x")
        custom_row.addWidget(self._static_x_label)
        self._static_h = QSpinBox()
        self._static_h.setRange(1, 10000)
        self._static_h.setValue(400)
        self._static_h.setFixedWidth(70)
        custom_row.addWidget(self._static_h)
        self._static_px_label = QLabel("px")
        custom_row.addWidget(self._static_px_label)
        custom_row.addSpacing(16)
        self._blur_size_label = QLabel(tr("static.custom_blur_label"))
        custom_row.addWidget(self._blur_size_label)
        self._blur_w = QSpinBox()
        self._blur_w.setRange(1, 10000)
        self._blur_w.setValue(450)
        self._blur_w.setFixedWidth(70)
        custom_row.addWidget(self._blur_w)
        self._blur_x_label = QLabel("x")
        custom_row.addWidget(self._blur_x_label)
        self._blur_h = QSpinBox()
        self._blur_h.setRange(1, 10000)
        self._blur_h.setValue(450)
        self._blur_h.setFixedWidth(70)
        custom_row.addWidget(self._blur_h)
        self._blur_px_label = QLabel("px")
        custom_row.addWidget(self._blur_px_label)
        custom_row.addStretch()
        layout.addLayout(custom_row)

        # Collect custom-row widgets for show/hide
        self._custom_widgets = [
            self._static_size_label, self._static_w, self._static_x_label,
            self._static_h, self._static_px_label,
            self._blur_size_label, self._blur_w, self._blur_x_label,
            self._blur_h, self._blur_px_label,
        ]
        for w in self._custom_widgets:
            w.setVisible(False)

        # --- Compact blur row ---
        blur_row = QHBoxLayout()
        blur_row.setSpacing(6)
        self._blur_cb = QCheckBox(tr("static.blur_enable"))
        self._blur_cb.setChecked(False)
        self._blur_cb.toggled.connect(self._on_blur_toggled)
        blur_row.addWidget(self._blur_cb)
        blur_row.addSpacing(10)
        self._blur_radius_label = QLabel(tr("static.blur_radius_label"))
        blur_row.addWidget(self._blur_radius_label)
        self._blur_slider = QSlider(Qt.Horizontal)
        self._blur_slider.setRange(1, 100)
        self._blur_slider.setValue(20)
        self._blur_slider.setFixedWidth(100)
        self._blur_slider.valueChanged.connect(
            lambda v: self._blur_spin.setValue(v))
        blur_row.addWidget(self._blur_slider)
        self._blur_spin = QSpinBox()
        self._blur_spin.setRange(1, 100)
        self._blur_spin.setValue(20)
        self._blur_spin.setFixedWidth(55)
        self._blur_spin.valueChanged.connect(
            lambda v: self._blur_slider.setValue(v))
        blur_row.addWidget(self._blur_spin)
        blur_row.addSpacing(10)
        self._blur_extra_label = QLabel(tr("static.blur_extra_label"))
        blur_row.addWidget(self._blur_extra_label)
        self._blur_extra_spin = QSpinBox()
        self._blur_extra_spin.setRange(0, 200)
        self._blur_extra_spin.setValue(40)
        self._blur_extra_spin.setFixedWidth(60)
        blur_row.addWidget(self._blur_extra_spin)
        blur_row.addStretch()
        layout.addLayout(blur_row)

        # Blur widgets initial state
        self._blur_slider.setEnabled(False)
        self._blur_spin.setEnabled(False)
        self._blur_extra_spin.setEnabled(False)
        self._blur_radius_label.setEnabled(False)
        self._blur_extra_label.setEnabled(False)

        # --- Output + Export row ---
        out_row = QHBoxLayout()
        out_row.setSpacing(4)
        self._output_label = QLabel(tr("static.output_label"))
        out_row.addWidget(self._output_label)
        self._output_edit = QLineEdit()
        out_row.addWidget(self._output_edit, 1)
        self._browse_btn = QPushButton(tr("static.browse"))
        self._browse_btn.clicked.connect(self._browse_output)
        out_row.addWidget(self._browse_btn)
        self._export_btn = QPushButton(tr("static.export_btn"))
        self._export_btn.clicked.connect(self._export)
        out_row.addWidget(self._export_btn)
        layout.addLayout(out_row)

        # Log — compact
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setFont(QFont("monospace", 10))
        self._log.setMaximumHeight(100)
        layout.addWidget(self._log)

        language_changed.connect(self._retranslate)

    # --- Language ---

    def _retranslate(self):
        idx = self._tabs.indexOf(self._page)
        if idx >= 0:
            self._tabs.setTabText(idx, tr("static.tab"))
        self._info.setText(tr("static.info"))
        self._settings_btn.setText(tr("static.export_settings_btn"))
        self._select_all_btn.setText(tr("static.select_all"))
        self._unselect_all_btn.setText(tr("static.unselect_all"))
        self._tree.setHeaderLabels([
            tr("static.tree.animation"),
            tr("static.tree.duration"),
        ])
        self._shape_label.setText(tr("static.shape_label"))
        self._shape_combo.setItemText(0, tr("static.shape_square"))
        self._shape_combo.setItemText(1, tr("static.shape_original"))
        self._shape_combo.setItemText(2, tr("static.shape_custom"))
        self._pad_label.setText(tr("static.padding_label"))
        self._center_cb.setText(tr("static.center_pivot"))
        self._static_size_label.setText(tr("static.custom_static_label"))
        self._blur_size_label.setText(tr("static.custom_blur_label"))
        self._blur_cb.setText(tr("static.blur_enable"))
        self._blur_radius_label.setText(tr("static.blur_radius_label"))
        self._blur_extra_label.setText(tr("static.blur_extra_label"))
        self._output_label.setText(tr("static.output_label"))
        self._browse_btn.setText(tr("static.browse"))
        self._export_btn.setText(tr("static.export_btn"))
        self._stats.setText(tr("static.default_stats"))

    # --- UI Helpers ---

    def _on_shape_changed(self, _index: int):
        is_custom = self._shape_combo.currentData() == "custom"
        for w in self._custom_widgets:
            w.setVisible(is_custom)
        # Hide auto-fit controls when custom is active
        self._pad_label.setVisible(not is_custom)
        self._pad_spin.setVisible(not is_custom)
        self._center_cb.setVisible(not is_custom)

    def _on_blur_toggled(self, checked: bool):
        self._blur_slider.setEnabled(checked)
        self._blur_spin.setEnabled(checked)
        self._blur_extra_spin.setEnabled(checked)
        self._blur_radius_label.setEnabled(checked)
        self._blur_extra_label.setEnabled(checked)

    def _select_all(self):
        for i in range(self._tree.topLevelItemCount()):
            self._tree.topLevelItem(i).setCheckState(0, Qt.Checked)

    def _unselect_all(self):
        for i in range(self._tree.topLevelItemCount()):
            self._tree.topLevelItem(i).setCheckState(0, Qt.Unchecked)

    def _open_export_settings(self):
        dlg = _ExportSettingsDialog(self._custom_settings, self._page)
        if dlg.exec() == QDialog.Accepted:
            self._custom_settings = dlg.get_settings()
            settings.set_static_export_settings(
                _json.dumps(self._custom_settings))

    def _browse_output(self):
        d = QFileDialog.getExistingDirectory(self._page, tr("static.browse"))
        if d:
            self._output_edit.setText(d)

    def _append(self, text: str, color: str = "#cdd6f4", bold: bool = False):
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        if bold:
            fmt.setFontWeight(QFont.Bold)
        cursor = self._log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text + "\n", fmt)
        self._log.setTextCursor(cursor)
        self._log.ensureCursorVisible()

    # --- Load animations from JSON ---

    def _load(self):
        json_path = self._get_config("json")
        if not json_path or not os.path.isfile(json_path):
            return

        try:
            data = load_spine_json(json_path)
        except Exception:
            return

        animations = data.get("animations", {})
        if not isinstance(animations, dict):
            return

        self._anim_data.clear()
        self._tree.clear()

        for anim_name, anim in sorted(animations.items()):
            # Calculate duration from timelines
            duration = 0.0
            if isinstance(anim, dict):
                for tl_type in anim.values():
                    if isinstance(tl_type, dict):
                        for tl in tl_type.values():
                            if isinstance(tl, dict):
                                for frames in tl.values():
                                    if isinstance(frames, list):
                                        for f in frames:
                                            if isinstance(f, dict):
                                                t = f.get("time", 0)
                                                if isinstance(t, (int, float)):
                                                    duration = max(duration, t)
                            elif isinstance(tl, list):
                                for f in tl:
                                    if isinstance(f, dict):
                                        t = f.get("time", 0)
                                        if isinstance(t, (int, float)):
                                            duration = max(duration, t)

            self._anim_data[anim_name] = duration
            item = QTreeWidgetItem([anim_name, f"{duration:.2f}s"])
            item.setCheckState(0, Qt.Checked)
            self._tree.addTopLevelItem(item)

        # Auto-set output dir
        mode = self._get_config("mode")
        spine_path = self._get_config("spine")
        json_path = self._get_config("json")
        exe = self._get_config("spine_exe")

        if mode == "spine" and spine_path:
            stem = Path(spine_path).stem
            out_dir = str(Path(spine_path).parent / f"_static_{stem}")
            self._output_edit.setText(out_dir)
        elif mode == "json" and json_path:
            stem = Path(json_path).stem
            out_dir = str(Path(json_path).parent / f"_static_{stem}")
            self._output_edit.setText(out_dir)

        # Update info based on mode — allow JSON mode when Spine CLI is available
        has_spine_cli = bool(exe)
        can_export = (mode == "spine" and spine_path) or (mode == "json" and json_path and has_spine_cli)

        if not can_export:
            self._info.setText(tr("static.info_no_spine"))
            self._export_btn.setEnabled(False)
        else:
            self._info.setText(tr("static.info"))
            self._export_btn.setEnabled(True)

        count = len(self._anim_data)
        self._stats.setText(tr("static.stats.loaded", count=count))

    # --- Export pipeline ---

    def _export(self):
        if not HAS_PIL:
            QMessageBox.critical(None, tr("err.title"), tr("static.info_no_pil"))
            return

        mode = self._get_config("mode")
        spine_path = self._get_config("spine")
        json_path = self._get_config("json")
        exe = self._get_config("spine_exe")

        if not exe:
            QMessageBox.warning(None, tr("err.title"), tr("static.err.no_spine"))
            return

        # Resolve the .spine file to use for export
        if mode == "spine" and spine_path:
            effective_spine = spine_path
            self._tmp_spine = None
        elif mode == "json" and json_path:
            effective_spine = None  # will be created below
            self._tmp_spine = None
        else:
            QMessageBox.warning(None, tr("err.title"), tr("static.err.no_spine"))
            return

        output_dir = self._output_edit.text().strip()
        if not output_dir:
            QMessageBox.warning(None, tr("err.title"), tr("static.err.no_output"))
            return

        # Collect checked animations
        checked: list[str] = []
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            if item.checkState(0) == Qt.Checked:
                checked.append(item.text(0))

        if not checked:
            QMessageBox.warning(None, tr("err.title"), tr("static.err.no_selection"))
            return

        if QMessageBox.question(
            None, tr("confirm.title"),
            tr("static.confirm", count=len(checked), dir=output_dir),
        ) != QMessageBox.Yes:
            return

        self._log.clear()
        self._append(tr("static.log.start"))
        QApplication.processEvents()

        # Step 1: Export all frames via Spine CLI (single run)
        from .spine_cli import export_first_frames, import_to_spine_project

        def on_output(line):
            if line is not None:
                self._append(line)
            QApplication.processEvents()

        self._stats.setText(tr("static.exporting"))
        QApplication.processEvents()

        # If JSON mode, import into a temporary .spine project first
        tmp_spine_path = None
        if effective_spine is None and json_path:
            import tempfile as _tmpmod
            fd, tmp_spine_path = _tmpmod.mkstemp(suffix=".spine", prefix="ssk_static_")
            os.close(fd)
            self._append("Importing JSON into temporary .spine project...")
            QApplication.processEvents()
            imp_result = import_to_spine_project(
                exe, json_path, tmp_spine_path, on_output=on_output,
            )
            if not imp_result.success:
                self._append(tr("static.log.error", error=imp_result.stderr),
                             "#f38ba8", bold=True)
                self._stats.setText(tr("static.stats.failed"))
                try:
                    Path(tmp_spine_path).unlink(missing_ok=True)
                except OSError:
                    pass
                return
            effective_spine = tmp_spine_path

        try:
            exported = export_first_frames(
                exe, effective_spine, output_dir, self._custom_settings, checked,
                on_output=on_output,
            )
        except Exception as e:
            self._append(tr("static.log.error", error=str(e)), "#f38ba8", bold=True)
            self._stats.setText(tr("static.stats.failed"))
            return
        finally:
            if tmp_spine_path:
                try:
                    Path(tmp_spine_path).unlink(missing_ok=True)
                except OSError:
                    pass

        if not exported:
            self._append(tr("static.log.error", error="No frames exported"),
                         "#f38ba8", bold=True)
            self._stats.setText(tr("static.stats.failed"))
            return

        self._append(f"Matched {len(exported)}/{len(checked)} animation frame(s)")

        # Step 2: Canvas normalization
        shape = self._shape_combo.currentData() or "square"
        padding = self._pad_spin.value()
        center = self._center_cb.isChecked()
        fixed_size = None
        blur_canvas_size = None

        if shape == "custom":
            fixed_size = (self._static_w.value(), self._static_h.value())
            blur_canvas_size = (self._blur_w.value(), self._blur_h.value())

        self._append(tr("static.log.normalizing"))
        QApplication.processEvents()

        try:
            _normalize_canvas(exported, shape, padding, center,
                              fixed_size=fixed_size)
        except Exception as e:
            self._append(tr("static.log.error", error=str(e)), "#f9e2af")

        # Step 3: Blur variants
        if self._blur_cb.isChecked():
            radius = self._blur_spin.value()
            extra = self._blur_extra_spin.value()
            for name in exported:
                self._append(tr("static.log.blur", anim=name))
                QApplication.processEvents()
            try:
                blur_paths = _create_blur_variants(
                    exported, radius, extra,
                    blur_canvas_size=blur_canvas_size)
                self._append(f"Created {len(blur_paths)} blur variant(s)")
            except Exception as e:
                self._append(tr("static.log.error", error=str(e)), "#f9e2af")

        # Done
        self._append("")
        self._append(
            tr("static.log.done", count=len(exported), dir=output_dir),
            "#a6e3a1", bold=True,
        )
        self._stats.setText(
            tr("static.stats.done",
               exported=len(exported), total=len(checked), dir=output_dir)
        )
