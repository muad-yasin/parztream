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
    assert res.json() == []


def test_list_media_filters_by_type(client, make_file):
    audio = make_file("song.mp3", b"a")
    video = make_file("clip.mp4", b"v")
    _insert_media(audio, media_type="audio")
    _insert_media(video, media_type="video")

    res = client.get("/api/library", params={"media_type": "audio"})

    body = res.json()
    assert len(body) == 1
    assert body[0]["media_type"] == "audio"


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
    assert len(client.get("/api/library").json()) == 1


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
