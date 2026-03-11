"""
Polygon Simplify tab — reduce vertex count on complex clipping masks
using Ramer-Douglas-Peucker algorithm, with live Canvas preview.
"""

import math
import os
import shutil

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QTabWidget, QMessageBox, QSplitter, QScrollArea, QCheckBox,
    QSlider,
)
from PySide6.QtGui import QPainter, QPen, QColor, QBrush, QFont
from PySide6.QtCore import Qt, QPointF

from .i18n import tr, language_changed
from .spine_json import load_spine_json, save_spine_json, normalize_skins, iter_clipping_attachments
from .style import CANVAS_BG, SUBTEXT


# ==========================================================================
# Ramer-Douglas-Peucker polygon simplification
# ==========================================================================

def _perpendicular_distance(px, py, lx1, ly1, lx2, ly2):
    dx = lx2 - lx1
    dy = ly2 - ly1
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return math.hypot(px - lx1, py - ly1)
    t = max(0, min(1, ((px - lx1) * dx + (py - ly1) * dy) / length_sq))
    proj_x = lx1 + t * dx
    proj_y = ly1 + t * dy
    return math.hypot(px - proj_x, py - proj_y)


def rdp_simplify(points: list[tuple[float, float]], epsilon: float) -> list[tuple[float, float]]:
    if len(points) <= 2:
        return list(points)
    max_dist = 0
    max_idx = 0
    start = points[0]
    end = points[-1]
    for i in range(1, len(points) - 1):
        d = _perpendicular_distance(points[i][0], points[i][1],
                                     start[0], start[1], end[0], end[1])
        if d > max_dist:
            max_dist = d
            max_idx = i
    if max_dist > epsilon:
        left = rdp_simplify(points[: max_idx + 1], epsilon)
        right = rdp_simplify(points[max_idx:], epsilon)
        return left[:-1] + right
    else:
        return [start, end]


def rdp_simplify_polygon(points: list[tuple[float, float]], epsilon: float) -> list[tuple[float, float]]:
    if len(points) <= 3:
        return list(points)
    closed = list(points) + [points[0]]
    simplified = rdp_simplify(closed, epsilon)
    if len(simplified) > 1 and simplified[0] == simplified[-1]:
        simplified = simplified[:-1]
    if len(simplified) < 3:
        return list(points)
    return simplified


def points_to_flat(points: list[tuple[float, float]]) -> list[float]:
    flat = []
    for x, y in points:
        flat.append(round(x, 2))
        flat.append(round(y, 2))
    return flat


# ==========================================================================
# Canvas widget for polygon comparison preview
# ==========================================================================

class _PolygonComparePreview(QWidget):
    def __init__(self):
        super().__init__()
        self._orig_pts = []
        self._simp_pts = []
        self._info_text = ""
        self.setMinimumSize(200, 200)

    def set_data(self, orig_pts, simp_pts, info_text=""):
        self._orig_pts = orig_pts
        self._simp_pts = simp_pts
        self._info_text = info_text
        self.update()

    def clear_preview(self):
        self._orig_pts = []
        self._simp_pts = []
        self._info_text = ""
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(CANVAS_BG))

        if not self._orig_pts:
            p.setPen(QColor(SUBTEXT))
            p.drawText(self.rect(), Qt.AlignCenter, tr("polygon.preview_empty"))
            p.end()
            return

        if self._info_text:
            p.setPen(QColor("#444"))
            p.setFont(QFont("sans-serif", 10))
            p.drawText(5, 15, self._info_text)

        padding = 40
        w, h = self.width(), self.height()
        all_pts = self._orig_pts + self._simp_pts
        xs = [pt[0] for pt in all_pts]
        ys = [pt[1] for pt in all_pts]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        data_w = max(max_x - min_x, 1)
        data_h = max(max_y - min_y, 1)
        scale = min((w - 2 * padding) / data_w, (h - 2 * padding) / data_h)

        def to_canvas(px, py):
            cx = padding + (px - min_x) * scale
            cy = padding + (max_y - py) * scale
            return QPointF(cx, cy)

        if self._orig_pts:
            orig_poly = [to_canvas(x, y) for x, y in self._orig_pts]
            p.setBrush(QColor(255, 204, 204, 128))
            p.setPen(QPen(QColor("#cc4444"), 1.5))
            p.drawPolygon(orig_poly)

        if self._simp_pts:
            simp_poly = [to_canvas(x, y) for x, y in self._simp_pts]
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor("#44aa44"), 2.5))
            p.drawPolygon(simp_poly)

        for px, py in self._orig_pts:
            c = to_canvas(px, py)
            p.setBrush(QColor("#cc4444"))
            p.setPen(Qt.NoPen)
            p.drawEllipse(c, 2, 2)

        for px, py in self._simp_pts:
            c = to_canvas(px, py)
            p.setBrush(QColor("#44aa44"))
            p.setPen(QPen(QColor("#228822"), 1))
            p.drawEllipse(c, 4, 4)

        p.end()


# ==========================================================================
# UI Tab
# ==========================================================================

class PolygonSimplifyTab:
    def __init__(self, tabs: QTabWidget, get_config, on_modified=None):
        self._get_config = get_config
        self._on_modified = on_modified
        self._tabs = tabs
        self._masks = []
        self._selected_idx = -1
        self._checkboxes = []
        self._count_labels = []

        self._page = QWidget()
        tabs.addTab(self._page, tr("polygon.tab"))
        layout = QVBoxLayout(self._page)
        layout.setContentsMargins(5, 5, 5, 5)

        self._info = QLabel(tr("polygon.info"))
        self._info.setWordWrap(True)
        layout.addWidget(self._info)

        # -- button row --
        btn_row = QHBoxLayout()
        self._apply_btn = QPushButton(tr("polygon.apply_btn"))
        self._apply_btn.setEnabled(False)
        self._apply_btn.clicked.connect(self._apply)
        btn_row.addWidget(self._apply_btn)
        self._select_all_btn = QPushButton(tr("polygon.select_all"))
        self._select_all_btn.clicked.connect(self._select_all)
        btn_row.addWidget(self._select_all_btn)
        self._unselect_all_btn = QPushButton(tr("polygon.unselect_all"))
        self._unselect_all_btn.clicked.connect(self._unselect_all)
        btn_row.addWidget(self._unselect_all_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._stats = QLabel(tr("polygon.default_stats"))
        self._stats.setStyleSheet("font-weight: bold; color: #6ec072;")
        layout.addWidget(self._stats)

        # -- splitter: left scroll area | right preview --
        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter, 1)

        # left panel: header row + scroll area with mask rows
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)

        # header row
        header = QWidget()
        hl = QHBoxLayout(header)
        hl.setContentsMargins(4, 2, 4, 2)
        hl.setSpacing(6)
        hl.addSpacing(22)  # space for the checkbox column
        self._hdr_slot = QLabel(tr("polygon.tree.slot"))
        self._hdr_slot.setStyleSheet("font-weight: bold; font-size: 11px;")
        hl.addWidget(self._hdr_slot, 2)
        self._hdr_result = QLabel(tr("polygon.tree.reduction"))
        self._hdr_result.setStyleSheet("font-weight: bold; font-size: 11px;")
        self._hdr_result.setFixedWidth(130)
        hl.addWidget(self._hdr_result)
        left_layout.addWidget(header)

        # scroll area
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll_inner = QWidget()
        self._scroll_inner_layout = QVBoxLayout(self._scroll_inner)
        self._scroll_inner_layout.setContentsMargins(2, 2, 2, 2)
        self._scroll_inner_layout.setSpacing(2)
        self._scroll.setWidget(self._scroll_inner)
        left_layout.addWidget(self._scroll, 1)

        splitter.addWidget(left)

        # right panel: tolerance slider + info + preview + legend
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        # Tolerance slider
        tol_row = QHBoxLayout()
        self._tol_label = QLabel(tr("polygon.tolerance_label"))
        tol_row.addWidget(self._tol_label)
        self._tol_slider = QSlider(Qt.Horizontal)
        self._tol_slider.setRange(5, 500)  # 0.5 to 50.0 (multiply by 10)
        self._tol_slider.setValue(50)  # default 5.0
        self._tol_slider.setTickInterval(50)
        self._tol_slider.setTickPosition(QSlider.TicksBelow)
        self._tol_slider.valueChanged.connect(self._on_slider_change)
        tol_row.addWidget(self._tol_slider, 1)
        self._tol_value_label = QLabel("5.0")
        self._tol_value_label.setFixedWidth(40)
        tol_row.addWidget(self._tol_value_label)
        right_layout.addLayout(tol_row)

        self._info_label = QLabel(tr("polygon.select_hint"))
        right_layout.addWidget(self._info_label)
        right_layout.addSpacing(8)

        self._preview = _PolygonComparePreview()
        right_layout.addWidget(self._preview, 1)

        legend_row = QHBoxLayout()
        red_swatch = QWidget()
        red_swatch.setFixedSize(20, 3)
        red_swatch.setStyleSheet("background-color: #cc4444;")
        legend_row.addWidget(red_swatch)
        self._legend_orig = QLabel(tr("polygon.legend.original"))
        legend_row.addWidget(self._legend_orig)
        legend_row.addSpacing(15)
        green_swatch = QWidget()
        green_swatch.setFixedSize(20, 3)
        green_swatch.setStyleSheet("background-color: #44aa44;")
        legend_row.addWidget(green_swatch)
        self._legend_simp = QLabel(tr("polygon.legend.simplified"))
        legend_row.addWidget(self._legend_simp)
        legend_row.addStretch()
        right_layout.addLayout(legend_row)

        splitter.addWidget(right)
        splitter.setSizes([420, 400])

        language_changed.connect(self._retranslate)

    # ------------------------------------------------------------------
    # Retranslate
    # ------------------------------------------------------------------

    def _retranslate(self):
        idx = self._tabs.indexOf(self._page)
        if idx >= 0:
            self._tabs.setTabText(idx, tr("polygon.tab"))
        self._info.setText(tr("polygon.info"))
        self._select_all_btn.setText(tr("polygon.select_all"))
        self._unselect_all_btn.setText(tr("polygon.unselect_all"))
        self._apply_btn.setText(tr("polygon.apply_btn"))
        self._stats.setText(tr("polygon.default_stats"))
        self._info_label.setText(tr("polygon.select_hint"))
        self._legend_orig.setText(tr("polygon.legend.original"))
        self._legend_simp.setText(tr("polygon.legend.simplified"))
        self._hdr_slot.setText(tr("polygon.tree.slot"))
        self._hdr_result.setText(tr("polygon.tree.reduction"))
        self._tol_label.setText(tr("polygon.tolerance_label"))

    # ------------------------------------------------------------------
    # Select all / Unselect all
    # ------------------------------------------------------------------

    def _select_all(self):
        for cb in self._checkboxes:
            cb.setChecked(True)
        self._update_apply_btn()

    def _unselect_all(self):
        for cb in self._checkboxes:
            cb.setChecked(False)
        self._update_apply_btn()

    def _update_apply_btn(self):
        checked = sum(1 for cb in self._checkboxes if cb.isChecked())
        self._apply_btn.setEnabled(checked > 0)

    # ------------------------------------------------------------------
    # Build the scroll area rows
    # ------------------------------------------------------------------

    def _build_mask_list(self):
        while self._scroll_inner_layout.count():
            item = self._scroll_inner_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._checkboxes = []
        self._count_labels = []

        for idx, m in enumerate(self._masks):
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(4, 2, 4, 2)
            rl.setSpacing(6)

            # checkbox
            cb = QCheckBox()
            cb.setChecked(True)
            cb.stateChanged.connect(lambda _state: self._update_apply_btn())
            rl.addWidget(cb)
            self._checkboxes.append(cb)

            # slot / attachment name label
            name_label = QLabel(f"{m['slot']}/{m['att_name']}")
            name_label.setToolTip(f"{m['slot']}/{m['att_name']}")
            rl.addWidget(name_label, 2)

            # vertex count label: "orig -> simp, -X%"
            orig = m["vertex_count"]
            simp = len(m["simplified_points"])
            pct = round((1 - simp / orig) * 100) if orig > 0 else 0
            count_lbl = QLabel(f"{orig} -> {simp}, -{pct}%")
            count_lbl.setFixedWidth(120)
            rl.addWidget(count_lbl)
            self._count_labels.append(count_lbl)

            # clicking row selects for preview
            row.mousePressEvent = lambda e, i=idx: self._on_row_click(i)

            self._scroll_inner_layout.addWidget(row)

        self._scroll_inner_layout.addStretch()
        self._update_apply_btn()

    # ------------------------------------------------------------------
    # Row interactions
    # ------------------------------------------------------------------

    def _on_row_click(self, idx):
        if 0 <= idx < len(self._masks):
            self._selected_idx = idx
            # Update slider to this mask's tolerance
            m = self._masks[idx]
            self._tol_slider.blockSignals(True)
            self._tol_slider.setValue(int(m["tolerance"] * 10))
            self._tol_slider.blockSignals(False)
            self._tol_value_label.setText(f"{m['tolerance']:.1f}")
            self._draw_preview()

    def _on_slider_change(self, value):
        real_value = value / 10.0
        self._tol_value_label.setText(f"{real_value:.1f}")
        if self._selected_idx < 0 or self._selected_idx >= len(self._masks):
            return
        m = self._masks[self._selected_idx]
        m["tolerance"] = real_value
        m["simplified_points"] = rdp_simplify_polygon(m["orig_points"], real_value)
        # Update count label
        orig = m["vertex_count"]
        simp = len(m["simplified_points"])
        pct = round((1 - simp / orig) * 100) if orig > 0 else 0
        if self._selected_idx < len(self._count_labels):
            self._count_labels[self._selected_idx].setText(f"{orig} -> {simp}, -{pct}%")
        self._update_stats()
        self._draw_preview()

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def _update_stats(self):
        total = len(self._masks)
        total_orig = sum(m["vertex_count"] for m in self._masks)
        total_simp = sum(len(m["simplified_points"]) for m in self._masks)
        self._stats.setText(
            tr("polygon.stats", total=total, orig=total_orig,
               simp=total_simp, reduced=total_orig - total_simp)
        )

    # ------------------------------------------------------------------
    # Analysis (called from app.py on JSON selection)
    # ------------------------------------------------------------------

    def _analyze(self):
        json_path = self._get_config("json")
        if not json_path or not os.path.isfile(json_path):
            QMessageBox.critical(None, tr("err.title"), tr("err.no_json"))
            return
        try:
            spine_data = load_spine_json(json_path)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("err.parse_json", error=e))
            return

        skins = normalize_skins(spine_data.get("skins", {}))
        self._masks = []
        default_epsilon = 5.0

        for skin_name, slot_name, att_name, att_data in iter_clipping_attachments(skins):
            vc = att_data.get("vertexCount", 0)
            verts = att_data.get("vertices", [])
            points = list(zip(verts[::2], verts[1::2]))
            if vc <= 4:
                continue
            simplified = rdp_simplify_polygon(points, default_epsilon)
            self._masks.append({
                "skin": skin_name, "slot": slot_name, "att_name": att_name,
                "orig_points": points, "simplified_points": simplified,
                "vertex_count": vc, "tolerance": default_epsilon,
            })

        self._build_mask_list()
        self._update_stats()
        self._selected_idx = -1
        self._preview.clear_preview()

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------

    def _draw_preview(self):
        if self._selected_idx < 0 or self._selected_idx >= len(self._masks):
            return
        mask = self._masks[self._selected_idx]
        self._info_label.setText(
            f"{mask['slot']}/{mask['att_name']}  —  "
            f"{mask['vertex_count']} vertices -> {len(mask['simplified_points'])} vertices"
        )
        self._preview.set_data(
            mask["orig_points"], mask["simplified_points"],
            f"{mask['slot']}/{mask['att_name']}",
        )

    # ------------------------------------------------------------------
    # Apply (checked masks only)
    # ------------------------------------------------------------------

    def _apply(self):
        if not self._masks:
            return
        to_apply = [self._masks[i] for i, cb in enumerate(self._checkboxes) if cb.isChecked()]
        if not to_apply:
            return
        json_path = self._get_config("json")
        count = len(to_apply)
        total_orig = sum(m["vertex_count"] for m in to_apply)
        total_simp = sum(len(m["simplified_points"]) for m in to_apply)

        if QMessageBox.question(None, tr("confirm.title"),
            tr("polygon.confirm", count=count, orig=total_orig, simp=total_simp,
               reduced=total_orig - total_simp, backup=json_path + ".backup")) != QMessageBox.Yes:
            return

        try:
            shutil.copy2(json_path, json_path + ".backup")
            spine_data = load_spine_json(json_path)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("err.load_backup", error=e))
            return

        skins = normalize_skins(spine_data.get("skins", {}))
        applied = 0
        for m in to_apply:
            try:
                att_data = skins[m["skin"]][m["slot"]][m["att_name"]]
                new_pts = m["simplified_points"]
                att_data["vertices"] = points_to_flat(new_pts)
                att_data["vertexCount"] = len(new_pts)
                applied += 1
            except KeyError:
                pass

        try:
            save_spine_json(json_path, spine_data)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("err.save_json", error=e))
            return

        QMessageBox.information(None, tr("done.title"),
            tr("polygon.done", applied=applied, count=count,
               orig=total_orig, simp=total_simp, backup=json_path + ".backup"))
        self._masks = []
        if self._on_modified:
            self._on_modified()
        else:
            self._analyze()
