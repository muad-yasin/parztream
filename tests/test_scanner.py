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


def test_classifies_by_extension_and_ignores_unknown_files(make_file, monkeypatch):
    monkeypatch.setattr(
        scanner, "_extract_metadata", lambda path, media_type: (path.stem, "Artist", "Album", 42.0)
    )
    make_file("song.mp3")
    make_file("clip.mkv")
    make_file("notes.txt")

    scanner.scan_media_dirs()

    rows = {r["path"].rsplit("/", 1)[-1]: r for r in _rows()}
    assert set(rows) == {"song.mp3", "clip.mkv"}
    assert rows["song.mp3"]["media_type"] == "audio"
    assert rows["clip.mkv"]["media_type"] == "video"
    assert rows["song.mp3"]["artist"] == "Artist"
    assert rows["song.mp3"]["duration"] == 42.0


def test_rescanning_updates_existing_row_instead_of_duplicating(make_file, monkeypatch):
    calls = {"n": 0}

    def fake_extract(path, media_type):
        calls["n"] += 1
        return (f"Title {calls['n']}", None, None, None)

    monkeypatch.setattr(scanner, "_extract_metadata", fake_extract)
    make_file("song.mp3")

    scanner.scan_media_dirs()
    scanner.scan_media_dirs()

    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["title"] == "Title 2"


def test_scan_removes_rows_for_files_deleted_from_disk(make_file, monkeypatch):
    monkeypatch.setattr(scanner, "_extract_metadata", lambda p, t: (p.stem, None, None, None))
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
