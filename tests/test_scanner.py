import shutil
import subprocess
from pathlib import Path

import pytest

from app import scanner, settings
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
        lambda path, media_type, *a, **kw: _metadata(title=path.stem, artist="Artist", album="Album", duration=42.0),
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

    def fake_extract(path, media_type, *args, **kwargs):
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
    monkeypatch.setattr(scanner, "_extract_metadata", lambda p, t, *a, **kw: _metadata(title=p.stem))
    f = make_file("song.mp3")
    scanner.scan_media_dirs()
    assert len(_rows()) == 1

    f.unlink()
    scanner.scan_media_dirs()

    assert _rows() == []


def test_ignores_configured_dir_that_does_not_exist(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "get_media_dirs", lambda: [tmp_path / "does-not-exist"])
    scanner.scan_media_dirs()  # should not raise
    assert _rows() == []


def test_symlinked_file_is_not_scanned(media_dir, tmp_path, monkeypatch):
    # A symlink inside a scanned dir could point anywhere on disk regardless
    # of its own filename -- must never be scanned/served, even if it looks
    # like an ordinary media file.
    secret = tmp_path / "secret.txt"
    secret.write_text("should never be exposed")
    (media_dir / "innocent_song.mp3").symlink_to(secret)

    scanner.scan_media_dirs()

    assert _rows() == []


def test_symlinked_directory_is_not_traversed(media_dir, tmp_path, monkeypatch):
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "secret.mp3").write_bytes(b"data")
    (media_dir / "linked_dir").symlink_to(outside_dir, target_is_directory=True)

    scanner.scan_media_dirs()

    assert _rows() == []


def test_real_file_alongside_a_symlink_is_still_scanned(media_dir, make_file, tmp_path, monkeypatch):
    make_file("real_song.mp3")
    (media_dir / "symlinked_song.mp3").symlink_to(tmp_path / "does-not-matter.mp3")

    scanner.scan_media_dirs()

    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["path"].endswith("real_song.mp3")


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


@pytest.mark.parametrize(
    "path,root,expected",
    [
        (
            "/media/TV/Breaking Bad/Season 1/Breaking Bad - S01E01 - Pilot.mkv",
            "/media/TV", ("Breaking Bad", 1, 1),
        ),
        (
            "/media/TV/Better Call Saul/Season 01/01 - Uno.mkv",
            "/media/TV", ("Better Call Saul", 1, 1),
        ),
        (
            "/media/TV/Show/Season 2/Episode 3.mkv",
            "/media/TV", ("Show", 2, 3),
        ),
        # Folder season wins over a conflicting season in the filename.
        (
            "/media/TV/Show/season 2/S01E05 - Title.mkv",
            "/media/TV", ("Show", 2, 5),
        ),
        (
            "/media/TV/Show/S2/07.mkv",
            "/media/TV", ("Show", 2, 7),
        ),
        # Trailing junk in the season folder name -> reject.
        (
            "/media/TV/Show/Season 1 (2013)/ep.mkv",
            "/media/TV", (None, None, None),
        ),
        # Not a season folder at all.
        (
            "/media/TV/Show/Extras/Bonus.mkv",
            "/media/TV", (None, None, None),
        ),
        # Season folder directly under the library root -- no show folder.
        (
            "/media/TV/Season 1/01 - Something.mkv",
            "/media/TV", (None, None, None),
        ),
        # No episode marker in the filename at all.
        (
            "/media/TV/Show/Season 1/Pilot.mkv",
            "/media/TV", (None, None, None),
        ),
        # A 4-digit "year" filename must never be read as an episode number.
        (
            "/media/TV/Show/Season 1/1984.mkv",
            "/media/TV", (None, None, None),
        ),
        # Season 00 (specials) is a legitimate season number.
        (
            "/media/TV/Show/Season 00/S00E01 - Recap.mkv",
            "/media/TV", ("Show", 0, 1),
        ),
    ],
)
def test_parse_folder_show_episode(path, root, expected):
    assert scanner._parse_folder_show_episode(Path(path), Path(root)) == expected


@pytest.mark.parametrize(
    "stem,is_trailer",
    [
        ("trailer", True),
        ("sample", True),
        ("Inception-trailer", True),
        ("Inception.trailer", True),
        ("Inception (Trailer)", True),
        ("sample1", True),
        ("Trailer-2", True),
        ("samples", True),
        # "trailer" appears but isn't the trailing token -- a real title.
        ("Trailer Park Boys", False),
        ("Inception", False),
    ],
)
def test_trailer_sample_regex(stem, is_trailer):
    assert bool(scanner._TRAILER_SAMPLE_RE.search(stem)) is is_trailer


def test_scan_populates_show_fields_from_season_folder_structure(make_file, monkeypatch):
    monkeypatch.setattr(scanner, "_probe_video_info", lambda path: (None, "h264", "aac"))
    make_file("TV/Breaking Bad/Season 1/Breaking Bad - S01E01 - Pilot.mkv")

    scanner.scan_media_dirs()

    row = _rows()[0]
    assert row["show_name"] == "Breaking Bad"
    assert row["season_number"] == 1
    assert row["episode_number"] == 1


def test_scan_leading_number_episode_style_under_season_folder(make_file, monkeypatch):
    monkeypatch.setattr(scanner, "_probe_video_info", lambda path: (None, "h264", "aac"))
    make_file("TV/Better Call Saul/Season 01/01 - Uno.mkv")

    scanner.scan_media_dirs()

    row = _rows()[0]
    assert row["show_name"] == "Better Call Saul"
    assert row["season_number"] == 1
    assert row["episode_number"] == 1


def test_scan_flat_filename_style_is_unaffected_by_folder_feature(make_file, monkeypatch):
    # Regression: a plain "Show S01E02" filename directly in the scanned
    # root (no season subfolder at all) must keep resolving via the
    # existing filename-only regex, unchanged.
    monkeypatch.setattr(scanner, "_probe_video_info", lambda path: (None, "h264", "aac"))
    make_file("Old Show S01E02.mkv")

    scanner.scan_media_dirs()

    row = _rows()[0]
    assert row["show_name"] == "Old Show"
    assert row["season_number"] == 1
    assert row["episode_number"] == 2


def test_scan_derives_movie_title_from_folder_name(make_file, monkeypatch):
    monkeypatch.setattr(scanner, "_probe_video_info", lambda path: (None, "h264", "aac"))
    make_file("Movies/Inception (2010)/Inception.2010.1080p.BluRay.x264-GROUP.mkv")

    scanner.scan_media_dirs()

    row = _rows()[0]
    assert row["title"] == "Inception (2010)"
    assert row["show_name"] is None


def test_scan_leaves_ambiguous_multi_video_folder_titles_alone(make_file, monkeypatch):
    # Two real videos, no season structure -- can't tell which "is" the
    # movie, so filenames keep their existing titles.
    monkeypatch.setattr(scanner, "_probe_video_info", lambda path: (None, "h264", "aac"))
    make_file("Movies/Double Feature/Movie One.mkv")
    make_file("Movies/Double Feature/Movie Two.mkv")

    scanner.scan_media_dirs()

    rows = {r["title"] for r in _rows()}
    assert rows == {"Movie One", "Movie Two"}


def test_scan_excludes_trailer_files_from_the_library(make_file, monkeypatch):
    monkeypatch.setattr(scanner, "_probe_video_info", lambda path: (None, "h264", "aac"))
    make_file("Movies/Inception (2010)/Inception.mkv")
    make_file("Movies/Inception (2010)/Inception-trailer.mkv")

    scanner.scan_media_dirs()

    rows = _rows()
    assert len(rows) == 1
    assert Path(rows[0]["path"]).name == "Inception.mkv"
    # The lone real video (trailer excluded from the count) still gets the
    # folder-derived title.
    assert rows[0]["title"] == "Inception (2010)"


def test_scan_removes_previously_scanned_file_once_renamed_to_look_like_a_trailer(make_file, monkeypatch):
    monkeypatch.setattr(scanner, "_probe_video_info", lambda path: (None, "h264", "aac"))
    f = make_file("Movies/Inception (2010)/Inception.mkv")
    scanner.scan_media_dirs()
    assert len(_rows()) == 1

    renamed = f.parent / "Inception-trailer.mkv"
    f.rename(renamed)
    scanner.scan_media_dirs()

    assert _rows() == []


def test_scan_does_not_retitle_a_season_folder_with_only_one_episode_so_far(make_file, monkeypatch):
    # A season folder with just one ripped episode also happens to be "a
    # folder with exactly one real video and no season subfolders inside
    # it" -- must not be mistaken for a movie folder and retitled to the
    # season folder's own name.
    monkeypatch.setattr(scanner, "_probe_video_info", lambda path: (None, "h264", "aac"))
    make_file("TV/Breaking Bad/Season 3/Breaking Bad - S03E01 - No Mas.mkv")

    scanner.scan_media_dirs()

    row = _rows()[0]
    assert row["show_name"] == "Breaking Bad"
    assert row["title"] != "Season 3"


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
