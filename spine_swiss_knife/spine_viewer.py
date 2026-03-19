"""Spine Skeleton Viewer tab — interactive skeleton preview with animation."""

import os
import platform
import subprocess

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QTabWidget, QMessageBox, QComboBox, QSlider,
    QScrollArea, QFrame, QListWidget,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap

from .spine_json import load_spine_json, normalize_skins
from .i18n import tr, language_changed

# Re-export from new modules for backward compatibility
from .spine_viewer_animation import (  # noqa: F401
    BoneTransform, AnimationState,
    solve_world_transforms, solve_ik_constraints,
    build_draw_list, _evaluate_draw_order, _affine_from_triangles,
    _color_to_floats, load_atlas_textures,
)
from .spine_viewer_canvas import SpineGLCanvas  # noqa: F401


# ==========================================================================
# UI Tab
# ==========================================================================

class SpineViewerTab:
    def __init__(self, tabs: QTabWidget, get_config):
        self._get_config = get_config
        self._tabs = tabs
        self._spine_data: dict | None = None
        self._textures: dict[str, QPixmap] = {}

        self._page = QWidget()
        tabs.addTab(self._page, tr("viewer.tab"))
        outer = QHBoxLayout(self._page)
        outer.setContentsMargins(5, 5, 5, 5)
        outer.setSpacing(5)

        # ══════════════════════════════════════════════════════════════
        # Left sidebar (scrollable)
        # ══════════════════════════════════════════════════════════════
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(265)
        scroll.setFrameShape(QFrame.NoFrame)
        sidebar_widget = QWidget()
        sb = QVBoxLayout(sidebar_widget)
        sb.setContentsMargins(2, 2, 2, 2)
        sb.setSpacing(4)
        scroll.setWidget(sidebar_widget)
        outer.addWidget(scroll)

        # ── Open Full Version ──
        self._btn_full = QPushButton(tr("viewer.open_full"))
        self._btn_full.clicked.connect(self._open_full_version)
        sb.addWidget(self._btn_full)

        self._add_separator(sb)

        # ── Stats ──
        self._stats = QLabel(tr("viewer.default_stats"))
        self._stats.setWordWrap(True)
        self._stats.setStyleSheet("font-weight: bold; color: #6ec072;")
        sb.addWidget(self._stats)

        self._add_separator(sb)

        # ── Skeleton Scale ──
        self._scale_title = QLabel(tr("viewer.scale_label"))
        sb.addWidget(self._scale_title)
        scale_row = QHBoxLayout()
        self._scale_slider = QSlider(Qt.Horizontal)
        self._scale_slider.setRange(10, 300)
        self._scale_slider.setValue(100)
        self._scale_slider.valueChanged.connect(self._on_scale_changed)
        scale_row.addWidget(self._scale_slider, 1)
        self._scale_label = QLabel("1.0")
        self._scale_label.setFixedWidth(30)
        scale_row.addWidget(self._scale_label)
        self._btn_scale_reset = QPushButton("R")
        self._btn_scale_reset.setFixedWidth(24)
        self._btn_scale_reset.setToolTip(tr("viewer.reset_tip"))
        self._btn_scale_reset.clicked.connect(lambda: self._scale_slider.setValue(100))
        scale_row.addWidget(self._btn_scale_reset)
        sb.addLayout(scale_row)

        # ── Zoom ──
        self._zoom_title = QLabel(tr("viewer.zoom_label"))
        sb.addWidget(self._zoom_title)
        zoom_row = QHBoxLayout()
        self._zoom_slider = QSlider(Qt.Horizontal)
        self._zoom_slider.setRange(1, 1000)
        self._zoom_slider.setValue(100)
        self._zoom_slider.valueChanged.connect(self._on_zoom_slider_changed)
        zoom_row.addWidget(self._zoom_slider, 1)
        self._zoom_label = QLabel("1.0")
        self._zoom_label.setFixedWidth(30)
        zoom_row.addWidget(self._zoom_label)
        self._btn_zoom_reset = QPushButton("R")
        self._btn_zoom_reset.setFixedWidth(24)
        self._btn_zoom_reset.setToolTip(tr("viewer.reset_zoom_tip"))
        self._btn_zoom_reset.clicked.connect(lambda: self._zoom_slider.setValue(100))
        zoom_row.addWidget(self._btn_zoom_reset)
        sb.addLayout(zoom_row)

        # ── Speed ──
        self._speed_title = QLabel(tr("viewer.speed_label"))
        sb.addWidget(self._speed_title)
        speed_row = QHBoxLayout()
        self._speed_slider = QSlider(Qt.Horizontal)
        self._speed_slider.setRange(1, 30)
        self._speed_slider.setValue(10)
        self._speed_slider.valueChanged.connect(self._on_speed_changed)
        speed_row.addWidget(self._speed_slider, 1)
        self._speed_label = QLabel("1.0x")
        self._speed_label.setFixedWidth(35)
        speed_row.addWidget(self._speed_label)
        self._btn_speed_reset = QPushButton("R")
        self._btn_speed_reset.setFixedWidth(24)
        self._btn_speed_reset.setToolTip(tr("viewer.reset_tip"))
        self._btn_speed_reset.clicked.connect(lambda: self._speed_slider.setValue(10))
        speed_row.addWidget(self._btn_speed_reset)
        sb.addLayout(speed_row)

        self._add_separator(sb)

        # ── Skin dropdown ──
        self._skin_title = QLabel(tr("viewer.skin_label"))
        sb.addWidget(self._skin_title)
        self._skin_combo = QComboBox()
        self._skin_combo.currentIndexChanged.connect(self._on_skin_changed)
        sb.addWidget(self._skin_combo)

        self._add_separator(sb)

        # ── Animation list (scrollable) ──
        self._anim_title = QLabel(tr("viewer.anim_label"))
        sb.addWidget(self._anim_title)
        self._anim_list = QListWidget()
        self._anim_list.currentRowChanged.connect(self._on_anim_changed)
        sb.addWidget(self._anim_list, 1)

        # ══════════════════════════════════════════════════════════════
        # Right panel: canvas + scrubber
        # ══════════════════════════════════════════════════════════════
        right = QVBoxLayout()
        right.setSpacing(3)

        self._canvas = SpineGLCanvas()
        self._canvas.time_updated.connect(self._on_time_updated)
        self._canvas.zoom_changed.connect(self._on_zoom_from_wheel)
        right.addWidget(self._canvas, 1)

        # ── Scrubber row below canvas ──
        scrub_row = QHBoxLayout()
        self._play_btn = QPushButton("\u25b6")
        self._play_btn.setFixedWidth(36)
        self._play_btn.clicked.connect(self._toggle_play)
        scrub_row.addWidget(self._play_btn)
        self._scrubber = QSlider(Qt.Horizontal)
        self._scrubber.setRange(0, 1000)
        self._scrubber.sliderMoved.connect(self._on_scrub)
        scrub_row.addWidget(self._scrubber, 1)
        self._time_label = QLabel("0.00s / 0.00s")
        self._time_label.setFixedWidth(120)
        scrub_row.addWidget(self._time_label)
        right.addLayout(scrub_row)

        outer.addLayout(right, 1)

        language_changed.connect(self._retranslate)

    def _retranslate(self):
        idx = self._tabs.indexOf(self._page)
        if idx >= 0:
            self._tabs.setTabText(idx, tr("viewer.tab"))
        self._btn_full.setText(tr("viewer.open_full"))
        if self._spine_data is None:
            self._stats.setText(tr("viewer.default_stats"))
        self._scale_title.setText(tr("viewer.scale_label"))
        self._btn_scale_reset.setToolTip(tr("viewer.reset_tip"))
        self._zoom_title.setText(tr("viewer.zoom_label"))
        self._btn_zoom_reset.setToolTip(tr("viewer.reset_zoom_tip"))
        self._speed_title.setText(tr("viewer.speed_label"))
        self._btn_speed_reset.setToolTip(tr("viewer.reset_tip"))
        self._skin_title.setText(tr("viewer.skin_label"))
        self._anim_title.setText(tr("viewer.anim_label"))

    # ── Helpers ──

    @staticmethod
    def _add_separator(layout):
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        layout.addWidget(line)

    # ── Callbacks ──

    def _on_scale_changed(self, value):
        scale = value / 100.0
        self._canvas._skeleton_scale = scale
        self._scale_label.setText(f"{scale:.1f}")
        self._canvas.update()

    def _on_zoom_slider_changed(self, value):
        zoom = value / 100.0
        self._canvas._zoom = zoom
        self._zoom_label.setText(f"{zoom:.1f}" if zoom < 10 else f"{zoom:.0f}")
        self._canvas.update()

    def _on_zoom_from_wheel(self, zoom):
        self._zoom_slider.blockSignals(True)
        self._zoom_slider.setValue(int(zoom * 100))
        self._zoom_slider.blockSignals(False)
        self._zoom_label.setText(f"{zoom:.1f}" if zoom < 10 else f"{zoom:.0f}")

    def _on_skin_changed(self, index):
        if self._spine_data is None:
            return
        skin_name = self._skin_combo.currentText()
        if skin_name == tr("viewer.all_skins"):
            self._canvas._active_skin = None
        else:
            self._canvas._active_skin = skin_name
        # Refresh display
        if self._canvas.has_any_track():
            self._canvas._update_frame()
        else:
            self._canvas.show_setup_pose()
        self._canvas.update()

    def _on_anim_changed(self, row):
        if self._spine_data is None:
            return

        if row <= 0:
            self._canvas.stop_animation()
            self._play_btn.setText("\u25b6")
            self._scrubber.setValue(0)
            self._scrubber.setRange(0, 1000)
            self._time_label.setText("0.00s / 0.00s")
            self._canvas.show_setup_pose()
            return

        item = self._anim_list.currentItem()
        if item is None:
            return
        anim_name = item.text()
        animations = self._spine_data.get("animations", {})
        anim_data = animations.get(anim_name)
        if anim_data is None:
            return

        self._canvas._track["name"] = anim_name
        self._canvas.play_animation(self._spine_data, anim_data)
        self._play_btn.setText("\u23f8")
        t = self._canvas._track
        if t["state"]:
            dur_ms = int(t["state"].duration * 1000)
            self._scrubber.setRange(0, max(dur_ms, 1))

    def _toggle_play(self):
        t = self._canvas._track
        if t["state"] is None:
            return
        if t["playing"]:
            self._canvas.pause()
            self._play_btn.setText("\u25b6")
        else:
            self._canvas.resume()
            self._play_btn.setText("\u23f8")

    def _on_speed_changed(self, value):
        speed = value / 10.0
        self._canvas.set_speed(speed)
        self._speed_label.setText(f"{speed:.1f}x")

    def _on_scrub(self, value):
        self._canvas.seek(value / 1000.0)

    def _on_time_updated(self, current_time, duration):
        self._scrubber.blockSignals(True)
        self._scrubber.setValue(int(current_time * 1000))
        self._scrubber.blockSignals(False)
        self._time_label.setText(f"{current_time:.2f}s / {duration:.2f}s")
        if not self._canvas._track["playing"]:
            self._play_btn.setText("\u25b6")

    # ── Open Full Version ──

    def _open_full_version(self):
        json_path = self._get_config("json")
        atlas_path = self._get_config("atlas")
        if not json_path or not os.path.isfile(json_path):
            QMessageBox.critical(None, tr("err.title"), tr("viewer.err.no_json"))
            return
        if not atlas_path or not os.path.isfile(atlas_path):
            QMessageBox.critical(None, tr("err.title"), tr("viewer.err.no_atlas"))
            return

        # Write JSON path into viewer preferences so it auto-loads
        prefs_path = os.path.expanduser("~/.prefs/spine-skeletonviewer")
        try:
            import re as _re
            if os.path.isfile(prefs_path):
                text = open(prefs_path, "r", encoding="utf-8").read()
                # Replace existing lastFile entry
                if '<entry key="lastFile">' in text:
                    text = _re.sub(
                        r'<entry key="lastFile">[^<]*</entry>',
                        f'<entry key="lastFile">{json_path}</entry>',
                        text,
                    )
                else:
                    # Insert before </properties>
                    text = text.replace(
                        "</properties>",
                        f'<entry key="lastFile">{json_path}</entry>\n</properties>',
                    )
                open(prefs_path, "w", encoding="utf-8").write(text)
            else:
                os.makedirs(os.path.dirname(prefs_path), exist_ok=True)
                with open(prefs_path, "w", encoding="utf-8") as f:
                    f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
                    f.write('<!DOCTYPE properties SYSTEM "http://java.sun.com/dtd/properties.dtd">\n')
                    f.write("<properties>\n")
                    f.write(f'<entry key="lastFile">{json_path}</entry>\n')
                    f.write("</properties>\n")
        except Exception:
            pass  # Non-critical — viewer will just open without a file

        viewer_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "GTSpineViewer_3653", "bin")
        if platform.system() == "Windows":
            script = os.path.join(viewer_dir, "GTSpineViewer_3653.bat")
        else:
            script = os.path.join(viewer_dir, "GTSpineViewer_3653")
        if not os.path.isfile(script):
            QMessageBox.critical(None, tr("err.title"), tr("viewer.err.viewer_not_found", path=script))
            return
        try:
            subprocess.Popen([script, json_path], cwd=viewer_dir)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("viewer.err.launch_failed", error=e))

    # ── Load ──

    def _load(self):
        json_path = self._get_config("json")
        atlas_path = self._get_config("atlas")

        if not json_path or not os.path.isfile(json_path):
            QMessageBox.critical(None, tr("err.title"), tr("err.no_json"))
            return
        if not atlas_path or not os.path.isfile(atlas_path):
            QMessageBox.critical(None, tr("err.title"), tr("err.no_atlas"))
            return

        try:
            spine_data = load_spine_json(json_path)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("viewer.err.load_json", error=e))
            return

        try:
            textures = load_atlas_textures(atlas_path)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("viewer.err.load_atlas", error=e))
            return

        self._spine_data = spine_data
        self._textures = textures
        self._canvas.set_base_data(spine_data, textures)

        # Populate animation list
        animations = spine_data.get("animations", {})
        self._anim_list.blockSignals(True)
        self._anim_list.setCurrentRow(-1)
        self._anim_list.clear()
        self._anim_list.addItem(tr("viewer.setup_pose"))
        for name in sorted(animations.keys()):
            self._anim_list.addItem(name)
        self._anim_list.blockSignals(False)

        # Populate skin dropdown
        skins = normalize_skins(spine_data.get("skins", {}))
        self._skin_combo.blockSignals(True)
        self._skin_combo.setCurrentIndex(-1)
        self._skin_combo.clear()
        self._skin_combo.addItem(tr("viewer.all_skins"))
        for name in sorted(skins.keys()):
            self._skin_combo.addItem(name)
        self._skin_combo.setCurrentIndex(0)
        self._skin_combo.blockSignals(False)

        # Show setup pose first
        self._canvas.show_setup_pose()

        bones = spine_data.get("bones", [])
        slots = spine_data.get("slots", [])
        n_skins = len(skins)
        n_anims = len(animations)
        self._stats.setText(
            tr("viewer.stats", bones=len(bones), slots=len(slots),
               skins=n_skins, textures=len(textures), anims=n_anims)
        )

        # Reset sliders
        self._scale_slider.setValue(100)
        self._zoom_slider.setValue(100)

        # Auto-play first animation if available
        if n_anims > 0:
            self._anim_list.setCurrentRow(1)
