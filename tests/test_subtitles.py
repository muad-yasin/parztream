from app import subtitles


def test_find_subtitle_path_prefers_vtt_over_srt(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"")
    (video.with_suffix(".srt")).write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n")
    (video.with_suffix(".vtt")).write_text("WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\nHi\n")

    found = subtitles.find_subtitle_path(video)

    assert found == video.with_suffix(".vtt")


def test_find_subtitle_path_falls_back_to_srt(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"")
    srt = video.with_suffix(".srt")
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n")

    assert subtitles.find_subtitle_path(video) == srt


def test_find_subtitle_path_returns_none_when_no_sidecar_exists(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"")

    assert subtitles.find_subtitle_path(video) is None


def test_vtt_sidecar_is_served_as_is(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"")
    vtt_content = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\nHello\n"
    video.with_suffix(".vtt").write_text(vtt_content)

    assert subtitles.get_webvtt(video) == vtt_content


def test_srt_is_converted_to_webvtt():
    srt_text = (
        "1\n"
        "00:00:01,000 --> 00:00:04,500\n"
        "Hello, world!\n"
        "\n"
        "2\n"
        "00:00:05,000 --> 00:00:08,000\n"
        "Second line\n"
    )

    vtt = subtitles._srt_to_vtt(srt_text)

    assert vtt.startswith("WEBVTT\n\n")
    assert "00:00:01.000 --> 00:00:04.500" in vtt
    assert "00:00:05.000 --> 00:00:08.000" in vtt
    # Commas inside actual dialogue text must survive untouched.
    assert "Hello, world!" in vtt
    # No comma-decimal timestamps should remain.
    assert "," not in vtt.split("\n\n", 1)[1].replace("Hello, world!", "")


def test_srt_sidecar_gets_converted_when_served(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"")
    video.with_suffix(".srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n")

    vtt = subtitles.get_webvtt(video)

    assert vtt.startswith("WEBVTT")
    assert "00:00:01.000 --> 00:00:02.000" in vtt


def test_get_webvtt_returns_none_when_no_sidecar_exists(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"")

    assert subtitles.get_webvtt(video) is None


def test_srt_with_utf8_bom_is_read_correctly(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"")
    srt_bytes = "1\n00:00:01,000 --> 00:00:02,000\nCafé\n".encode("utf-8-sig")
    video.with_suffix(".srt").write_bytes(srt_bytes)

    vtt = subtitles.get_webvtt(video)

    assert vtt is not None
    assert "Café" in vtt
    assert not vtt.startswith("﻿")
