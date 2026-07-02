from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.mp4 import MP4Cover


def get_cover_art(path: Path, media_type: str):
    """Return (image_bytes, mime_type) for embedded cover art, or None if
    there isn't any (or media_type isn't audio -- video thumbnails aren't
    supported yet)."""
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
