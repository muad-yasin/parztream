import shutil
import subprocess

import pytest

from app import artwork

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)


def test_video_returns_none_without_touching_the_file(tmp_path):
    # Not a real video, but get_cover_art should short-circuit on media_type
    # before ever trying to parse it -- video thumbnails aren't supported yet.
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"not a real video")
    assert artwork.get_cover_art(f, "video") is None


def test_corrupt_audio_file_returns_none_gracefully(tmp_path):
    f = tmp_path / "broken.mp3"
    f.write_bytes(b"not actually an mp3")
    assert artwork.get_cover_art(f, "audio") is None


@requires_ffmpeg
def test_returns_none_when_no_embedded_art(tmp_path):
    mp3_path = tmp_path / "no_art.mp3"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            str(mp3_path),
        ],
        check=True,
    )

    assert artwork.get_cover_art(mp3_path, "audio") is None


@requires_ffmpeg
def test_extracts_embedded_cover_art_from_id3_mp3(tmp_path):
    from mutagen.id3 import ID3, APIC

    mp3_path = tmp_path / "with_art.mp3"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            str(mp3_path),
        ],
        check=True,
    )

    image_bytes = b"\x89PNG\r\n\x1a\nfake-png-bytes"
    tags = ID3()
    tags.add(APIC(encoding=3, mime="image/png", type=3, desc="Cover", data=image_bytes))
    tags.save(mp3_path)

    art = artwork.get_cover_art(mp3_path, "audio")

    assert art is not None
    data, mime = art
    assert data == image_bytes
    assert mime == "image/png"
