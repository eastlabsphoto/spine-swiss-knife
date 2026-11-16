# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Spine Swiss Knife."""

import platform
import re

block_cipher = None

# Pure-logic modules (no PySide6): needed by the headless API / CLI / MCP.
_PURE_MODULES = [
    'spine_swiss_knife.' + m for m in (
        'atlas_parser',
        'spine_json',
        'spine_refs',
        'spine_validate',
        'spine_cli',
        'dead_bones_core',
        'downscale_core',
        'draw_order_core',
        'flatten_core',
        'hidden_attachments_core',
        'keyframe_core',
        'mask_core',
        'merge_core',
        'splitter_core',
        'static_exporter_core',
        'unused_core',
    )
]

# Read version dynamically
with open('spine_swiss_knife/__init__.py') as _f:
    _VERSION = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', _f.read()).group(1)

a = Analysis(
    ['pyinstaller_entry.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('spine_swiss_knife/locales', 'spine_swiss_knife/locales'),
        ('spine_swiss_knife/resources', 'spine_swiss_knife/resources'),
        ('GTSpineViewer_3653', 'GTSpineViewer_3653'),
    ],
    hiddenimports=['certifi', 'numpy', 'PIL',
                   'spine_swiss_knife.api', 'spine_swiss_knife.cli',
                   'spine_swiss_knife.mcp_server', 'spine_swiss_knife.launcher']
                  + _PURE_MODULES,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# Console build for MCP clients (Windows GUI exe is windowed and has no stdio).
mcp_a = Analysis(
    ['pyinstaller_mcp_entry.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['PIL', 'numpy', 'spine_swiss_knife.api', 'spine_swiss_knife.cli',
                   'spine_swiss_knife.mcp_server'] + _PURE_MODULES,
    hookspath=[],
    excludes=['PySide6', 'tkinter'],
    cipher=block_cipher,
    noarchive=False,
)
mcp_pyz = PYZ(mcp_a.pure, mcp_a.zipped_data, cipher=block_cipher)

if platform.system() == 'Darwin':
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name='SpineSwissKnife',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        icon='spine_swiss_knife/resources/icon.icns',
    )
    coll = COLLECT(
        exe, a.binaries, a.zipfiles, a.datas,
        strip=False,
        upx=False,
        name='SpineSwissKnife',
    )
    app = BUNDLE(
        coll,
        name='SpineSwissKnife.app',
        icon='spine_swiss_knife/resources/icon.icns',
        bundle_identifier='com.greentube.spineswissknife',
        info_plist={
            'CFBundleShortVersionString': _VERSION,
            'NSHighResolutionCapable': True,
        },
    )
else:
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name='SpineSwissKnife',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        icon='spine_swiss_knife/resources/icon.ico',
    )
    mcp_exe = EXE(
        mcp_pyz, mcp_a.scripts, [],
        exclude_binaries=True,
        name='SpineSwissKnifeMCP',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=True,
        icon='spine_swiss_knife/resources/icon.ico',
    )
    coll = COLLECT(
        exe, a.binaries, a.zipfiles, a.datas,
        mcp_exe, mcp_a.binaries, mcp_a.zipfiles, mcp_a.datas,
        strip=False,
        upx=False,
        name='SpineSwissKnife',
    )
