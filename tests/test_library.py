import shutil
import subprocess

import pytest

from app import scanner
from app.db import get_connection

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)


def _insert_media(
    path, media_type="audio", show_name=None, season_number=None, episode_number=None,
    duration=None, title=None, artist=None, album=None, is_movie=False, is_extra=False,
):
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO media
                (path, media_type, title, artist, album, size_bytes,
                 show_name, season_number, episode_number, duration, is_movie, is_extra)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(path), media_type, title or path.stem, artist, album, path.stat().st_size,
                show_name, season_number, episode_number, duration,
                int(is_movie), int(is_extra),
            ),
        )
        return cur.lastrowid


def test_list_media_empty_by_default(client):
    res = client.get("/api/library")
    assert res.status_code == 200
    assert res.json() == {"items": [], "total": 0, "limit": 100, "offset": 0}


def test_list_media_filters_by_type(client, make_file):
    audio = make_file("song.mp3", b"a")
    video = make_file("clip.mp4", b"v")
    _insert_media(audio, media_type="audio")
    _insert_media(video, media_type="video")

    res = client.get("/api/library", params={"media_type": "audio"})

    body = res.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["media_type"] == "audio"


def test_list_media_is_paginated(client, make_file):
    for i in range(5):
        f = make_file(f"song{i}.mp3", b"a")
        _insert_media(f)

    page1 = client.get("/api/library", params={"limit": 2, "offset": 0}).json()
    page2 = client.get("/api/library", params={"limit": 2, "offset": 2}).json()

    assert page1["total"] == 5
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 2
    assert {i["id"] for i in page1["items"]}.isdisjoint({i["id"] for i in page2["items"]})


def test_list_media_limit_is_clamped_to_a_sane_range(client):
    too_big = client.get("/api/library", params={"limit": 10000}).json()
    assert too_big["limit"] == 500

    too_small = client.get("/api/library", params={"limit": 0}).json()
    assert too_small["limit"] == 1


def test_list_media_sorts_case_insensitively(client, make_file):
    # SQLite's default BINARY collation would sort "Zebra" before "apple"
    # (all uppercase before all lowercase) -- that reads as broken
    # alphabetical order to a user.
    _insert_media(make_file("a.mp3"), title="apple")
    _insert_media(make_file("b.mp3"), title="Banana")
    _insert_media(make_file("c.mp3"), title="Zebra")

    res = client.get("/api/library")

    titles = [i["title"] for i in res.json()["items"]]
    assert titles == ["apple", "Banana", "Zebra"]


def test_list_shows_sorts_case_insensitively(client, make_file):
    _insert_media(make_file("a.mp4"), "video", show_name="apple show", season_number=1, episode_number=1)
    _insert_media(make_file("b.mp4"), "video", show_name="Banana Show", season_number=1, episode_number=1)
    _insert_media(make_file("c.mp4"), "video", show_name="Zebra Show", season_number=1, episode_number=1)

    res = client.get("/api/shows")

    names = [s["show_name"] for s in res.json()]
    assert names == ["apple show", "Banana Show", "Zebra Show"]


def test_search_matches_title(client, make_file):
    _insert_media(make_file("a.mp3"), title="Bohemian Rhapsody")
    _insert_media(make_file("b.mp3"), title="Some Other Song")

    res = client.get("/api/library", params={"q": "rhapsody"})

    body = res.json()
    assert body["total"] == 1
    assert body["items"][0]["title"] == "Bohemian Rhapsody"


def test_search_is_case_insensitive_and_matches_substrings(client, make_file):
    _insert_media(make_file("a.mp3"), title="Bohemian Rhapsody")

    res = client.get("/api/library", params={"q": "RHAP"})

    assert res.json()["total"] == 1


def test_search_matches_artist_album_and_show_name(client, make_file):
    _insert_media(make_file("a.mp3"), title="Track One", artist="Queen")
    _insert_media(make_file("b.mp3"), title="Track Two", album="Queen's Greatest Hits")
    _insert_media(
        make_file("c.mp4"), media_type="video", title="Ep",
        show_name="Queen: The Story", season_number=1, episode_number=1,
    )
    _insert_media(make_file("d.mp3"), title="Unrelated", artist="Nobody")

    res = client.get("/api/library", params={"q": "queen"})

    titles = {i["title"] for i in res.json()["items"]}
    assert titles == {"Track One", "Track Two", "Ep"}


def test_search_combines_with_other_filters(client, make_file):
    _insert_media(make_file("a.mp3"), media_type="audio", title="Overlap")
    _insert_media(make_file("b.mp4"), media_type="video", title="Overlap")

    res = client.get("/api/library", params={"q": "overlap", "media_type": "audio"})

    body = res.json()
    assert body["total"] == 1
    assert body["items"][0]["media_type"] == "audio"


def test_search_with_no_matches_returns_empty(client, make_file):
    _insert_media(make_file("a.mp3"), title="Something")

    res = client.get("/api/library", params={"q": "nonexistent"})

    assert res.json() == {"items": [], "total": 0, "limit": 100, "offset": 0}


def test_list_shows_groups_by_show_name_with_episode_counts(client, make_file):
    _insert_media(make_file("chosen1.mp4"), "video", show_name="The Chosen", season_number=1, episode_number=1)
    _insert_media(make_file("chosen2.mp4"), "video", show_name="The Chosen", season_number=1, episode_number=2)
    _insert_media(make_file("other.mp4"), "video", show_name="Other Show", season_number=1, episode_number=1)
    _insert_media(make_file("movie.mp4"), "video", is_movie=True)  # no show_name -- shouldn't appear

    res = client.get("/api/shows")

    assert res.status_code == 200
    shows = {s["show_name"]: s["episode_count"] for s in res.json()}
    assert shows == {"The Chosen": 2, "Other Show": 1}


def test_list_shows_excludes_extras_from_counts_and_grouping(client, make_file):
    _insert_media(make_file("chosen1.mp4"), "video", show_name="The Chosen", season_number=1, episode_number=1)
    _insert_media(make_file("extra.mp4"), "video", show_name="The Chosen", is_extra=True)
    _insert_media(make_file("phantom.mp4"), "video", show_name="Phantom Extras Only", is_extra=True)

    res = client.get("/api/shows")

    shows = {s["show_name"]: s["episode_count"] for s in res.json()}
    assert shows == {"The Chosen": 1}


def test_list_shows_includes_sample_media_id_for_poster_tiles(client, make_file):
    id1 = _insert_media(make_file("chosen1.mp4"), "video", show_name="The Chosen", season_number=1, episode_number=1)
    _insert_media(make_file("chosen2.mp4"), "video", show_name="The Chosen", season_number=1, episode_number=2)

    res = client.get("/api/shows")

    show = next(s for s in res.json() if s["show_name"] == "The Chosen")
    assert show["sample_media_id"] == id1


def test_list_media_excludes_extras_by_default(client, make_file):
    _insert_media(make_file("ep1.mp4"), "video", show_name="The Chosen", season_number=1, episode_number=1)
    _insert_media(make_file("extra.mp4"), "video", show_name="The Chosen", is_extra=True)

    res = client.get("/api/library", params={"show_name": "The Chosen"})

    assert res.json()["total"] == 1


def test_list_media_extras_true_returns_only_extras(client, make_file):
    _insert_media(make_file("ep1.mp4"), "video", show_name="The Chosen", season_number=1, episode_number=1)
    extra_id = _insert_media(make_file("extra.mp4"), "video", show_name="The Chosen", is_extra=True)

    res = client.get("/api/library", params={"show_name": "The Chosen", "extras": "true"})

    body = res.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == extra_id


def test_search_excludes_extras(client, make_file):
    _insert_media(make_file("a.mp4"), "video", title="Rhapsody Extra", is_extra=True)
    _insert_media(make_file("b.mp4"), "video", title="Rhapsody Real")

    res = client.get("/api/library", params={"q": "rhapsody"})

    titles = {i["title"] for i in res.json()["items"]}
    assert titles == {"Rhapsody Real"}


def test_list_media_filters_by_is_movie(client, make_file):
    _insert_media(make_file("movie.mp4"), "video", is_movie=True)
    _insert_media(make_file("ep.mp4"), "video", show_name="Show", season_number=1, episode_number=1)

    res = client.get("/api/library", params={"is_movie": "true", "media_type": "video"})

    body = res.json()
    assert body["total"] == 1
    assert body["items"][0]["title"] == "movie"


def test_list_media_filters_by_show_name_ordered_by_episode(client, make_file):
    _insert_media(make_file("c3.mp4"), "video", show_name="The Chosen", season_number=1, episode_number=3)
    _insert_media(make_file("c1.mp4"), "video", show_name="The Chosen", season_number=1, episode_number=1)
    _insert_media(make_file("c2.mp4"), "video", show_name="The Chosen", season_number=1, episode_number=2)
    _insert_media(make_file("other.mp4"), "video", show_name="Other Show", season_number=1, episode_number=1)

    res = client.get("/api/library", params={"show_name": "The Chosen"})

    body = res.json()
    assert body["total"] == 3
    assert [i["episode_number"] for i in body["items"]] == [1, 2, 3]


def test_get_single_media_item(client, make_file):
    f = make_file("song.mp3", b"a")
    media_id = _insert_media(f)

    res = client.get(f"/api/library/{media_id}")

    assert res.status_code == 200
    assert res.json()["id"] == media_id


def test_get_missing_media_item_returns_404(client):
    res = client.get("/api/library/999")
    assert res.status_code == 404


def test_scan_endpoint_runs_in_background_and_updates_status(client, make_file):
    make_file("song.mp3", b"a")

    res = client.post("/api/scan")

    assert res.status_code == 200
    assert res.json() == {"status": "started"}
    # TestClient runs BackgroundTasks to completion before returning, so the
    # scan has already finished by this point.
    status = client.get("/api/scan/status").json()
    assert status["status"] == "idle"
    assert status["last_scan_at"] is not None
    assert client.get("/api/library").json()["total"] == 1
    assert status["scanned_count"] == 1
    assert status["failed_count"] == 0
    assert status["failed_examples"] == []
    assert status["incomplete_count"] == 0
    assert status["incomplete_examples"] == []


def test_art_endpoint_404s_when_file_has_no_embedded_artwork(client, make_file):
    f = make_file("song.mp3", b"not a real audio file")
    media_id = _insert_media(f)

    res = client.get(f"/api/library/{media_id}/art")

    assert res.status_code == 404


def test_art_endpoint_404s_for_video_with_no_extractable_thumbnail(client, make_file):
    f = make_file("clip.mp4", b"not a real video file")
    media_id = _insert_media(f, media_type="video", duration=10.0)

    res = client.get(f"/api/library/{media_id}/art")

    assert res.status_code == 404


@requires_ffmpeg
def test_art_endpoint_serves_a_real_video_thumbnail(client, make_file, h264_encoder):
    f = make_file("clip.mp4", b"")
    f.unlink()
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=green:size=64x64:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", h264_encoder, "-c:a", "aac", "-shortest",
            str(f),
        ],
        check=True,
    )
    media_id = _insert_media(f, media_type="video", duration=2.0)

    res = client.get(f"/api/library/{media_id}/art")

    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
    assert res.content[:2] == b"\xff\xd8"


def test_art_endpoint_404s_for_unknown_media(client):
    res = client.get("/api/library/999/art")
    assert res.status_code == 404


def test_art_endpoint_404s_when_file_missing_on_disk(client, make_file):
    f = make_file("gone.mp3", b"data")
    media_id = _insert_media(f)
    f.unlink()

    res = client.get(f"/api/library/{media_id}/art")

    assert res.status_code == 404


def test_subtitles_endpoint_404s_when_no_sidecar_file_exists(client, make_file):
    f = make_file("clip.mp4", b"data")
    media_id = _insert_media(f, media_type="video")

    res = client.get(f"/api/library/{media_id}/subtitles")

    assert res.status_code == 404


def test_subtitles_endpoint_404s_for_audio(client, make_file):
    f = make_file("song.mp3", b"data")
    media_id = _insert_media(f, media_type="audio")
    f.with_suffix(".srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n")

    res = client.get(f"/api/library/{media_id}/subtitles")

    assert res.status_code == 404


def test_subtitles_endpoint_serves_converted_srt_sidecar(client, make_file):
    f = make_file("clip.mp4", b"data")
    media_id = _insert_media(f, media_type="video")
    f.with_suffix(".srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n")

    res = client.get(f"/api/library/{media_id}/subtitles")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/vtt")
    assert res.text.startswith("WEBVTT")
    assert "00:00:01.000 --> 00:00:02.000" in res.text


def test_subtitles_endpoint_serves_vtt_sidecar_as_is(client, make_file):
    f = make_file("clip.mp4", b"data")
    media_id = _insert_media(f, media_type="video")
    vtt_content = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\nHello\n"
    f.with_suffix(".vtt").write_text(vtt_content)

    res = client.get(f"/api/library/{media_id}/subtitles")

    assert res.status_code == 200
    assert res.text == vtt_content


def test_concurrent_scan_trigger_is_rejected_by_the_lock():
    # A second trigger while one is in flight can't be reproduced through the
    # client (background tasks run to completion before the first request
    # returns), so this exercises the lock scan_media_dirs/routers rely on
    # directly.
    assert scanner.start_scan() is True
    assert scanner.start_scan() is False
    scanner.run_claimed_scan()
    assert scanner.start_scan() is True
    scanner.run_claimed_scan()
