"""Entry point for the macOS .app build (see parztream.spec /
parztream-wrapper.sh).

Not app/main.py directly -- mirrors packaging/windows/launcher.py and
packaging/linux/launcher.py, solving the same class of problems that only
exist once parztream is a double-clickable app bundle instead of something
started from a terminal in the project checkout:

- A PyInstaller onefile build unpacks itself into a temporary folder
  (sys._MEIPASS) that's deleted after every run. app/config.py's own
  defaults for the database and cache point at the project root, which
  would silently lose the whole library on every restart if used as-is
  here -- so this points them at a persistent, per-user folder instead
  (~/Library/Application Support/parztream, the standard macOS location
  for this kind of thing) before app/config.py ever gets imported and
  reads those environment variables at import time.
- Without a fixed PARZTREAM_SECRET_KEY, every launch would generate a
  new random one and sign everyone out (see app/config.py) -- this
  generates one once and reuses it on every subsequent launch.
- The bundled ffmpeg/ffprobe (see parztream.spec) need to be on PATH
  for app/transcode.py, app/scanner.py, and app/artwork.py to find them
  -- they all just call "ffmpeg"/"ffprobe" and rely on PATH lookup.
- Opens the default browser automatically once the server is actually
  ready to accept connections, rather than immediately (which would
  show a connection-refused page).
"""

import multiprocessing
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

# Standard PyInstaller precaution -- harmless if nothing in the dependency
# tree actually uses multiprocessing, but cheap to always include.
multiprocessing.freeze_support()

HOST = "0.0.0.0"
PORT = 8000


def _persistent_data_dir() -> Path:
    data_dir = Path.home() / "Library" / "Application Support" / "parztream"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def _persistent_secret_key(data_dir: Path) -> str:
    key_path = data_dir / "secret_key.txt"
    existing = key_path.read_text().strip() if key_path.is_file() else ""
    if existing:
        return existing
    key = secrets.token_hex(32)
    key_path.write_text(key)
    try:
        # Best-effort -- this signs every session cookie, so any other
        # local account being able to read it could mint valid sessions.
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return key


def _add_bundled_ffmpeg_to_path() -> None:
    if not getattr(sys, "frozen", False):
        return
    ffmpeg_dir = Path(sys._MEIPASS) / "ffmpeg"
    if ffmpeg_dir.is_dir():
        os.environ["PATH"] = str(ffmpeg_dir) + os.pathsep + os.environ.get("PATH", "")


def _open_browser_once_ready(url: str) -> None:
    def wait_and_open():
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.25)
        try:
            webbrowser.open(url)
        except Exception:
            # No browser available -- the startup banner already printed
            # the URL, so this is silently skippable rather than a real
            # failure.
            pass

    threading.Thread(target=wait_and_open, daemon=True).start()


def main() -> None:
    data_dir = _persistent_data_dir()

    # app/config.py reads these into module-level constants at import time
    # -- must be set before the first `import app...` below, not after.
    os.environ.setdefault("PARZTREAM_DB_PATH", str(data_dir / "parztream.db"))
    os.environ.setdefault("PARZTREAM_CACHE_DIR", str(data_dir / "cache"))
    os.environ.setdefault("PARZTREAM_SECRET_KEY", _persistent_secret_key(data_dir))
    os.environ.setdefault("PARZTREAM_PORT", str(PORT))

    _add_bundled_ffmpeg_to_path()

    import uvicorn

    from app.main import app

    print("=" * 64)
    print("  parztream is starting...")
    print(f"  Your library will open at: http://localhost:{PORT}")
    print(f"  Settings, database, and cache are stored in: {data_dir}")
    print("  To stop parztream, close this Terminal window (or press Ctrl+C).")
    print("=" * 64)

    _open_browser_once_ready(f"http://localhost:{PORT}")

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
