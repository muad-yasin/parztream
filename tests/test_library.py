from app import scanner
from app.db import get_connection


def _insert_media(path, media_type="audio"):
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO media (path, media_type, title, size_bytes) VALUES (?, ?, ?, ?)",
            (str(path), media_type, path.stem, path.stat().st_size),
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


def test_art_endpoint_404s_when_file_has_no_embedded_artwork(client, make_file):
    f = make_file("song.mp3", b"not a real audio file")
    media_id = _insert_media(f)

    res = client.get(f"/api/library/{media_id}/art")

    assert res.status_code == 404


def test_art_endpoint_404s_for_unknown_media(client):
    res = client.get("/api/library/999/art")
    assert res.status_code == 404


def test_art_endpoint_404s_when_file_missing_on_disk(client, make_file):
    f = make_file("gone.mp3", b"data")
    media_id = _insert_media(f)
    f.unlink()

    res = client.get(f"/api/library/{media_id}/art")

    assert res.status_code == 404


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
