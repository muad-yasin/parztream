import shutil
import subprocess

import pytest

from app import scanner
from app.db import get_connection

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)


def _rows():
    with get_connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM media")]


def _metadata(**overrides):
    base = {
        "title": "Untitled",
        "artist": None,
        "album": None,
        "duration": None,
        "video_codec": None,
        "audio_codec": None,
        "show_name": None,
        "season_number": None,
        "episode_number": None,
    }
    base.update(overrides)
    return base


def test_classifies_by_extension_and_ignores_unknown_files(make_file, monkeypatch):
    monkeypatch.setattr(
        scanner,
        "_extract_metadata",
        lambda path, media_type: _metadata(title=path.stem, artist="Artist", album="Album", duration=42.0),
    )
    make_file("song.mp3")
    make_file("audiobook.m4b")
    make_file("clip.mkv")
    make_file("notes.txt")

    scanner.scan_media_dirs()

    rows = {r["path"].rsplit("/", 1)[-1]: r for r in _rows()}
    assert set(rows) == {"song.mp3", "audiobook.m4b", "clip.mkv"}
    assert rows["song.mp3"]["media_type"] == "audio"
    assert rows["audiobook.m4b"]["media_type"] == "audio"
    assert rows["clip.mkv"]["media_type"] == "video"
    assert rows["song.mp3"]["artist"] == "Artist"
    assert rows["song.mp3"]["duration"] == 42.0


def test_rescanning_updates_existing_row_instead_of_duplicating(make_file, monkeypatch):
    calls = {"n": 0}

    def fake_extract(path, media_type):
        calls["n"] += 1
        return _metadata(title=f"Title {calls['n']}")

    monkeypatch.setattr(scanner, "_extract_metadata", fake_extract)
    make_file("song.mp3")

    scanner.scan_media_dirs()
    scanner.scan_media_dirs()

    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["title"] == "Title 2"


def test_scan_removes_rows_for_files_deleted_from_disk(make_file, monkeypatch):
    monkeypatch.setattr(scanner, "_extract_metadata", lambda p, t: _metadata(title=p.stem))
    f = make_file("song.mp3")
    scanner.scan_media_dirs()
    assert len(_rows()) == 1

    f.unlink()
    scanner.scan_media_dirs()

    assert _rows() == []


def test_ignores_configured_dir_that_does_not_exist(monkeypatch, tmp_path):
    monkeypatch.setattr(scanner, "MEDIA_DIRS", [tmp_path / "does-not-exist"])
    scanner.scan_media_dirs()  # should not raise
    assert _rows() == []


def test_first_tag_falls_back_on_missing_or_malformed_values():
    assert scanner._first_tag({"title": ["Real Title"]}, "title", "fallback") == "Real Title"
    assert scanner._first_tag({}, "title", "fallback") == "fallback"
    assert scanner._first_tag({"title": []}, "title", "fallback") == "fallback"
    assert scanner._first_tag(None, "title", "fallback") == "fallback"


@pytest.mark.parametrize(
    "stem,expected",
    [
        ("The Chosen S01E01", ("The Chosen", 1, 1)),
        ("The.Chosen.S01E03.1080p", ("The Chosen", 1, 3)),
        ("the_chosen_s01e04", ("the chosen", 1, 4)),
        ("Show Name - S1E5", ("Show Name", 1, 5)),
        ("Some Movie (2020)", (None, None, None)),
        ("random_video_clip", (None, None, None)),
    ],
)
def test_parse_show_episode(stem, expected):
    assert scanner._parse_show_episode(stem) == expected


def test_scan_populates_show_fields_for_episode_style_filenames(make_file, monkeypatch):
    monkeypatch.setattr(
        scanner, "_probe_video_info", lambda path: (None, "h264", "aac")
    )
    make_file("The Chosen S01E02.mp4")
    make_file("random_clip.mp4")

    scanner.scan_media_dirs()

    rows = {r["path"].rsplit("/", 1)[-1]: r for r in _rows()}
    assert rows["The Chosen S01E02.mp4"]["show_name"] == "The Chosen"
    assert rows["The Chosen S01E02.mp4"]["season_number"] == 1
    assert rows["The Chosen S01E02.mp4"]["episode_number"] == 2
    assert rows["random_clip.mp4"]["show_name"] is None


@requires_ffmpeg
def test_real_mp4_video_and_audio_codecs_are_recorded(media_dir):
    mp4_path = media_dir / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=blue:size=64x64:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx264", "-c:a", "aac", "-shortest",
            str(mp4_path),
        ],
        check=True,
    )

    scanner.scan_media_dirs()

    row = _rows()[0]
    assert row["media_type"] == "video"
    assert row["video_codec"] == "h264"
    assert row["audio_codec"] == "aac"
    assert row["duration"] == pytest.approx(1.0, abs=0.2)


@requires_ffmpeg
def test_real_mp3_tags_and_duration_are_extracted(media_dir):
    mp3_path = media_dir / "real.mp3"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-metadata", "title=Real Title",
            "-metadata", "artist=Real Artist",
            str(mp3_path),
        ],
        check=True,
    )

    scanner.scan_media_dirs()

    row = _rows()[0]
    assert row["media_type"] == "audio"
    assert row["title"] == "Real Title"
    assert row["artist"] == "Real Artist"
    assert row["duration"] == pytest.approx(1.0, abs=0.2)
