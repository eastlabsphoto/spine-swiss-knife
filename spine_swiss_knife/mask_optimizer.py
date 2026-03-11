"""Rectangle Masks tab — snap almost-rectangle clipping masks to clean rectangles, with preview."""

import os
import shutil

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QLineEdit,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QHeaderView, QMessageBox,
    QSplitter, QScrollArea, QCheckBox, QFrame,
)
from PySide6.QtGui import QPainter, QPen, QColor, QBrush, QFont
from PySide6.QtCore import Qt, QPointF

from .i18n import tr, language_changed
from .spine_json import load_spine_json, save_spine_json, normalize_skins, iter_clipping_attachments
from .style import CANVAS_BG, SUBTEXT


# ==========================================================================
# Analysis logic (unchanged)
# ==========================================================================

def analyze_clipping(att_data: dict, tolerance: float = 5.0) -> dict:
    vc = att_data.get("vertexCount", 0)
    verts = att_data.get("vertices", [])
    points = list(zip(verts[::2], verts[1::2]))
    info = {"vertex_count": vc, "vertices": verts, "points": points, "is_rect_candidate": False}

    if vc != 4 or len(verts) != 8:
        return info

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    snapped_xs = [x_min if abs(x - x_min) < abs(x - x_max) else x_max for x in xs]
    snapped_ys = [y_min if abs(y - y_min) < abs(y - y_max) else y_max for y in ys]
    max_dev = max(
        max(abs(x - sx) for x, sx in zip(xs, snapped_xs)),
        max(abs(y - sy) for y, sy in zip(ys, snapped_ys)),
    )
    if max_dev <= tolerance and len(set(snapped_xs)) == 2 and len(set(snapped_ys)) == 2:
        snapped_verts = []
        for sx, sy in zip(snapped_xs, snapped_ys):
            snapped_verts.append(round(sx))
            snapped_verts.append(round(sy))
        info["is_rect_candidate"] = True
        info["max_deviation"] = round(max_dev, 2)
        info["snapped_vertices"] = snapped_verts
        info["snapped_points"] = list(zip(snapped_verts[::2], snapped_verts[1::2]))
    return info


# ==========================================================================
# Canvas widget for polygon preview
# ==========================================================================

class _PolygonPreview(QWidget):
    def __init__(self):
        super().__init__()
        self._orig_pts = []
        self._snap_pts = []
        self._mode = "empty"
        self._info_text = ""
        self.setMinimumSize(200, 200)

    def set_rect_comparison(self, orig_pts, snap_pts, info_text=""):
        self._orig_pts = orig_pts
        self._snap_pts = snap_pts
        self._mode = "rect"
        self._info_text = info_text
        self.update()

    def set_polygon(self, points, info_text=""):
        self._orig_pts = points
        self._snap_pts = []
        self._mode = "polygon"
        self._info_text = info_text
        self.update()

    def clear_preview(self):
        self._mode = "empty"
        self._info_text = ""
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(CANVAS_BG))

        if self._mode == "empty":
            p.setPen(QColor(SUBTEXT))
            p.drawText(self.rect(), Qt.AlignCenter, tr("mask.preview_empty"))
            p.end()
            return

        if self._info_text:
            p.setPen(QColor("#444"))
            p.setFont(QFont("sans-serif", 10))
            p.drawText(5, 15, self._info_text)

        padding = 40
        w, h = self.width(), self.height()
        all_pts = self._orig_pts + self._snap_pts
        if not all_pts:
            p.end()
            return

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

        if self._mode == "rect":
            if self._snap_pts:
                snap_poly = [to_canvas(x, y) for x, y in self._snap_pts]
                p.setBrush(QColor("#ccffcc"))
                p.setPen(QPen(QColor("#44aa44"), 2.5))
                p.drawPolygon(snap_poly)

            if self._orig_pts:
                orig_poly = [to_canvas(x, y) for x, y in self._orig_pts]
                p.setBrush(Qt.NoBrush)
                pen = QPen(QColor("#cc4444"), 1.5, Qt.DashLine)
                p.setPen(pen)
                p.drawPolygon(orig_poly)

            if self._orig_pts and self._snap_pts:
                pen = QPen(QColor("#ff8800"), 1, Qt.DashLine)
                p.setPen(pen)
                for (ox, oy), (sx, sy) in zip(self._orig_pts, self._snap_pts):
                    p.drawLine(to_canvas(ox, oy), to_canvas(sx, sy))

            for px, py in self._orig_pts:
                c = to_canvas(px, py)
                p.setBrush(QColor("#cc4444"))
                p.setPen(Qt.NoPen)
                p.drawEllipse(c, 4, 4)
            for px, py in self._snap_pts:
                c = to_canvas(px, py)
                p.setBrush(QColor("#44aa44"))
                p.setPen(QPen(QColor("#228822"), 1))
                p.drawEllipse(c, 5, 5)

        elif self._mode == "polygon":
            poly = [to_canvas(x, y) for x, y in self._orig_pts]
            p.setBrush(QColor("#ffcccc"))
            p.setPen(QPen(QColor("#cc4444"), 2))
            p.drawPolygon(poly)
            for px, py in self._orig_pts:
                c = to_canvas(px, py)
                p.setBrush(QColor("#cc4444"))
                p.setPen(Qt.NoPen)
                p.drawEllipse(c, 3, 3)

        p.end()


# ==========================================================================
# UI Tab
# ==========================================================================

class MaskOptimizerTab:
    def __init__(self, tabs: QTabWidget, get_config, on_modified=None):
        self._get_config = get_config
        self._on_modified = on_modified
        self._tabs = tabs
        self._fixable = []
        self._skipped = []
        self._checkboxes = []

        self._page = QWidget()
        tabs.addTab(self._page, tr("mask.tab"))
        layout = QVBoxLayout(self._page)
        layout.setContentsMargins(5, 5, 5, 5)

        self._info = QLabel(tr("mask.info"))
        self._info.setWordWrap(True)
        layout.addWidget(self._info)

        btn_row = QHBoxLayout()
        self._tol_label = QLabel(tr("mask.tolerance_label"))
        btn_row.addWidget(self._tol_label)
        self._tol_edit = QLineEdit("10")
        self._tol_edit.setFixedWidth(50)
        self._tol_edit.editingFinished.connect(self._on_tolerance_changed)
        btn_row.addWidget(self._tol_edit)
        self._fix_btn = QPushButton(tr("mask.fix_btn"))
        self._fix_btn.setEnabled(False)
        self._fix_btn.clicked.connect(self._fix)
        btn_row.addWidget(self._fix_btn)
        self._check_btn = QPushButton(tr("mask.check_all"))
        self._check_btn.clicked.connect(self._check_all)
        btn_row.addWidget(self._check_btn)
        self._uncheck_btn = QPushButton(tr("mask.uncheck_all"))
        self._uncheck_btn.clicked.connect(self._uncheck_all)
        btn_row.addWidget(self._uncheck_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._stats = QLabel(tr("mask.default_stats"))
        self._stats.setStyleSheet("font-weight: bold; color: #6ec072;")
        layout.addWidget(self._stats)

        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter, 1)

        self._left_tabs = QTabWidget()
        splitter.addWidget(self._left_tabs)

        fix_page = QWidget()
        self._left_tabs.addTab(fix_page, tr("mask.tab_fixable"))
        fix_layout = QVBoxLayout(fix_page)
        fix_layout.setContentsMargins(0, 0, 0, 0)
        self._fix_scroll = QScrollArea()
        self._fix_scroll.setWidgetResizable(True)
        self._fix_inner = QWidget()
        self._fix_inner_layout = QVBoxLayout(self._fix_inner)
        self._fix_inner_layout.setContentsMargins(2, 2, 2, 2)
        self._fix_inner_layout.setSpacing(2)
        self._fix_scroll.setWidget(self._fix_inner)
        fix_layout.addWidget(self._fix_scroll)

        skip_page = QWidget()
        self._left_tabs.addTab(skip_page, tr("mask.tab_skip"))
        skip_layout = QVBoxLayout(skip_page)
        skip_layout.setContentsMargins(0, 0, 0, 0)
        self._skip_tree = QTreeWidget()
        self._skip_tree.setHeaderLabels([
            tr("mask.tree.slot"), tr("mask.tree.attachment"),
            tr("mask.tree.vertices"), tr("mask.tree.reason"),
        ])
        self._skip_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self._skip_tree.currentItemChanged.connect(self._on_skip_select)
        skip_layout.addWidget(self._skip_tree)

        self._preview = _PolygonPreview()
        splitter.addWidget(self._preview)
        splitter.setSizes([400, 400])

        language_changed.connect(self._retranslate)

    def _retranslate(self):
        idx = self._tabs.indexOf(self._page)
        if idx >= 0:
            self._tabs.setTabText(idx, tr("mask.tab"))
        self._info.setText(tr("mask.info"))
        self._tol_label.setText(tr("mask.tolerance_label"))
        self._check_btn.setText(tr("mask.check_all"))
        self._uncheck_btn.setText(tr("mask.uncheck_all"))
        self._stats.setText(tr("mask.default_stats"))
        self._left_tabs.setTabText(0, tr("mask.tab_fixable"))
        self._left_tabs.setTabText(1, tr("mask.tab_skip"))
        self._skip_tree.setHeaderLabels([
            tr("mask.tree.slot"), tr("mask.tree.attachment"),
            tr("mask.tree.vertices"), tr("mask.tree.reason"),
        ])
        self._update_fix_btn()

    def _on_tolerance_changed(self):
        if self._fixable or self._skipped:
            self._analyze()

    def _check_all(self):
        for cb in self._checkboxes:
            cb.setChecked(True)
        self._update_fix_btn()

    def _uncheck_all(self):
        for cb in self._checkboxes:
            cb.setChecked(False)
        self._update_fix_btn()

    def _update_fix_btn(self):
        checked = sum(1 for cb in self._checkboxes if cb.isChecked())
        self._fix_btn.setText(tr("mask.fix_btn_count", count=checked))
        self._fix_btn.setEnabled(checked > 0)

    def _build_fix_list(self):
        while self._fix_inner_layout.count():
            item = self._fix_inner_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._checkboxes = []

        for idx, item in enumerate(self._fixable):
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(2, 1, 2, 1)
            cb = QCheckBox()
            cb.setChecked(True)
            cb.stateChanged.connect(lambda: self._update_fix_btn())
            rl.addWidget(cb)
            rl.addWidget(QLabel(item["slot"]), 1)
            rl.addWidget(QLabel(item["att_name"]), 1)
            rl.addWidget(QLabel(f'{item["info"]["max_deviation"]:.1f}px'))
            self._checkboxes.append(cb)

            row.mousePressEvent = lambda e, i=idx: self._on_fix_click(i)
            self._fix_inner_layout.addWidget(row)

        self._fix_inner_layout.addStretch()
        self._update_fix_btn()

    def _on_fix_click(self, idx):
        if 0 <= idx < len(self._fixable):
            item = self._fixable[idx]
            info = item["info"]
            self._preview.set_rect_comparison(
                info["points"], info["snapped_points"],
                f'{item["slot"]}/{item["att_name"]}  —  deviation: {info["max_deviation"]:.1f}px',
            )

    def _on_skip_select(self, current, previous):
        if current is None:
            return
        idx = self._skip_tree.indexOfTopLevelItem(current)
        if 0 <= idx < len(self._skipped):
            item = self._skipped[idx]
            info = item["info"]
            self._preview.set_polygon(
                info["points"],
                f'{item["slot"]}/{item["att_name"]}  —  {info["vertex_count"]} vertices',
            )

    def _analyze(self):
        json_path = self._get_config("json")
        if not json_path or not os.path.isfile(json_path):
            QMessageBox.critical(None, tr("err.title"), tr("err.no_json"))
            return
        try:
            tolerance = float(self._tol_edit.text())
        except ValueError:
            QMessageBox.critical(None, tr("err.title"), tr("mask.err.tolerance"))
            return
        try:
            spine_data = load_spine_json(json_path)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("err.parse_json", error=e))
            return

        skins = normalize_skins(spine_data.get("skins", {}))
        self._fixable = []
        self._skipped = []
        self._skip_tree.clear()

        for skin_name, slot_name, att_name, att_data in iter_clipping_attachments(skins):
            info = analyze_clipping(att_data, tolerance)
            if info["is_rect_candidate"]:
                self._fixable.append({"skin": skin_name, "slot": slot_name, "att_name": att_name, "info": info})
            else:
                vc = info["vertex_count"]
                reason = tr("mask.reason.not4", count=vc) if vc != 4 else tr("mask.reason.too_large", tol=tolerance)
                self._skipped.append({"skin": skin_name, "slot": slot_name, "att_name": att_name, "info": info, "reason": reason})
                QTreeWidgetItem(self._skip_tree, [slot_name, att_name, str(vc), reason])

        self._build_fix_list()
        total = len(self._fixable) + len(self._skipped)
        self._stats.setText(
            tr("mask.stats", total=total, fixable=len(self._fixable), skipped=len(self._skipped))
        )
        self._preview.clear_preview()

    def _fix(self):
        if not self._fixable:
            return
        to_fix = [self._fixable[i] for i, cb in enumerate(self._checkboxes) if cb.isChecked()]
        if not to_fix:
            QMessageBox.information(None, tr("info.title"), tr("mask.info_no_selection"))
            return

        json_path = self._get_config("json")
        if QMessageBox.question(None, tr("confirm.title"),
            tr("mask.confirm", count=len(to_fix), backup=json_path + ".backup")) != QMessageBox.Yes:
            return

        try:
            shutil.copy2(json_path, json_path + ".backup")
            spine_data = load_spine_json(json_path)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("err.load_backup", error=e))
            return

        skins = normalize_skins(spine_data.get("skins", {}))
        fixed = 0
        for item in to_fix:
            try:
                att_data = skins[item["skin"]][item["slot"]][item["att_name"]]
                att_data["vertices"] = item["info"]["snapped_vertices"]
                fixed += 1
            except KeyError:
                pass

        try:
            save_spine_json(json_path, spine_data)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("err.save_json", error=e))
            return

        QMessageBox.information(None, tr("done.title"),
            tr("mask.done", fixed=fixed, total=len(to_fix), backup=json_path + ".backup"))
        self._fixable = []
        self._skipped = []
        if self._on_modified:
            self._on_modified()
        else:
            self._analyze()
