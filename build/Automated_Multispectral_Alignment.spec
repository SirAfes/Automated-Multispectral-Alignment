# -*- mode: python ; coding: utf-8 -*-

import os
import sys

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

# Paths in pathex are resolved from the working directory, not from this
# file, so build an absolute path to src/ (SPECPATH = folder of this spec).
SRC = os.path.abspath(os.path.join(SPECPATH, '..', 'src'))
sys.path.insert(0, SRC)

hiddenimports = (
    collect_submodules('msalign')
    + collect_submodules('cv2')
    + collect_submodules('tifffile')
    + collect_submodules('rawpy')
)
if not any(name.startswith('msalign.') for name in hiddenimports):
    raise SystemExit(f'msalign package not found in {SRC}')

a = Analysis(
    ['launch_gui.py'],
    pathex=[SRC],
    binaries=collect_dynamic_libs('rawpy'),
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Automated_Multispectral_Alignment',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
