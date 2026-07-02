import subprocess
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.mp4 import MP4Cover

from . import cache
from .config import CACHE_DIR


def get_cover_art(path: Path, media_type: str):
    """Return (image_bytes, mime_type) for embedded cover art, or None if
    there isn't any. Audio only -- see get_video_thumbnail for video."""
    if media_type != "audio":
        return None

    try:
        audio = MutagenFile(path)
    except Exception:
        return None
    if audio is None or audio.tags is None:
        return None

    tags = audio.tags

    # MP4 container (.m4a, .m4b)
    covr = tags.get("covr") if hasattr(tags, "get") else None
    if covr:
        cover = covr[0]
        mime = "image/png" if cover.imageformat == MP4Cover.FORMAT_PNG else "image/jpeg"
        return bytes(cover), mime

    # FLAC
    pictures = getattr(audio, "pictures", None)
    if pictures:
        pic = pictures[0]
        return pic.data, pic.mime or "image/jpeg"

    # ID3 (mp3)
    if hasattr(tags, "getall"):
        apics = tags.getall("APIC")
        if apics:
            return apics[0].data, apics[0].mime or "image/jpeg"

    return None


def get_video_thumbnail(media_id: int, path: Path, duration):
    """Return a Path to a cached JPEG thumbnail for this video, generating
    (and caching) one via ffmpeg if needed. Returns None if ffmpeg is
    unavailable or can't grab a frame (e.g. a corrupt file)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    thumb_path = CACHE_DIR / f"{media_id}_thumb.jpg"

    # Same reasoning as app/transcode.py's remux lock: without this,
    # concurrent requests for the same not-yet-cached thumbnail would each
    # spawn their own ffmpeg process racing to write the same output file.
    with cache.lock_for(str(thumb_path)):
        if thumb_path.is_file() and thumb_path.stat().st_mtime >= path.stat().st_mtime:
            return thumb_path

        # A frame a little into the video reads better than frame 0, which is
        # often a black/blank intro; 10% in (capped at 10s) is a cheap
        # heuristic, not an attempt at a "best" thumbnail.
        seek = min(10.0, duration * 0.1) if duration else 0.0

        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error",
                    "-ss", str(seek), "-i", str(path),
                    "-frames:v", "1", "-vf", "scale=320:-1",
                    "-q:v", "4",
                    str(thumb_path),
                ],
                check=True,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            return None

        if not thumb_path.is_file():
            return None

        cache.prune(protect=thumb_path)
        return thumb_path
