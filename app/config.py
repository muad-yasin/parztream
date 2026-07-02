import mimetypes
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

MEDIA_DIRS = [
    Path(p) for p in os.environ.get("PARZTREAM_MEDIA_DIRS", "").split(os.pathsep) if p
]

DB_PATH = Path(os.environ.get("PARZTREAM_DB_PATH", BASE_DIR / "parztream.db"))

# Where remuxed/audio-transcoded copies of videos get cached (see
# app/transcode.py). Roughly the size of the originals that need it, since
# video is copied, not re-encoded -- not pruned automatically.
CACHE_DIR = Path(os.environ.get("PARZTREAM_CACHE_DIR", BASE_DIR / "cache"))

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".m4b", ".ogg", ".wav", ".aac"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm"}

# .m4b (audiobook chapters) uses the same MPEG-4 container as .m4a, but
# Python's mimetypes registry doesn't know the extension, so without this
# streaming would fall back to application/octet-stream and browsers
# wouldn't know how to play it.
mimetypes.add_type("audio/mp4", ".m4b")

AUTH_USERNAME = os.environ.get("PARZTREAM_USERNAME", "parztream")
AUTH_PASSWORD = os.environ.get("PARZTREAM_PASSWORD")
