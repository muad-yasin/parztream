from app.db import get_connection


def _insert_media(path, media_type="audio"):
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO media (path, media_type, title, size_bytes) VALUES (?, ?, ?, ?)",
            (str(path), media_type, path.stem, path.stat().st_size),
        )
        return cur.lastrowid


def test_full_file_returned_when_no_range_header(client, make_file):
    content = b"x" * 1000
    f = make_file("clip.mp4", content)
    media_id = _insert_media(f, "video")

    res = client.get(f"/api/stream/{media_id}")

    assert res.status_code == 200
    assert res.headers["content-length"] == "1000"
    assert res.headers["content-type"] == "video/mp4"
    assert res.content == content


def test_content_type_is_derived_from_file_extension(client, make_file):
    f = make_file("clip.webm", b"y" * 10)
    media_id = _insert_media(f, "video")

    res = client.get(f"/api/stream/{media_id}")

    assert res.headers["content-type"] == "video/webm"


def test_m4b_audiobook_content_type_is_playable_not_octet_stream(client, make_file):
    # .m4b isn't in Python's mimetypes registry by default; config.py
    # registers it as audio/mp4 (same container as .m4a) so browsers can
    # actually play it instead of getting application/octet-stream.
    f = make_file("book.m4b", b"z" * 10)
    media_id = _insert_media(f, "audio")

    res = client.get(f"/api/stream/{media_id}")

    assert res.headers["content-type"] == "audio/mp4"


def test_partial_range_returns_exact_byte_slice(client, make_file):
    content = bytes(range(256)) * 4
    f = make_file("song.mp3", content)
    media_id = _insert_media(f, "audio")

    res = client.get(f"/api/stream/{media_id}", headers={"Range": "bytes=10-19"})

    assert res.status_code == 206
    assert res.headers["content-range"] == f"bytes 10-19/{len(content)}"
    assert res.headers["content-length"] == "10"
    assert res.content == content[10:20]


def test_open_ended_range_returns_rest_of_file(client, make_file):
    content = b"0123456789"
    f = make_file("song.mp3", content)
    media_id = _insert_media(f, "audio")

    res = client.get(f"/api/stream/{media_id}", headers={"Range": "bytes=5-"})

    assert res.status_code == 206
    assert res.content == b"56789"
    assert res.headers["content-range"] == "bytes 5-9/10"


def test_suffix_range_returns_last_n_bytes(client, make_file):
    content = b"0123456789"
    f = make_file("song.mp3", content)
    media_id = _insert_media(f, "audio")

    res = client.get(f"/api/stream/{media_id}", headers={"Range": "bytes=-3"})

    assert res.status_code == 206
    assert res.content == b"789"
    assert res.headers["content-range"] == "bytes 7-9/10"


def test_suffix_range_larger_than_file_clamps_to_whole_file(client, make_file):
    content = b"0123456789"
    f = make_file("song.mp3", content)
    media_id = _insert_media(f, "audio")

    res = client.get(f"/api/stream/{media_id}", headers={"Range": "bytes=-999"})

    assert res.status_code == 206
    assert res.content == content
    assert res.headers["content-range"] == "bytes 0-9/10"


def test_out_of_bounds_start_returns_416_not_a_broken_response(client, make_file):
    f = make_file("song.mp3", b"0123456789")
    media_id = _insert_media(f, "audio")

    res = client.get(f"/api/stream/{media_id}", headers={"Range": "bytes=999-"})

    assert res.status_code == 416
    assert res.headers["content-range"] == "bytes */10"


def test_malformed_range_header_returns_416(client, make_file):
    f = make_file("song.mp3", b"0123456789")
    media_id = _insert_media(f, "audio")

    res = client.get(f"/api/stream/{media_id}", headers={"Range": "bytes=abc"})

    assert res.status_code == 416


def test_unknown_media_id_returns_404(client):
    res = client.get("/api/stream/999")
    assert res.status_code == 404


def test_missing_file_on_disk_returns_404(client, make_file):
    f = make_file("gone.mp3", b"data")
    media_id = _insert_media(f, "audio")
    f.unlink()

    res = client.get(f"/api/stream/{media_id}")

    assert res.status_code == 404
