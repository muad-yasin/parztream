import os
import shutil
import subprocess
import time
from unittest.mock import patch

import pytest

from app import transcode

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)


def _row(**overrides):
    base = {
        "id": 1,
        "path": "/media/clip.mp4",
        "media_type": "video",
        "video_codec": None,
        "audio_codec": None,
    }
    base.update(overrides)
    return base


def test_audio_files_always_direct_play(tmp_path):
    f = tmp_path / "song.mp3"
    f.write_bytes(b"x")
    row = _row(media_type="audio", path=str(f), video_codec="hevc")

    assert transcode.resolve_playable_path(row) == f


def test_compatible_mp4_h264_aac_direct_plays_without_calling_ffmpeg(tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x")
    row = _row(path=str(f), video_codec="h264", audio_codec="aac")

    with patch("subprocess.run") as mock_run:
        result = transcode.resolve_playable_path(row)

    assert result == f
    mock_run.assert_not_called()


def test_unknown_codec_info_falls_back_to_direct_play(tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x")
    row = _row(path=str(f), video_codec=None, audio_codec=None)

    with patch("subprocess.run") as mock_run:
        result = transcode.resolve_playable_path(row)

    assert result == f
    mock_run.assert_not_called()


def test_incompatible_video_codec_raises(tmp_path):
    f = tmp_path / "clip.mkv"
    f.write_bytes(b"x")
    row = _row(path=str(f), video_codec="hevc", audio_codec="aac")

    with pytest.raises(transcode.UnsupportedVideoCodec):
        transcode.resolve_playable_path(row)


@requires_ffmpeg
def test_mkv_with_compatible_codecs_gets_remuxed_and_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")

    mkv_path = tmp_path / "clip.mkv"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=blue:size=64x64:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx264", "-c:a", "aac", "-shortest",
            str(mkv_path),
        ],
        check=True,
    )
    row = _row(id=42, path=str(mkv_path), video_codec="h264", audio_codec="aac")

    result = transcode.resolve_playable_path(row)

    assert result != mkv_path
    assert result.suffix == ".mp4"
    assert result.is_file()

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(result)],
        capture_output=True, text=True, check=True,
    )
    assert "h264" in probe.stdout

    # Second call should hit the cache, not invoke ffmpeg again.
    with patch("subprocess.run") as mock_run:
        cached_result = transcode.resolve_playable_path(row)
    assert cached_result == result
    mock_run.assert_not_called()


@requires_ffmpeg
def test_incompatible_audio_gets_transcoded_while_video_is_copied(tmp_path, monkeypatch):
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")

    mkv_path = tmp_path / "clip.mkv"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=blue:size=64x64:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx264", "-c:a", "ac3", "-shortest",
            str(mkv_path),
        ],
        check=True,
    )
    row = _row(id=7, path=str(mkv_path), video_codec="h264", audio_codec="ac3")

    result = transcode.resolve_playable_path(row)

    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_type,codec_name",
            "-of", "csv=p=0", str(result),
        ],
        capture_output=True, text=True, check=True,
    )
    assert "h264,video" in probe.stdout
    assert "aac,audio" in probe.stdout


def _cache_file(cache_dir, name, size, age_seconds):
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / name
    path.write_bytes(b"x" * size)
    now = time.time()
    os.utime(path, (now - age_seconds, now - age_seconds))
    return path


def test_prune_does_nothing_when_no_limit_configured(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(transcode, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(transcode, "CACHE_MAX_BYTES", None)
    old = _cache_file(cache_dir, "1.mp4", 1000, age_seconds=1000)
    new = _cache_file(cache_dir, "2.mp4", 1000, age_seconds=0)

    transcode._prune_cache(protect=new)

    assert old.is_file()
    assert new.is_file()


def test_prune_evicts_oldest_first_until_under_budget(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(transcode, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(transcode, "CACHE_MAX_BYTES", 250)
    oldest = _cache_file(cache_dir, "1.mp4", 100, age_seconds=200)
    middle = _cache_file(cache_dir, "2.mp4", 100, age_seconds=100)
    newest = _cache_file(cache_dir, "3.mp4", 100, age_seconds=0)

    transcode._prune_cache(protect=newest)

    assert not oldest.is_file()
    assert middle.is_file()
    assert newest.is_file()


def test_prune_never_deletes_the_protected_file_even_if_alone_over_budget(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(transcode, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(transcode, "CACHE_MAX_BYTES", 10)
    protected = _cache_file(cache_dir, "1.mp4", 1000, age_seconds=0)

    transcode._prune_cache(protect=protected)

    assert protected.is_file()


@requires_ffmpeg
def test_creating_a_new_remux_prunes_older_ones_once_over_budget(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(transcode, "CACHE_DIR", cache_dir)

    def make_mkv(name):
        path = tmp_path / name
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=blue:size=64x64:duration=1",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-c:v", "libx264", "-c:a", "aac", "-shortest",
                str(path),
            ],
            check=True,
        )
        return path

    first_src = make_mkv("first.mkv")
    second_src = make_mkv("second.mkv")

    first_row = _row(id=1, path=str(first_src), video_codec="h264", audio_codec="ac3")
    second_row = _row(id=2, path=str(second_src), video_codec="h264", audio_codec="ac3")

    first_cached = transcode.resolve_playable_path(first_row)
    assert first_cached.is_file()

    # Cap the budget to roughly one cached file's worth, then create a
    # second -- the first should get evicted to make room.
    monkeypatch.setattr(transcode, "CACHE_MAX_BYTES", first_cached.stat().st_size + 1000)
    second_cached = transcode.resolve_playable_path(second_row)

    assert second_cached.is_file()
    assert not first_cached.is_file()
