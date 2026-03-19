"""Compare dialog — side-by-side, wipe, and overlay viewer for draw order comparison."""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QSlider, QComboBox, QStackedWidget, QDialog, QApplication,
)
from PySide6.QtGui import QPainter, QColor, QPen
from PySide6.QtCore import Qt, Signal, QTimer, QPointF

from .draw_order_core import count_blend_groups
from .spine_viewer_canvas import SpineGLCanvas


# ---------------------------------------------------------------------------
# Composite view for wipe / overlay modes
# ---------------------------------------------------------------------------

class _CompositeView(QWidget):
    """Captures two GL canvases and composites them as wipe or overlay."""

    def __init__(self, left_canvas, right_canvas, parent=None):
        super().__init__(parent)
        self._left = left_canvas
        self._right = right_canvas
        self._mode = "wipe"       # "wipe" or "overlay"
        self._position = 0.5      # 0.0 = all original, 1.0 = all optimized

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)

    def set_mode(self, mode):
        self._mode = mode

    def set_position(self, pos):
        self._position = max(0.0, min(1.0, pos))

    def start(self):
        if not self._timer.isActive():
            self._timer.start(50)  # 20fps — balance quality/perf

    def stop(self):
        self._timer.stop()

    def paintEvent(self, event):
        w, h = self.width(), self.height()
        if w < 2 or h < 2:
            return

        left_pm = self._left.grab()
        right_pm = self._right.grab()

        left_s = left_pm.scaled(w, h, Qt.KeepAspectRatio,
                                Qt.SmoothTransformation)
        right_s = right_pm.scaled(w, h, Qt.KeepAspectRatio,
                                  Qt.SmoothTransformation)

        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#2b2b2b"))

        # Center scaled pixmaps
        ox = (w - left_s.width()) // 2
        oy = (h - left_s.height()) // 2

        if self._mode == "wipe":
            split_x = int(left_s.width() * self._position)
            # Draw left (original) full
            p.drawPixmap(ox, oy, left_s)
            # Draw right (optimized) clipped to right side
            p.setClipRect(ox + split_x, oy,
                          left_s.width() - split_x, left_s.height())
            p.drawPixmap(ox, oy, right_s)
            p.setClipping(False)
            # Split line
            pen = QPen(QColor("#ffffff"), 2)
            p.setPen(pen)
            p.drawLine(ox + split_x, oy,
                       ox + split_x, oy + left_s.height())
            # Labels
            p.setPen(QColor("#e8a838"))
            p.drawText(ox + 8, oy + 18, "Original")
            p.setPen(QColor("#6ec072"))
            p.drawText(ox + split_x + 8, oy + 18, "Optimized")

        elif self._mode == "overlay":
            # Original at full opacity
            p.drawPixmap(ox, oy, left_s)
            # Optimized on top with slider opacity
            p.setOpacity(self._position)
            p.drawPixmap(ox, oy, right_s)
            p.setOpacity(1.0)
            # Labels
            p.setPen(QColor("#e8a838"))
            pct = int((1.0 - self._position) * 100)
            p.drawText(ox + 8, oy + 18, f"Original {pct}%")
            p.setPen(QColor("#6ec072"))
            pct2 = int(self._position * 100)
            p.drawText(ox + 8, oy + 34, f"Optimized {pct2}%")

        p.end()


# ---------------------------------------------------------------------------
# Compare Dialog — side-by-side original vs optimized viewer
# ---------------------------------------------------------------------------

class CompareDialog(QDialog):
    """Side-by-side viewer comparing original vs optimized draw order."""

    accepted_result = Signal(bool)

    def __init__(self, spine_data_original, spine_data_optimized,
                 textures, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Draw Order Comparison")
        self.setModal(True)

        # Size to 80% of screen or 1200x700, whichever is smaller
        screen = QApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            w = min(1200, int(geom.width() * 0.8))
            h = min(700, int(geom.height() * 0.8))
            self.resize(w, h)
        else:
            self.resize(1200, 700)

        self._spine_original = spine_data_original
        self._spine_optimized = spine_data_optimized
        self._textures = textures
        self._syncing_zoom = False

        orig_groups = count_blend_groups(
            spine_data_original.get("slots", []))
        opt_groups = count_blend_groups(
            spine_data_optimized.get("slots", []))

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        # ── Top control bar ──────────────────────────────────────────
        ctrl = QHBoxLayout()
        ctrl.setSpacing(6)

        ctrl.addWidget(QLabel("Animation:"))
        self._anim_combo = QComboBox()
        self._anim_combo.addItem("Setup Pose")
        for name in spine_data_original.get("animations", {}).keys():
            self._anim_combo.addItem(name)
        self._anim_combo.currentIndexChanged.connect(self._on_anim_changed)
        ctrl.addWidget(self._anim_combo)

        ctrl.addWidget(QLabel("View:"))
        self._view_combo = QComboBox()
        self._view_combo.addItems(["Side by Side", "Wipe", "Overlay"])
        self._view_combo.currentIndexChanged.connect(self._on_view_changed)
        ctrl.addWidget(self._view_combo)

        self._play_btn = QPushButton("\u25b6  Play")
        self._play_btn.setFixedWidth(80)
        self._play_btn.clicked.connect(self._toggle_play)
        ctrl.addWidget(self._play_btn)

        ctrl.addWidget(QLabel("Speed:"))
        self._speed_slider = QSlider(Qt.Horizontal)
        self._speed_slider.setRange(1, 30)  # 0.1x to 3.0x
        self._speed_slider.setValue(10)      # 1.0x
        self._speed_slider.setFixedWidth(100)
        self._speed_slider.valueChanged.connect(self._on_speed_changed)
        ctrl.addWidget(self._speed_slider)
        self._speed_label = QLabel("1.0x")
        self._speed_label.setFixedWidth(35)
        ctrl.addWidget(self._speed_label)

        ctrl.addWidget(QLabel("Zoom:"))
        self._zoom_slider = QSlider(Qt.Horizontal)
        self._zoom_slider.setRange(5, 2000)  # 0.05 to 20.0
        self._zoom_slider.setValue(100)       # 1.0
        self._zoom_slider.setFixedWidth(100)
        self._zoom_slider.valueChanged.connect(self._on_zoom_slider_changed)
        ctrl.addWidget(self._zoom_slider)
        self._zoom_label = QLabel("1.0")
        self._zoom_label.setFixedWidth(30)
        ctrl.addWidget(self._zoom_label)

        ctrl.addStretch()
        root.addLayout(ctrl)

        # ── Canvas area ──────────────────────────────────────────────
        canvas_row = QHBoxLayout()
        canvas_row.setSpacing(4)

        # Left side: original
        left_col = QVBoxLayout()
        self._left_label = QLabel(
            f"Original ({orig_groups} groups)")
        self._left_label.setAlignment(Qt.AlignCenter)
        self._left_label.setStyleSheet("font-weight: bold; color: #e8a838;")
        left_col.addWidget(self._left_label)

        self._left_canvas = SpineGLCanvas()
        self._left_canvas.set_base_data(spine_data_original, textures)
        self._left_canvas.show_setup_pose()
        left_col.addWidget(self._left_canvas, 1)
        canvas_row.addLayout(left_col, 1)

        # Right side: optimized
        right_col = QVBoxLayout()
        self._right_label = QLabel(
            f"Optimized ({opt_groups} groups)")
        self._right_label.setAlignment(Qt.AlignCenter)
        self._right_label.setStyleSheet("font-weight: bold; color: #6ec072;")
        right_col.addWidget(self._right_label)

        self._right_canvas = SpineGLCanvas()
        self._right_canvas.set_base_data(spine_data_optimized, textures)
        self._right_canvas.show_setup_pose()
        right_col.addWidget(self._right_canvas, 1)
        canvas_row.addLayout(right_col, 1)

        self._canvas_stack = QStackedWidget()
        side_widget = QWidget()
        side_widget.setLayout(canvas_row)
        self._canvas_stack.addWidget(side_widget)

        # Page 1: composite view (wipe / overlay)
        self._composite = _CompositeView(
            self._left_canvas, self._right_canvas)
        self._canvas_stack.addWidget(self._composite)

        root.addWidget(self._canvas_stack, 1)

        # Blend slider (for wipe / overlay) — hidden by default
        blend_row = QHBoxLayout()
        self._blend_label = QLabel("Wipe:")
        self._blend_label.setFixedWidth(55)
        blend_row.addWidget(self._blend_label)
        self._blend_slider = QSlider(Qt.Horizontal)
        self._blend_slider.setRange(0, 100)
        self._blend_slider.setValue(50)
        self._blend_slider.valueChanged.connect(self._on_blend_changed)
        blend_row.addWidget(self._blend_slider, 1)
        self._blend_value_label = QLabel("50%")
        self._blend_value_label.setFixedWidth(35)
        blend_row.addWidget(self._blend_value_label)
        root.addLayout(blend_row)
        self._blend_label.hide()
        self._blend_slider.hide()
        self._blend_value_label.hide()

        # ── Timeline scrubber ────────────────────────────────────────
        scrub_row = QHBoxLayout()
        self._timeline = QSlider(Qt.Horizontal)
        self._timeline.setRange(0, 1000)
        self._timeline.setValue(0)
        self._timeline.sliderMoved.connect(self._on_scrub)
        scrub_row.addWidget(self._timeline, 1)
        self._time_label = QLabel("0.00s / 0.00s")
        self._time_label.setFixedWidth(120)
        scrub_row.addWidget(self._time_label)
        root.addLayout(scrub_row)

        # ── Bottom buttons ───────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        self._discard_btn = QPushButton("Discard")
        self._discard_btn.clicked.connect(self.reject)
        btn_row.addWidget(self._discard_btn)

        self._accept_btn = QPushButton("Use Optimized")
        self._accept_btn.setProperty("role", "primary")
        self._accept_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._accept_btn)
        root.addLayout(btn_row)

        # ── Signal connections ───────────────────────────────────────
        self._left_canvas.time_updated.connect(self._on_time_update)
        self._left_canvas.zoom_changed.connect(
            lambda z: self._sync_zoom(z, self._right_canvas))
        self._right_canvas.zoom_changed.connect(
            lambda z: self._sync_zoom(z, self._left_canvas))

    # ── Slots ────────────────────────────────────────────────────────

    def _on_anim_changed(self, index):
        if index <= 0:
            # Setup Pose
            self._left_canvas.stop_animation()
            self._right_canvas.stop_animation()
            self._left_canvas.show_setup_pose()
            self._right_canvas.show_setup_pose()
            self._play_btn.setText("\u25b6  Play")
            self._timeline.setValue(0)
            self._timeline.setRange(0, 1000)
            self._time_label.setText("0.00s / 0.00s")
            return

        anim_name = self._anim_combo.currentText()
        anims_orig = self._spine_original.get("animations", {})
        anims_opt = self._spine_optimized.get("animations", {})
        anim_orig = anims_orig.get(anim_name)
        anim_opt = anims_opt.get(anim_name)

        if anim_orig:
            self._left_canvas.play_animation(
                self._spine_original, anim_orig)
        if anim_opt:
            self._right_canvas.play_animation(
                self._spine_optimized, anim_opt)

        self._play_btn.setText("\u23f8  Pause")

        # Set timeline range from left canvas duration
        t = self._left_canvas._track
        if t["state"]:
            dur_ms = int(t["state"].duration * 1000)
            self._timeline.setRange(0, max(dur_ms, 1))

    def _toggle_play(self):
        t = self._left_canvas._track
        if t["state"] is None:
            return
        if t["playing"]:
            self._left_canvas.pause()
            self._right_canvas.pause()
            self._play_btn.setText("\u25b6  Play")
        else:
            self._left_canvas.resume()
            self._right_canvas.resume()
            self._play_btn.setText("\u23f8  Pause")

    def _on_speed_changed(self, value):
        speed = value / 10.0
        self._left_canvas.set_speed(speed)
        self._right_canvas.set_speed(speed)
        self._speed_label.setText(f"{speed:.1f}x")

    def _on_zoom_slider_changed(self, value):
        if self._syncing_zoom:
            return
        zoom = value / 100.0
        self._syncing_zoom = True
        self._left_canvas._zoom = zoom
        self._left_canvas.update()
        self._right_canvas._zoom = zoom
        self._right_canvas.update()
        self._zoom_label.setText(
            f"{zoom:.1f}" if zoom < 10 else f"{zoom:.0f}")
        self._syncing_zoom = False

    def _sync_zoom(self, zoom, target):
        if self._syncing_zoom:
            return
        self._syncing_zoom = True
        target._zoom = zoom
        target.update()
        self._zoom_slider.blockSignals(True)
        self._zoom_slider.setValue(int(zoom * 100))
        self._zoom_slider.blockSignals(False)
        self._zoom_label.setText(
            f"{zoom:.1f}" if zoom < 10 else f"{zoom:.0f}")
        self._syncing_zoom = False

    def _on_time_update(self, current_time, duration):
        self._timeline.blockSignals(True)
        self._timeline.setMaximum(int(duration * 1000))
        self._timeline.setValue(int(current_time * 1000))
        self._timeline.blockSignals(False)
        self._time_label.setText(
            f"{current_time:.2f}s / {duration:.2f}s")
        # Sync right canvas to same time
        self._right_canvas.seek(current_time)

    def _on_scrub(self, value):
        t = value / 1000.0
        self._left_canvas.seek(t)
        self._right_canvas.seek(t)

    def _on_view_changed(self, index):
        if index == 0:  # Side by Side
            self._composite.stop()
            self._canvas_stack.setCurrentIndex(0)
            self._blend_label.hide()
            self._blend_slider.hide()
            self._blend_value_label.hide()
        elif index == 1:  # Wipe
            self._composite.set_mode("wipe")
            self._blend_label.setText("Wipe:")
            self._canvas_stack.setCurrentIndex(1)
            self._composite.start()
            self._blend_label.show()
            self._blend_slider.show()
            self._blend_value_label.show()
        elif index == 2:  # Overlay
            self._composite.set_mode("overlay")
            self._blend_label.setText("Opacity:")
            self._canvas_stack.setCurrentIndex(1)
            self._composite.start()
            self._blend_label.show()
            self._blend_slider.show()
            self._blend_value_label.show()

    def _on_blend_changed(self, value):
        self._composite.set_position(value / 100.0)
        self._blend_value_label.setText(f"{value}%")
