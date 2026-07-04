import shutil
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app import artwork

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)


def test_get_cover_art_returns_none_for_video(tmp_path):
    # Not a real video, but get_cover_art should short-circuit on media_type
    # before ever trying to parse it -- video thumbnails are a separate
    # function, get_video_thumbnail, tested below.
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


def test_get_video_thumbnail_returns_none_when_ffmpeg_cant_read_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(artwork, "CACHE_DIR", tmp_path / "cache")
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"not a real video")

    assert artwork.get_video_thumbnail(1, f, duration=10.0) is None


@requires_ffmpeg
def test_get_video_thumbnail_extracts_and_caches_a_real_frame(tmp_path, monkeypatch, h264_encoder):
    monkeypatch.setattr(artwork, "CACHE_DIR", tmp_path / "cache")
    video_path = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=red:size=64x64:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", h264_encoder, "-c:a", "aac", "-shortest",
            str(video_path),
        ],
        check=True,
    )

    thumb_path = artwork.get_video_thumbnail(99, video_path, duration=2.0)

    assert thumb_path is not None
    assert thumb_path.is_file()
    assert thumb_path.suffix == ".jpg"
    assert thumb_path.read_bytes()[:2] == b"\xff\xd8"  # JPEG magic bytes

    # Second call should reuse the cached file, not re-invoke ffmpeg.
    with patch("subprocess.run") as mock_run:
        cached = artwork.get_video_thumbnail(99, video_path, duration=2.0)
    assert cached == thumb_path
    mock_run.assert_not_called()


def test_concurrent_requests_for_the_same_uncached_thumbnail_only_invoke_ffmpeg_once(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(artwork, "CACHE_DIR", tmp_path / "cache")
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"source bytes")

    call_count = 0
    call_count_lock = threading.Lock()

    def fake_run(cmd, **kwargs):
        nonlocal call_count
        with call_count_lock:
            call_count += 1
        time.sleep(0.1)  # widen the race window so the bug would reproduce
        Path(cmd[-1]).write_bytes(b"fake jpeg output")
        return MagicMock(returncode=0)

    results = []
    with patch("subprocess.run", side_effect=fake_run):
        threads = [
            threading.Thread(
                target=lambda: results.append(artwork.get_video_thumbnail(42, video_path, 10.0))
            )
            for _ in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert call_count == 1
    assert len(results) == 8
    assert all(r == results[0] for r in results)


def test_concurrent_thumbnails_for_different_files_are_capped(tmp_path, monkeypatch):
    # Regression test: before the semaphore existed, N different videos'
    # first-ever thumbnail requests (e.g. a poster grid loading) each spawned
    # their own ffmpeg process with no cap at all -- cache.lock_for only
    # dedups the *same* file's concurrent requests, not different files'.
    monkeypatch.setattr(artwork, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(artwork, "_thumbnail_semaphore", threading.Semaphore(2))

    in_flight = 0
    max_in_flight = 0
    in_flight_lock = threading.Lock()

    def fake_run(cmd, **kwargs):
        nonlocal in_flight, max_in_flight
        with in_flight_lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        time.sleep(0.1)  # widen the race window so the bug would reproduce
        Path(cmd[-1]).write_bytes(b"fake jpeg output")
        with in_flight_lock:
            in_flight -= 1
        return MagicMock(returncode=0)

    video_paths = []
    for i in range(6):
        p = tmp_path / f"clip{i}.mp4"
        p.write_bytes(b"source bytes")
        video_paths.append(p)

    with patch("subprocess.run", side_effect=fake_run):
        threads = [
            threading.Thread(target=artwork.get_video_thumbnail, args=(i, video_paths[i], 10.0))
            for i in range(6)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert max_in_flight <= 2
