"""Image Downscaler tab — downscale images via _DOWNSCALE/<percent>/ folders, update JSON."""

from __future__ import annotations

import os

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QLineEdit,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QHeaderView, QMessageBox,
)
from PySide6.QtCore import Qt

from .i18n import tr, language_changed
from .spine_json import load_spine_json
from .downscale_core import (  # noqa: F401  (re-exported)
    IMAGE_EXTS, Image, resize_image, scan_downscale_folder, build_plan,
    process_plan, move_to_originals,
)


class OptimizerTab:
    def __init__(self, tabs: QTabWidget, get_config, on_modified=None):
        self._get_config = get_config
        self._on_modified = on_modified
        self._tabs = tabs
        self._opt_plan = []

        self._page = QWidget()
        tabs.addTab(self._page, tr("optimizer.tab"))
        layout = QVBoxLayout(self._page)
        layout.setContentsMargins(5, 5, 5, 5)

        self._info = QLabel(tr("optimizer.info"))
        self._info.setWordWrap(True)
        layout.addWidget(self._info)

        btn_row = QHBoxLayout()
        self._folders_label = QLabel(tr("optimizer.folders_label"))
        btn_row.addWidget(self._folders_label)
        self._folders_edit = QLineEdit("20, 25, 50")
        self._folders_edit.setFixedWidth(120)
        btn_row.addWidget(self._folders_edit)
        self._create_btn = QPushButton(tr("optimizer.create_btn"))
        self._create_btn.clicked.connect(self._create_downscale_folder)
        btn_row.addWidget(self._create_btn)
        self._analyze_btn = QPushButton(tr("optimizer.analyze_btn"))
        self._analyze_btn.clicked.connect(self._analyze)
        btn_row.addWidget(self._analyze_btn)
        self._process_btn = QPushButton(tr("optimizer.process_btn"))
        self._process_btn.setEnabled(False)
        self._process_btn.clicked.connect(self._process)
        btn_row.addWidget(self._process_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._stats = QLabel(tr("optimizer.default_stats"))
        self._stats.setStyleSheet("font-weight: bold; color: #6ec072;")
        layout.addWidget(self._stats)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels([
            tr("optimizer.tree.image"), tr("optimizer.tree.scale"),
            tr("optimizer.tree.current"), tr("optimizer.tree.new"),
            tr("optimizer.tree.orig_size"), tr("optimizer.tree.new_size"),
        ])
        self._tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, 6):
            self._tree.header().setSectionResizeMode(i, QHeaderView.ResizeToContents)
        layout.addWidget(self._tree, 1)

        language_changed.connect(self._retranslate)

    def _retranslate(self):
        idx = self._tabs.indexOf(self._page)
        if idx >= 0:
            self._tabs.setTabText(idx, tr("optimizer.tab"))
        self._info.setText(tr("optimizer.info"))
        self._folders_label.setText(tr("optimizer.folders_label"))
        self._create_btn.setText(tr("optimizer.create_btn"))
        self._analyze_btn.setText(tr("optimizer.analyze_btn"))
        self._process_btn.setText(tr("optimizer.process_btn"))
        self._stats.setText(tr("optimizer.default_stats"))
        self._tree.setHeaderLabels([
            tr("optimizer.tree.image"), tr("optimizer.tree.scale"),
            tr("optimizer.tree.current"), tr("optimizer.tree.new"),
            tr("optimizer.tree.orig_size"), tr("optimizer.tree.new_size"),
        ])

    def _create_downscale_folder(self):
        images_dir = self._get_config("images")
        if not images_dir or not os.path.isdir(images_dir):
            QMessageBox.critical(None, tr("err.title"), tr("optimizer.err.no_images"))
            return
        raw = self._folders_edit.text()
        percentages = []
        for part in raw.replace(",", " ").split():
            try:
                val = int(part.strip())
                if 0 < val < 100:
                    percentages.append(val)
            except ValueError:
                pass
        if not percentages:
            QMessageBox.critical(None, tr("err.title"), tr("optimizer.err.bad_pct"))
            return
        ds_dir = os.path.join(images_dir, "_DOWNSCALE")
        for pct in percentages:
            os.makedirs(os.path.join(ds_dir, str(pct)), exist_ok=True)
        folder_list = "\n  ".join(str(p) + "/" for p in sorted(percentages))
        QMessageBox.information(None, tr("created.title"),
            tr("optimizer.created", dir=ds_dir, list=folder_list))

    def _analyze(self):
        json_path = self._get_config("json")
        images_dir = self._get_config("images")
        if not json_path or not os.path.isfile(json_path):
            QMessageBox.critical(None, tr("err.title"), tr("err.no_json"))
            return
        if not images_dir or not os.path.isdir(images_dir):
            QMessageBox.critical(None, tr("err.title"), tr("err.no_images"))
            return

        ds_dir = os.path.join(images_dir, "_DOWNSCALE")
        if not os.path.isdir(ds_dir):
            QMessageBox.critical(None, tr("err.title"), tr("optimizer.err.no_downscale", path=ds_dir))
            return

        ds_map = scan_downscale_folder(ds_dir)
        if not ds_map:
            QMessageBox.information(None, tr("empty.title"), tr("optimizer.empty"))
            return

        try:
            spine_data = load_spine_json(json_path)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("err.parse_json", error=e))
            return

        self._opt_plan = build_plan(ds_dir, images_dir, spine_data)
        warnings = []
        for item in self._opt_plan:
            orig_w, orig_h = item["orig_w"], item["orig_h"]
            item["orig_size"] = f"{orig_w}x{orig_h}" if orig_w else "?x?"
            item["new_size"] = f"{item['new_w']}x{item['new_h']}" if orig_w else "?x?"
            inv_factor = item["inv_factor"]
            if not item["matches"]:
                warnings.append(tr("optimizer.no_attachment", key=item["image_key"]))
            region_scales = [(ad.get("scaleX", 1), ad.get("scaleY", 1))
                             for _, _, _, ad in item["matches"]
                             if ad.get("type") in (None, "region")]
            if region_scales:
                rep_sx, rep_sy = region_scales[0]
                item["cur_scale_str"] = f"{rep_sx}, {rep_sy}"
                item["new_scale_str"] = f"{round(rep_sx * inv_factor, 4)}, {round(rep_sy * inv_factor, 4)}"
            elif item["matches"]:  # mesh only: geometry is independent of image size
                item["cur_scale_str"] = item["new_scale_str"] = "-"
            else:
                item["cur_scale_str"] = "1, 1"
                item["new_scale_str"] = f"{inv_factor}, {inv_factor}"

        self._tree.clear()
        for item in self._opt_plan:
            QTreeWidgetItem(self._tree, [
                item["image_key"], f"{item['percent']}%",
                item["cur_scale_str"], item["new_scale_str"],
                item["orig_size"], item["new_size"],
            ])

        total = len(self._opt_plan)
        matched = sum(1 for p in self._opt_plan if p["matches"])
        self._stats.setText(tr("optimizer.stats", total=total, matched=matched, no_match=total - matched))

        if warnings:
            QMessageBox.warning(None, tr("warnings.title"),
                tr("optimizer.warn.no_match", count=len(warnings)) + "\n\n"
                + "\n".join(warnings[:20])
                + ("\n..." if len(warnings) > 20 else ""))

        self._process_btn.setEnabled(bool(self._opt_plan))

    def _process(self):
        if not self._opt_plan:
            return
        if Image is None:
            QMessageBox.critical(None, tr("err.title"), tr("optimizer.err.no_pillow"))
            return

        json_path = self._get_config("json")
        count = len(self._opt_plan)

        if QMessageBox.question(None, tr("confirm.title"),
            tr("optimizer.confirm.resize", count=count)) != QMessageBox.Yes:
            return

        images_dir = self._get_config("images")
        try:
            res = process_plan(json_path, images_dir, self._opt_plan)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("err.load_backup", error=e))
            return
        if not res["json_saved"]:
            QMessageBox.critical(
                None, tr("err.title"),
                tr("err.save_json", error=res["save_error"]) + "\n\n"
                + tr("optimizer.sources_kept"))
            return
        resized, json_updated, errors = res["resized"], res["json_updated"], res["errors"]

        ds_dir = os.path.join(images_dir, "_DOWNSCALE")
        for dirpath, _, _ in os.walk(ds_dir, topdown=False):
            if not os.listdir(dirpath) and dirpath != ds_dir:
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass

        msg = tr("optimizer.done", resized=resized, total=count, json_updated=json_updated, backup=json_path + ".backup")
        msg += "\n\n" + tr("optimizer.originals_moved", count=res["moved"],
                            dir=os.path.join(images_dir, "_ORIGINALS"))
        if errors:
            msg += f"\n\nErrors ({len(errors)}):\n" + "\n".join(errors[:10])
        QMessageBox.information(None, tr("done.title"), msg)
        self._opt_plan = []
        self._tree.clear()
        self._process_btn.setEnabled(False)
        self._stats.setText(tr("optimizer.done_stats"))
        if self._on_modified:
            self._on_modified()
