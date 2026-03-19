"""OpenGL canvas widget for Spine skeleton rendering and playback."""

from PySide6.QtCore import Qt, QPointF, QTimer, QElapsedTimer, Signal
from PySide6.QtGui import (
    QPixmap, QPainter, QPainterPath, QTransform, QColor,
    QWheelEvent, QMouseEvent, QPolygonF,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from .spine_viewer_animation import (
    BoneTransform, AnimationState,
    solve_world_transforms, solve_ik_constraints,
    build_draw_list, _evaluate_draw_order, _affine_from_triangles,
)
from .style import CANVAS_BG, SUBTEXT


class SpineGLCanvas(QOpenGLWidget):
    """GPU-accelerated canvas that draws Spine skeleton with animation."""

    time_updated = Signal(float, float)  # current_time, duration
    zoom_changed = Signal(float)  # emitted when zoom changes (scroll wheel)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._zoom = 1.0
        self._pan = QPointF(0, 0)
        self._last_mouse = QPointF()
        self._panning = False

        self._spine_data: dict | None = None
        self._world_transforms: dict[str, BoneTransform] = {}
        self._textures: dict[str, QPixmap] = {}
        self._draw_list: list[dict] = []

        # Single-track animation state
        self._track = {
            "state": None, "name": "", "time": 0.0,
            "playing": False, "speed": 1.0, "loop": True,
        }
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self._elapsed = QElapsedTimer()

        self._skeleton_scale = 1.0
        self._active_skin: str | None = None

        self.setMinimumSize(200, 200)

    def set_base_data(self, spine_data: dict, textures: dict[str, QPixmap]):
        """Store spine data and textures; reset camera."""
        self.stop_animation()
        self._spine_data = spine_data
        self._textures = textures
        self._zoom = 1.0
        self._pan = QPointF(0, 0)
        self._skeleton_scale = 1.0
        self._active_skin = None

    def show_setup_pose(self):
        """Stop animation and display the setup pose."""
        self.stop_animation()
        if self._spine_data:
            bones = self._spine_data.get("bones", [])
            wt = solve_world_transforms(bones)
            ik_constraints = self._spine_data.get("ik", [])
            if ik_constraints:
                solve_ik_constraints(bones, wt, ik_constraints)
            dl = build_draw_list(self._spine_data, wt, self._textures,
                                 active_skin=self._active_skin)
            self._world_transforms = wt
            self._draw_list = dl
        self.update()

    def play_animation(self, spine_data: dict, anim_data: dict, **_kw):
        """Start playing an animation."""
        self._spine_data = spine_data
        t = self._track
        t["state"] = AnimationState(spine_data, anim_data)
        t["time"] = 0.0
        t["playing"] = True
        if not self._timer.isActive():
            self._elapsed.start()
            self._timer.start()
        self._update_frame()
        self.update()

    def stop_animation(self):
        """Stop playback."""
        t = self._track
        t["playing"] = False
        t["state"] = None
        t["time"] = 0.0
        t["name"] = ""
        self._timer.stop()

    def stop_track(self, _track: int = 0):
        """Stop playback (single-track)."""
        self.stop_animation()

    def pause(self):
        self._track["playing"] = False
        self._timer.stop()

    def resume(self):
        t = self._track
        if t["state"] is None:
            return
        t["playing"] = True
        if not self._timer.isActive():
            self._elapsed.start()
            self._timer.start()

    def seek(self, time: float):
        t = self._track
        if t["state"]:
            t["time"] = max(0.0, min(time, t["state"].duration))
            self._update_frame()
            self.update()
            self.time_updated.emit(t["time"], t["state"].duration)

    def set_speed(self, speed: float):
        self._track["speed"] = speed

    def set_loop(self, loop: bool):
        self._track["loop"] = loop

    def has_any_track(self):
        """True if there is an active AnimationState."""
        return self._track["state"] is not None

    def clear_data(self):
        self.stop_animation()
        self._spine_data = None
        self._world_transforms = {}
        self._textures = {}
        self._draw_list = []
        self.update()

    # ── Animation tick ──

    def _tick(self):
        dt = self._elapsed.restart() / 1000.0
        t = self._track
        if not t["playing"] or t["state"] is None:
            self._timer.stop()
            return
        t["time"] += dt * t["speed"]
        duration = t["state"].duration
        if t["time"] > duration:
            if t["loop"]:
                t["time"] %= duration
            else:
                t["time"] = duration
                t["playing"] = False
        if not t["playing"]:
            self._timer.stop()
        self._update_frame()
        self.update()
        if t["state"]:
            self.time_updated.emit(t["time"], t["state"].duration)

    def _update_frame(self):
        if not self._spine_data:
            return
        t = self._track
        if t["state"] is None:
            return

        bone_ov, slot_st, deform_st = t["state"].evaluate(t["time"])
        base_slots = self._spine_data.get("slots", [])
        slot_order = _evaluate_draw_order(
            base_slots, t["state"]._draw_order_keys, t["time"])

        bones = self._spine_data.get("bones", [])
        wt = solve_world_transforms(
            bones,
            bone_overrides=bone_ov if bone_ov else None)
        ik_constraints = self._spine_data.get("ik", [])
        if ik_constraints:
            solve_ik_constraints(bones, wt, ik_constraints)
        dl = build_draw_list(
            self._spine_data, wt, self._textures,
            slot_states=slot_st if slot_st else None,
            slot_order=slot_order,
            deform_states=deform_st if deform_st else None,
            active_skin=self._active_skin)
        self._world_transforms = wt
        self._draw_list = dl

    # ── Rendering ──

    def initializeGL(self):
        pass

    def paintGL(self):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        bg = QColor(CANVAS_BG)
        painter.fillRect(self.rect(), bg)

        if not self._draw_list:
            painter.setPen(QColor(SUBTEXT))
            painter.drawText(self.rect(), Qt.AlignCenter, "No skeleton loaded")
            painter.end()
            return

        cx = self.width() / 2.0 + self._pan.x()
        cy = self.height() / 2.0 + self._pan.y()
        effective_zoom = self._zoom * self._skeleton_scale
        flip_sx = 1.0
        flip_sy = 1.0

        clip_end = ""
        active_clip_path = None

        _BLEND_MODES = {
            "additive": QPainter.CompositionMode_Plus,
            "multiply": QPainter.CompositionMode_Multiply,
            "screen": QPainter.CompositionMode_Screen,
        }

        for item in self._draw_list:
            item_type = item.get("type")

            # Clip-end marker: stop clipping (emitted for slots with no attachment)
            if item_type == "clip_end_marker":
                if clip_end:
                    painter.setClipping(False)
                    clip_end = ""
                    active_clip_path = None
                continue

            # Start clipping mask
            if item_type == "clip":
                bt = self._world_transforms.get(item["bone"])
                if bt is None:
                    continue
                verts = item["vertices"]
                points = []
                for i in range(0, len(verts), 2):
                    lx, ly = verts[i], verts[i + 1]
                    wx = bt.a * lx + bt.b * ly + bt.worldX
                    wy = bt.c * lx + bt.d * ly + bt.worldY
                    sx = cx + wx * flip_sx * effective_zoom
                    sy = cy - wy * flip_sy * effective_zoom
                    points.append(QPointF(sx, sy))
                if points:
                    path = QPainterPath()
                    path.addPolygon(QPolygonF(points))
                    active_clip_path = path
                    painter.resetTransform()
                    painter.setClipPath(path)
                clip_end = item.get("clip_end", "")
                continue

            # Render mesh attachment
            if item_type == "mesh":
                bt = self._world_transforms.get(item["bone"])
                if bt is None:
                    continue
                verts = item["vertices"]
                uvs = item["uvs"]
                tris = item["triangles"]
                pixmap = item["pixmap"]
                pw, ph = pixmap.width(), pixmap.height()

                color = item.get("color")
                if color:
                    painter.setOpacity(color[3])

                blend_mode = _BLEND_MODES.get(item.get("blend"))
                if blend_mode is not None:
                    painter.setCompositionMode(blend_mode)

                # Pre-compute screen-space vertex positions
                screen_pts = []
                for i in range(0, len(verts), 2):
                    lx, ly = verts[i], verts[i + 1]
                    wx = bt.a * lx + bt.b * ly + bt.worldX
                    wy = bt.c * lx + bt.d * ly + bt.worldY
                    screen_pts.append((cx + wx * flip_sx * effective_zoom,
                                       cy - wy * flip_sy * effective_zoom))

                # Pre-compute UV pixel coordinates
                uv_pts = [(uvs[i] * pw, uvs[i + 1] * ph) for i in range(0, len(uvs), 2)]

                # Render each triangle
                for t in range(0, len(tris), 3):
                    i0, i1, i2 = tris[t], tris[t + 1], tris[t + 2]
                    src = [uv_pts[i0], uv_pts[i1], uv_pts[i2]]
                    dst = [screen_pts[i0], screen_pts[i1], screen_pts[i2]]

                    xform = _affine_from_triangles(src, dst)
                    if xform is None:
                        continue

                    tri_path = QPainterPath()
                    tri_path.moveTo(*dst[0])
                    tri_path.lineTo(*dst[1])
                    tri_path.lineTo(*dst[2])
                    tri_path.closeSubpath()

                    painter.resetTransform()
                    if clip_end and active_clip_path is not None:
                        combined = active_clip_path.intersected(tri_path)
                        painter.setClipPath(combined)
                    else:
                        painter.setClipPath(tri_path)
                    painter.setTransform(xform)
                    painter.drawPixmap(0, 0, pixmap)

                # Restore clip state after mesh
                painter.setClipping(False)
                if clip_end and active_clip_path is not None:
                    painter.setClipPath(active_clip_path)

                if blend_mode is not None:
                    painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
                if color:
                    painter.setOpacity(1.0)
                continue

            # Render region attachment
            bone_name = item["bone"]
            pixmap = item["pixmap"]
            att = item["att_data"]
            bt = self._world_transforms.get(bone_name)
            if bt is None:
                continue

            ax = att.get("x", 0.0)
            ay = att.get("y", 0.0)
            a_rot = att.get("rotation", 0.0)
            a_sx = att.get("scaleX", 1.0)
            a_sy = att.get("scaleY", 1.0)

            pw = pixmap.width()
            ph = pixmap.height()

            # JSON width/height = intended display size (always at full scale).
            # Pixmap may be smaller when atlas was exported at < 1.0 scale.
            att_w = att.get("width", pw)
            att_h = att.get("height", ph)

            bone_t = QTransform(
                bt.a, -bt.c,
                -bt.b, bt.d,
                bt.worldX, -bt.worldY,
            )

            att_t = QTransform()
            att_t.translate(ax, -ay)
            att_t.rotate(-a_rot)
            att_t.scale(a_sx * att_w / pw, a_sy * att_h / ph)
            att_t.translate(-pw / 2.0, -ph / 2.0)

            cam_t = QTransform()
            cam_t.translate(cx, cy)
            cam_t.scale(effective_zoom * flip_sx, effective_zoom * flip_sy)

            t = att_t * bone_t * cam_t

            painter.setTransform(t)

            color = item.get("color")
            if color:
                painter.setOpacity(color[3])

            blend_mode = _BLEND_MODES.get(item.get("blend"))
            if blend_mode is not None:
                painter.setCompositionMode(blend_mode)

            painter.drawPixmap(0, 0, pixmap)

            if blend_mode is not None:
                painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            if color:
                painter.setOpacity(1.0)

        painter.resetTransform()
        painter.setClipping(False)
        painter.setOpacity(1.0)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)

        painter.end()

    # ── Camera controls ──

    def wheelEvent(self, event: QWheelEvent):
        delta = event.angleDelta().y()
        factor = 1.1 if delta > 0 else 1 / 1.1
        self._zoom = max(0.05, min(self._zoom * factor, 20.0))
        self.zoom_changed.emit(self._zoom)
        self.update()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MiddleButton or (
            event.button() == Qt.LeftButton and event.modifiers() & Qt.ControlModifier
        ):
            self._panning = True
            self._last_mouse = event.position()
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._panning:
            delta = event.position() - self._last_mouse
            self._pan += delta
            self._last_mouse = event.position()
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self._panning:
            self._panning = False
            self.unsetCursor()

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        self._zoom = 1.0
        self._pan = QPointF(0, 0)
        self.update()
