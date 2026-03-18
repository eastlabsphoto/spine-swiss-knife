"""Welcome / mode-selection page shown at application startup."""

from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QGroupBox, QFileDialog, QSizePolicy, QMessageBox,
    QFrame, QProgressDialog, QApplication,
)
from PySide6.QtCore import Qt, Signal

from .i18n import tr, language_changed
from .settings import settings
from .spine_cli import detect_spine_executable


class WelcomePage(QWidget):
    spine_mode_selected = Signal(str, bool, str)  # (.spine path, pack_atlas, version)
    json_mode_selected = Signal(str)  # (json path)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("welcomePage")
        self._build_ui()
        language_changed.connect(self._retranslate)

        # Restore saved spine executable
        saved = settings.spine_executable()
        if saved:
            self._path_edit.setText(saved)
            self._validate_path(saved)
        else:
            self._try_auto_detect(silent=True)

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # Center everything vertically
        outer.addStretch(2)

        # Container with fixed max width
        container = QWidget()
        container.setMaximumWidth(520)
        cl = QVBoxLayout(container)
        cl.setSpacing(16)

        self._title = QLabel(tr("welcome.title"))
        self._title.setObjectName("welcomeTitle")
        self._title.setAlignment(Qt.AlignCenter)
        cl.addWidget(self._title)

        self._subtitle = QLabel(tr("welcome.subtitle"))
        self._subtitle.setObjectName("welcomeSubtitle")
        self._subtitle.setAlignment(Qt.AlignCenter)
        cl.addWidget(self._subtitle)

        # Buttons row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(16)
        btn_row.addStretch()

        self._btn_spine = QPushButton(tr("welcome.btn_spine"))
        self._btn_spine.setProperty("role", "welcome-primary")
        self._btn_spine.setCursor(Qt.PointingHandCursor)
        self._btn_spine.setEnabled(False)
        self._btn_spine.clicked.connect(self._on_spine_clicked)
        btn_row.addWidget(self._btn_spine)

        self._btn_json = QPushButton(tr("welcome.btn_json"))
        self._btn_json.setProperty("role", "welcome-secondary")
        self._btn_json.setCursor(Qt.PointingHandCursor)
        self._btn_json.clicked.connect(self._on_json_clicked)
        btn_row.addWidget(self._btn_json)

        btn_row.addStretch()
        cl.addLayout(btn_row)

        # Info text (below buttons)
        self._info = QLabel(tr("welcome.info_spine"))
        self._info.setProperty("role", "info")
        self._info.setAlignment(Qt.AlignCenter)
        self._info.setWordWrap(True)
        cl.addWidget(self._info)

        # Spine path group (below actions)
        self._path_group = QGroupBox(tr("welcome.spine_path_group"))
        pg_layout = QVBoxLayout(self._path_group)

        path_row = QHBoxLayout()
        self._path_label = QLabel(tr("welcome.spine_path"))
        path_row.addWidget(self._path_label)
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText(tr("welcome.spine_path_placeholder"))
        self._path_edit.textChanged.connect(self._on_path_changed)
        path_row.addWidget(self._path_edit, 1)
        self._browse_btn = QPushButton(tr("app.browse"))
        self._browse_btn.clicked.connect(self._browse_spine)
        path_row.addWidget(self._browse_btn)
        self._auto_btn = QPushButton(tr("welcome.auto_detect"))
        self._auto_btn.clicked.connect(lambda: self._try_auto_detect(silent=False))
        path_row.addWidget(self._auto_btn)
        pg_layout.addLayout(path_row)

        self._status_label = QLabel(tr("welcome.spine_not_set"))
        self._status_label.setObjectName("spinePathStatus")
        self._status_label.setProperty("role", "info")
        pg_layout.addWidget(self._status_label)

        cl.addWidget(self._path_group)

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
        cl.addWidget(self._update_banner)

        # Store update info
        self._pending_update_url = ""
        self._pending_update_changelog = ""
        self._update_worker = None

        # Center container horizontally
        h = QHBoxLayout()
        h.addStretch()
        h.addWidget(container)
        h.addStretch()
        outer.addLayout(h)
        outer.addStretch(3)

    # -- Path handling --

    def _on_path_changed(self, text: str):
        text = text.strip()
        if not text:
            self._set_status("info", tr("welcome.spine_not_set"))
            self._btn_spine.setEnabled(False)
            return
        self._validate_path(text)

    def _validate_path(self, path: str):
        if Path(path).is_file():
            self._set_status("ok", tr("welcome.spine_found"))
            self._btn_spine.setEnabled(True)
            settings.set_spine_executable(path)
        else:
            self._set_status("error", tr("welcome.spine_not_found"))
            self._btn_spine.setEnabled(False)

    def _set_status(self, role: str, text: str):
        self._status_label.setText(text)
        self._status_label.setProperty("role", role)
        self._status_label.style().unpolish(self._status_label)
        self._status_label.style().polish(self._status_label)

    def _browse_spine(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("welcome.dialog.select_spine"), "",
            tr("welcome.filter.spine_exe"),
        )
        if path:
            self._path_edit.setText(path)

    def _try_auto_detect(self, silent: bool = False):
        found = detect_spine_executable()
        if found:
            self._path_edit.setText(found)
        elif not silent:
            self._set_status("error", tr("welcome.spine_not_found"))

    # -- Mode selection --

    def _on_json_clicked(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("app.dialog.select_json"), "",
            tr("app.filter.json"),
        )
        if path:
            self.json_mode_selected.emit(path)

    def _on_spine_clicked(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("welcome.dialog.select_spine_project"), "",
            tr("welcome.filter.spine_project"),
        )
        if not path:
            return

        reply = QMessageBox.question(
            self, tr("welcome.pack_atlas_title"),
            tr("welcome.pack_atlas_question"),
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
        )
        if reply == QMessageBox.Cancel:
            return
        self.spine_mode_selected.emit(path, reply == QMessageBox.Yes, "")

    # -- Auto-update --

    def show_update_available(self, version: str, zipball_url: str, changelog: str):
        self._pending_update_url = zipball_url
        self._pending_update_changelog = changelog
        self._update_label.setText(tr("update.available", version=version))
        self._update_banner.show()

    def _do_update(self):
        from .updater import UpdateWorker, restart_app

        msg = tr("update.confirm",
                 version=self._update_label.text(),
                 changelog=self._pending_update_changelog or "—")
        reply = QMessageBox.question(self, tr("confirm.title"), msg,
                                     QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        self._update_btn.setEnabled(False)
        self._update_label.setText(tr("update.downloading"))

        self._update_worker = UpdateWorker(self._pending_update_url, self)
        self._update_worker.progress.connect(
            lambda text: self._update_label.setText(text))
        self._update_worker.finished.connect(self._on_update_finished)
        self._update_worker.failed.connect(self._on_update_failed)
        self._update_worker.start()

    def _on_update_finished(self):
        from .updater import restart_app
        self._update_label.setText(tr("update.restart"))
        QApplication.processEvents()
        restart_app()

    def _on_update_failed(self, error: str):
        self._update_btn.setEnabled(True)
        self._update_label.setText(tr("update.failed", error=error))

    # -- Retranslate --

    def _retranslate(self):
        self._title.setText(tr("welcome.title"))
        self._subtitle.setText(tr("welcome.subtitle"))
        self._path_group.setTitle(tr("welcome.spine_path_group"))
        self._path_label.setText(tr("welcome.spine_path"))
        self._path_edit.setPlaceholderText(tr("welcome.spine_path_placeholder"))
        self._browse_btn.setText(tr("app.browse"))
        self._auto_btn.setText(tr("welcome.auto_detect"))
        self._btn_spine.setText(tr("welcome.btn_spine"))
        self._btn_json.setText(tr("welcome.btn_json"))
        self._info.setText(tr("welcome.info_spine"))
        # Re-validate to update status text
        text = self._path_edit.text().strip()
        if not text:
            self._set_status("info", tr("welcome.spine_not_set"))
        elif Path(text).is_file():
            self._set_status("ok", tr("welcome.spine_found"))
        else:
            self._set_status("error", tr("welcome.spine_not_found"))
