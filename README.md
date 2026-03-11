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

| Tool | Description |
|------|-------------|
| **Project Analyzer** | Overview of skeleton structure — bones, slots, skins, animations, attachments |
| **Image Downscaler** | Batch-resize spine images and update JSON references (width/height) |
| **Rect Mask Optimizer** | Detect clipping masks that can be snapped to bounding boxes and fix them |
| **Polygon Simplifier** | Reduce vertex count on polygon clipping masks with per-mask tolerance |
| **Keyframe Optimizer** | Find and remove redundant animation keyframes (linear interpolation) |
| **Dead Bones** | Detect bones not referenced by any slot, constraint or animation — remove them |
| **Hidden Attachments** | Find slots with alpha=0 that still have active attachments wasting GPU — fix setup pose |
| **Unused Images** | Compare images on disk vs. referenced in JSON/atlas — move unused to `_UNUSED` |
| **Animation Splitter** | Split a multi-animation skeleton into separate JSON files by group |
| **Spine Downgrader** | Downgrade Spine 4.x JSON to 3.8 format |
| **Texture Unpacker** | Extract individual sprites from atlas sheets (`.atlas` or JSON atlas) |
| **Skeleton Viewer** | Preview skeleton with skin/animation selection, playback controls, bone overlay |

## Requirements

- Python 3.10+
- PySide6
- Pillow

```bash
pip install PySide6 Pillow
```

## Usage

```bash
# Run as module
python -m spine_swiss_knife

# Or via launcher script
python spine_swiss_knife.py
```

1. Set **Spine JSON**, **Atlas** and **Images** paths in the top config panel
2. All tabs auto-analyze on load
3. Use each tool's action button (Fix / Apply / Remove) — a `.backup` file is always created before changes
4. Changes propagate across tabs automatically (fresh JSON is reloaded after every action)

## Skeleton Viewer

Includes a bundled GTSpineViewer (Java) for full skeleton preview. The **Open Full Version** button launches it with the current JSON loaded.

Requires Java runtime for the external viewer.

## Localization

English and Slovak (`EN` / `SK`). Switch via the language dropdown in the sidebar.

## Project Structure

```
spine_swiss_knife/
  app.py              # Main window, sidebar, config panel
  style.py            # Dark theme QSS stylesheet
  i18n.py             # Translation system
  spine_json.py       # JSON load/save utilities
  atlas_parser.py     # Spine atlas format parser
  optimizer.py        # Image Downscaler
  mask_optimizer.py   # Rect Mask Optimizer
  polygon_simplify.py # Polygon Simplifier
  keyframe_optimizer.py
  dead_bones.py
  hidden_attachments.py
  unused_finder.py
  splitter.py         # Animation Splitter
  spine_downgrader.py
  texture_unpacker.py
  spine_viewer.py     # Built-in skeleton viewer
  locales/
    en.json
    sk.json
  resources/
    icon.png / .icns / .ico
    arrow_down.png
GTSpineViewer_3653/   # Bundled Java skeleton viewer
Logo/                 # App logo source files
```

## License

Internal tool — GreentubeSK.
