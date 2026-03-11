"""Main application window — sidebar navigation, config panel, dark theme."""

import os
import sys
from pathlib import Path

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QTabWidget, QFileDialog,
    QStackedWidget, QSizePolicy, QComboBox, QFrame, QMessageBox,
    QProgressDialog,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon

from . import __version__
from .i18n import tr, set_language, language_changed
from .project_analyzer import ProjectAnalyzerTab
from .optimizer import OptimizerTab
from .mask_optimizer import MaskOptimizerTab
from .polygon_simplify import PolygonSimplifyTab
from .keyframe_optimizer import KeyframeOptimizerTab
from .dead_bones import DeadBonesTab
from .hidden_attachments import HiddenAttachmentsTab
from .unused_finder import UnusedFinderTab
from .splitter import SplitterTab
from .spine_downgrader import SpineDowngraderTab
from .texture_unpacker import TextureUnpackerTab
from .spine_viewer import SpineViewerTab


_SIDEBAR_KEYS = [
    ("app.sidebar.analyzer", "app.tip.analyzer"),
    ("app.sidebar.downscaler", "app.tip.downscaler"),
    ("app.sidebar.rect_masks", "app.tip.rect_masks"),
    ("app.sidebar.polygon", "app.tip.polygon"),
    ("app.sidebar.keyframes", "app.tip.keyframes"),
    ("app.sidebar.dead_bones", "app.tip.dead_bones"),
    ("app.sidebar.hidden", "app.tip.hidden"),
    ("app.sidebar.unused", "app.tip.unused"),
    ("app.sidebar.splitter", "app.tip.splitter"),
    ("app.sidebar.downgrader", "app.tip.downgrader"),
    ("app.sidebar.unpacker", "app.tip.unpacker"),
    ("app.sidebar.viewer", "app.tip.viewer"),
]


class SpineSwissKnifeApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"GreentubeSK Spine Swiss Knife v{__version__}")
        self.resize(1200, 800)
        self.setMinimumSize(900, 600)
        icon_path = Path(__file__).parent / "resources" / "icon.png"
        if icon_path.is_file():
            self.setWindowIcon(QIcon(str(icon_path)))
        self._sidebar_btns = []
        self._build_ui()
        language_changed.connect(self._retranslate)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # ── Sidebar ──
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(150)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(0)

        self._title_label = QLabel(tr("app.title"))
        self._title_label.setObjectName("sidebarTitle")
        self._title_label.setAlignment(Qt.AlignLeft)
        sidebar_layout.addWidget(self._title_label)

        for idx, (name_key, tip_key) in enumerate(_SIDEBAR_KEYS):
            btn = QPushButton(tr(name_key))
            btn.setToolTip(tr(tip_key))
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, i=idx: self._switch_tool(i))
            sidebar_layout.addWidget(btn)
            self._sidebar_btns.append(btn)

        sidebar_layout.addStretch()

        # Language switcher (bottom of sidebar)
        self._lang_combo = QComboBox()
        self._lang_combo.setFixedWidth(130)
        self._lang_combo.addItem("English", "en")
        self._lang_combo.addItem("Slovenčina", "sk")
        self._lang_combo.setCurrentIndex(0)
        self._lang_combo.currentIndexChanged.connect(self._on_language_changed)
        sidebar_layout.addWidget(self._lang_combo)
        root_layout.addWidget(sidebar)

        # ── Main area ──
        main_area = QWidget()
        main_layout = QVBoxLayout(main_area)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(8)

        # Update banner (hidden by default)
        self._update_banner = QFrame()
        self._update_banner.setObjectName("updateBanner")
        banner_layout = QHBoxLayout(self._update_banner)
        banner_layout.setContentsMargins(8, 4, 8, 4)
        self._update_label = QLabel("")
        self._update_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        banner_layout.addWidget(self._update_label)
        self._update_btn = QPushButton(tr("update.btn"))
        self._update_btn.setCursor(Qt.PointingHandCursor)
        self._update_btn.clicked.connect(self._do_update)
        banner_layout.addWidget(self._update_btn)
        self._update_banner.hide()
        main_layout.addWidget(self._update_banner)

        # Store update info for later use
        self._pending_update_url = ""
        self._pending_update_changelog = ""

        # Config panel
        config_panel = QWidget()
        config_panel.setObjectName("configPanel")
        config_grid = QGridLayout(config_panel)
        config_grid.setContentsMargins(10, 8, 10, 8)
        config_grid.setVerticalSpacing(6)
        config_grid.setHorizontalSpacing(8)

        self._json_label = QLabel(tr("app.json_label"))
        config_grid.addWidget(self._json_label, 0, 0)
        self._json_edit = QLineEdit()
        self._json_edit.setPlaceholderText(tr("app.json_placeholder"))
        config_grid.addWidget(self._json_edit, 0, 1)
        self._btn_json = QPushButton(tr("app.browse"))
        self._btn_json.clicked.connect(self._browse_json)
        config_grid.addWidget(self._btn_json, 0, 2)

        self._atlas_label = QLabel(tr("app.atlas_label"))
        config_grid.addWidget(self._atlas_label, 1, 0)
        self._atlas_edit = QLineEdit()
        self._atlas_edit.setPlaceholderText(tr("app.atlas_placeholder"))
        config_grid.addWidget(self._atlas_edit, 1, 1)
        self._btn_atlas = QPushButton(tr("app.browse"))
        self._btn_atlas.clicked.connect(self._browse_atlas)
        config_grid.addWidget(self._btn_atlas, 1, 2)

        self._images_label = QLabel(tr("app.images_label"))
        config_grid.addWidget(self._images_label, 2, 0)
        self._images_edit = QLineEdit()
        self._images_edit.setPlaceholderText(tr("app.images_placeholder"))
        config_grid.addWidget(self._images_edit, 2, 1)
        self._btn_images = QPushButton(tr("app.browse"))
        self._btn_images.clicked.connect(self._browse_images)
        config_grid.addWidget(self._btn_images, 2, 2)

        config_grid.setColumnStretch(1, 1)
        main_layout.addWidget(config_panel)

        # Tool pages (hidden QTabWidget — sidebar controls it)
        self._tabs = QTabWidget()
        self._tabs.tabBar().setVisible(False)
        main_layout.addWidget(self._tabs, 1)

        root_layout.addWidget(main_area, 1)

        # ── Create all tool tabs ──
        notify = self._run_all_tabs
        self._tab_instances = [
            ProjectAnalyzerTab(self._tabs, self._get_config),
            OptimizerTab(self._tabs, self._get_config, on_modified=notify),
            MaskOptimizerTab(self._tabs, self._get_config, on_modified=notify),
            PolygonSimplifyTab(self._tabs, self._get_config, on_modified=notify),
            KeyframeOptimizerTab(self._tabs, self._get_config, on_modified=notify),
            DeadBonesTab(self._tabs, self._get_config, on_modified=notify),
            HiddenAttachmentsTab(self._tabs, self._get_config, on_modified=notify),
            UnusedFinderTab(self._tabs, self._get_config),
            SplitterTab(self._tabs, self._get_config),
            SpineDowngraderTab(self._tabs, self._get_config),
            TextureUnpackerTab(self._tabs, self._get_config),
            SpineViewerTab(self._tabs, self._get_config),
        ]

        # Select first tool
        self._switch_tool(0)

    def _on_language_changed(self, index):
        lang = self._lang_combo.itemData(index)
        if lang:
            set_language(lang)

    def _retranslate(self):
        self.setWindowTitle(f"GreentubeSK Spine Swiss Knife v{__version__}")
        self._update_btn.setText(tr("update.btn"))
        self._title_label.setText(tr("app.title"))
        self._json_label.setText(tr("app.json_label"))
        self._json_edit.setPlaceholderText(tr("app.json_placeholder"))
        self._atlas_label.setText(tr("app.atlas_label"))
        self._atlas_edit.setPlaceholderText(tr("app.atlas_placeholder"))
        self._images_label.setText(tr("app.images_label"))
        self._images_edit.setPlaceholderText(tr("app.images_placeholder"))
        self._btn_json.setText(tr("app.browse"))
        self._btn_atlas.setText(tr("app.browse"))
        self._btn_images.setText(tr("app.browse"))
        for idx, (name_key, tip_key) in enumerate(_SIDEBAR_KEYS):
            self._sidebar_btns[idx].setText(tr(name_key))
            self._sidebar_btns[idx].setToolTip(tr(tip_key))
        for tab in self._tab_instances:
            if hasattr(tab, "_retranslate"):
                tab._retranslate()

    def _switch_tool(self, index: int):
        self._tabs.setCurrentIndex(index)
        for i, btn in enumerate(self._sidebar_btns):
            btn.setProperty("active", i == index)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _get_config(self, key: str) -> str:
        if key == "json":
            return self._json_edit.text().strip()
        elif key == "atlas":
            return self._atlas_edit.text().strip()
        elif key == "images":
            return self._images_edit.text().strip()
        return ""

    # --- Browse dialogs ---

    def _browse_json(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("app.dialog.select_json"), "", tr("app.filter.json"),
        )
        if path:
            self._json_edit.setText(path)
            self._auto_fill_from_json(path)
            self._run_all_tabs()

    def _auto_fill_from_json(self, json_path: str):
        p = Path(json_path)
        stem = p.stem
        parent = p.parent

        if not self._atlas_edit.text():
            for pattern in (f"{stem}.atlas.txt", f"{stem}.atlas"):
                candidate = parent / pattern
                if candidate.is_file():
                    self._atlas_edit.setText(str(candidate))
                    break
            else:
                for ext in ("*.atlas.txt", "*.atlas"):
                    found = list(parent.glob(ext))
                    if found:
                        self._atlas_edit.setText(str(found[0]))
                        break

        if not self._images_edit.text():
            img_dir = parent / "images"
            if img_dir.is_dir():
                self._images_edit.setText(str(img_dir))

    def _browse_atlas(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("app.dialog.select_atlas"), "", tr("app.filter.atlas"),
        )
        if path:
            self._atlas_edit.setText(path)

    def _browse_images(self):
        path = QFileDialog.getExistingDirectory(self, tr("app.dialog.select_images"))
        if path:
            self._images_edit.setText(path)

    # --- Run all tabs after JSON selection ---

    def _run_all_tabs(self):
        """Trigger analyze/load/detect on all tabs so everything is ready."""
        tabs = self._tab_instances
        has_atlas = bool(self._atlas_edit.text().strip())
        has_images = bool(self._images_edit.text().strip())

        # JSON-only tabs: Analyzer, Rect Masks, Polygon, Keyframes, Dead Bones, Hidden Att.
        for tab in [tabs[0], tabs[2], tabs[3], tabs[4], tabs[5], tabs[6]]:
            try:
                tab._analyze()
            except Exception:
                pass
        # Unused — needs atlas + images
        if has_atlas and has_images:
            try:
                tabs[7]._analyze()
            except Exception:
                pass
        # Splitter — Load Animations
        try:
            tabs[8]._load()
        except Exception:
            pass
        # Downgrader — Detect Version
        try:
            tabs[9]._detect()
        except Exception:
            pass
        # Viewer — needs atlas
        if has_atlas:
            try:
                tabs[11]._load()
            except Exception:
                pass

    # --- Auto-update ---

    def show_update_available(self, version: str, zipball_url: str, changelog: str):
        self._pending_update_url = zipball_url
        self._pending_update_changelog = changelog
        self._update_label.setText(tr("update.available", version=version))
        self._update_banner.show()

    def _do_update(self):
        from .updater import perform_update, restart_app

        msg = tr("update.confirm",
                 version=self._update_label.text(),
                 changelog=self._pending_update_changelog or "—")
        reply = QMessageBox.question(self, tr("confirm.title"), msg,
                                     QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        progress = QProgressDialog(tr("update.downloading"), None, 0, 0, self)
        progress.setWindowTitle(tr("update.btn"))
        progress.setWindowModality(Qt.WindowModal)
        progress.setCancelButton(None)
        progress.show()

        try:
            from PySide6.QtWidgets import QApplication
            QApplication.processEvents()
            progress.setLabelText(tr("update.installing"))
            QApplication.processEvents()
            perform_update(self._pending_update_url)
            progress.close()
            QMessageBox.information(self, tr("done.title"), tr("update.restart"))
            restart_app()
        except Exception as e:
            progress.close()
            QMessageBox.warning(self, tr("err.title"),
                                tr("update.failed", error=str(e)))
