import mimetypes
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

MEDIA_DIRS = [
    Path(p) for p in os.environ.get("PARZTREAM_MEDIA_DIRS", "").split(os.pathsep) if p
]

DB_PATH = Path(os.environ.get("PARZTREAM_DB_PATH", BASE_DIR / "parztream.db"))

# Where remuxed/audio-transcoded copies of videos get cached (see
# app/transcode.py). Roughly the size of the originals that need it, since
# video is copied, not re-encoded.
CACHE_DIR = Path(os.environ.get("PARZTREAM_CACHE_DIR", BASE_DIR / "cache"))

# Optional cap on CACHE_DIR's total size, in bytes -- oldest cached files are
# deleted (after a new one is created) once it's exceeded. Unset/0 means no
# limit, matching prior behavior, since deleting things nobody asked to be
# capped by default would be a surprising default.
_cache_max_bytes_raw = os.environ.get("PARZTREAM_CACHE_MAX_BYTES")
CACHE_MAX_BYTES = int(_cache_max_bytes_raw) if _cache_max_bytes_raw else None

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".m4b", ".ogg", ".wav", ".aac"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm"}

# .m4b (audiobook chapters) uses the same MPEG-4 container as .m4a, but
# Python's mimetypes registry doesn't know the extension, so without this
# streaming would fall back to application/octet-stream and browsers
# wouldn't know how to play it.
mimetypes.add_type("audio/mp4", ".m4b")

AUTH_USERNAME = os.environ.get("PARZTREAM_USERNAME", "parztream")
AUTH_PASSWORD = os.environ.get("PARZTREAM_PASSWORD")

# Signs session cookies (see app/auth.py). If unset, a random key is
# generated at every process start -- simplest zero-config default, at the
# cost of everyone's session getting invalidated (and needing to log in
# again) on every restart. Set PARZTREAM_SECRET_KEY to a fixed random value
# to keep sessions alive across restarts.
SECRET_KEY = os.environ.get("PARZTREAM_SECRET_KEY") or secrets.token_hex(32)

# How long a login lasts before needing to sign in again, in seconds.
# Deliberately long (90 days): this gates a home media library behind a
# single shared password, not sensitive per-user data, and the people most
# affected by frequent forced re-logins are exactly the least technical
# users this is meant to be easy for.
SESSION_MAX_AGE = 60 * 60 * 24 * 90
