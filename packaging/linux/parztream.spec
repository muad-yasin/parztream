# -*- mode: python ; coding: utf-8 -*-
#
# Builds the onefile Linux binary that gets wrapped into an AppImage. Run
# from the repository root:
#   pyinstaller packaging/linux/parztream.spec --distpath dist
#
# packaging/linux/vendor/ffmpeg/{ffmpeg,ffprobe} are bundled in if present
# (see .github/workflows/build-linux-appimage.yml, which downloads them
# before this runs) -- the build still works without them, it just produces
# a binary that behaves like running parztream with no ffmpeg on PATH (see
# README's "What you'll need" for what that means).

from pathlib import Path

REPO_ROOT = Path(SPECPATH).resolve().parent.parent
FFMPEG_DIR = REPO_ROOT / "packaging" / "linux" / "vendor" / "ffmpeg"

datas = [
    (str(REPO_ROOT / "static"), "static"),
]
if FFMPEG_DIR.is_dir():
    datas.append((str(FFMPEG_DIR), "ffmpeg"))

# uvicorn and zeroconf both do enough dynamic importing that PyInstaller's
# static analysis misses some of it -- these are the modules known to get
# silently dropped otherwise, causing an ImportError only at runtime on a
# clean machine, not at build time.
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
]

a = Analysis(
    [str(REPO_ROOT / "packaging" / "linux" / "launcher.py")],
    pathex=[str(REPO_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="parztream",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
