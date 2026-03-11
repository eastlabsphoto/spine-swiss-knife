"""Image Downscaler tab — downscale images via _DOWNSCALE/<percent>/ folders, update JSON."""

import os
import shutil

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QLineEdit,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QHeaderView, QMessageBox,
)
from PySide6.QtCore import Qt

from .i18n import tr, language_changed
from .spine_json import (
    load_spine_json, save_spine_json, normalize_skins,
    iter_region_attachments, attachment_image_key,
)

try:
    from PIL import Image
except ImportError:
    Image = None

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tga", ".webp"}


def resize_image(src_path: str, dst_path: str, scale_percent: int):
    if Image is None:
        raise RuntimeError("Pillow (PIL) is not installed. Run: pip3 install Pillow")
    img = Image.open(src_path)
    factor = scale_percent / 100.0
    new_w = max(1, round(img.width * factor))
    new_h = max(1, round(img.height * factor))
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    resized.save(dst_path)
    return new_w, new_h


def scan_downscale_folder(downscale_dir: str) -> dict[int, list[str]]:
    result = {}
    if not os.path.isdir(downscale_dir):
        return result
    for entry in sorted(os.listdir(downscale_dir)):
        folder_path = os.path.join(downscale_dir, entry)
        if not os.path.isdir(folder_path):
            continue
        try:
            percent = int(entry)
        except ValueError:
            continue
        if percent <= 0 or percent >= 100:
            continue
        images = []
        for root, dirs, files in os.walk(folder_path):
            for fname in files:
                if os.path.splitext(fname)[1].lower() in IMAGE_EXTS:
                    images.append(os.path.join(root, fname))
        if images:
            result[percent] = sorted(images)
    return result


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

        att_lookup = {}
        skins = normalize_skins(spine_data.get("skins", {}))
        for skin_name, slot_name, att_name, att_data in iter_region_attachments(skins):
            key = attachment_image_key(att_name, att_data)
            att_lookup.setdefault(key, []).append((skin_name, slot_name, att_name, att_data))

        self._opt_plan = []
        warnings = []

        for percent, file_list in sorted(ds_map.items()):
            inv_factor = 100.0 / percent
            pct_dir = os.path.join(ds_dir, str(percent))
            for src_path in file_list:
                rel_to_pct = os.path.relpath(src_path, pct_dir)
                image_key = os.path.splitext(rel_to_pct)[0]
                dst_path = os.path.join(images_dir, rel_to_pct)

                orig_w, orig_h = "?", "?"
                if Image:
                    try:
                        with Image.open(src_path) as img:
                            orig_w, orig_h = img.width, img.height
                    except Exception:
                        pass

                new_w = max(1, round(orig_w * percent / 100)) if isinstance(orig_w, int) else "?"
                new_h = max(1, round(orig_h * percent / 100)) if isinstance(orig_h, int) else "?"

                matches = att_lookup.get(image_key, [])
                if not matches:
                    for k, v in att_lookup.items():
                        if k.lower() == image_key.lower():
                            matches = v
                            break

                current_scales = set()
                for _, _, _, ad in matches:
                    current_scales.add((ad.get("scaleX", 1), ad.get("scaleY", 1)))

                if not matches:
                    warnings.append(tr("optimizer.no_attachment", key=image_key))

                if current_scales:
                    rep_sx, rep_sy = list(current_scales)[0]
                    cur_str = f"{rep_sx}, {rep_sy}"
                    new_str = f"{round(rep_sx * inv_factor, 4)}, {round(rep_sy * inv_factor, 4)}"
                else:
                    cur_str = "1, 1"
                    new_str = f"{inv_factor}, {inv_factor}"

                self._opt_plan.append({
                    "src_path": src_path, "dst_path": dst_path, "image_key": image_key,
                    "percent": percent, "inv_factor": inv_factor, "matches": matches,
                    "orig_size": f"{orig_w}x{orig_h}", "new_size": f"{new_w}x{new_h}",
                    "cur_scale_str": cur_str, "new_scale_str": new_str,
                })

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

        try:
            shutil.copy2(json_path, json_path + ".backup")
            spine_data = load_spine_json(json_path)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("err.load_backup", error=e))
            return

        skins = normalize_skins(spine_data.get("skins", {}))
        att_lookup = {}
        for skin_name, slot_name, att_name, att_data in iter_region_attachments(skins):
            key = attachment_image_key(att_name, att_data)
            att_lookup.setdefault(key, []).append((skin_name, slot_name, att_name, att_data))

        resized = json_updated = 0
        errors = []
        for item in self._opt_plan:
            try:
                new_w, new_h = resize_image(item["src_path"], item["dst_path"], item["percent"])
                resized += 1
            except Exception as e:
                errors.append(f"Resize {item['image_key']}: {e}")
                continue

            matches = att_lookup.get(item["image_key"], [])
            if not matches:
                for k, v in att_lookup.items():
                    if k.lower() == item["image_key"].lower():
                        matches = v
                        break

            inv = item["inv_factor"]
            for _, _, _, att_data in matches:
                att_data["scaleX"] = round(att_data.get("scaleX", 1) * inv, 6)
                att_data["scaleY"] = round(att_data.get("scaleY", 1) * inv, 6)
                att_data["width"] = new_w
                att_data["height"] = new_h
                json_updated += 1

            try:
                os.remove(item["src_path"])
            except OSError:
                pass

        try:
            save_spine_json(json_path, spine_data)
        except Exception as e:
            QMessageBox.critical(None, tr("err.title"), tr("err.save_json", error=e))
            return

        images_dir = self._get_config("images")
        ds_dir = os.path.join(images_dir, "_DOWNSCALE")
        for dirpath, _, _ in os.walk(ds_dir, topdown=False):
            if not os.listdir(dirpath) and dirpath != ds_dir:
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass

        msg = tr("optimizer.done", resized=resized, total=count, json_updated=json_updated, backup=json_path + ".backup")
        if errors:
            msg += f"\n\nErrors ({len(errors)}):\n" + "\n".join(errors[:10])
        QMessageBox.information(None, tr("done.title"), msg)
        self._opt_plan = []
        self._tree.clear()
        self._process_btn.setEnabled(False)
        self._stats.setText(tr("optimizer.done_stats"))
        if self._on_modified:
            self._on_modified()
