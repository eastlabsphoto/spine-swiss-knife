"""Main application window — sidebar navigation, config panel, dark theme."""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QTabWidget, QFileDialog,
    QStackedWidget, QSizePolicy, QComboBox, QFrame, QMessageBox,
    QProgressDialog, QApplication, QDialog, QDialogButtonBox,
    QFormLayout, QListWidget, QListWidgetItem, QInputDialog,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon

from . import __version__
from .i18n import tr, language_changed
from .settings import settings
from .spine_cli import export_spine_project, import_to_spine_project
from .spine_downgrader import SpineDowngraderTab, convert_to_destination
from .spine_json import (
    create_backup, list_backups, load_spine_json, save_spine_json,
)
from .spine_validate import validate_skeleton
from .atlas_parser import parse_atlas
from .image_flattener import ImageFlattenerTab
from .merge_skeletons import MergeSkeletonsTab
from .welcome_page import WelcomePage
from .project_analyzer import ProjectAnalyzerTab
from .optimizer import OptimizerTab
from .mask_optimizer import MaskOptimizerTab
from .polygon_simplify import PolygonSimplifyTab
from .keyframe_optimizer import KeyframeOptimizerTab
from .dead_bones import DeadBonesTab
from .hidden_attachments import HiddenAttachmentsTab
from .blend_switcher import BlendSwitcherTab
from .draw_order_optimizer import DrawOrderOptimizerTab
from .slot_renamer import SlotRenamerTab
from .splitter import SplitterTab
from .texture_unpacker import TextureUnpackerTab
from .spine_viewer import SpineViewerTab
from .static_exporter import StaticExporterTab
from .mcp_setup import McpSetupTab
from .debug_log import DebugConsole, log_error, log_exception
from .unused_finder import UnusedFinderTab


_SIDEBAR_KEYS = [
    ("app.sidebar.analyzer", "app.tip.analyzer"),
    ("app.sidebar.downscaler", "app.tip.downscaler"),
    ("app.sidebar.rect_masks", "app.tip.rect_masks"),
    ("app.sidebar.polygon", "app.tip.polygon"),
    ("app.sidebar.keyframes", "app.tip.keyframes"),
    ("app.sidebar.dead_bones", "app.tip.dead_bones"),
    ("app.sidebar.hidden", "app.tip.hidden"),
    ("app.sidebar.unused", "app.tip.unused"),
    ("app.sidebar.blend", "app.tip.blend"),
    ("app.sidebar.draw_order", "app.tip.draw_order"),
    ("app.sidebar.renamer", "app.tip.renamer"),
    ("app.sidebar.splitter", "app.tip.splitter"),
    ("app.sidebar.converter", "app.tip.converter"),
    ("app.sidebar.unpacker", "app.tip.unpacker"),
    ("app.sidebar.static_export", "app.tip.static_export"),
    ("app.sidebar.flattener", "app.tip.flattener"),
    ("app.sidebar.merge", "app.tip.merge"),
    ("app.sidebar.viewer", "app.tip.viewer"),
    ("app.sidebar.mcp", "app.tip.mcp"),
]


# ---------------------------------------------------------------------------
# Pure helpers (no widgets) — kept module-level so they can be unit tested.
# ---------------------------------------------------------------------------

def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _atlas_stem(path: str) -> str:
    name = os.path.basename(path)
    for ext in (".atlas.txt", ".atlas"):
        if name.lower().endswith(ext):
            return name[:-len(ext)]
    return os.path.splitext(name)[0]


def pair_atlas(json_path: str, atlases, default: str = "") -> str:
    """Atlas of *json_path* by file stem; *default* when none matches."""
    stem = Path(json_path).stem
    for a in atlases or []:
        if _atlas_stem(a) == stem:
            return a
    return default


def resolve_images_dir(json_path: str, data) -> str:
    """``skeleton.images`` (relative to the JSON) when it is an existing folder."""
    try:
        images = data.get("skeleton", {}).get("images", "")
    except AttributeError:
        return ""
    if not isinstance(images, str) or not images.strip():
        return ""
    candidate = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(json_path)), images))
    return candidate if os.path.isdir(candidate) else ""


def import_plan(json_path: str, export_jsons) -> list:
    """JSON files to import, in order, when saving back into a .spine project.

    With several exported skeletons every one is imported (the edited one in
    its place); otherwise just *json_path*.
    """
    export_jsons = list(export_jsons or [])
    if len(export_jsons) > 1:
        mine = _norm_path(json_path)
        if any(_norm_path(j) == mine for j in export_jsons):
            return [json_path if _norm_path(j) == mine else j for j in export_jsons]
    return [json_path]


def restore_spine_backup(backup_path, spine_path) -> bool:
    """Put *backup_path* back as *spine_path*; a partial file is removed first."""
    try:
        backup = Path(backup_path)
        spine = Path(spine_path)
        if not backup.is_file():
            return False
        if spine.exists():
            spine.unlink()
        shutil.copy2(str(backup), str(spine))
        return spine.is_file()
    except OSError:
        return False


_BACKUP_STAMP_RE = re.compile(r"\.(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})_\d{6}\.json$")


def describe_backup(path: str) -> str:
    """'YYYY-MM-DD HH:MM:SS  (12.3 KB)' for a history backup file."""
    m = _BACKUP_STAMP_RE.search(os.path.basename(path))
    stamp = ("%s-%s-%s %s:%s:%s" % m.groups()) if m else os.path.basename(path)
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    return "%s  (%.1f KB)" % (stamp, size / 1024.0)


def restore_json_backup(json_path: str, backup_path: str) -> None:
    """Replace *json_path* by *backup_path*; the current file is backed up first.

    The chosen backup is read before ``create_backup`` runs (which prunes old
    history files and could otherwise delete it).
    """
    with open(backup_path, "rb") as f:
        content = f.read()
    if os.path.isfile(json_path):
        create_backup(json_path)
    tmp = json_path + ".restore.tmp"
    with open(tmp, "wb") as f:
        f.write(content)
    os.replace(tmp, json_path)


def summarize_issues(issues) -> tuple:
    """``(errors, warnings, details_text)`` for a validate_skeleton result."""
    errors = [i for i in issues if i.get("severity") == "error"]
    warnings = [i for i in issues if i.get("severity") != "error"]
    lines = []
    for i in errors + warnings:
        lines.append("[%s] %s: %s (%s)" % (
            i.get("severity", "warning").upper(), i.get("where", ""),
            i.get("message", ""), i.get("code", "")))
    return len(errors), len(warnings), "\n".join(lines)



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
        self._export_jsons: list = []   # every JSON exported from a .spine project
        self._spine_pack_atlas = True
        self._sidebar_btns = []
        self._validation_state = None   # None | ("ok",) | ("issues", e, w) | ("unreadable",)
        self._validation_details = ""
        self._refreshing = False

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

        # (Update banner is on the welcome page)

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

        # JSON buttons container (Browse + JSON-mode-only buttons)
        self._json_btn_container = QWidget()
        self._json_btn_container.setStyleSheet("background: transparent;")
        json_btn_layout = QHBoxLayout(self._json_btn_container)
        json_btn_layout.setContentsMargins(0, 0, 0, 0)
        json_btn_layout.setSpacing(4)
        self._btn_json = QPushButton(tr("app.browse"))
        self._btn_json.clicked.connect(self._browse_json)
        json_btn_layout.addWidget(self._btn_json)
        self._btn_restore = QPushButton(tr("app.restore_btn"))
        self._btn_restore.setCursor(Qt.PointingHandCursor)
        self._btn_restore.clicked.connect(self._restore_backup_dialog)
        json_btn_layout.addWidget(self._btn_restore)
        self._btn_export_atlas = QPushButton(tr("json_mode.export_atlas_btn"))
        self._btn_export_atlas.setCursor(Qt.PointingHandCursor)
        self._btn_export_atlas.clicked.connect(self._export_atlas_from_json)
        self._btn_export_atlas.hide()
        json_btn_layout.addWidget(self._btn_export_atlas)
        self._btn_save_spine = QPushButton(tr("json_mode.save_spine_btn"))
        self._btn_save_spine.setProperty("role", "primary")
        self._btn_save_spine.setCursor(Qt.PointingHandCursor)
        self._btn_save_spine.clicked.connect(self._save_json_as_spine)
        self._btn_save_spine.hide()
        json_btn_layout.addWidget(self._btn_save_spine)
        config_grid.addWidget(self._json_btn_container, 1, 2)

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

        # Skeleton validation status line (never blocks the UI)
        self._validation_btn = QPushButton("")
        self._validation_btn.setFlat(True)
        self._validation_btn.setCursor(Qt.PointingHandCursor)
        self._validation_btn.setStyleSheet(
            "text-align: left; padding: 2px 6px; background: transparent;"
            "border: none; font-weight: bold;")
        self._validation_btn.clicked.connect(self._show_validation_details)
        self._validation_btn.hide()
        main_layout.addWidget(self._validation_btn)

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
        # Named references (the order of _tab_instances is the sidebar order
        # and must match _SIDEBAR_KEYS).
        self._tab_analyzer = ProjectAnalyzerTab(self._tabs, self._get_config)
        self._tab_optimizer = OptimizerTab(self._tabs, self._get_config, on_modified=notify)
        self._tab_masks = MaskOptimizerTab(self._tabs, self._get_config, on_modified=notify)
        self._tab_polygon = PolygonSimplifyTab(self._tabs, self._get_config, on_modified=notify)
        self._tab_keyframes = KeyframeOptimizerTab(self._tabs, self._get_config, on_modified=notify)
        self._tab_dead_bones = DeadBonesTab(self._tabs, self._get_config, on_modified=notify)
        self._tab_hidden = HiddenAttachmentsTab(self._tabs, self._get_config, on_modified=notify)
        self._tab_unused = UnusedFinderTab(self._tabs, self._get_config)
        self._tab_blend = BlendSwitcherTab(self._tabs, self._get_config, on_modified=notify)
        self._tab_draw_order = DrawOrderOptimizerTab(self._tabs, self._get_config, on_modified=notify)
        self._tab_renamer = SlotRenamerTab(self._tabs, self._get_config, on_modified=notify)
        self._tab_splitter = SplitterTab(self._tabs, self._get_config)
        self._tab_converter = SpineDowngraderTab(self._tabs, self._get_config)
        self._tab_unpacker = TextureUnpackerTab(self._tabs, self._get_config)
        self._tab_static = StaticExporterTab(self._tabs, self._get_config)
        self._tab_flattener = ImageFlattenerTab(self._tabs, self._get_config, on_modified=notify)
        self._tab_merge = MergeSkeletonsTab(self._tabs, self._get_config)
        self._tab_viewer = SpineViewerTab(self._tabs, self._get_config)
        self._tab_mcp = McpSetupTab(self._tabs, self._get_config)
        self._tab_instances = [
            self._tab_analyzer, self._tab_optimizer, self._tab_masks,
            self._tab_polygon, self._tab_keyframes, self._tab_dead_bones,
            self._tab_hidden, self._tab_unused, self._tab_blend,
            self._tab_draw_order, self._tab_renamer, self._tab_splitter,
            self._tab_converter, self._tab_unpacker, self._tab_static,
            self._tab_flattener, self._tab_merge, self._tab_viewer,
            self._tab_mcp,
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
        self._btn_export_atlas.hide()
        self._btn_save_spine.hide()
        self._validation_state = None
        self._render_validation()

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

    def _pick_skeleton(self, jsons, preselect: str = "") -> Optional[str]:
        """Let the user choose one of several exported skeletons (by file stem).

        Returns the chosen JSON path, or None when cancelled.
        """
        if len(jsons) == 1:
            return jsons[0]
        stems = [Path(j).stem for j in jsons]
        current = 0
        if preselect:
            for i, j in enumerate(jsons):
                if _norm_path(j) == _norm_path(preselect):
                    current = i
        chosen, ok = QInputDialog.getItem(
            self, tr("spine_mode.pick_title"), tr("spine_mode.pick_label"),
            stems, current, False,
        )
        if not ok:
            return None
        return jsons[stems.index(chosen)]

    def _select_export(self, result, preselect: str = ""):
        """``(json, atlas)`` of an export result, asking when several skeletons
        were exported; None when the user cancelled."""
        jsons = list(result.output_jsons or [])
        if not jsons and result.output_json:
            jsons = [result.output_json]
        if not jsons:
            return "", result.output_atlas
        json_path = self._pick_skeleton(jsons, preselect)
        if json_path is None:
            return None
        self._export_jsons = jsons
        atlas = pair_atlas(json_path, result.output_atlases, result.output_atlas)
        return json_path, atlas

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
        try:
            result = export_spine_project(
                exe, spine_path, export_dir,
                on_output=self._cli_output_updater(progress),
                pack=pack,
            )
        finally:
            progress.close()

        if not result.success:
            error = result.stderr or result.stdout or "Unknown error"
            QMessageBox.warning(self, tr("err.title"),
                                tr("spine_mode.export_failed", error=error))
            return

        previous_jsons = self._export_jsons
        self._export_jsons = []
        picked = self._select_export(result)
        if picked is None:
            self._export_jsons = previous_jsons
            return
        json_path, atlas_path = picked

        self._mode = "spine"
        self._spine_project_path = spine_path
        self._export_dir = export_dir
        settings.set_last_mode("spine")
        settings.set_last_spine_file(spine_path)

        # Fill config fields
        self._spine_project_edit.setText(spine_path)
        self._json_edit.setText(json_path)
        self._atlas_edit.setText(atlas_path)

        # Auto-detect images folder from export dir
        img_dir = Path(export_dir) / "images"
        if img_dir.is_dir():
            self._images_edit.setText(str(img_dir))
        else:
            # Try parent of .spine file
            img_dir = Path(spine_path).parent / "images"
            self._images_edit.setText(str(img_dir) if img_dir.is_dir() else "")

        # Show .spine row, make fields read-only; hide JSON-mode buttons
        self._spine_project_label.show()
        self._spine_project_edit.show()
        self._spine_btn_container.show()
        self._btn_export_atlas.hide()
        self._btn_save_spine.hide()
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

    def _enter_json_mode(self, json_path: str = ""):
        """Switch to tool area in JSON mode with optional pre-selected JSON."""
        self._mode = "json"
        self._spine_project_path = ""
        self._export_dir = ""
        self._export_jsons = []
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

        # Show JSON-mode-only buttons
        self._btn_export_atlas.show()
        self._btn_save_spine.show()

        # Clear paths
        self._json_edit.clear()
        self._atlas_edit.clear()
        self._images_edit.clear()

        self._sidebar.show()
        self._stack.setCurrentIndex(1)

        if json_path:
            self._json_edit.setText(json_path)
            self._auto_fill_from_json(json_path)
            self._run_all_tabs()
        else:
            self._update_validation()

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
        the Spine CLI creates a fresh project (avoids duplicate skeletons).

        When the project had several skeletons, the project is rebuilt from
        ALL exported skeletons (the edited one included); on any failure the
        backup is restored.  (Untested against a real Spine.)
        """
        json_path = self._json_edit.text().strip()
        if not json_path or not self._spine_project_path:
            return

        spine = Path(self._spine_project_path)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = spine.with_name(f"{spine.stem}_backup_{stamp}.spine")
        plan = import_plan(json_path, self._export_jsons)
        multi = len(plan) > 1

        # --- version picker dialog ---
        dlg = QDialog(self)
        dlg.setWindowTitle(tr("spine_mode.save_as_title"))
        layout = QVBoxLayout(dlg)

        if multi:
            text = tr("spine_mode.confirm_import_multi",
                      count=len(plan),
                      skeletons=", ".join(Path(j).stem for j in plan),
                      spine_file=self._spine_project_path,
                      backup=str(backup_path))
        else:
            text = tr("spine_mode.confirm_import",
                      spine_file=self._spine_project_path,
                      backup=str(backup_path))
        info = QLabel(text)
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

        # --- optionally downgrade the JSON(s) before import ---
        # Convert in memory first so the user can cancel on warnings before
        # any file is touched.
        converted = {}
        if target_version:
            all_warnings = []
            try:
                for jp in plan:
                    data = load_spine_json(jp)
                    data, warnings = convert_to_destination(data, target_version)
                    converted[jp] = data
                    prefix = "[%s] " % Path(jp).stem if multi else ""
                    all_warnings.extend(prefix + w for w in warnings)
            except Exception as e:
                QMessageBox.warning(self, tr("err.title"),
                                    tr("spine_mode.downgrade_failed", error=str(e)))
                return
            if all_warnings:
                shown = all_warnings[:15]
                if len(all_warnings) > len(shown):
                    shown.append("... (+%d more)" % (len(all_warnings) - len(shown)))
                reply = QMessageBox.question(
                    self, tr("confirm.title"),
                    tr("spine_mode.downgrade_warnings", version=target_version,
                       warnings="\n".join("- " + w for w in shown)),
                    QMessageBox.Yes | QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    return
            try:
                for jp, data in converted.items():
                    create_backup(jp)
                    save_spine_json(jp, data)
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
        failure = ""
        try:
            for jp in plan:
                result = import_to_spine_project(
                    exe, jp, self._spine_project_path,
                    on_output=self._cli_output_updater(progress),
                )
                if not result.success:
                    failure = result.stderr or result.stdout or "Unknown error"
                    break
        except Exception as e:  # never leave the project deleted
            failure = str(e)
        finally:
            progress.close()

        if not failure:
            QMessageBox.information(
                self, tr("done.title"),
                tr("spine_mode.import_done",
                   spine_file=self._spine_project_path,
                   backup=str(backup_path)),
            )
        else:
            # Restore from backup (also replaces a partially written project)
            if restore_spine_backup(backup_path, spine):
                QMessageBox.warning(self, tr("err.title"),
                                    tr("spine_mode.import_failed", error=failure))
            else:
                QMessageBox.warning(
                    self, tr("err.title"),
                    tr("spine_mode.import_restore_failed", error=failure,
                       backup=str(backup_path)))

    # ── JSON mode operations ──

    def _export_atlas_from_json(self):
        """Export atlas (pack texture) from the current JSON via Spine CLI.

        Imports the JSON into a temporary .spine file, then exports with
        atlas packing.  Atlas files are placed next to the JSON.
        """
        json_path = self._json_edit.text().strip()
        if not json_path or not Path(json_path).is_file():
            QMessageBox.warning(self, tr("err.title"), tr("err.no_json"))
            return

        exe = settings.spine_executable()
        if not exe:
            QMessageBox.warning(self, tr("err.title"), tr("welcome.spine_not_set"))
            return

        # Read version from JSON
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            version = data.get("skeleton", {}).get("spine", "")
        except Exception as e:
            QMessageBox.warning(self, tr("err.title"), str(e))
            return

        json_dir = Path(json_path).parent
        stem = Path(json_path).stem
        temp_spine = str(json_dir / f"_ssk_temp_{stem}.spine")
        export_dir = str(json_dir / f"_ssk_atlas_export_{stem}")

        progress = self._make_cli_progress()

        # Step 1: Import JSON to temp .spine
        result = import_to_spine_project(
            exe, json_path, temp_spine,
            on_output=self._cli_output_updater(progress),
            target_version=version or None,
        )
        if not result.success:
            progress.close()
            try:
                Path(temp_spine).unlink(missing_ok=True)
            except OSError:
                pass
            error = result.stderr or result.stdout or "Unknown error"
            QMessageBox.warning(self, tr("err.title"),
                                tr("json_mode.atlas_export_failed", error=error))
            return

        # Step 2: Export with atlas pack from temp .spine
        result = export_spine_project(
            exe, temp_spine, export_dir,
            on_output=self._cli_output_updater(progress),
            pack=True,
        )
        progress.close()

        # Clean up temp .spine
        try:
            Path(temp_spine).unlink(missing_ok=True)
        except OSError:
            pass

        if not result.success:
            shutil.rmtree(export_dir, ignore_errors=True)
            error = result.stderr or result.stdout or "Unknown error"
            QMessageBox.warning(self, tr("err.title"),
                                tr("json_mode.atlas_export_failed", error=error))
            return

        # Move atlas files to JSON directory
        export_path = Path(export_dir)
        atlas_dst = ""
        moved_files = []
        for f in export_path.iterdir():
            if f.name.endswith(".atlas") or f.name.endswith(".atlas.txt"):
                dst = json_dir / f.name
                shutil.move(str(f), str(dst))
                atlas_dst = str(dst)
                moved_files.append(dst.name)
            elif f.suffix == ".png":
                dst = json_dir / f.name
                shutil.move(str(f), str(dst))
                moved_files.append(dst.name)

        # Clean up export dir
        shutil.rmtree(export_dir, ignore_errors=True)

        if atlas_dst:
            self._atlas_edit.setText(atlas_dst)
            QMessageBox.information(
                self, tr("done.title"),
                tr("json_mode.atlas_done",
                   files=", ".join(moved_files)))
        else:
            QMessageBox.warning(self, tr("err.title"),
                                tr("json_mode.no_atlas_produced"))

    def _save_json_as_spine(self):
        """Save current JSON as a .spine project file via Spine CLI."""
        json_path = self._json_edit.text().strip()
        if not json_path or not Path(json_path).is_file():
            QMessageBox.warning(self, tr("err.title"), tr("err.no_json"))
            return

        exe = settings.spine_executable()
        if not exe:
            QMessageBox.warning(self, tr("err.title"), tr("welcome.spine_not_set"))
            return

        # Read version from JSON
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            version = data.get("skeleton", {}).get("spine", "")
        except Exception:
            version = ""

        # Let user choose save location
        suggested = str(Path(json_path).with_suffix(".spine"))
        spine_path, _ = QFileDialog.getSaveFileName(
            self, tr("json_mode.save_spine_title"), suggested,
            tr("welcome.filter.spine_project"),
        )
        if not spine_path:
            return

        progress = self._make_cli_progress()
        result = import_to_spine_project(
            exe, json_path, spine_path,
            on_output=self._cli_output_updater(progress),
            target_version=version or None,
        )
        progress.close()

        if result.success:
            QMessageBox.information(
                self, tr("done.title"),
                tr("json_mode.save_spine_done", path=spine_path))
        else:
            error = result.stderr or result.stdout or "Unknown error"
            QMessageBox.warning(self, tr("err.title"),
                                tr("json_mode.save_spine_failed", error=error))

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
        try:
            result = export_spine_project(
                exe, self._spine_project_path, self._export_dir,
                on_output=self._cli_output_updater(progress),
                pack=getattr(self, "_spine_pack_atlas", True),
            )
        finally:
            progress.close()

        if not result.success:
            error = result.stderr or result.stdout or "Unknown error"
            QMessageBox.warning(self, tr("err.title"),
                                tr("spine_mode.export_failed", error=error))
            return

        previous_jsons = self._export_jsons
        picked = self._select_export(result, preselect=self._json_edit.text().strip())
        if picked is None:
            self._export_jsons = previous_jsons
            return
        json_path, atlas_path = picked
        self._json_edit.setText(json_path)
        self._atlas_edit.setText(atlas_path)
        self._run_all_tabs()

    # ── Language ──

    def _retranslate(self):
        self.setWindowTitle(f"GreentubeSK Spine Swiss Knife v{__version__}")
        self._loading_overlay.setText(tr("app.loading"))
        self._title_label.setText(tr("app.title"))
        self._home_btn.setText(tr("app.home"))
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
        self._btn_restore.setText(tr("app.restore_btn"))
        self._btn_export_atlas.setText(tr("json_mode.export_atlas_btn"))
        self._btn_save_spine.setText(tr("json_mode.save_spine_btn"))
        self._btn_atlas.setText(tr("app.browse"))
        self._btn_images.setText(tr("app.browse"))
        for idx, (name_key, tip_key) in enumerate(_SIDEBAR_KEYS):
            if idx < len(self._sidebar_btns):
                self._sidebar_btns[idx].setText(tr(name_key))
                self._sidebar_btns[idx].setToolTip(tr(tip_key))
        self._render_validation()
        for tab in self._tab_instances:
            if hasattr(tab, "_retranslate"):
                try:
                    tab._retranslate()
                except Exception:
                    log_exception("retranslate failed for %s" % type(tab).__name__)

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

        # Always re-detect images folder for the new JSON: the folder named in
        # skeleton.images (relative to the JSON) wins over ./images
        images_found = ""
        try:
            images_found = resolve_images_dir(json_path, load_spine_json(json_path))
        except Exception:
            images_found = ""
        if not images_found:
            img_dir = parent / "images"
            images_found = str(img_dir) if img_dir.is_dir() else ""
        self._images_edit.setText(images_found)

    def _browse_atlas(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("app.dialog.select_atlas"), "", tr("app.filter.atlas"),
        )
        if path:
            self._atlas_edit.setText(path)
            self._run_all_tabs()

    def _browse_images(self):
        path = QFileDialog.getExistingDirectory(self, tr("app.dialog.select_images"))
        if path:
            self._images_edit.setText(path)
            self._run_all_tabs()

    # --- Run all tabs after JSON selection ---

    def _run_all_tabs(self):
        """Trigger analyze/load/detect on all tabs so everything is ready."""
        if self._refreshing:
            return
        self._refreshing = True
        overlay = self._loading_overlay
        overlay.resize(self._tab_container.size())
        overlay.show()
        overlay.raise_()
        QApplication.processEvents()
        try:
            self._run_all_tabs_inner()
            self._update_validation()
        except Exception:
            log_exception("refresh failed")
        finally:
            overlay.hide()
            self._refreshing = False

    def _refresh_steps(self):
        """(name, callable) for every automatic refresh; no step may show a
        modal dialog for an expected state (silent flag / pre-checks)."""
        has_atlas = bool(self._atlas_edit.text().strip())
        has_images = bool(self._images_edit.text().strip())
        steps = [
            ("Analyzer", self._tab_analyzer._analyze),
            ("Rect Masks", self._tab_masks._analyze),
            ("Polygon", self._tab_polygon._analyze),
            ("Keyframes", lambda: self._tab_keyframes._analyze(silent=True)),
            ("Dead Bones", self._tab_dead_bones._analyze),
            ("Hidden Attachments", self._tab_hidden._analyze),
            ("Blend Modes", self._tab_blend._analyze),
            ("Draw Order", self._tab_draw_order._analyze),
            ("Slot Renamer", self._tab_renamer._analyze),
            ("Splitter", self._tab_splitter._load),
            ("Static Exporter", self._tab_static._load),
        ]
        if has_images:
            steps.append(("Unused Finder",
                          lambda: self._tab_unused._analyze(silent=True)))
        if has_atlas:
            steps.append(("Viewer", lambda: self._tab_viewer._load(silent=True)))
        return steps

    def _run_all_tabs_inner(self):
        json_path = self._json_edit.text().strip()
        if not json_path or not os.path.isfile(json_path):
            return
        # Pre-check: an unreadable JSON would otherwise raise one error dialog
        # per tab.  Report it once in the console instead.
        try:
            load_spine_json(json_path)
        except Exception as e:
            log_error("Cannot read %s: %s" % (json_path, e))
            return

        for name, step in self._refresh_steps():
            try:
                step()
            except Exception:
                log_exception("%s refresh failed" % name)

    # --- Skeleton validation status line ---

    def _update_validation(self):
        """Validate the current JSON (and atlas regions) and update the status line."""
        json_path = self._json_edit.text().strip()
        if not json_path or not os.path.isfile(json_path):
            self._validation_state = None
            self._validation_details = ""
            self._render_validation()
            return
        try:
            data = load_spine_json(json_path)
        except Exception as e:
            self._validation_state = ("unreadable",)
            self._validation_details = "Cannot read %s: %s" % (json_path, e)
            self._render_validation()
            return
        regions = None
        atlas_path = self._atlas_edit.text().strip()
        if atlas_path and os.path.isfile(atlas_path):
            try:
                regions = parse_atlas(atlas_path)
            except Exception:
                regions = None
        try:
            issues = validate_skeleton(data, regions)
        except Exception as e:  # validate_skeleton does not raise; be safe anyway
            issues = [{"severity": "error", "code": "validator_failure",
                       "where": "skeleton", "message": str(e)}]
        errors, warnings, details = summarize_issues(issues)
        self._validation_details = details
        self._validation_state = ("ok",) if not (errors or warnings) else ("issues", errors, warnings)
        self._render_validation()

    def _render_validation(self):
        state = self._validation_state
        btn = self._validation_btn
        if state is None:
            btn.hide()
            return
        if state[0] == "ok":
            text, color = tr("app.validation.ok"), "#6ec072"
        elif state[0] == "unreadable":
            text, color = tr("app.validation.unreadable"), "#f38ba8"
        else:
            text = tr("app.validation.issues", errors=state[1], warnings=state[2])
            color = "#f38ba8" if state[1] else "#e8a838"
        btn.setText(text)
        btn.setStyleSheet(
            "text-align: left; padding: 2px 6px; background: transparent;"
            "border: none; font-weight: bold; color: %s;" % color)
        btn.show()

    def _show_validation_details(self):
        if not self._validation_details:
            return
        self._debug_console.append_text("Skeleton check:\n" + self._validation_details)
        self._debug_console.expand()

    # --- Backup history ---

    def _restore_backup_dialog(self):
        json_path = self._json_edit.text().strip()
        if not json_path or not Path(json_path).is_file():
            QMessageBox.warning(self, tr("err.title"), tr("err.no_json"))
            return
        backups = list_backups(json_path)
        if not backups:
            QMessageBox.information(self, tr("info.title"), tr("app.restore.empty"))
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(tr("app.restore.title"))
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(tr("app.restore.label", name=Path(json_path).name)))
        listing = QListWidget()
        for b in backups:
            item = QListWidgetItem(describe_backup(b))
            item.setData(Qt.UserRole, b)
            listing.addItem(item)
        listing.setCurrentRow(0)
        layout.addWidget(listing)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        if dlg.exec() != QDialog.Accepted or listing.currentItem() is None:
            return
        chosen = listing.currentItem().data(Qt.UserRole)

        reply = QMessageBox.question(
            self, tr("confirm.title"),
            tr("app.restore.confirm", backup=describe_backup(chosen)),
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            restore_json_backup(json_path, chosen)
        except Exception as e:
            QMessageBox.warning(self, tr("err.title"),
                                tr("app.restore.failed", error=str(e)))
            return
        self._run_all_tabs()

    # --- Auto-update ---

    def show_update_available(self, version: str, zipball_url: str, changelog: str):
        self._welcome_page.show_update_available(version, zipball_url, changelog)
