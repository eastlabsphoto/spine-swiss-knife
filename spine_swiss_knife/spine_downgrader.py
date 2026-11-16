"""Spine Version Converter tab — convert Spine files between versions via CLI.

The pure JSON-level downgrade helpers (``convert_to_38``, ``convert_to_37``,
...) live in ``downgrade_core`` (no Qt) and are re-exported here for the
existing imports (``app.py`` uses ``convert_to_destination``).
"""

from __future__ import annotations

import json as _json
import shutil
import tempfile

from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QComboBox,
    QTabWidget, QTextEdit, QMessageBox, QTreeWidget, QTreeWidgetItem,
    QFileDialog, QHeaderView, QAbstractItemView, QApplication,
)
from PySide6.QtGui import QTextCharFormat, QColor, QFont, QTextCursor
from PySide6.QtCore import Qt

from .i18n import tr, language_changed
from .spine_json import load_spine_json, save_spine_json
from .spine_cli import (
    read_spine_file_version, export_spine_project, import_to_spine_project,
)
from .downgrade_core import (  # noqa: F401  (re-exported names)
    _read_source_version, _normalize_version, _RANK, _ensure_skeleton_tag,
    _linearize_curves, _rollback_curves, _rename_constraint_mixes,
    convert_to_38, convert_to_37, convert_to_3653, convert_to_destination,
)


# ==========================================================================
# UI Tab
# ==========================================================================

class SpineVersionConverterTab:
    """Bulk version converter — upgrade or downgrade .spine / .json files via CLI."""

    DEST_VERSIONS = ["4.2.18", "4.1.24", "4.0.64", "3.8.99", "3.7.94", "3.6.53"]

    def __init__(self, tabs: QTabWidget, get_config):
        self._get_config = get_config
        self._tabs = tabs
        self._files: list[dict] = []  # {path, type, version, item}
        self._warnings: list = []

        self._page = QWidget()
        tabs.addTab(self._page, tr("converter.tab"))
        layout = QVBoxLayout(self._page)
        layout.setContentsMargins(5, 5, 5, 5)

        # Info label
        self._info = QLabel(tr("converter.info"))
        self._info.setWordWrap(True)
        layout.addWidget(self._info)

        # Toolbar row
        toolbar = QHBoxLayout()
        self._btn_add = QPushButton(tr("converter.add_files"))
        self._btn_add.setCursor(Qt.PointingHandCursor)
        self._btn_add.clicked.connect(self._add_files)
        toolbar.addWidget(self._btn_add)
        self._btn_remove = QPushButton(tr("converter.remove_selected"))
        self._btn_remove.setCursor(Qt.PointingHandCursor)
        self._btn_remove.clicked.connect(self._remove_selected)
        toolbar.addWidget(self._btn_remove)
        self._btn_clear = QPushButton(tr("converter.clear_all"))
        self._btn_clear.setCursor(Qt.PointingHandCursor)
        self._btn_clear.clicked.connect(self._clear_all)
        toolbar.addWidget(self._btn_clear)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        # File list
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels([
            tr("converter.col.filename"),
            tr("converter.col.type"),
            tr("converter.col.version"),
            tr("converter.col.status"),
        ])
        self._tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._tree.setRootIsDecorated(False)
        self._tree.setAlternatingRowColors(True)
        header = self._tree.header()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        layout.addWidget(self._tree, 1)

        # Bottom row: target version + convert button
        bottom = QHBoxLayout()
        self._target_label = QLabel(tr("converter.target_label"))
        bottom.addWidget(self._target_label)
        self._dest_combo = QComboBox()
        self._dest_combo.setEditable(True)
        self._dest_combo.addItems(self.DEST_VERSIONS)
        self._dest_combo.setCurrentText("3.8.99")
        bottom.addWidget(self._dest_combo)
        bottom.addStretch()
        self._convert_btn = QPushButton(tr("converter.convert_btn"))
        self._convert_btn.setProperty("role", "primary")
        self._convert_btn.setCursor(Qt.PointingHandCursor)
        self._convert_btn.clicked.connect(self._convert_all)
        bottom.addWidget(self._convert_btn)
        layout.addLayout(bottom)

        # Stats
        self._stats = QLabel(tr("converter.default_stats"))
        self._stats.setStyleSheet("font-weight: bold; color: #6ec072;")
        layout.addWidget(self._stats)

        # Log area
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setFont(QFont("monospace", 10))
        self._log.setMaximumHeight(140)
        layout.addWidget(self._log)

        language_changed.connect(self._retranslate)

    # ── i18n ──

    def _retranslate(self):
        idx = self._tabs.indexOf(self._page)
        if idx >= 0:
            self._tabs.setTabText(idx, tr("converter.tab"))
        self._info.setText(tr("converter.info"))
        self._btn_add.setText(tr("converter.add_files"))
        self._btn_remove.setText(tr("converter.remove_selected"))
        self._btn_clear.setText(tr("converter.clear_all"))
        self._tree.setHeaderLabels([
            tr("converter.col.filename"),
            tr("converter.col.type"),
            tr("converter.col.version"),
            tr("converter.col.status"),
        ])
        self._target_label.setText(tr("converter.target_label"))
        self._convert_btn.setText(tr("converter.convert_btn"))
        self._stats.setText(tr("converter.default_stats"))

    # ── Log helpers ──

    def _append(self, text: str, color: str = "#cdd6f4", bold: bool = False):
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        if bold:
            fmt.setFontWeight(QFont.Bold)
        cursor = self._log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text + "\n", fmt)
        self._log.setTextCursor(cursor)
        self._log.ensureCursorVisible()

    def _clear_log(self):
        self._log.clear()

    # ── File management ──

    def _add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            None, tr("converter.dialog.add_files"), "",
            tr("converter.filter"),
        )
        if not paths:
            return
        for p in paths:
            # Skip duplicates
            if any(f["path"] == p for f in self._files):
                continue
            ext = Path(p).suffix.lower()
            ftype = ".spine" if ext == ".spine" else ".json"
            version = self._detect_version(p, ftype)
            item = QTreeWidgetItem([
                Path(p).name, ftype, version or "?", tr("converter.status.ready"),
            ])
            self._tree.addTopLevelItem(item)
            self._files.append({"path": p, "type": ftype, "version": version, "item": item})

    def _detect_version(self, path: str, ftype: str) -> str:
        if ftype == ".spine":
            return read_spine_file_version(path)
        # .json — read skeleton.spine
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            return str(data.get("skeleton", {}).get("spine", ""))
        except Exception:
            return ""

    def _remove_selected(self):
        selected = self._tree.selectedItems()
        if not selected:
            return
        for item in selected:
            idx = self._tree.indexOfTopLevelItem(item)
            if idx >= 0:
                self._tree.takeTopLevelItem(idx)
                self._files = [f for f in self._files if f["item"] is not item]

    def _clear_all(self):
        self._tree.clear()
        self._files.clear()

    # ── No-op stub (called by _run_all_tabs_inner) ──

    def _detect(self):
        pass

    # ── Conversion ──

    def _cli_progress(self):
        """Callback for the CLI wrappers: log lines and keep the UI alive."""
        def _on_output(line):
            if line:
                self._append(line, "#9ea8be")
            QApplication.processEvents()
        return _on_output

    def _convert_all(self):
        if not self._files:
            QMessageBox.information(None, tr("info.title"), tr("converter.no_files"))
            return

        target = self._dest_combo.currentText().strip()
        if not target:
            QMessageBox.critical(None, tr("err.title"), tr("converter.err.no_target"))
            return

        exe = self._get_config("spine_exe")
        if not exe:
            QMessageBox.critical(None, tr("err.title"), tr("converter.err.no_exe"))
            return

        self._clear_log()
        self._append(tr("converter.log.start", target=target), bold=True)

        ok_count = 0
        fail_count = 0

        for entry in self._files:
            path = entry["path"]
            ftype = entry["type"]
            item = entry["item"]
            name = Path(path).name

            item.setText(3, tr("converter.status.converting"))
            QApplication.processEvents()

            self._warnings = []
            try:
                if ftype == ".spine":
                    out = self._convert_spine_file(exe, path, target)
                else:
                    out = self._convert_json_file(exe, path, target)
                item.setText(3, tr("converter.status.done"))
                self._append(tr("converter.log.ok", name=name, output=Path(out).name), "#a6e3a1")
                ok_count += 1
            except Exception as e:
                item.setText(3, tr("converter.status.failed"))
                self._append(tr("converter.log.fail", name=name, error=str(e)), "#f38ba8")
                fail_count += 1
            for w in self._warnings:
                self._append("  ! " + w, "#f9e2af")

        self._stats.setText(tr("converter.stats.done", ok=ok_count, fail=fail_count, total=len(self._files)))
        self._append("")
        self._append(tr("converter.log.finished", ok=ok_count, fail=fail_count), "#a6e3a1", bold=True)

    def _patch_json_for_target(self, json_path: str, target: str, out_path: str,
                               label: str = "") -> None:
        """Write *json_path* patched for *target* to *out_path*.

        The JSON patch is only applied when the target is older than the
        source; its warnings are collected in ``self._warnings``.
        """
        data = load_spine_json(json_path)
        src_norm = _normalize_version(_read_source_version(data))
        dest_norm = _normalize_version(target)
        if src_norm != "unknown" and _RANK.get(src_norm, 99) > _RANK.get(dest_norm, 99):
            data, warnings = convert_to_destination(data, target)
        elif src_norm == "unknown" and _RANK.get(dest_norm, 99) <= 3:
            data, warnings = convert_to_destination(data, target)
        else:
            warnings = []
        prefix = f"[{label}] " if label else ""
        self._warnings.extend(prefix + w for w in warnings)
        save_spine_json(out_path, data)

    def _convert_spine_file(self, exe: str, path: str, target: str) -> str:
        """Convert .spine -> .spine at target version.

        Flow: export every skeleton to JSON in tmp -> patch the JSON for the
        target version (when older than the source) -> import every skeleton
        into the output project at the target version.
        (Untested against a real Spine.)
        """
        tmp_dir = tempfile.mkdtemp(prefix="ssk_conv_")
        try:
            # Step 1: export .spine -> JSON (all skeletons)
            result = export_spine_project(exe, path, tmp_dir, pack=False,
                                          on_output=self._cli_progress())
            if not result.success:
                raise RuntimeError(result.stderr or "Export failed")

            json_paths = list(getattr(result, "output_jsons", None) or [])
            if not json_paths and result.output_json:
                json_paths = [result.output_json]
            if not json_paths:
                raise RuntimeError("Export produced no JSON")

            # Step 2: patch + import every skeleton at the target version
            stem = Path(path).stem
            out_path = str(Path(path).parent / f"{stem}_{target}.spine")
            multi = len(json_paths) > 1
            for jp in json_paths:
                label = Path(jp).stem if multi else ""
                patched = str(Path(tmp_dir) / ("patched_" + Path(jp).name))
                self._patch_json_for_target(jp, target, patched, label)
                result2 = import_to_spine_project(
                    exe, patched, out_path, target_version=target,
                    on_output=self._cli_progress(),
                )
                if not result2.success:
                    raise RuntimeError(result2.stderr or result2.stdout or "Import failed")

            return out_path
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _convert_json_file(self, exe: str, path: str, target: str) -> str:
        """Convert .json -> .json at target version.

        Flow: patch JSON for target version -> import to temp .spine
        at target -> export back to JSON.
        """
        tmp_dir = tempfile.mkdtemp(prefix="ssk_conv_")
        try:
            # Step 0: patch JSON for target version compatibility
            patched_json = str(Path(tmp_dir) / "patched.json")
            data = load_spine_json(path)
            data, warnings = convert_to_destination(data, target)
            self._warnings.extend(warnings)
            save_spine_json(patched_json, data)

            # Step 1: import patched JSON -> temp .spine at target version
            tmp_spine = str(Path(tmp_dir) / "temp.spine")
            result = import_to_spine_project(
                exe, patched_json, tmp_spine, target_version=target,
                on_output=self._cli_progress(),
            )
            if not result.success:
                raise RuntimeError(result.stderr or "Import failed")

            # Step 2: export temp .spine -> JSON
            export_dir = str(Path(tmp_dir) / "export")
            result2 = export_spine_project(exe, tmp_spine, export_dir, pack=False,
                                           on_output=self._cli_progress())
            if not result2.success:
                raise RuntimeError(result2.stderr or "Export failed")

            if not result2.output_json:
                raise RuntimeError("Export produced no JSON")

            # Step 3: copy to output
            stem = Path(path).stem
            out_path = str(Path(path).parent / f"{stem}_{target}.json")
            shutil.copy2(result2.output_json, out_path)
            return out_path
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


# Backward-compatible alias — keeps existing imports in app.py working
SpineDowngraderTab = SpineVersionConverterTab
