import json
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
    title, artist, album, duration, video_codec, audio_codec = _extract_metadata(path, media_type)
    size_bytes = path.stat().st_size
    conn.execute(
        """
        INSERT INTO media
            (path, media_type, title, artist, album, duration, size_bytes, video_codec, audio_codec)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            title=excluded.title, artist=excluded.artist, album=excluded.album,
            duration=excluded.duration, size_bytes=excluded.size_bytes,
            video_codec=excluded.video_codec, audio_codec=excluded.audio_codec
        """,
        (str(path), media_type, title, artist, album, duration, size_bytes, video_codec, audio_codec),
    )


def _extract_metadata(path: Path, media_type: str):
    title = path.stem
    artist = None
    album = None
    duration = None
    video_codec = None
    audio_codec = None

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
        duration, video_codec, audio_codec = _probe_video_info(path)

    return title, artist, album, duration, video_codec, audio_codec


def _first_tag(tags, key: str, default):
    try:
        values = tags.get(key)
        return values[0] if values else default
    except Exception:
        return default


def _probe_video_info(path: Path):
    """Return (duration, video_codec, audio_codec) via a single ffprobe call.
    video_codec/audio_codec are the *first* video/audio stream's codec name
    (e.g. "h264", "ac3"), used by app/transcode.py to decide whether a file
    can be played directly in a browser."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration:stream=codec_type,codec_name",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
    except (FileNotFoundError, subprocess.SubprocessError, json.JSONDecodeError):
        return None, None, None

    duration = None
    try:
        duration = float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        pass

    video_codec = None
    audio_codec = None
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and video_codec is None:
            video_codec = stream.get("codec_name")
        elif stream.get("codec_type") == "audio" and audio_codec is None:
            audio_codec = stream.get("codec_name")

    return duration, video_codec, audio_codec


def _remove_missing(conn, found_paths: set):
    existing = conn.execute("SELECT id, path FROM media").fetchall()
    for row in existing:
        if row["path"] not in found_paths:
            conn.execute("DELETE FROM media WHERE id = ?", (row["id"],))
