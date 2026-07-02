import re
from pathlib import Path

# Matches SRT's comma-decimal timestamp ("00:00:01,000"); WebVTT uses a dot
# instead. Scoped to the specific HH:MM:SS,mmm pattern so it only touches
# real timestamps, never a comma inside subtitle dialogue text.
_SRT_TIMESTAMP_RE = re.compile(r"(\d{2}:\d{2}:\d{2}),(\d{3})")


def find_subtitle_path(video_path: Path):
    """Return the sidecar subtitle file for video_path (same directory,
    same filename stem), preferring .vtt (no conversion needed) over .srt,
    or None if neither exists. Only a single, same-stem file is supported --
    no language selection between multiple tracks."""
    vtt_path = video_path.with_suffix(".vtt")
    if vtt_path.is_file():
        return vtt_path
    srt_path = video_path.with_suffix(".srt")
    if srt_path.is_file():
        return srt_path
    return None


def get_webvtt(video_path: Path):
    """Return WebVTT text for video_path's sidecar subtitle file (.vtt
    served as-is, .srt converted), or None if there isn't one or it can't
    be read."""
    subtitle_path = find_subtitle_path(video_path)
    if subtitle_path is None:
        return None

    try:
        text = subtitle_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None

    if subtitle_path.suffix.lower() == ".vtt":
        return text
    return _srt_to_vtt(text)


def _srt_to_vtt(srt_text: str) -> str:
    body = _SRT_TIMESTAMP_RE.sub(r"\1.\2", srt_text)
    return "WEBVTT\n\n" + body
