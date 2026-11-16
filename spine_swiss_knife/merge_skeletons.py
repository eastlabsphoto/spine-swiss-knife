"""Merge Skeletons — combine multiple Spine JSON files into one project."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QLineEdit,
    QTabWidget, QTextEdit, QMessageBox, QTreeWidget, QTreeWidgetItem,
    QFileDialog, QHeaderView, QAbstractItemView, QApplication,
    QProgressDialog,
)
from PySide6.QtGui import QTextCharFormat, QColor, QFont, QTextCursor
from PySide6.QtCore import Qt

from .i18n import tr, language_changed
from .spine_json import load_spine_json, save_spine_json
from .merge_core import (  # noqa: F401 - re-exported for existing importers
    merge_image_dirs as _core_merge_image_dirs,
    merge_spine_data as _core_merge_spine_data,
    _file_hash, _rename_slots_in_animation, _rename_events_in_animation,
    _apply_image_renames_to_skins, _unique_name,
)
from .spine_cli import (
    read_spine_file_version, export_spine_project,
)
from .settings import settings


# ==========================================================================
# Core wrappers (pure logic lives in merge_core; warnings are translated here)
# ==========================================================================

def _translate_warning(w) -> str:
    code, params = w
    return tr("merge.warn." + code, **params)


def merge_image_dirs(
    source_dirs: list[str],
    source_labels: list[str],
    output_dir: str,
) -> tuple[list[dict[str, str]], list[str]]:
    """Merge image directories into *output_dir* (translated warnings)."""
    maps, warnings = _core_merge_image_dirs(source_dirs, source_labels, output_dir)
    return maps, [_translate_warning(w) for w in warnings]


def merge_spine_data(
    sources: list[dict],
    source_labels: list[str],
    output_name: str,
    image_rename_maps: list[dict[str, str]] | None = None,
) -> tuple[dict, list[str]]:
    """Merge multiple Spine JSON dicts into one (translated warnings)."""
    merged, warnings = _core_merge_spine_data(
        sources, source_labels, output_name, image_rename_maps)
    return merged, [_translate_warning(w) for w in warnings]


# ==========================================================================
# UI Tab
# ==========================================================================

class MergeSkeletonsTab:
    def __init__(self, tabs: QTabWidget, get_config):
        self._get_config = get_config
        self._tabs = tabs
        self._files: list[dict] = []  # {path, type, version, images_dir, item}

        self._page = QWidget()
        tabs.addTab(self._page, tr("merge.tab"))
        layout = QVBoxLayout(self._page)
        layout.setContentsMargins(5, 5, 5, 5)

        # Info
        self._info = QLabel(tr("merge.info"))
        self._info.setWordWrap(True)
        layout.addWidget(self._info)

        # Toolbar
        toolbar = QHBoxLayout()
        self._btn_add = QPushButton(tr("merge.add_files"))
        self._btn_add.setCursor(Qt.PointingHandCursor)
        self._btn_add.clicked.connect(self._add_files)
        toolbar.addWidget(self._btn_add)
        self._btn_remove = QPushButton(tr("merge.remove_selected"))
        self._btn_remove.setCursor(Qt.PointingHandCursor)
        self._btn_remove.clicked.connect(self._remove_selected)
        toolbar.addWidget(self._btn_remove)
        self._btn_clear = QPushButton(tr("merge.clear_all"))
        self._btn_clear.setCursor(Qt.PointingHandCursor)
        self._btn_clear.clicked.connect(self._clear_all)
        toolbar.addWidget(self._btn_clear)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        # File list
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels([
            tr("merge.col.filename"),
            tr("merge.col.type"),
            tr("merge.col.version"),
            tr("merge.col.images"),
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

        # Config row: skeleton name + output dir
        config_row = QHBoxLayout()
        self._name_label = QLabel(tr("merge.name_label"))
        config_row.addWidget(self._name_label)
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText(tr("merge.name_placeholder"))
        self._name_edit.setMaximumWidth(250)
        config_row.addWidget(self._name_edit)
        self._output_label = QLabel(tr("merge.output_label"))
        config_row.addWidget(self._output_label)
        self._output_edit = QLineEdit()
        self._output_edit.setPlaceholderText(tr("merge.output_placeholder"))
        config_row.addWidget(self._output_edit, 1)
        self._btn_output = QPushButton(tr("app.browse"))
        self._btn_output.clicked.connect(self._browse_output)
        config_row.addWidget(self._btn_output)
        layout.addLayout(config_row)

        # Merge button + stats
        bottom = QHBoxLayout()
        self._merge_btn = QPushButton(tr("merge.merge_btn"))
        self._merge_btn.setProperty("role", "primary")
        self._merge_btn.setCursor(Qt.PointingHandCursor)
        self._merge_btn.clicked.connect(self._merge)
        bottom.addWidget(self._merge_btn)
        bottom.addStretch()
        layout.addLayout(bottom)

        self._stats = QLabel(tr("merge.default_stats"))
        self._stats.setStyleSheet("font-weight: bold; color: #6ec072;")
        layout.addWidget(self._stats)

        # Log
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setFont(QFont("monospace", 10))
        self._log.setMaximumHeight(180)
        layout.addWidget(self._log)

        language_changed.connect(self._retranslate)

    # ── i18n ──

    def _retranslate(self):
        idx = self._tabs.indexOf(self._page)
        if idx >= 0:
            self._tabs.setTabText(idx, tr("merge.tab"))
        self._info.setText(tr("merge.info"))
        self._btn_add.setText(tr("merge.add_files"))
        self._btn_remove.setText(tr("merge.remove_selected"))
        self._btn_clear.setText(tr("merge.clear_all"))
        self._tree.setHeaderLabels([
            tr("merge.col.filename"),
            tr("merge.col.type"),
            tr("merge.col.version"),
            tr("merge.col.images"),
        ])
        self._name_label.setText(tr("merge.name_label"))
        self._name_edit.setPlaceholderText(tr("merge.name_placeholder"))
        self._output_label.setText(tr("merge.output_label"))
        self._output_edit.setPlaceholderText(tr("merge.output_placeholder"))
        self._btn_output.setText(tr("app.browse"))
        self._merge_btn.setText(tr("merge.merge_btn"))
        self._stats.setText(tr("merge.default_stats"))

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
            None, tr("merge.dialog.add_files"), "",
            tr("merge.filter"),
        )
        for path in paths:
            # Skip duplicates
            if any(f["path"] == path for f in self._files):
                continue
            self._add_file(path)

    def _add_file(self, path: str):
        p = Path(path)
        ftype = p.suffix.lstrip(".")
        version = ""
        images_dir = ""

        if ftype == "spine":
            version = read_spine_file_version(path) or "?"
        elif ftype == "json":
            try:
                data = load_spine_json(path)
                version = data.get("skeleton", {}).get("spine", "?")
            except Exception:
                version = "?"

        # Auto-detect images dir
        candidate = p.parent / "images"
        if candidate.is_dir():
            images_dir = str(candidate)

        item = QTreeWidgetItem(self._tree, [
            p.name, ftype, version,
            Path(images_dir).name if images_dir else "—",
        ])
        self._files.append({
            "path": path,
            "type": ftype,
            "version": version,
            "images_dir": images_dir,
            "label": p.stem,
            "item": item,
        })

    def _remove_selected(self):
        selected = self._tree.selectedItems()
        for sel in selected:
            for i, f in enumerate(self._files):
                if f["item"] is sel:
                    self._files.pop(i)
                    break
            idx = self._tree.indexOfTopLevelItem(sel)
            if idx >= 0:
                self._tree.takeTopLevelItem(idx)

    def _clear_all(self):
        self._files.clear()
        self._tree.clear()

    def _browse_output(self):
        path = QFileDialog.getExistingDirectory(None, tr("merge.dialog.select_output"))
        if path:
            self._output_edit.setText(path)

    # ── Merge ──

    def _merge(self):
        self._clear_log()

        if len(self._files) < 2:
            QMessageBox.warning(None, tr("err.title"), tr("merge.err.need_files"))
            return

        output_name = self._name_edit.text().strip()
        if not output_name:
            QMessageBox.warning(None, tr("err.title"), tr("merge.err.no_name"))
            return

        output_dir = self._output_edit.text().strip()
        if not output_dir:
            QMessageBox.warning(None, tr("err.title"), tr("merge.err.no_output"))
            return

        # Validate versions match
        versions = set()
        for f in self._files:
            v = f["version"]
            if v and v != "?":
                versions.add(v)
        if len(versions) > 1:
            QMessageBox.warning(
                None, tr("err.title"),
                tr("merge.err.version_mismatch",
                   versions=", ".join(sorted(versions))))
            return

        exe = settings.spine_executable() or ""

        # Step 1: Load all JSONs (export .spine files first)
        self._append(tr("merge.log.start"), bold=True)
        json_datas: list[dict] = []
        labels: list[str] = []
        images_dirs: list[str] = []
        temp_dirs: list[str] = []

        for f in self._files:
            label = f["label"]
            labels.append(label)

            if f["type"] == "spine":
                if not exe:
                    QMessageBox.warning(
                        None, tr("err.title"), tr("merge.err.no_exe"))
                    self._cleanup_temps(temp_dirs)
                    return

                self._append(tr("merge.log.exporting", name=Path(f["path"]).name))
                QApplication.processEvents()

                temp_dir = tempfile.mkdtemp(
                    prefix="ssk_merge_%s_" % "".join(
                        ch if ch.isalnum() or ch in "-_" else "_" for ch in label))
                temp_dirs.append(temp_dir)

                result = export_spine_project(
                    exe, f["path"], temp_dir, pack=False,
                )
                if not result.success:
                    error = result.stderr or result.stdout or "Unknown error"
                    self._append(f"ERROR: {error}", color="#f38ba8")
                    QMessageBox.warning(
                        None, tr("err.title"),
                        tr("merge.err.export_failed",
                           name=Path(f["path"]).name, error=error))
                    self._cleanup_temps(temp_dirs)
                    return

                try:
                    data = load_spine_json(result.output_json)
                except Exception as e:
                    self._append(f"ERROR: {e}", color="#f38ba8")
                    self._cleanup_temps(temp_dirs)
                    return
                json_datas.append(data)

                # Use source .spine file's images dir (not the export)
                images_dirs.append(f["images_dir"])
            else:
                self._append(tr("merge.log.loading", name=Path(f["path"]).name))
                QApplication.processEvents()
                try:
                    data = load_spine_json(f["path"])
                except Exception as e:
                    self._append(f"ERROR: {e}", color="#f38ba8")
                    QMessageBox.warning(
                        None, tr("err.title"),
                        tr("merge.err.load_failed",
                           name=Path(f["path"]).name, error=str(e)))
                    self._cleanup_temps(temp_dirs)
                    return
                json_datas.append(data)
                images_dirs.append(f["images_dir"])

        # Step 2: Merge images
        output_images = str(Path(output_dir) / "images")
        self._append(tr("merge.log.merging_images"))
        QApplication.processEvents()

        image_rename_maps, img_warnings = merge_image_dirs(
            images_dirs, labels, output_images,
        )
        for w in img_warnings:
            self._append(f"  {w}", color="#f9e2af")

        # Step 3: Merge JSON
        self._append(tr("merge.log.merging_json"))
        QApplication.processEvents()

        merged, merge_warnings = merge_spine_data(
            json_datas, labels, output_name, image_rename_maps,
        )
        for w in merge_warnings:
            self._append(f"  {w}", color="#f9e2af")

        # Step 4: Save
        output_json = str(Path(output_dir) / f"{output_name}.json")
        try:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            save_spine_json(output_json, merged)
        except Exception as e:
            self._append(f"ERROR: {e}", color="#f38ba8")
            QMessageBox.warning(None, tr("err.title"), str(e))
            self._cleanup_temps(temp_dirs)
            return

        # Cleanup temp dirs
        self._cleanup_temps(temp_dirs)

        total_warnings = len(img_warnings) + len(merge_warnings)
        self._append(
            tr("merge.log.done",
               output=output_json,
               warnings=total_warnings),
            color="#a6e3a1", bold=True)

        self._stats.setText(
            tr("merge.stats.done",
               sources=len(self._files),
               bones=len(merged.get("bones", [])),
               slots=len(merged.get("slots", [])),
               anims=len(merged.get("animations", {})),
               warnings=total_warnings))

        QMessageBox.information(
            None, tr("done.title"),
            tr("merge.done",
               output=output_json,
               warnings=total_warnings))

    def _cleanup_temps(self, temp_dirs: list[str]):
        for d in temp_dirs:
            shutil.rmtree(d, ignore_errors=True)
