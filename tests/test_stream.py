import shutil
import subprocess

import pytest

from app import transcode
from app.db import get_connection

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)


def _insert_media(path, media_type="audio", video_codec=None, audio_codec=None, duration=None):
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO media (path, media_type, title, size_bytes, video_codec, audio_codec, duration)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (str(path), media_type, path.stem, path.stat().st_size, video_codec, audio_codec, duration),
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


def test_unsupported_video_codec_returns_415(client, make_file):
    f = make_file("clip.mkv", b"not real video data")
    media_id = _insert_media(f, "video", video_codec="hevc", audio_codec="aac")

    res = client.get(f"/api/stream/{media_id}")

    assert res.status_code == 415


def test_original_param_bypasses_codec_check_so_downloads_still_work(client, make_file):
    # The "download instead" fallback for an unsupported codec has to hit
    # this same endpoint -- without ?original=1 bypassing the compatibility
    # check, it would 415 exactly like in-browser playback just did, and
    # there'd be no way to get the file's bytes out of parztream at all.
    content = b"raw hevc bytes (not really, just test content)"
    f = make_file("clip.mkv", content)
    media_id = _insert_media(f, "video", video_codec="hevc", audio_codec="aac")

    res = client.get(f"/api/stream/{media_id}", params={"original": "1"})

    assert res.status_code == 200
    assert res.content == content


def test_original_param_sets_content_disposition_for_download(client, make_file):
    f = make_file("My Movie.mkv", b"data")
    media_id = _insert_media(f, "video", video_codec="hevc", audio_codec="aac")

    res = client.get(f"/api/stream/{media_id}", params={"original": "1"})

    assert 'attachment; filename*=UTF-8\'\'My%20Movie.mkv' == res.headers["content-disposition"]


def test_original_param_supports_range_requests(client, make_file):
    content = b"0123456789"
    f = make_file("clip.mkv", content)
    media_id = _insert_media(f, "video", video_codec="hevc", audio_codec="aac")

    res = client.get(
        f"/api/stream/{media_id}", params={"original": "1"}, headers={"Range": "bytes=2-4"}
    )

    assert res.status_code == 206
    assert res.content == b"234"
    assert res.headers["content-disposition"].startswith("attachment")


def test_original_param_serves_source_file_not_remuxed_cache(client, make_file):
    # For a file that WOULD normally get remuxed (compatible codecs, just
    # needs a container fix), ?original=1 should still serve the untouched
    # source bytes -- it's an unconditional bypass, not just for codecs
    # resolve_playable_path can't fix.
    content = b"raw mkv bytes"
    f = make_file("clip.mkv", content)
    media_id = _insert_media(f, "video", video_codec="h264", audio_codec="aac")

    res = client.get(f"/api/stream/{media_id}", params={"original": "1"})

    assert res.status_code == 200
    assert res.content == content
    assert res.headers["content-type"] == "video/x-matroska"


def test_without_original_param_still_gets_415_as_before(client, make_file):
    f = make_file("clip.mkv", b"data")
    media_id = _insert_media(f, "video", video_codec="hevc", audio_codec="aac")

    res = client.get(f"/api/stream/{media_id}")

    assert res.status_code == 415
    assert "content-disposition" not in res.headers


def test_compatible_mp4_streams_directly_without_transcoding(client, make_file):
    content = b"x" * 1000
    f = make_file("clip.mp4", content)
    media_id = _insert_media(f, "video", video_codec="h264", audio_codec="aac")

    res = client.get(f"/api/stream/{media_id}")

    assert res.status_code == 200
    assert res.content == content


def test_mkv_needing_remux_returns_an_hls_playlist_pointer(client, make_file):
    # A container/audio remux is no longer done synchronously inline --
    # the main stream endpoint just tells the frontend where to find the
    # HLS playlist, so this doesn't need real ffmpeg to verify.
    f = make_file("clip.mkv", b"not real video data")
    media_id = _insert_media(f, "video", video_codec="h264", audio_codec="aac", duration=12.0)

    res = client.get(f"/api/stream/{media_id}")

    assert res.status_code == 200
    assert res.headers["content-type"] == "application/json"
    assert res.json() == {"hls_playlist": f"/api/stream/{media_id}/hls/playlist.m3u8"}


def test_hls_playlist_endpoint_serves_a_valid_vod_playlist(client, make_file):
    f = make_file("clip.mkv", b"not real video data")
    media_id = _insert_media(f, "video", video_codec="h264", audio_codec="aac", duration=12.0)

    res = client.get(f"/api/stream/{media_id}/hls/playlist.m3u8")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    assert "#EXT-X-ENDLIST" in res.text
    assert "segment_00000.ts" in res.text


def test_hls_playlist_endpoint_errors_cleanly_when_duration_unknown(client, make_file):
    f = make_file("clip.mkv", b"not real video data")
    media_id = _insert_media(f, "video", video_codec="h264", audio_codec="aac", duration=None)

    res = client.get(f"/api/stream/{media_id}/hls/playlist.m3u8")

    assert res.status_code == 500


def test_hls_playlist_endpoint_404s_for_a_file_that_doesnt_need_remuxing(client, make_file):
    f = make_file("clip.mp4", b"x" * 10)
    media_id = _insert_media(f, "video", video_codec="h264", audio_codec="aac", duration=5.0)

    res = client.get(f"/api/stream/{media_id}/hls/playlist.m3u8")

    assert res.status_code == 400


def test_hls_playlist_endpoint_still_415s_for_a_truly_unsupported_codec(client, make_file):
    f = make_file("clip.mkv", b"not real video data")
    media_id = _insert_media(f, "video", video_codec="hevc", audio_codec="aac", duration=5.0)

    res = client.get(f"/api/stream/{media_id}/hls/playlist.m3u8")

    assert res.status_code == 415


def test_hls_segment_endpoint_rejects_malformed_segment_names(client, make_file):
    f = make_file("clip.mkv", b"not real video data")
    media_id = _insert_media(f, "video", video_codec="h264", audio_codec="aac", duration=12.0)

    res = client.get(f"/api/stream/{media_id}/hls/../../etc/passwd")

    assert res.status_code == 404


@requires_ffmpeg
def test_mkv_is_remuxed_into_hls_segments_and_served(client, media_dir, h264_encoder):
    mkv_path = media_dir / "clip.mkv"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=blue:size=64x64:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", h264_encoder, "-c:a", "aac", "-shortest",
            str(mkv_path),
        ],
        check=True,
    )
    media_id = _insert_media(mkv_path, "video", video_codec="h264", audio_codec="aac", duration=1.0)

    probe_res = client.get(f"/api/stream/{media_id}")
    assert probe_res.status_code == 200
    hls_url = probe_res.json()["hls_playlist"]

    playlist_res = client.get(hls_url)
    assert playlist_res.status_code == 200
    assert "segment_00000.ts" in playlist_res.text

    segment_res = client.get(f"/api/stream/{media_id}/hls/segment_00000.ts")
    assert segment_res.status_code == 200
    assert segment_res.headers["content-type"] == "video/mp2t"
    assert len(segment_res.content) > 0


@requires_ffmpeg
def test_hls_segment_generation_failure_returns_a_clean_500(client, media_dir):
    # Not real video data -- ffmpeg will fail to produce any segment from it.
    bogus_path = media_dir / "clip.mkv"
    bogus_path.write_bytes(b"this is not a real video file")
    media_id = _insert_media(bogus_path, "video", video_codec="h264", audio_codec="aac", duration=5.0)

    res = client.get(f"/api/stream/{media_id}/hls/segment_00000.ts")

    assert res.status_code == 500
    assert "conversion failed" in res.json()["detail"]


def test_hls_segment_endpoint_returns_503_when_transcode_slot_unavailable(client, media_dir, monkeypatch):
    f = media_dir / "clip.mkv"
    f.write_bytes(b"data")
    media_id = _insert_media(f, "video", video_codec="h264", audio_codec="aac", duration=5.0)

    def fake_ensure_segment(*args, **kwargs):
        raise transcode.TranscodeUnavailable()

    monkeypatch.setattr(transcode, "ensure_segment", fake_ensure_segment)

    res = client.get(f"/api/stream/{media_id}/hls/segment_00000.ts")

    assert res.status_code == 503
