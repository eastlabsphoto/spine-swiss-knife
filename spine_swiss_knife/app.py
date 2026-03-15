"""Main application window — sidebar navigation, config panel, dark theme."""

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QTabWidget, QFileDialog,
    QStackedWidget, QSizePolicy, QComboBox, QFrame, QMessageBox,
    QProgressDialog, QApplication, QDialog, QDialogButtonBox,
    QFormLayout,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon

from . import __version__
from .i18n import tr, set_language, language_changed
from .settings import settings
from .spine_cli import export_spine_project, import_to_spine_project
from .spine_downgrader import SpineDowngraderTab, convert_to_destination
from .welcome_page import WelcomePage
from .project_analyzer import ProjectAnalyzerTab
from .optimizer import OptimizerTab
from .mask_optimizer import MaskOptimizerTab
from .polygon_simplify import PolygonSimplifyTab
from .keyframe_optimizer import KeyframeOptimizerTab
from .dead_bones import DeadBonesTab
from .hidden_attachments import HiddenAttachmentsTab
from .blend_switcher import BlendSwitcherTab
from .unused_finder import UnusedFinderTab
from .splitter import SplitterTab
from .spine_downgrader import SpineDowngraderTab
from .texture_unpacker import TextureUnpackerTab
from .spine_viewer import SpineViewerTab
from .static_exporter import StaticExporterTab
from .debug_log import DebugConsole


_SIDEBAR_KEYS = [
    ("app.sidebar.analyzer", "app.tip.analyzer"),
    ("app.sidebar.downscaler", "app.tip.downscaler"),
    ("app.sidebar.rect_masks", "app.tip.rect_masks"),
    ("app.sidebar.polygon", "app.tip.polygon"),
    ("app.sidebar.keyframes", "app.tip.keyframes"),
    ("app.sidebar.dead_bones", "app.tip.dead_bones"),
    ("app.sidebar.hidden", "app.tip.hidden"),
    ("app.sidebar.blend", "app.tip.blend"),
    ("app.sidebar.unused", "app.tip.unused"),
    ("app.sidebar.splitter", "app.tip.splitter"),
    ("app.sidebar.downgrader", "app.tip.downgrader"),
    ("app.sidebar.unpacker", "app.tip.unpacker"),
    ("app.sidebar.viewer", "app.tip.viewer"),
    ("app.sidebar.static_export", "app.tip.static_export"),
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

        self._mode = None            # "spine" / "json" / None
        self._spine_project_path = ""
        self._export_dir = ""
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
        self._sidebar = QWidget()
        sidebar = self._sidebar
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(150)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(0)

        self._title_label = QLabel(tr("app.title"))
        self._title_label.setObjectName("sidebarTitle")
        self._title_label.setAlignment(Qt.AlignLeft)
        sidebar_layout.addWidget(self._title_label)

        self._home_btn = QPushButton(tr("app.home"))
        self._home_btn.setObjectName("homeButton")
        self._home_btn.setCursor(Qt.PointingHandCursor)
        self._home_btn.clicked.connect(self._switch_mode)
        sidebar_layout.addWidget(self._home_btn)

        for idx, (name_key, tip_key) in enumerate(_SIDEBAR_KEYS):
            btn = QPushButton(tr(name_key))
            btn.setToolTip(tr(tip_key))
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, i=idx: self._switch_tool(i))
            sidebar_layout.addWidget(btn)
            self._sidebar_btns.append(btn)

        sidebar_layout.addStretch()

        # Switch Mode button
        self._switch_mode_btn = QPushButton(tr("app.switch_mode"))
        self._switch_mode_btn.setCursor(Qt.PointingHandCursor)
        self._switch_mode_btn.clicked.connect(self._switch_mode)
        sidebar_layout.addWidget(self._switch_mode_btn)

        # Language switcher (bottom of sidebar)
        self._lang_combo = QComboBox()
        self._lang_combo.setFixedWidth(130)
        self._lang_combo.addItem("English", "en")
        self._lang_combo.addItem("Slovenčina", "sk")
        self._lang_combo.setCurrentIndex(0)
        self._lang_combo.currentIndexChanged.connect(self._on_language_changed)
        sidebar_layout.addWidget(self._lang_combo)
        root_layout.addWidget(sidebar)

        # ── Main stacked area ──
        self._stack = QStackedWidget()

        # Page 0: Welcome page
        self._welcome_page = WelcomePage()
        self._welcome_page.spine_mode_selected.connect(
            lambda path, pack, version: self._enter_spine_mode(path, pack, version)
        )
        self._welcome_page.json_mode_selected.connect(self._enter_json_mode)
        self._stack.addWidget(self._welcome_page)

        # Page 1: Tool area (config panel + tabs)
        self._tool_area = QWidget()
        main_layout = QVBoxLayout(self._tool_area)
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
        self._config_grid = config_grid = QGridLayout(config_panel)
        config_grid.setContentsMargins(10, 8, 10, 8)
        config_grid.setVerticalSpacing(6)
        config_grid.setHorizontalSpacing(8)

        # Row 0: .spine project (hidden in JSON mode)
        self._spine_project_label = QLabel(tr("app.spine_project_label"))
        config_grid.addWidget(self._spine_project_label, 0, 0)
        self._spine_project_edit = QLineEdit()
        self._spine_project_edit.setReadOnly(True)
        config_grid.addWidget(self._spine_project_edit, 0, 1)

        # Spine mode buttons in row 0, column 2
        self._spine_btn_container = QWidget()
        self._spine_btn_container.setStyleSheet("background: transparent;")
        spine_btn_layout = QHBoxLayout(self._spine_btn_container)
        spine_btn_layout.setContentsMargins(0, 0, 0, 0)
        spine_btn_layout.setSpacing(4)
        self._btn_reimport = QPushButton(tr("spine_mode.reimport_btn"))
        self._btn_reimport.setProperty("role", "primary")
        self._btn_reimport.setCursor(Qt.PointingHandCursor)
        self._btn_reimport.clicked.connect(self._import_back_to_spine)
        spine_btn_layout.addWidget(self._btn_reimport)
        self._btn_reexport = QPushButton(tr("spine_mode.reexport_btn"))
        self._btn_reexport.setCursor(Qt.PointingHandCursor)
        self._btn_reexport.clicked.connect(self._reexport_spine_project)
        spine_btn_layout.addWidget(self._btn_reexport)
        config_grid.addWidget(self._spine_btn_container, 0, 2)

        # Row 1: JSON
        self._json_label = QLabel(tr("app.json_label"))
        config_grid.addWidget(self._json_label, 1, 0)
        self._json_edit = QLineEdit()
        self._json_edit.setPlaceholderText(tr("app.json_placeholder"))
        config_grid.addWidget(self._json_edit, 1, 1)
        self._btn_json = QPushButton(tr("app.browse"))
        self._btn_json.clicked.connect(self._browse_json)
        config_grid.addWidget(self._btn_json, 1, 2)

        # Row 2: Atlas
        self._atlas_label = QLabel(tr("app.atlas_label"))
        config_grid.addWidget(self._atlas_label, 2, 0)
        self._atlas_edit = QLineEdit()
        self._atlas_edit.setPlaceholderText(tr("app.atlas_placeholder"))
        config_grid.addWidget(self._atlas_edit, 2, 1)
        self._btn_atlas = QPushButton(tr("app.browse"))
        self._btn_atlas.clicked.connect(self._browse_atlas)
        config_grid.addWidget(self._btn_atlas, 2, 2)

        # Row 3: Images
        self._images_label = QLabel(tr("app.images_label"))
        config_grid.addWidget(self._images_label, 3, 0)
        self._images_edit = QLineEdit()
        self._images_edit.setPlaceholderText(tr("app.images_placeholder"))
        config_grid.addWidget(self._images_edit, 3, 1)
        self._btn_images = QPushButton(tr("app.browse"))
        self._btn_images.clicked.connect(self._browse_images)
        config_grid.addWidget(self._btn_images, 3, 2)

        config_grid.setColumnStretch(1, 1)
        main_layout.addWidget(config_panel)

        # Tool pages (hidden QTabWidget — sidebar controls it)
        tab_container = QWidget()
        tab_container_layout = QVBoxLayout(tab_container)
        tab_container_layout.setContentsMargins(0, 0, 0, 0)
        self._tabs = QTabWidget()
        self._tabs.tabBar().setVisible(False)
        tab_container_layout.addWidget(self._tabs, 1)

        # Loading overlay
        self._loading_overlay = QLabel(tr("app.loading"))
        self._loading_overlay.setAlignment(Qt.AlignCenter)
        self._loading_overlay.setStyleSheet(
            "background-color: rgba(26, 26, 42, 200);"
            "color: #6ec072;"
            "font-size: 16px;"
            "font-weight: bold;"
        )
        self._loading_overlay.setParent(tab_container)
        self._loading_overlay.hide()

        main_layout.addWidget(tab_container, 1)
        self._tab_container = tab_container

        # Debug console (bottom)
        self._debug_console = DebugConsole()
        main_layout.addWidget(self._debug_console)

        self._stack.addWidget(self._tool_area)
        root_layout.addWidget(self._stack, 1)

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
            BlendSwitcherTab(self._tabs, self._get_config, on_modified=notify),
            UnusedFinderTab(self._tabs, self._get_config),
            SplitterTab(self._tabs, self._get_config),
            SpineDowngraderTab(self._tabs, self._get_config),
            TextureUnpackerTab(self._tabs, self._get_config),
            SpineViewerTab(self._tabs, self._get_config),
            StaticExporterTab(self._tabs, self._get_config),
        ]

        # Select first tool
        self._switch_tool(0)

        # Start on welcome page
        self._show_welcome()

    # ── Mode management ──

    def _show_welcome(self):
        """Show welcome page, hide spine row and sidebar."""
        self._mode = None
        self._stack.setCurrentIndex(0)
        self._sidebar.hide()
        self._spine_project_label.hide()
        self._spine_project_edit.hide()
        self._spine_btn_container.hide()

    def _make_cli_progress(self) -> QProgressDialog:
        """Create a modal progress dialog for Spine CLI operations."""
        progress = QProgressDialog("", None, 0, 0, self)
        progress.setWindowTitle(tr("app.title").replace("\n", " "))
        progress.setWindowModality(Qt.WindowModal)
        progress.setCancelButton(None)
        progress.setMinimumWidth(500)
        progress.setMaximumWidth(self.width() // 2)
        progress.show()
        QApplication.processEvents()
        return progress

    def _cli_output_updater(self, progress: QProgressDialog):
        """Return a callback that updates progress dialog label with CLI output.

        Called with ``None`` as a keepalive tick (every ~250 ms) when the
        subprocess produces no output, so the Qt event loop stays responsive.
        """
        def _on_output(line: Optional[str]):
            if line is not None:
                progress.setLabelText(line)
            QApplication.processEvents()
        return _on_output

    def _enter_spine_mode(self, spine_path: str, pack: bool = True,
                          version: str = ""):
        """Export .spine project via CLI, fill config paths, switch to tool area."""
        exe = settings.spine_executable()
        if not exe:
            QMessageBox.warning(self, tr("err.title"), tr("welcome.spine_not_set"))
            return

        self._spine_pack_atlas = pack
        stem = Path(spine_path).stem
        export_dir = str(Path(spine_path).parent / f"_ssk_export_{stem}")

        progress = self._make_cli_progress()
        result = export_spine_project(
            exe, spine_path, export_dir,
            on_output=self._cli_output_updater(progress),
            pack=pack,
        )
        progress.close()

        if not result.success:
            error = result.stderr or result.stdout or "Unknown error"
            QMessageBox.warning(self, tr("err.title"),
                                tr("spine_mode.export_failed", error=error))
            return

        self._mode = "spine"
        self._spine_project_path = spine_path
        self._export_dir = export_dir
        settings.set_last_mode("spine")
        settings.set_last_spine_file(spine_path)

        # Fill config fields
        self._spine_project_edit.setText(spine_path)
        self._json_edit.setText(result.output_json)
        self._atlas_edit.setText(result.output_atlas)

        # Auto-detect images folder from export dir
        img_dir = Path(export_dir) / "images"
        if img_dir.is_dir():
            self._images_edit.setText(str(img_dir))
        else:
            # Try parent of .spine file
            img_dir = Path(spine_path).parent / "images"
            self._images_edit.setText(str(img_dir) if img_dir.is_dir() else "")

        # Show .spine row, make fields read-only
        self._spine_project_label.show()
        self._spine_project_edit.show()
        self._spine_btn_container.show()
        self._json_edit.setReadOnly(True)
        self._atlas_edit.setReadOnly(True)
        self._images_edit.setReadOnly(True)
        self._btn_json.setEnabled(False)
        self._btn_atlas.setEnabled(False)
        self._btn_images.setEnabled(False)

        # Switch to tool area and run all tabs
        self._sidebar.show()
        self._stack.setCurrentIndex(1)
        self._run_all_tabs()

    def _enter_json_mode(self):
        """Switch to tool area in JSON mode — existing behavior."""
        self._mode = "json"
        self._spine_project_path = ""
        self._export_dir = ""
        settings.set_last_mode("json")

        # Hide .spine row, make fields editable
        self._spine_project_label.hide()
        self._spine_project_edit.hide()
        self._spine_btn_container.hide()
        self._json_edit.setReadOnly(False)
        self._atlas_edit.setReadOnly(False)
        self._images_edit.setReadOnly(False)
        self._btn_json.setEnabled(True)
        self._btn_atlas.setEnabled(True)
        self._btn_images.setEnabled(True)

        # Clear paths
        self._json_edit.clear()
        self._atlas_edit.clear()
        self._images_edit.clear()

        self._sidebar.show()
        self._stack.setCurrentIndex(1)

    def _switch_mode(self):
        """Return to welcome page, clearing current state."""
        if self._mode is not None:
            reply = QMessageBox.question(
                self, tr("confirm.title"), tr("app.confirm_switch_mode"),
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        self._json_edit.clear()
        self._atlas_edit.clear()
        self._images_edit.clear()
        self._spine_project_edit.clear()
        self._json_edit.setReadOnly(False)
        self._atlas_edit.setReadOnly(False)
        self._images_edit.setReadOnly(False)
        self._btn_json.setEnabled(True)
        self._btn_atlas.setEnabled(True)
        self._btn_images.setEnabled(True)
        self._show_welcome()

    def _import_back_to_spine(self):
        """Save current JSON back into the .spine project.

        Shows a dialog letting the user optionally downgrade the JSON to a
        different Spine version before importing.  Creates a timestamped
        backup of the original .spine file, then removes the original so
        the Spine CLI creates a fresh project with only the imported
        skeleton (avoids duplicate skeletons).
        """
        json_path = self._json_edit.text().strip()
        if not json_path or not self._spine_project_path:
            return

        spine = Path(self._spine_project_path)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = spine.with_name(f"{spine.stem}_backup_{stamp}.spine")

        # --- version picker dialog ---
        dlg = QDialog(self)
        dlg.setWindowTitle(tr("spine_mode.save_as_title"))
        layout = QVBoxLayout(dlg)

        info = QLabel(tr("spine_mode.confirm_import",
                         spine_file=self._spine_project_path,
                         backup=str(backup_path)))
        info.setWordWrap(True)
        layout.addWidget(info)

        form = QFormLayout()
        version_combo = QComboBox()
        version_combo.addItem(tr("spine_mode.keep_original"), "")
        for v in SpineDowngraderTab.DEST_VERSIONS:
            version_combo.addItem(v, v)
        form.addRow(tr("spine_mode.target_version"), version_combo)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        if dlg.exec() != QDialog.Accepted:
            return

        target_version = version_combo.currentData()

        # --- optionally downgrade JSON before import ---
        if target_version:
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data, warnings = convert_to_destination(data, target_version)
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, separators=(",", ":"))
            except Exception as e:
                QMessageBox.warning(self, tr("err.title"),
                                    tr("spine_mode.downgrade_failed", error=str(e)))
                return

        # Create timestamped backup and remove original so import starts fresh
        try:
            shutil.copy2(str(spine), str(backup_path))
            spine.unlink()
        except OSError as e:
            QMessageBox.warning(self, tr("err.title"),
                                tr("spine_mode.backup_failed", error=str(e)))
            return

        exe = settings.spine_executable()
        progress = self._make_cli_progress()
        result = import_to_spine_project(
            exe, json_path, self._spine_project_path,
            on_output=self._cli_output_updater(progress),
        )
        progress.close()

        if result.success:
            QMessageBox.information(
                self, tr("done.title"),
                tr("spine_mode.import_done",
                   spine_file=self._spine_project_path,
                   backup=str(backup_path)),
            )
        else:
            # Restore from backup on failure
            if backup_path.is_file() and not spine.is_file():
                shutil.copy2(str(backup_path), str(spine))
            error = result.stderr or result.stdout or "Unknown error"
            QMessageBox.warning(self, tr("err.title"),
                                tr("spine_mode.import_failed", error=error))

    def _reexport_spine_project(self):
        """Re-export from .spine, overwriting current JSON and atlas."""
        if not self._spine_project_path:
            return

        reply = QMessageBox.question(
            self, tr("confirm.title"), tr("spine_mode.reexport_confirm"),
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        exe = settings.spine_executable()
        progress = self._make_cli_progress()
        result = export_spine_project(
            exe, self._spine_project_path, self._export_dir,
            on_output=self._cli_output_updater(progress),
            pack=getattr(self, "_spine_pack_atlas", True),
        )
        progress.close()

        if not result.success:
            error = result.stderr or result.stdout or "Unknown error"
            QMessageBox.warning(self, tr("err.title"),
                                tr("spine_mode.export_failed", error=error))
            return

        self._json_edit.setText(result.output_json)
        self._atlas_edit.setText(result.output_atlas)
        self._run_all_tabs()

    # ── Language ──

    def _on_language_changed(self, index):
        lang = self._lang_combo.itemData(index)
        if lang:
            set_language(lang)
            settings.set_language(lang)

    def _retranslate(self):
        self.setWindowTitle(f"GreentubeSK Spine Swiss Knife v{__version__}")
        self._update_btn.setText(tr("update.btn"))
        self._loading_overlay.setText(tr("app.loading"))
        self._title_label.setText(tr("app.title"))
        self._home_btn.setText(tr("app.home"))
        self._switch_mode_btn.setText(tr("app.switch_mode"))
        self._spine_project_label.setText(tr("app.spine_project_label"))
        self._btn_reimport.setText(tr("spine_mode.reimport_btn"))
        self._btn_reexport.setText(tr("spine_mode.reexport_btn"))
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
        elif key == "spine":
            return getattr(self, "_spine_project_path", "")
        elif key == "spine_exe":
            return settings.spine_executable() or ""
        elif key == "mode":
            return self._mode or ""
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

        # Always re-detect atlas for the new JSON
        atlas_found = ""
        for pattern in (f"{stem}.atlas.txt", f"{stem}.atlas"):
            candidate = parent / pattern
            if candidate.is_file():
                atlas_found = str(candidate)
                break
        if not atlas_found:
            for ext in ("*.atlas.txt", "*.atlas"):
                found = list(parent.glob(ext))
                if found:
                    atlas_found = str(found[0])
                    break
        self._atlas_edit.setText(atlas_found)

        # Always re-detect images folder for the new JSON
        img_dir = parent / "images"
        self._images_edit.setText(str(img_dir) if img_dir.is_dir() else "")

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
        overlay = self._loading_overlay
        overlay.resize(self._tab_container.size())
        overlay.show()
        overlay.raise_()
        QApplication.processEvents()
        try:
            self._run_all_tabs_inner()
        finally:
            overlay.hide()

    def _run_all_tabs_inner(self):
        tabs = self._tab_instances
        has_atlas = bool(self._atlas_edit.text().strip())
        has_images = bool(self._images_edit.text().strip())

        # JSON-only tabs: Analyzer, Rect Masks, Polygon, Keyframes, Dead Bones, Hidden Att., Blend
        for tab in [tabs[0], tabs[2], tabs[3], tabs[4], tabs[5], tabs[6], tabs[7]]:
            try:
                tab._analyze()
            except Exception:
                pass
        # Unused — needs atlas + images
        if has_atlas and has_images:
            try:
                tabs[8]._analyze()
            except Exception:
                pass
        # Splitter — Load Animations
        try:
            tabs[9]._load()
        except Exception:
            pass
        # Downgrader — Detect Version
        try:
            tabs[10]._detect()
        except Exception:
            pass
        # Viewer — needs atlas
        if has_atlas:
            try:
                tabs[12]._load()
            except Exception:
                pass
        # Static Exporter — reads animation list from JSON
        try:
            tabs[13]._load()
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
