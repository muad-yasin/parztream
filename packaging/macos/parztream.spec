# -*- mode: python ; coding: utf-8 -*-
#
# Builds parztream.app. Run from the repository root:
#   pyinstaller packaging/macos/parztream.spec --distpath dist
#
# The EXE is deliberately named "parztream-bin", not "parztream" -- the
# build workflow adds packaging/macos/parztream-wrapper.sh into the
# resulting Contents/MacOS/ as "parztream" (see CFBundleExecutable below)
# so double-clicking the .app opens a visible Terminal window instead of
# running invisibly with no way to see it's running or stop it.
#
# packaging/macos/vendor/ffmpeg/{ffmpeg,ffprobe} are bundled in if present
# (see .github/workflows/build-macos-app.yml, which installs them via
# Homebrew before this runs) -- the build still works without them, it
# just produces an app that behaves like running parztream with no ffmpeg
# on PATH (see README's "What you'll need" for what that means).

from pathlib import Path

REPO_ROOT = Path(SPECPATH).resolve().parent.parent
FFMPEG_DIR = REPO_ROOT / "packaging" / "macos" / "vendor" / "ffmpeg"

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
    [str(REPO_ROOT / "packaging" / "macos" / "launcher.py")],
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
    name="parztream-bin",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    # Apple Silicon only for now (Macs sold since late 2020) -- explicit
    # rather than relying on the build runner's default, so this doesn't
    # silently change if that default ever does.
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=None,
)

app = BUNDLE(
    exe,
    name="parztream.app",
    icon=None,
    bundle_identifier="com.parztream.app",
    info_plist={
        "CFBundleExecutable": "parztream",
        "CFBundleName": "parztream",
        "CFBundleDisplayName": "parztream",
        "CFBundleShortVersionString": "1.0.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
    },
)
