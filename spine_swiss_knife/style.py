"""Dark theme stylesheet and color constants for GreentubeSK Spine Swiss Knife."""

import os as _os

_ARROW_PATH = _os.path.join(_os.path.dirname(__file__), "resources", "arrow_down.png").replace("\\", "/")

# Dark palette with green accent (GreentubeSK brand)
BASE = "#1a1a2a"
MANTLE = "#151524"
SURFACE0 = "#2a2a3c"
SURFACE1 = "#3a3a50"
SURFACE2 = "#4e4e66"
OVERLAY0 = "#62627a"
TEXT = "#d0d8e8"
SUBTEXT = "#9ea8be"
ACCENT = "#6ec072"         # bright green — text accents, active states
ACCENT_HOVER = "#85d189"   # lighter green — hover
ACCENT_DARK = "#00600e"    # brand dark green — buttons, checkboxes, sliders
ACCENT_DARKER = "#003f06"  # brand darkest green — pressed states
GREEN = "#6ec072"
RED = "#f38ba8"
YELLOW = "#f9e2af"
PEACH = "#fab387"
MAUVE = "#cba6f7"
TEAL = "#94e2d5"

CANVAS_BG = "#222236"

STYLESHEET = f"""
/* ── Global ── */
QMainWindow, QWidget {{
    background-color: {BASE};
    color: {TEXT};
    font-family: ".AppleSystemUIFont", "Helvetica Neue", "Segoe UI", sans-serif;
    font-size: 13px;
}}

/* ── Sidebar ── */
#sidebar {{
    background-color: {MANTLE};
    border-right: 1px solid {SURFACE0};
}}
#sidebar QPushButton {{
    background: transparent;
    color: {SUBTEXT};
    border: none;
    border-radius: 6px;
    padding: 10px 14px;
    text-align: left;
    font-size: 12px;
    margin: 1px 6px;
}}
#sidebar QPushButton:hover {{
    background-color: {SURFACE0};
    color: {TEXT};
}}
#sidebar QPushButton[active="true"] {{
    background-color: {SURFACE1};
    color: {ACCENT};
    font-weight: bold;
}}

#sidebarTitle {{
    color: {ACCENT};
    font-size: 14px;
    font-weight: bold;
    padding: 12px 14px 8px 14px;
}}

/* ── Config panel ── */
#configPanel {{
    background-color: {SURFACE0};
    border: 1px solid {SURFACE1};
    border-radius: 8px;
    padding: 8px;
}}
#configPanel QLabel {{
    color: {SUBTEXT};
    font-size: 12px;
}}

/* ── Labels ── */
QLabel {{
    background-color: transparent;
    color: {TEXT};
}}
QLabel[role="heading"] {{
    font-weight: bold;
    font-size: 13px;
    color: {TEXT};
}}
QLabel[role="stats"] {{
    font-weight: bold;
    color: {ACCENT};
    font-size: 13px;
    padding: 4px 0;
}}
QLabel[role="info"] {{
    color: {SUBTEXT};
    font-size: 12px;
}}

/* ── Inputs ── */
QLineEdit {{
    background-color: {SURFACE0};
    color: {TEXT};
    border: 1px solid {SURFACE1};
    border-radius: 6px;
    padding: 5px 8px;
    selection-background-color: {ACCENT_DARK};
    selection-color: {TEXT};
}}
QLineEdit:focus {{
    border-color: {ACCENT};
}}

QComboBox {{
    background-color: {SURFACE0};
    color: {TEXT};
    border: 1px solid {SURFACE1};
    border-radius: 6px;
    padding: 5px 8px;
    min-width: 80px;
}}
QComboBox::drop-down {{
    border: none;
    width: 24px;
    subcontrol-position: right center;
}}
QComboBox::down-arrow {{
    image: url({_ARROW_PATH});
    width: 12px;
    height: 12px;
}}
QComboBox QAbstractItemView {{
    background-color: {SURFACE0};
    color: {TEXT};
    border: 1px solid {SURFACE1};
    selection-background-color: {ACCENT_DARK};
    selection-color: {TEXT};
}}

/* ── Buttons ── */
QPushButton {{
    background-color: {SURFACE1};
    color: {TEXT};
    border: none;
    border-radius: 6px;
    padding: 6px 16px;
    font-weight: 500;
}}
QPushButton:hover {{
    background-color: {SURFACE2};
}}
QPushButton:pressed {{
    background-color: {OVERLAY0};
}}
QPushButton:disabled {{
    background-color: {SURFACE0};
    color: {OVERLAY0};
}}
QPushButton[role="primary"] {{
    background-color: {ACCENT_DARK};
    color: {TEXT};
    font-weight: bold;
}}
QPushButton[role="primary"]:hover {{
    background-color: #007a12;
}}
QPushButton[role="primary"]:disabled {{
    background-color: {SURFACE1};
    color: {OVERLAY0};
}}
QPushButton[role="danger"] {{
    background-color: {RED};
    color: {BASE};
}}

/* ── Tree Widget ── */
QTreeWidget {{
    background-color: {SURFACE0};
    color: {TEXT};
    border: 1px solid {SURFACE1};
    border-radius: 6px;
    outline: none;
    selection-background-color: {SURFACE1};
}}
QTreeWidget::item {{
    padding: 4px 6px;
    border-radius: 3px;
}}
QTreeWidget::item:selected {{
    background-color: {SURFACE1};
    color: {ACCENT};
}}
QTreeWidget::item:hover {{
    background-color: {SURFACE1};
}}

QHeaderView::section {{
    background-color: {SURFACE0};
    color: {SUBTEXT};
    border: none;
    border-bottom: 1px solid {SURFACE1};
    padding: 6px 8px;
    font-weight: bold;
    font-size: 11px;
    text-transform: uppercase;
}}

/* ── List Widget ── */
QListWidget {{
    background-color: {SURFACE0};
    color: {TEXT};
    border: 1px solid {SURFACE1};
    border-radius: 6px;
    outline: none;
}}
QListWidget::item {{
    padding: 4px 6px;
    border-radius: 3px;
}}
QListWidget::item:selected {{
    background-color: {SURFACE1};
    color: {ACCENT};
}}
QListWidget::item:hover {{
    background-color: {SURFACE1};
}}

/* ── Text Edit / Log ── */
QTextEdit {{
    background-color: {SURFACE0};
    color: {TEXT};
    border: 1px solid {SURFACE1};
    border-radius: 6px;
    padding: 6px;
    selection-background-color: {ACCENT_DARK};
    selection-color: {TEXT};
}}

/* ── Scroll Area ── */
QScrollArea {{
    background-color: transparent;
    border: 1px solid {SURFACE1};
    border-radius: 6px;
}}
QScrollArea > QWidget > QWidget {{
    background-color: transparent;
}}

/* ── Scrollbar ── */
QScrollBar:vertical {{
    background-color: transparent;
    width: 8px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background-color: {SURFACE2};
    border-radius: 4px;
    min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{
    background-color: {OVERLAY0};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    background-color: transparent;
    height: 8px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background-color: {SURFACE2};
    border-radius: 4px;
    min-width: 20px;
}}
QScrollBar::handle:horizontal:hover {{
    background-color: {OVERLAY0};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}

/* ── Splitter ── */
QSplitter::handle {{
    background-color: {SURFACE1};
    width: 2px;
    margin: 2px;
}}
QSplitter::handle:hover {{
    background-color: {ACCENT};
}}

/* ── Group Box ── */
QGroupBox {{
    background-color: {SURFACE0};
    border: 1px solid {SURFACE1};
    border-radius: 8px;
    margin-top: 8px;
    padding-top: 14px;
    font-weight: bold;
    color: {SUBTEXT};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: {SUBTEXT};
}}

/* ── Checkbox ── */
QCheckBox {{
    color: {TEXT};
    spacing: 6px;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 2px solid {SURFACE2};
    border-radius: 4px;
    background-color: {SURFACE0};
}}
QCheckBox::indicator:checked {{
    background-color: {ACCENT_DARK};
    border-color: {ACCENT};
}}
QCheckBox::indicator:hover {{
    border-color: {ACCENT};
}}

/* ── Slider ── */
QSlider::groove:horizontal {{
    background-color: {SURFACE1};
    height: 4px;
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background-color: {ACCENT_DARK};
    border: 2px solid {ACCENT};
    width: 12px;
    height: 12px;
    margin: -5px 0;
    border-radius: 7px;
}}
QSlider::handle:horizontal:hover {{
    background-color: {ACCENT};
}}

/* ── Tab Widget (sub-tabs inside tools) ── */
QTabWidget::pane {{
    background-color: {SURFACE0};
    border: 1px solid {SURFACE1};
    border-radius: 6px;
    top: -1px;
}}
QTabBar::tab {{
    background-color: transparent;
    color: {SUBTEXT};
    padding: 6px 16px;
    border: none;
    border-bottom: 2px solid transparent;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    color: {ACCENT};
    border-bottom: 2px solid {ACCENT};
}}
QTabBar::tab:hover {{
    color: {TEXT};
}}

/* ── Message Box ── */
QMessageBox {{
    background-color: {BASE};
    color: {TEXT};
}}
QMessageBox QPushButton {{
    min-width: 80px;
    padding: 6px 20px;
}}

/* ── Tooltip ── */
QToolTip {{
    background-color: {SURFACE0};
    color: {TEXT};
    border: 1px solid {SURFACE1};
    border-radius: 4px;
    padding: 4px 8px;
}}

/* ── Update banner ── */
#updateBanner {{
    background-color: {ACCENT_DARK};
    border: 1px solid {ACCENT};
    border-radius: 8px;
    padding: 6px 12px;
}}
#updateBanner QLabel {{
    color: {TEXT};
    font-size: 12px;
    background: transparent;
}}
#updateBanner QPushButton {{
    background-color: {ACCENT};
    color: {BASE};
    font-weight: bold;
    border-radius: 4px;
    padding: 4px 14px;
    font-size: 12px;
}}
#updateBanner QPushButton:hover {{
    background-color: {ACCENT_HOVER};
}}
"""
