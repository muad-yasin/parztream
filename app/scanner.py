import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from mutagen import File as MutagenFile

from .config import AUDIO_EXTENSIONS, MEDIA_DIRS, VIDEO_EXTENSIONS
from .db import get_connection

_scan_lock = threading.Lock()
_scan_state = {"status": "idle", "error": None, "last_scan_at": None}


def get_scan_status():
    return dict(_scan_state)


def start_scan():
    """Try to claim the scan lock. Returns False if a scan is already running."""
    if not _scan_lock.acquire(blocking=False):
        return False
    _scan_state["status"] = "scanning"
    _scan_state["error"] = None
    return True


def run_claimed_scan():
    """Run a scan previously claimed with start_scan(). Releases the lock when done."""
    try:
        scan_media_dirs()
    except Exception as exc:
        _scan_state["status"] = "error"
        _scan_state["error"] = str(exc)
    else:
        _scan_state["status"] = "idle"
        _scan_state["last_scan_at"] = datetime.now(timezone.utc).isoformat()
    finally:
        _scan_lock.release()


def scan_media_dirs():
    found_paths = set()
    with get_connection() as conn:
        for media_dir in MEDIA_DIRS:
            if not media_dir.is_dir():
                continue
            for path in media_dir.rglob("*"):
                if not path.is_file():
                    continue
                ext = path.suffix.lower()
                if ext in AUDIO_EXTENSIONS:
                    media_type = "audio"
                elif ext in VIDEO_EXTENSIONS:
                    media_type = "video"
                else:
                    continue
                found_paths.add(str(path))
                _upsert_media(conn, path, media_type)

        _remove_missing(conn, found_paths)


def _upsert_media(conn, path: Path, media_type: str):
    title, artist, album, duration = _extract_metadata(path, media_type)
    size_bytes = path.stat().st_size
    conn.execute(
        """
        INSERT INTO media (path, media_type, title, artist, album, duration, size_bytes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            title=excluded.title, artist=excluded.artist, album=excluded.album,
            duration=excluded.duration, size_bytes=excluded.size_bytes
        """,
        (str(path), media_type, title, artist, album, duration, size_bytes),
    )


def _extract_metadata(path: Path, media_type: str):
    title = path.stem
    artist = None
    album = None
    duration = None

    if media_type == "audio":
        try:
            audio = MutagenFile(path, easy=True)
        except Exception:
            audio = None
        if audio is not None:
            if audio.tags:
                title = _first_tag(audio.tags, "title", title)
                artist = _first_tag(audio.tags, "artist", artist)
                album = _first_tag(audio.tags, "album", album)
            try:
                if audio.info:
                    duration = audio.info.length
            except Exception:
                pass
    else:
        duration = _probe_duration(path)

    return title, artist, album, duration


def _first_tag(tags, key: str, default):
    try:
        values = tags.get(key)
        return values[0] if values else default
    except Exception:
        return default


def _probe_duration(path: Path):
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            capture_output=True, text=True, timeout=10,
        )
        return float(result.stdout.strip())
    except (FileNotFoundError, ValueError, subprocess.SubprocessError):
        return None


def _remove_missing(conn, found_paths: set):
    existing = conn.execute("SELECT id, path FROM media").fetchall()
    for row in existing:
        if row["path"] not in found_paths:
            conn.execute("DELETE FROM media WHERE id = ?", (row["id"],))
