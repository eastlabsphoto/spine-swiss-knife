# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Spine Swiss Knife."""

import platform
import re

block_cipher = None

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
    hiddenimports=[],
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
    coll = COLLECT(
        exe, a.binaries, a.zipfiles, a.datas,
        strip=False,
        upx=False,
        name='SpineSwissKnife',
    )
