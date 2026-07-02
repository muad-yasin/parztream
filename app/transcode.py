import subprocess
from pathlib import Path

from .config import CACHE_DIR

# Codecs essentially every modern browser can decode natively.
COMPATIBLE_VIDEO_CODECS = {"h264", "vp8", "vp9", "av1"}
COMPATIBLE_AUDIO_CODECS = {"aac", "mp3", "opus", "vorbis"}

# Containers browsers can open directly, independent of what's inside.
DIRECT_PLAY_CONTAINERS = {".mp4", ".webm"}


class UnsupportedVideoCodec(Exception):
    """Raised when a video's codec itself (not just its container or audio
    track) can't be played in a browser without a full re-encode, which
    isn't implemented -- only cheap container/audio fixes are."""


def resolve_playable_path(row) -> Path:
    """Return the path that should actually be streamed for this media row:
    the original file if a browser can play it directly, or a cached
    remuxed copy if only the container/audio track needed fixing."""
    path = Path(row["path"])
    if row["media_type"] != "video":
        return path

    video_codec = row["video_codec"]
    audio_codec = row["audio_codec"]

    # No codec info yet (ffprobe unavailable at scan time, or this row was
    # scanned before this feature existed) -- don't guess, just direct play
    # as before.
    if video_codec is None:
        return path

    video_ok = video_codec.lower() in COMPATIBLE_VIDEO_CODECS
    audio_ok = audio_codec is None or audio_codec.lower() in COMPATIBLE_AUDIO_CODECS
    container_ok = path.suffix.lower() in DIRECT_PLAY_CONTAINERS

    if video_ok and audio_ok and container_ok:
        return path

    if not video_ok:
        raise UnsupportedVideoCodec(video_codec)

    return _get_or_create_remux(row["id"], path, remux_audio=not audio_ok)


def _get_or_create_remux(media_id: int, src_path: Path, remux_audio: bool) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"{media_id}.mp4"

    if cache_path.is_file() and cache_path.stat().st_mtime >= src_path.stat().st_mtime:
        return cache_path

    audio_args = ["-c:a", "aac"] if remux_audio else ["-c:a", "copy"]
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-i", str(src_path),
            "-c:v", "copy", *audio_args,
            "-movflags", "+faststart",
            "-f", "mp4",
            str(cache_path),
        ],
        check=True,
        timeout=600,
    )
    return cache_path
