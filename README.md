<p align="center">
  <img src="Logo/logo.png" width="128" alt="Spine Swiss Knife">
</p>

<h1 align="center">Spine Swiss Knife</h1>

<p align="center">
  Desktop tool for optimizing and managing <a href="http://esotericsoftware.com/">Spine 2D</a> animation assets.<br>
  Built with PySide6. Supports Spine 3.x / 4.x JSON format.
</p>

---

## Tools

The sidebar offers these tools (in this order). Every tool that changes a JSON writes a backup first.

| Tool | Description |
|------|-------------|
| **Project Analyzer** | Overview of the skeleton: bones, slots, skins, animations, attachments |
| **Image Downscaler** | Batch-resize images and update the JSON (width/height, scale). Processed source images are moved to `<images>/_ORIGINALS/` (never deleted) |
| **Rect Masks** | Snap near-rectangular clipping masks to exact rectangles |
| **Polygon Simplifier** | Reduce the vertex count of polygon clipping masks with a per-mask tolerance |
| **Keyframe Optimizer** | Find and remove redundant keyframes (value stays within a tolerance of the interpolation) |
| **Dead Bones** | Find bones used by no slot, constraint, animation, skin or mesh weight and remove them (weighted meshes are remapped) |
| **Hidden Attachments** | Find slots with active attachments that are never visible (alpha 0 / zero-scale bone) and clear them in the setup pose |
| **Blend Switcher** | Detect and fix problematic blend modes |
| **Draw Order Optimizer** | Reorder slots to reduce draw calls; verifies all skins and needs an atlas |
| **Slot Renamer** | Add a blend-mode suffix to slot names (all references are updated) |
| **Animation Splitter** | Split a multi-animation skeleton into separate JSON files by group; optional "include static visible slots" |
| **Version Converter** | Convert between Spine versions (3.x / 4.x); downgrades are lossy and list their warnings |
| **Texture Unpacker** | Extract individual sprites from an atlas |
| **Static Image Exporter** | Export frame 0 of an animation as PNG (trim / center / fixed canvas) with optional blur variant (needs Spine) |
| **Image Flattener** | Move images from sub-folders into the images root and update the JSON |
| **Merge Skeletons** | Merge several skeletons (bones, slots, skins, animations, atlas pages) into one |
| **Skeleton Viewer** | Built-in preview with skin/animation selection, playback and bone overlay |
| **Unused Images** | Compare images on disk with the atlas and/or the JSON references and move unused ones to `_UNUSED` |
| **Claude / MCP** | Shows the setup snippet for connecting Claude to the headless tools (see below) |

## Requirements

- Python 3.10+
- PySide6, Pillow, numpy, certifi (`pip install -r requirements.txt`)
- Spine installed for the features that call the Spine command line (`.spine` projects, export, static images). JSON-only work needs no Spine.

## Usage

```bash
python -m spine_swiss_knife      # or: python spine_swiss_knife.py
```

Two ways to start:

- **`.spine` project mode**: pick a project; the tool exports it with the Spine CLI, you optimize the exported JSON/atlas/images, and the result can be imported back. A project with several skeletons shows a skeleton picker; the multi-skeleton import-back is rebuilt from the exports.
- **JSON mode**: pick a skeleton JSON (plus atlas and images folder) directly.

Set the paths in the config panel on top; tabs analyze on load and reload after every change.

### Backups and validation

- Before every modification the tool writes `<file>.json.backup` (state before the last change) and a timestamped copy in `_ssk_backups/` next to the file (the last 20 are kept).
- The **Restore backup...** button brings back the `.backup` or any history entry.
- A validator status line shows structural problems of the current JSON (broken bone/slot references, bad draw-order offsets, missing atlas regions, ...).

## Headless API / CLI / MCP

No GUI needed (PySide6 is not imported). Tools marked "Spine" need the Spine executable; the JSON tools do not. Everything that writes is a dry-run unless `apply` is set, writes a backup first, and validates the result (if applying would introduce validation errors the file is restored and an error is returned).

| Tool | What it does |
|------|--------------|
| `project_info` | Skeletons, animations, skins, bone/slot counts of a `.spine` (Spine) |
| `export_json` | Export every skeleton to compact JSON + packed atlas (Spine) |
| `check_regions` | Verify every region/mesh attachment exists in the atlas |
| `fix_attachment_name` | Rename an attachment in an exported JSON |
| `export_static` | Frame 0 of an animation as PNG, optional blur (Spine, Pillow) |
| `validate` | Structural validation of a JSON (optionally against an atlas) |
| `optimize_keyframes` | Remove redundant keyframes (`tolerance`, default 0.1) |
| `remove_dead_bones` | Remove unused bones |
| `fix_hidden_attachments` | Clear never-visible setup attachments |
| `snap_rect_masks` | Snap near-rectangular clipping masks (`tolerance`, default 10) |
| `unused_images` | Report images on disk not referenced by JSON/atlas (read-only) |
| `report` | Read-only overview: counts, validation, and what each optimizer would save |
| `batch` | Run one JSON tool on every skeleton JSON in a folder (`tool`, `recursive`, `apply`) |

```bash
# CLI (prints JSON)
python -m spine_swiss_knife.cli report --json-path joker.json
python -m spine_swiss_knife.cli optimize-keyframes --json-path joker.json --tolerance 0.1 --apply
python -m spine_swiss_knife.cli batch --folder exports --tool remove_dead_bones

# MCP server (stdio, no extra dependencies, Python 3.9+)
claude mcp add spine-swiss-knife -- python3 -m spine_swiss_knife.mcp_server
```

### Using the released app as the MCP server

- **macOS:** `SpineSwissKnife.app/Contents/MacOS/SpineSwissKnife --mcp` (also `--cli <tool> ...`)
- **Windows:** `SpineSwissKnifeMCP.exe` next to `SpineSwissKnife.exe` (the GUI exe is windowed and has no stdio)

The **Claude / MCP** tool in the app shows the exact config for Claude Desktop and the `claude mcp add` command for this installation.

## Known limitations

- The bundled Java viewer (`GTSpineViewer_3653`, "Open Full Version") only supports Spine 3.6.53 and needs a Java runtime.
- The built-in viewer does not evaluate transform, path or physics constraints, so those poses can differ from Spine.
- JSON downgrade (4.x to 3.8) is lossy: unsupported features are dropped and reported as warnings.
- Multi-skeleton `.spine` import-back is rebuilt from the exports, not edited in place.
- The language is English only.

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests
```

CI runs the tests on every push and pull request; release builds (PyInstaller, macOS and Windows) only run after they pass.

## Project structure

`*_core.py` modules are pure logic (no Qt) shared by the GUI tabs and the headless API.

```
spine_swiss_knife/
  Shared pure logic
    spine_json.py       # load/save, versions, skins, backups, image keys
    atlas_parser.py     # atlas parsing, image collection
    spine_refs.py       # bone/slot references, draw-order offsets
    spine_validate.py   # skeleton validator
    spine_cli.py        # Spine command-line wrapper
    *_core.py           # keyframe, dead_bones, hidden_attachments, mask, downscale,
                        # splitter, merge, flatten, unused, draw_order, static_exporter
  Headless
    api.py  cli.py  mcp_server.py  launcher.py
  GUI
    app.py welcome_page.py style.py i18n.py settings.py updater.py debug_log.py
    project_analyzer.py optimizer.py mask_optimizer.py polygon_simplify.py
    keyframe_optimizer.py dead_bones.py hidden_attachments.py blend_switcher.py
    draw_order_optimizer.py slot_renamer.py splitter.py spine_downgrader.py
    texture_unpacker.py static_exporter.py image_flattener.py merge_skeletons.py
    spine_viewer*.py unused_finder.py mcp_setup.py
  locales/en.json
  resources/            # icons
GTSpineViewer_3653/     # bundled Java skeleton viewer
Logo/
tests/
```

## License

Internal tool — GreentubeSK.
