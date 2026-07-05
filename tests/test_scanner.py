import json
import shutil
import subprocess
from pathlib import Path
from unittest import mock

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
        "video_width": None,
        "video_height": None,
        "audio_channels": None,
        "audio_stream_index": None,
        "show_name": None,
        "season_number": None,
        "episode_number": None,
        "is_movie": False,
        "is_extra": False,
        "segment_boundaries": None,
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

    rows = {Path(r["path"]).name: r for r in _rows()}
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


def test_unavailable_media_dir_does_not_wipe_its_existing_rows(monkeypatch, tmp_path):
    # Simulates an unmounted NAS / unplugged USB drive: a configured root
    # that has real scanned rows from a previous run, but isn't a directory
    # at all during this scan. Its rows must survive untouched rather than
    # being wiped just because os.walk saw nothing under it this time.
    available_dir = tmp_path / "available"
    unavailable_dir = tmp_path / "unavailable"
    available_dir.mkdir()
    unavailable_dir.mkdir()
    (available_dir / "song.mp3").write_bytes(b"data")
    (unavailable_dir / "clip.mp4").write_bytes(b"data")

    monkeypatch.setattr(settings, "get_media_dirs", lambda: [available_dir, unavailable_dir])
    scanner.scan_media_dirs()
    assert len(_rows()) == 2

    shutil.rmtree(unavailable_dir)  # the drive backing it is now "gone"
    scanner.scan_media_dirs()

    rows = _rows()
    assert len(rows) == 2
    assert {Path(r["path"]).name for r in rows} == {"song.mp3", "clip.mp4"}


def test_scan_movie_bonus_content_is_not_a_fake_movie(make_file, monkeypatch):
    monkeypatch.setattr(scanner, "_probe_video_info", lambda path, *_: (None, "h264", "aac", None, None, None, 0))
    make_file("Movies/Movie (2010)/Movie.2010.mkv")
    make_file("Movies/Movie (2010)/Special Features/bonus.mkv")

    scanner.scan_media_dirs()

    rows = {Path(r["path"]).name: r for r in _rows()}
    assert rows["Movie.2010.mkv"]["is_movie"] == 1
    assert rows["Movie.2010.mkv"]["title"] == "Movie (2010)"
    assert rows["bonus.mkv"]["is_movie"] == 0
    assert rows["bonus.mkv"]["is_extra"] == 1
    assert rows["bonus.mkv"]["title"] != "Special Features"


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
        scanner, "_probe_video_info", lambda path, *_: (None, "h264", "aac", None, None, None, 0)
    )
    make_file("The Chosen S01E02.mp4")
    make_file("random_clip.mp4")

    scanner.scan_media_dirs()

    rows = {Path(r["path"]).name: r for r in _rows()}
    assert rows["The Chosen S01E02.mp4"]["show_name"] == "The Chosen"
    assert rows["The Chosen S01E02.mp4"]["season_number"] == 1
    assert rows["The Chosen S01E02.mp4"]["episode_number"] == 2
    assert rows["random_clip.mp4"]["show_name"] is None


@pytest.mark.parametrize(
    "path,root,expected",
    [
        (
            "/media/TV/Breaking Bad/Season 1/Breaking Bad - S01E01 - Pilot.mkv",
            "/media/TV", ("Breaking Bad", 1, 1, False),
        ),
        (
            "/media/TV/Better Call Saul/Season 01/01 - Uno.mkv",
            "/media/TV", ("Better Call Saul", 1, 1, False),
        ),
        (
            "/media/TV/Show/Season 2/Episode 3.mkv",
            "/media/TV", ("Show", 2, 3, False),
        ),
        # Folder season wins over a conflicting season in the filename.
        (
            "/media/TV/Show/season 2/S01E05 - Title.mkv",
            "/media/TV", ("Show", 2, 5, False),
        ),
        (
            "/media/TV/Show/S2/07.mkv",
            "/media/TV", ("Show", 2, 7, False),
        ),
        # Trailing junk in the season folder name -> reject.
        (
            "/media/TV/Show/Season 1 (2013)/ep.mkv",
            "/media/TV", (None, None, None, False),
        ),
        # An extras-bucket folder name, but nothing on disk to confirm a
        # real show above it (these paths don't exist on disk, so the
        # sibling-season-folder check can't find anything either way) --
        # still recognized as bonus content, just with no show to
        # attribute it to (show_name stays None).
        (
            "/media/TV/Show/Extras/Bonus.mkv",
            "/media/TV", (None, None, None, True),
        ),
        # Season folder directly under the library root -- no show folder.
        (
            "/media/TV/Season 1/01 - Something.mkv",
            "/media/TV", (None, None, None, False),
        ),
        # No episode marker in the filename at all.
        (
            "/media/TV/Show/Season 1/Pilot.mkv",
            "/media/TV", (None, None, None, False),
        ),
        # A 4-digit "year" filename must never be read as an episode number.
        (
            "/media/TV/Show/Season 1/1984.mkv",
            "/media/TV", (None, None, None, False),
        ),
        # Season 00 (specials) is a legitimate season number.
        (
            "/media/TV/Show/Season 00/S00E01 - Recap.mkv",
            "/media/TV", ("Show", 0, 1, False),
        ),
    ],
)
def test_parse_folder_show_episode(path, root, expected):
    assert scanner._parse_folder_show_episode(Path(path), Path(root)) == expected


def test_parse_folder_show_episode_featurettes_inside_season_folder(tmp_path):
    # The literal reported bug: a "Featurettes" bucket folder that itself
    # contains a "Season NN"-named subfolder must still resolve to the real
    # show above it, not to "Featurettes" as if it were the show.
    show_dir = tmp_path / "Smallville (2001)"
    (show_dir / "Season 01").mkdir(parents=True)
    (show_dir / "Season 10").mkdir(parents=True)
    featurette_dir = show_dir / "Featurettes" / "Season 10"
    featurette_dir.mkdir(parents=True)
    video = featurette_dir / "Back in the Jacket - A Smallville Homecoming.mkv"
    video.touch()

    assert scanner._parse_folder_show_episode(video, tmp_path) == (
        "Smallville (2001)", None, None, True,
    )


def test_parse_folder_show_episode_extras_bucket_directly_under_show(tmp_path):
    show_dir = tmp_path / "Show"
    (show_dir / "Season 01").mkdir(parents=True)
    (show_dir / "Featurettes").mkdir()
    video = show_dir / "Featurettes" / "Making Of.mkv"
    video.touch()

    assert scanner._parse_folder_show_episode(video, tmp_path) == ("Show", None, None, True)


def test_parse_folder_show_episode_extras_bucket_inside_season_folder(tmp_path):
    show_dir = tmp_path / "Show"
    season_dir = show_dir / "Season 03"
    season_dir.mkdir(parents=True)
    extras_dir = season_dir / "Deleted Scenes"
    extras_dir.mkdir()
    video = extras_dir / "Cut Scene.mkv"
    video.touch()

    assert scanner._parse_folder_show_episode(video, tmp_path) == ("Show", None, None, True)


def test_parse_folder_show_episode_loose_extras_file_in_season_folder(tmp_path):
    show_dir = tmp_path / "Show"
    season_dir = show_dir / "Season 01"
    season_dir.mkdir(parents=True)
    video = season_dir / "Gag Reel.mkv"
    video.touch()

    assert scanner._parse_folder_show_episode(video, tmp_path) == ("Show", None, None, True)


def test_parse_folder_show_episode_movie_special_features_not_mistaken_for_show(tmp_path):
    # A movie's own bonus-features folder must never fabricate a phantom
    # one-episode "TV show" -- there's no real Season NN folder anywhere
    # near "Movie (2010)", so show_name stays None. It's still recognized
    # as bonus content (is_extra=True), just with no show to attach it to
    # -- this is what keeps it from being retitled/counted as a second
    # "movie" named after its bucket folder (see is_movie_folder in
    # scan_media_dirs and test_scan_movie_bonus_content_is_not_a_fake_movie).
    movie_dir = tmp_path / "Movie (2010)"
    features_dir = movie_dir / "Special Features"
    features_dir.mkdir(parents=True)
    video = features_dir / "bonus.mkv"
    video.touch()

    assert scanner._parse_folder_show_episode(video, tmp_path) == (None, None, None, True)


def test_parse_folder_show_episode_extras_keyword_not_trailing_is_not_flagged(tmp_path):
    # A real episode filename that happens to contain an extras keyword in
    # a non-trailing position must never be misflagged as bonus content.
    show_dir = tmp_path / "Show"
    season_dir = show_dir / "Season 01"
    season_dir.mkdir(parents=True)
    video = season_dir / "01 - Interview with the Vampire.mkv"
    video.touch()

    show_name, season_number, episode_number, is_extra = scanner._parse_folder_show_episode(
        video, tmp_path
    )
    assert is_extra is False
    assert show_name == "Show"
    assert season_number == 1
    assert episode_number == 1


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
    monkeypatch.setattr(scanner, "_probe_video_info", lambda path, *_: (None, "h264", "aac", None, None, None, 0))
    make_file("TV/Breaking Bad/Season 1/Breaking Bad - S01E01 - Pilot.mkv")

    scanner.scan_media_dirs()

    row = _rows()[0]
    assert row["show_name"] == "Breaking Bad"
    assert row["season_number"] == 1
    assert row["episode_number"] == 1


def test_scan_leading_number_episode_style_under_season_folder(make_file, monkeypatch):
    monkeypatch.setattr(scanner, "_probe_video_info", lambda path, *_: (None, "h264", "aac", None, None, None, 0))
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
    monkeypatch.setattr(scanner, "_probe_video_info", lambda path, *_: (None, "h264", "aac", None, None, None, 0))
    make_file("Old Show S01E02.mkv")

    scanner.scan_media_dirs()

    row = _rows()[0]
    assert row["show_name"] == "Old Show"
    assert row["season_number"] == 1
    assert row["episode_number"] == 2


@pytest.mark.parametrize(
    "path,root,expected",
    [
        # The literal M8 bug: a real season pack whose filenames carry no
        # show prefix, inside a release-named folder.
        (
            "/media/Movies/House of David [2025] S02 Dual YG/S02E06 Forged in Fire.mkv",
            "/media/Movies", ("House of David", 2, 6),
        ),
        ("/media/TV/show [x265]/s01e02.mkv", "/media/TV", ("show", 1, 2)),
        # Dotted release folder: season token and everything after it is
        # junk, separators collapse to spaces.
        (
            "/media/TV/The.Wire.S03.1080p.WEBRip/S03E01.mkv",
            "/media/TV", ("The Wire", 3, 1),
        ),
        # Directly under the library root: no folder to name the show.
        ("/media/TV/S01E01 Pilot.mkv", "/media/TV", (None, None, None)),
        # The folder is nothing but a season marker once cleaned -- the
        # deliberate "don't use the library root as a show name" rejection
        # in the season-folder heuristic must not be re-guessed here.
        (
            "/media/TV/Season 2 (2013)/S02E01 Title.mkv",
            "/media/TV", (None, None, None),
        ),
        # A show prefix in the filename means this isn't the bare-tag
        # convention -- that's _parse_show_episode's territory.
        (
            "/media/TV/Somewhere/Show S01E02.mkv",
            "/media/TV", (None, None, None),
        ),
        # "S1m0ne"-style titles: S+digit not followed by E+digits.
        ("/media/Movies/Folder/S1m0ne.mkv", "/media/Movies", (None, None, None)),
    ],
)
def test_parse_bare_episode_with_folder_show(path, root, expected):
    assert scanner._parse_bare_episode_with_folder_show(Path(path), Path(root)) == expected


def test_scan_groups_bare_episode_tags_under_the_release_folder_show(make_file, monkeypatch):
    # Regression for M8 (confirmed live): "House of David [2025] S02 Dual
    # YG/S02E06 Forged in Fire.mkv" classified every episode as its own
    # movie -- no show prefix in the filenames for the flat heuristic, and
    # the folder isn't a plain season folder for the folder heuristic.
    monkeypatch.setattr(scanner, "_probe_video_info", lambda path, *_: (None, "h264", "aac", None, None, None, 0))
    make_file("House of David [2025] S02 Dual YG/S02E01 A Tale of Two Swords.mkv")
    make_file("House of David [2025] S02 Dual YG/S02E06 Forged in Fire.mkv")

    scanner.scan_media_dirs()

    rows = {Path(r["path"]).name: r for r in _rows()}
    for name, episode in [
        ("S02E01 A Tale of Two Swords.mkv", 1),
        ("S02E06 Forged in Fire.mkv", 6),
    ]:
        assert rows[name]["show_name"] == "House of David"
        assert rows[name]["season_number"] == 2
        assert rows[name]["episode_number"] == episode
        assert not rows[name]["is_movie"]


def test_scan_derives_movie_title_from_folder_name(make_file, monkeypatch):
    monkeypatch.setattr(scanner, "_probe_video_info", lambda path, *_: (None, "h264", "aac", None, None, None, 0))
    make_file("Movies/Inception (2010)/Inception.2010.1080p.BluRay.x264-GROUP.mkv")

    scanner.scan_media_dirs()

    row = _rows()[0]
    assert row["title"] == "Inception (2010)"
    assert row["show_name"] is None
    assert row["is_movie"] == 1
    assert row["is_extra"] == 0


def test_scan_loose_movie_at_library_root_keeps_filename_title(make_file, monkeypatch):
    # Regression test found on a real collection: a lone movie file sitting
    # directly in the configured library root (no dedicated subfolder at
    # all, e.g. "Braveheart.1995....mkv" loose alongside other folders)
    # satisfied the same "one video, no season subfolder" condition as a
    # real movie folder -- but path.parent for a root-level file *is* the
    # library root itself, so its title got silently overwritten with the
    # library root's own configured folder name (e.g. "Movies") instead of
    # keeping anything derived from the actual filename.
    monkeypatch.setattr(scanner, "_probe_video_info", lambda path, *_: (None, "h264", "aac", None, None, None, 0))
    make_file("Braveheart.1995.Bluray.1080p.AV1.OPUS.5.1-UH.mkv")

    scanner.scan_media_dirs()

    row = _rows()[0]
    assert row["title"] == "Braveheart.1995.Bluray.1080p.AV1.OPUS.5.1-UH"
    assert row["is_movie"] == 1


def test_scan_persists_is_movie_and_is_extra_flags(make_file, monkeypatch):
    monkeypatch.setattr(scanner, "_probe_video_info", lambda path, *_: (None, "h264", "aac", None, None, None, 0))
    make_file("TV/Smallville/Season 01/Smallville - S01E01 - Pilot.mkv")
    make_file("TV/Smallville/Season 10/Smallville - S10E01 - Lazarus.mkv")
    make_file("TV/Smallville/Featurettes/Season 10/Homecoming.mkv")
    make_file("Movies/Her (2013)/Her.2013.mkv")
    make_file("song.mp3")

    scanner.scan_media_dirs()

    rows = {Path(r["path"]).name: r for r in _rows()}
    assert rows["Smallville - S01E01 - Pilot.mkv"]["is_movie"] == 0
    assert rows["Smallville - S01E01 - Pilot.mkv"]["is_extra"] == 0
    assert rows["Homecoming.mkv"]["is_movie"] == 0
    assert rows["Homecoming.mkv"]["is_extra"] == 1
    assert rows["Homecoming.mkv"]["show_name"] == "Smallville"
    assert rows["Her.2013.mkv"]["is_movie"] == 1
    assert rows["Her.2013.mkv"]["is_extra"] == 0
    assert rows["song.mp3"]["is_movie"] == 0
    assert rows["song.mp3"]["is_extra"] == 0


@pytest.mark.parametrize(
    "audio_streams,expected_index",
    [
        # (position, codec, channels, language) tuples per stream.
        # English present but not first -> still picked over French.
        ([(0, "ac3", 6, "fre"), (1, "ac3", 6, "eng")], 1),
        # No language tags at all -- untagged must not be treated as
        # "wrong", first stream wins.
        ([(0, "ac3", 6, None), (1, "ac3", 2, None)], 0),
        # Only a non-English track exists -- fall back to first rather
        # than dropping audio entirely.
        ([(0, "ac3", 6, "fre")], 0),
        # "en" (not just "eng") must also count as English.
        ([(0, "ac3", 6, "jpn"), (1, "ac3", 2, "en")], 1),
        # No audio streams at all.
        ([], None),
    ],
)
def test_choose_audio_stream(audio_streams, expected_index):
    chosen = scanner._choose_audio_stream(audio_streams)
    if expected_index is None:
        assert chosen is None
    else:
        assert chosen[0] == expected_index


@requires_ffmpeg
def test_scan_picks_english_audio_track_over_earlier_non_english_one(make_file, h264_encoder):
    path = make_file("multitrack.mkv")
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=blue:size=64x64:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
            "-map", "0:v", "-map", "1:a", "-map", "2:a",
            "-c:v", h264_encoder, "-c:a", "ac3",
            "-metadata:s:a:0", "language=fre",
            "-metadata:s:a:1", "language=eng",
            str(path),
        ],
        check=True,
    )

    scanner.scan_media_dirs()

    row = _rows()[0]
    assert row["audio_stream_index"] == 1


@requires_ffmpeg
def test_scan_falls_back_to_first_track_when_no_english_available(make_file, h264_encoder):
    path = make_file("foreign_only.mkv")
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=blue:size=64x64:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", h264_encoder, "-c:a", "ac3",
            "-metadata:s:a:0", "language=fre",
            str(path),
        ],
        check=True,
    )

    scanner.scan_media_dirs()

    row = _rows()[0]
    assert row["audio_stream_index"] == 0


@requires_ffmpeg
def test_scan_records_multichannel_audio_channel_count(make_file, h264_encoder):
    path = make_file("surround.mkv")
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=blue:size=64x64:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-filter_complex", "[1:a]pan=5.1|FL=c0|FR=c0|FC=c0|LFE=c0|BL=c0|BR=c0[a51]",
            "-map", "0:v", "-map", "[a51]",
            "-c:v", h264_encoder, "-c:a", "ac3",
            str(path),
        ],
        check=True,
    )

    scanner.scan_media_dirs()

    row = _rows()[0]
    assert row["audio_channels"] == 6


def test_scan_leaves_ambiguous_multi_video_folder_titles_alone(make_file, monkeypatch):
    # Two real videos, no season structure -- can't tell which "is" the
    # movie, so filenames keep their existing titles.
    monkeypatch.setattr(scanner, "_probe_video_info", lambda path, *_: (None, "h264", "aac", None, None, None, 0))
    make_file("Movies/Double Feature/Movie One.mkv")
    make_file("Movies/Double Feature/Movie Two.mkv")

    scanner.scan_media_dirs()

    rows = {r["title"] for r in _rows()}
    assert rows == {"Movie One", "Movie Two"}


def test_scan_excludes_trailer_files_from_the_library(make_file, monkeypatch):
    monkeypatch.setattr(scanner, "_probe_video_info", lambda path, *_: (None, "h264", "aac", None, None, None, 0))
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
    monkeypatch.setattr(scanner, "_probe_video_info", lambda path, *_: (None, "h264", "aac", None, None, None, 0))
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
    monkeypatch.setattr(scanner, "_probe_video_info", lambda path, *_: (None, "h264", "aac", None, None, None, 0))
    make_file("TV/Breaking Bad/Season 3/Breaking Bad - S03E01 - No Mas.mkv")

    scanner.scan_media_dirs()

    row = _rows()[0]
    assert row["show_name"] == "Breaking Bad"
    assert row["title"] != "Season 3"


@requires_ffmpeg
def test_real_mp4_video_and_audio_codecs_are_recorded(media_dir, h264_encoder):
    mp4_path = media_dir / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=blue:size=64x64:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", h264_encoder, "-c:a", "aac", "-shortest",
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
def test_mkv_with_no_header_duration_falls_back_to_packet_scan(media_dir, h264_encoder):
    # Piping ffmpeg's matroska output through stdout (rather than writing
    # directly to a seekable file) means the muxer can never seek back to
    # write Segment Duration -- this reproduces real "Featurettes"/bonus-
    # content files from certain scene releases, where format.duration
    # comes back empty even though the file plays fine.
    mkv_path = media_dir / "featurette.mkv"
    proc = subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=blue:size=64x64:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", h264_encoder, "-c:a", "aac", "-shortest",
            "-f", "matroska", "-",
        ],
        check=True, capture_output=True,
    )
    mkv_path.write_bytes(proc.stdout)

    scanner.scan_media_dirs()

    row = _rows()[0]
    assert row["media_type"] == "video"
    assert row["duration"] == pytest.approx(1.0, abs=0.3)


def _make_headerless_mkv(path, duration_seconds, encoder):
    # Same trick as test_mkv_with_no_header_duration_falls_back_to_packet_scan:
    # piping ffmpeg's matroska output through stdout means the muxer can
    # never seek back to write Segment Duration.
    proc = subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c=blue:size=64x64:duration={duration_seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration_seconds}",
            "-c:v", encoder, "-c:a", "aac", "-shortest",
            "-f", "matroska", "-",
        ],
        check=True, capture_output=True,
    )
    path.write_bytes(proc.stdout)


@requires_ffmpeg
def test_packet_scan_duration_is_cached_and_not_repeated_on_unchanged_rescan(media_dir, h264_encoder):
    # Regression test: this fallback's cost scales with a file's duration/
    # bitrate (proportionally expensive for a large TV episode), so
    # re-running it on every single rescan of an unchanged file would be
    # a real, size-correlated performance problem -- it must only run once
    # per file, not once per scan.
    mkv_path = media_dir / "featurette.mkv"
    _make_headerless_mkv(mkv_path, duration_seconds=1, encoder=h264_encoder)

    call_count = {"n": 0}
    real_fallback = scanner._probe_duration_via_packets

    def counting_fallback(path):
        call_count["n"] += 1
        return real_fallback(path)

    with mock.patch.object(scanner, "_probe_duration_via_packets", counting_fallback):
        scanner.scan_media_dirs()
        assert call_count["n"] == 1
        first_duration = _rows()[0]["duration"]
        assert first_duration is not None

        scanner.scan_media_dirs()  # rescan, file unchanged
        assert call_count["n"] == 1  # not called again
        assert _rows()[0]["duration"] == first_duration


@requires_ffmpeg
def test_packet_scan_duration_reruns_if_file_size_changes(media_dir, h264_encoder):
    mkv_path = media_dir / "featurette.mkv"
    _make_headerless_mkv(mkv_path, duration_seconds=1, encoder=h264_encoder)

    call_count = {"n": 0}
    real_fallback = scanner._probe_duration_via_packets

    def counting_fallback(path):
        call_count["n"] += 1
        return real_fallback(path)

    with mock.patch.object(scanner, "_probe_duration_via_packets", counting_fallback):
        scanner.scan_media_dirs()
        assert call_count["n"] == 1

        # Replace with a different-duration (different-size) render --
        # the cached duration must not be reused across a real content
        # change.
        _make_headerless_mkv(mkv_path, duration_seconds=3, encoder=h264_encoder)
        scanner.scan_media_dirs()
        assert call_count["n"] == 2


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


def test_hard_failure_on_one_file_does_not_abort_the_scan(make_file):
    # Regression test for a real bug: before this, _upsert_media had no
    # try/except around path.stat()/the DB upsert, so one vanished/corrupt
    # file raised all the way out of scan_media_dirs's loop and silently
    # skipped every file that would have come after it in the walk.
    make_file("good1.mp3")
    bad_path = make_file("vanishes.mp3")
    make_file("good2.mp3")

    real_upsert = scanner._upsert_media

    def flaky_upsert(conn, path, *args, **kwargs):
        if path == bad_path:
            path.unlink()  # forces a real FileNotFoundError from path.stat()
        return real_upsert(conn, path, *args, **kwargs)

    scanner.start_scan()
    with mock.patch.object(scanner, "_upsert_media", side_effect=flaky_upsert):
        scanner.scan_media_dirs()
    scanner._scan_lock.release()

    rows = {Path(r["path"]).name for r in _rows()}
    assert rows == {"good1.mp3", "good2.mp3"}
    assert scanner._scan_state["scanned_count"] == 2
    assert scanner._scan_state["failed_count"] == 1
    assert scanner._scan_state["failed_examples"][0]["path"] == str(bad_path)
    assert "FileNotFoundError" in scanner._scan_state["failed_examples"][0]["error"]


def test_incomplete_metadata_is_tracked_without_failing_the_file(make_file, monkeypatch):
    make_file("good.mkv")
    make_file("no_duration.mkv")

    def flaky_probe(path, *_):
        if path.name == "no_duration.mkv":
            return None, None, None, None, None, None, None
        return 100.0, "h264", "aac", 1920, 1080, 2, 0

    monkeypatch.setattr(scanner, "_probe_video_info", flaky_probe)
    scanner.start_scan()
    scanner.scan_media_dirs()
    scanner._scan_lock.release()

    rows = {Path(r["path"]).name: r for r in _rows()}
    assert rows["no_duration.mkv"]["duration"] is None
    assert scanner._scan_state["scanned_count"] == 2
    assert scanner._scan_state["failed_count"] == 0
    assert scanner._scan_state["incomplete_count"] == 1
    assert scanner._scan_state["incomplete_examples"][0]["path"].endswith("no_duration.mkv")


def test_incomplete_metadata_not_flagged_for_a_legitimately_silent_video(make_file, monkeypatch):
    # A real video with no audio track has audio_codec=None without ffprobe
    # having failed at all -- must not be misreported as incomplete.
    make_file("silent.mkv")
    monkeypatch.setattr(scanner, "_probe_video_info", lambda path, *_: (100.0, "h264", None, None, None, None, None))

    scanner.start_scan()
    scanner.scan_media_dirs()
    scanner._scan_lock.release()

    assert scanner._scan_state["incomplete_count"] == 0


def test_failed_examples_are_capped_but_failed_count_keeps_counting(make_file):
    for i in range(scanner._MAX_DIAGNOSTIC_EXAMPLES + 5):
        make_file(f"bad{i}.mp3")

    scanner.start_scan()
    with mock.patch.object(scanner, "_upsert_media", side_effect=RuntimeError("boom")):
        scanner.scan_media_dirs()
    scanner._scan_lock.release()

    assert scanner._scan_state["failed_count"] == scanner._MAX_DIAGNOSTIC_EXAMPLES + 5
    assert len(scanner._scan_state["failed_examples"]) == scanner._MAX_DIAGNOSTIC_EXAMPLES


def _probe_result(duration=14.0, video_codec="h264", audio_codec="ac3",
                  width=640, height=480, channels=2, stream_index=0):
    return (duration, video_codec, audio_codec, width, height, channels, stream_index)


def test_scan_stores_segment_boundaries_for_files_that_need_hls(make_file, monkeypatch):
    make_file("clip.mkv")  # mkv container + h264 -> routes through HLS
    monkeypatch.setattr(scanner, "_probe_video_info", lambda path, *_: _probe_result())
    monkeypatch.setattr(scanner, "probe_keyframes", lambda path: [0.0, 6.0, 12.0])

    scanner.scan_media_dirs()

    row = _rows()[0]
    assert json.loads(row["segment_boundaries"]) == [0.0, 6.0, 12.0]


def test_scan_skips_the_keyframe_probe_for_direct_play_files(make_file, monkeypatch):
    # The keyframe probe walks every packet -- for an mp4/h264/aac file
    # that will never touch the HLS path, running it would make scans of a
    # mostly-compatible library expensive for nothing.
    make_file("clip.mp4")
    monkeypatch.setattr(scanner, "_probe_video_info", lambda path, *_: _probe_result(audio_codec="aac"))
    probe_calls = []
    monkeypatch.setattr(scanner, "probe_keyframes", lambda path: probe_calls.append(path) or [0.0])

    scanner.scan_media_dirs()

    assert probe_calls == []
    assert _rows()[0]["segment_boundaries"] is None


def test_boundaries_are_cached_and_not_reprobed_on_unchanged_rescan(make_file, monkeypatch):
    # Same path+size caching as the packet-scan duration fallback, for the
    # same reason: both walk every packet of the file.
    make_file("clip.mkv")
    monkeypatch.setattr(scanner, "_probe_video_info", lambda path, *_: _probe_result())
    probe_calls = []

    def fake_probe(path):
        probe_calls.append(path)
        return [0.0, 6.0, 12.0]

    monkeypatch.setattr(scanner, "probe_keyframes", fake_probe)

    scanner.scan_media_dirs()
    scanner.scan_media_dirs()

    assert len(probe_calls) == 1
    assert json.loads(_rows()[0]["segment_boundaries"]) == [0.0, 6.0, 12.0]


def test_legacy_row_gaining_boundaries_invalidates_its_cached_segments(make_file, monkeypatch):
    # A row scanned before the segment_boundaries column existed can have
    # segments cached on the old fixed 6s grid -- once real boundaries are
    # computed for it, those segments describe different cut points than
    # the playlist will now advertise and must not survive.
    from app import transcode

    make_file("clip.mkv")
    monkeypatch.setattr(scanner, "_probe_video_info", lambda path, *_: _probe_result())
    monkeypatch.setattr(scanner, "probe_keyframes", lambda path: [0.0, 6.0, 12.0])

    scanner.scan_media_dirs()
    row = _rows()[0]

    # Simulate the legacy state: boundaries missing, stale segments cached.
    with get_connection() as conn:
        conn.execute("UPDATE media SET segment_boundaries = NULL WHERE id = ?", (row["id"],))
    stale = transcode.hls_dir_for(row["id"]) / "segment_00000.ts"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"cut on the old fixed grid")

    scanner.scan_media_dirs()

    assert json.loads(_rows()[0]["segment_boundaries"]) == [0.0, 6.0, 12.0]
    assert not stale.exists()
