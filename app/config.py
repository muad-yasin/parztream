import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

MEDIA_DIRS = [
    Path(p) for p in os.environ.get("PARZTREAM_MEDIA_DIRS", "").split(os.pathsep) if p
]

DB_PATH = Path(os.environ.get("PARZTREAM_DB_PATH", BASE_DIR / "parztream.db"))

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".ogg", ".wav", ".aac"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm"}
