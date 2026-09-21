# -*- mode: python ; coding: utf-8 -*-
# Build with:  pyinstaller main.spec   (from the activated venv; output in dist/079RaadDownloader/)
#
# The Playwright *driver* (node.exe + cli.js) ships inside the bundle; the Chromium browser
# itself does not. scraper.install_chromium() downloads it to %LOCALAPPDATA%\ms-playwright
# on first use, which is also where a `playwright install chromium` on the dev machine puts it.
import os
from PyInstaller.utils.hooks import collect_data_files, collect_all

site = os.path.join('venv', 'Lib', 'site-packages')

datas = []
binaries = []
hiddenimports = []
for pkg in ('customtkinter', 'playwright', 'imageio_ffmpeg'):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
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
    [],
    exclude_binaries=True,
    name='079RaadDownloader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Keep a console: the same exe doubles as the CLI (python main.py --url ... --output ...).
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='079RaadDownloader',
)
