import shutil
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app import config, encoder_detect, transcode

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)


def _row(**overrides):
    base = {
        "id": 1,
        "path": "/media/clip.mp4",
        "media_type": "video",
        "video_codec": None,
        "audio_codec": None,
    }
    base.update(overrides)
    return base


def _wait_until(predicate, timeout=5, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class _FakeProcess:
    """Stand-in for subprocess.Popen used by the concurrency/failure tests
    below -- avoids depending on real ffmpeg for tests that are really
    about the locking/dedup/error-surfacing logic, not encoding itself."""

    def __init__(self, on_communicate):
        self._on_communicate = on_communicate
        self.returncode = None

    def poll(self):
        return self.returncode

    def communicate(self):
        stdout, stderr, returncode = self._on_communicate()
        self.returncode = returncode
        return stdout, stderr


def test_audio_files_always_direct_play(tmp_path):
    f = tmp_path / "song.mp3"
    f.write_bytes(b"x")
    row = _row(media_type="audio", path=str(f), video_codec="hevc")

    assert transcode.resolve_playable_path(row) == f


def test_compatible_mp4_h264_aac_direct_plays_without_calling_ffmpeg(tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x")
    row = _row(path=str(f), video_codec="h264", audio_codec="aac")

    with patch("subprocess.Popen") as mock_popen:
        result = transcode.resolve_playable_path(row)

    assert result == f
    mock_popen.assert_not_called()


def test_unknown_codec_info_falls_back_to_direct_play(tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x")
    row = _row(path=str(f), video_codec=None, audio_codec=None)

    with patch("subprocess.Popen") as mock_popen:
        result = transcode.resolve_playable_path(row)

    assert result == f
    mock_popen.assert_not_called()


def test_incompatible_video_codec_raises(tmp_path):
    f = tmp_path / "clip.mkv"
    f.write_bytes(b"x")
    row = _row(path=str(f), video_codec="hevc", audio_codec="aac")

    with pytest.raises(transcode.UnsupportedVideoCodec):
        transcode.resolve_playable_path(row)


def test_compatible_mkv_raises_needs_hls_remux_for_container_only(tmp_path):
    f = tmp_path / "clip.mkv"
    f.write_bytes(b"x")
    row = _row(path=str(f), video_codec="h264", audio_codec="aac")

    with pytest.raises(transcode.NeedsHlsRemux) as exc_info:
        transcode.resolve_playable_path(row)
    assert exc_info.value.remux_audio is False


def test_incompatible_audio_raises_needs_hls_remux_with_remux_audio_true(tmp_path):
    f = tmp_path / "clip.mkv"
    f.write_bytes(b"x")
    row = _row(path=str(f), video_codec="h264", audio_codec="ac3")

    with pytest.raises(transcode.NeedsHlsRemux) as exc_info:
        transcode.resolve_playable_path(row)
    assert exc_info.value.remux_audio is True


def test_build_playlist_lists_one_segment_per_chunk_and_ends_the_list():
    playlist = transcode.build_playlist(duration=14.0)

    assert "#EXTM3U" in playlist
    assert "#EXT-X-ENDLIST" in playlist
    # 14s / 6s-per-segment -> 3 segments (0, 1, 2), last one shorter.
    assert "segment_00000.ts" in playlist
    assert "segment_00001.ts" in playlist
    assert "segment_00002.ts" in playlist
    assert "segment_00003.ts" not in playlist


@requires_ffmpeg
def test_mkv_with_compatible_codecs_gets_remuxed_into_a_playable_segment(tmp_path, monkeypatch):
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")

    mkv_path = tmp_path / "clip.mkv"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=blue:size=64x64:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx264", "-c:a", "aac", "-shortest",
            str(mkv_path),
        ],
        check=True,
    )

    segment = transcode.ensure_segment(42, mkv_path, remux_audio=False, index=0)

    assert segment.is_file()
    assert segment.parent == transcode.hls_dir_for(42)

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(segment)],
        capture_output=True, text=True, check=True,
    )
    assert "h264" in probe.stdout

    # Second call should hit the already-generated segment, not invoke
    # ffmpeg again.
    with patch("subprocess.Popen") as mock_popen:
        cached_segment = transcode.ensure_segment(42, mkv_path, remux_audio=False, index=0)
    assert cached_segment == segment
    mock_popen.assert_not_called()


@requires_ffmpeg
def test_incompatible_audio_gets_transcoded_while_video_is_copied(tmp_path, monkeypatch):
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")

    mkv_path = tmp_path / "clip.mkv"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=blue:size=64x64:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx264", "-c:a", "ac3", "-shortest",
            str(mkv_path),
        ],
        check=True,
    )

    segment = transcode.ensure_segment(7, mkv_path, remux_audio=True, index=0)

    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_type,codec_name",
            "-of", "csv=p=0", str(segment),
        ],
        capture_output=True, text=True, check=True,
    )
    assert "h264,video" in probe.stdout
    assert "aac,audio" in probe.stdout


@requires_ffmpeg
def test_seeking_ahead_of_generated_segments_triggers_a_new_job_from_that_point(tmp_path, monkeypatch):
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")

    mkv_path = tmp_path / "clip.mkv"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=blue:size=64x64:duration=30",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=30",
            "-c:v", "libx264", "-c:a", "aac", "-shortest",
            str(mkv_path),
        ],
        check=True,
    )

    # Ask directly for a segment well beyond what a sequential job starting
    # at 0 would have reached yet -- this should seek straight there rather
    # than blocking on the whole video being generated from the start.
    segment = transcode.ensure_segment(5, mkv_path, remux_audio=False, index=3)

    assert segment.is_file()
    assert segment.name == "segment_00003.ts"


@requires_ffmpeg
def test_creating_a_new_segment_prunes_older_ones_once_over_budget(tmp_path, monkeypatch):
    from app import cache as cache_module

    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(transcode, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(cache_module, "CACHE_DIR", cache_dir)

    def make_mkv(name):
        path = tmp_path / name
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=blue:size=64x64:duration=1",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-c:v", "libx264", "-c:a", "aac", "-shortest",
                str(path),
            ],
            check=True,
        )
        return path

    first_src = make_mkv("first.mkv")
    second_src = make_mkv("second.mkv")

    first_segment = transcode.ensure_segment(1, first_src, remux_audio=False, index=0)
    assert first_segment.is_file()
    first_size = first_segment.stat().st_size

    # Cap the budget to roughly one segment's worth, then create a second --
    # pruning happens after a job finishes (see _watch_job), so give it a
    # moment rather than asserting immediately.
    monkeypatch.setattr(cache_module, "CACHE_MAX_BYTES", first_size + 1000)
    second_segment = transcode.ensure_segment(2, second_src, remux_audio=False, index=0)

    assert second_segment.is_file()
    assert _wait_until(lambda: not first_segment.is_file())


def test_concurrent_requests_for_the_same_segment_only_invoke_ffmpeg_once(tmp_path, monkeypatch):
    # Regression test for the same class of race app/cache.py's lock_for
    # guards against: N concurrent requests for the same not-yet-generated
    # segment must collapse into exactly one ffmpeg process, not each spawn
    # their own.
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")

    f = tmp_path / "clip.mkv"
    f.write_bytes(b"source bytes")

    call_count = 0
    call_count_lock = threading.Lock()

    def fake_popen(cmd, **kwargs):
        nonlocal call_count
        with call_count_lock:
            call_count += 1
        segment_path = Path(cmd[-1].replace("%05d", "00000"))

        def on_communicate():
            time.sleep(0.1)  # widen the race window so the bug would reproduce
            segment_path.parent.mkdir(parents=True, exist_ok=True)
            segment_path.write_bytes(b"fake segment")
            # A second, later-numbered file lets ensure_segment's
            # completion check treat segment 0 as finished.
            Path(cmd[-1].replace("%05d", "00001")).write_bytes(b"fake next segment")
            return b"", b"", 0

        return _FakeProcess(on_communicate)

    results = []
    with patch("subprocess.Popen", side_effect=fake_popen):
        threads = [
            threading.Thread(
                target=lambda: results.append(transcode.ensure_segment(99, f, remux_audio=False, index=0))
            )
            for _ in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert call_count == 1
    assert len(results) == 8
    assert all(r == results[0] for r in results)


def test_ffmpeg_failure_surfaces_as_remux_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")

    f = tmp_path / "clip.mkv"
    f.write_bytes(b"source bytes")

    def fake_popen(cmd, **kwargs):
        return _FakeProcess(lambda: (b"", b"ffmpeg: something went wrong", 1))

    with patch("subprocess.Popen", side_effect=fake_popen):
        with pytest.raises(transcode.RemuxFailed):
            transcode.ensure_segment(123, f, remux_audio=False, index=0)


def test_segment_requested_past_end_of_video_raises_file_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")

    f = tmp_path / "clip.mkv"
    f.write_bytes(b"source bytes")

    def fake_popen(cmd, **kwargs):
        # Job "finishes" immediately without producing any segment --
        # simulates seeking past the actual end of the video.
        return _FakeProcess(lambda: (b"", b"", 0))

    with patch("subprocess.Popen", side_effect=fake_popen):
        with pytest.raises(FileNotFoundError):
            transcode.ensure_segment(124, f, remux_audio=False, index=50)


def test_terminate_all_jobs_stops_still_running_processes(tmp_path, monkeypatch):
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")

    f = tmp_path / "clip.mkv"
    f.write_bytes(b"source bytes")

    terminated = threading.Event()

    class _NeverEndingFakeProcess:
        def __init__(self):
            self.returncode = None

        def poll(self):
            return self.returncode

        def communicate(self):
            terminated.wait(timeout=5)
            self.returncode = 0
            return b"", b""

        def terminate(self):
            terminated.set()

        def wait(self, timeout=None):
            if not terminated.wait(timeout=timeout):
                raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
            self.returncode = 0
            return self.returncode

    process = _NeverEndingFakeProcess()
    with patch("subprocess.Popen", return_value=process):
        transcode._find_or_start_job(transcode.hls_dir_for(1), f, False, 0)

    transcode.terminate_all_jobs()
    assert terminated.is_set()


def test_incompatible_codec_with_transcode_disabled_raises_unsupported(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRANSCODE_ENABLED", False)
    f = tmp_path / "clip.mkv"
    f.write_bytes(b"x")
    row = _row(path=str(f), video_codec="hevc", audio_codec="aac")

    with pytest.raises(transcode.UnsupportedVideoCodec) as exc_info:
        transcode.resolve_playable_path(row)
    # The two ways to land here (never opted in vs. opted in but no working
    # encoder) call for different next steps -- the message should point
    # the user at turning transcoding on, not claim it was already tried.
    assert exc_info.value.transcode_enabled is False
    assert "PARZTREAM_ENABLE_TRANSCODE" in exc_info.value.user_message()


def test_incompatible_codec_with_transcode_enabled_but_no_encoder_raises_unsupported(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRANSCODE_ENABLED", True)
    monkeypatch.setattr(encoder_detect, "get_encoder", lambda: None)
    f = tmp_path / "clip.mkv"
    f.write_bytes(b"x")
    row = _row(path=str(f), video_codec="hevc", audio_codec="aac")

    with pytest.raises(transcode.UnsupportedVideoCodec) as exc_info:
        transcode.resolve_playable_path(row)
    # Already enabled -- telling the user to set the flag again would be
    # actively misleading, the message must say no encoder was found instead.
    assert exc_info.value.transcode_enabled is True
    message = exc_info.value.user_message()
    assert "PARZTREAM_ENABLE_TRANSCODE" not in message
    assert "no working" in message


def test_incompatible_codec_with_transcode_enabled_and_encoder_found_needs_hls_remux(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRANSCODE_ENABLED", True)
    monkeypatch.setattr(encoder_detect, "get_encoder", lambda: "libopenh264")
    f = tmp_path / "clip.mkv"
    f.write_bytes(b"x")
    row = _row(path=str(f), video_codec="hevc", audio_codec="ac3")

    with pytest.raises(transcode.NeedsHlsRemux) as exc_info:
        transcode.resolve_playable_path(row)
    assert exc_info.value.reencode_video is True
    assert exc_info.value.remux_audio is True  # ac3 is also incompatible


def test_transcode_disabled_never_calls_encoder_detection(tmp_path, monkeypatch):
    # Proves the flag short-circuits before encoder_detect is even touched
    # -- zero new code runs when the feature is off, exactly today's
    # behavior.
    monkeypatch.setattr(config, "TRANSCODE_ENABLED", False)
    called = []
    monkeypatch.setattr(encoder_detect, "get_encoder", lambda: called.append(True))
    f = tmp_path / "clip.mkv"
    f.write_bytes(b"x")
    row = _row(path=str(f), video_codec="hevc", audio_codec="aac")

    with pytest.raises(transcode.UnsupportedVideoCodec):
        transcode.resolve_playable_path(row)
    assert called == []


def test_scale_args_noop_when_dimensions_unknown():
    assert transcode._scale_args(None, None) == []


def test_scale_args_noop_when_at_or_under_cap():
    assert transcode._scale_args(1920, 1080) == []
    assert transcode._scale_args(640, 480) == []


def test_scale_args_present_when_over_cap_landscape():
    args = transcode._scale_args(3840, 2160)
    assert args[0] == "-vf"
    assert "1920" in args[1] and "1080" in args[1]


def test_scale_args_present_when_over_cap_portrait():
    args = transcode._scale_args(2160, 3840)
    assert args[0] == "-vf"
    assert "1920" in args[1] and "1080" in args[1]


def test_reencode_jobs_are_limited_by_the_transcode_semaphore(tmp_path, monkeypatch):
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(transcode, "_transcode_semaphore", threading.Semaphore(1))
    # Avoid real encoder detection running (subprocess.run) while
    # subprocess.Popen is mocked below -- subprocess.run's internal `with
    # Popen(...)` would break against our non-context-manager fake.
    monkeypatch.setattr(encoder_detect, "get_encoder", lambda: "libopenh264")

    f = tmp_path / "clip.mkv"
    f.write_bytes(b"source bytes")
    release_first = threading.Event()

    def fake_popen(cmd, **kwargs):
        def on_communicate():
            release_first.wait(timeout=5)
            return b"", b"", 0
        return _FakeProcess(on_communicate)

    with patch("subprocess.Popen", side_effect=fake_popen):
        # Holds the only semaphore slot.
        transcode._find_or_start_job(transcode.hls_dir_for(201), f, False, 0, True, None, None)

        job2_started = threading.Event()

        def start_job2():
            transcode._find_or_start_job(transcode.hls_dir_for(202), f, False, 0, True, None, None)
            job2_started.set()

        t = threading.Thread(target=start_job2)
        t.start()
        time.sleep(0.2)
        assert not job2_started.is_set(), "second re-encode job should be blocked on the semaphore"

        release_first.set()  # job 1 finishes, releasing its slot
        assert job2_started.wait(timeout=5), "second job never unblocked after the first released its slot"
        t.join()


def test_stream_copy_jobs_never_touch_the_transcode_semaphore(tmp_path, monkeypatch):
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(transcode, "_transcode_semaphore", threading.Semaphore(1))

    f = tmp_path / "clip.mkv"
    f.write_bytes(b"source bytes")

    def fake_popen(cmd, **kwargs):
        return _FakeProcess(lambda: (b"", b"", 0))

    with patch("subprocess.Popen", side_effect=fake_popen):
        transcode._transcode_semaphore.acquire()  # exhaust it first
        # A stream-copy (reencode_video=False, the default) job must still
        # start immediately -- this call would hang until test timeout if
        # it incorrectly waited on the exhausted semaphore.
        job = transcode._find_or_start_job(transcode.hls_dir_for(301), f, False, 0)
        assert job is not None


@requires_ffmpeg
def test_hevc_source_is_transcoded_to_h264_via_software_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(encoder_detect, "get_encoder", lambda: "libopenh264")

    hevc_path = tmp_path / "clip.mkv"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=blue:size=64x64:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx265", "-c:a", "aac", "-shortest",
            str(hevc_path),
        ],
        check=True,
    )

    segment = transcode.ensure_segment(500, hevc_path, remux_audio=False, index=0, reencode_video=True)

    assert segment.is_file()
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(segment)],
        capture_output=True, text=True, check=True,
    )
    assert "h264" in probe.stdout


@requires_ffmpeg
def test_resolution_cap_applied_during_reencode(tmp_path, monkeypatch):
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(encoder_detect, "get_encoder", lambda: "libopenh264")

    hevc_path = tmp_path / "clip4k.mkv"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=blue:size=3840x2160:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx265", "-c:a", "aac", "-shortest",
            str(hevc_path),
        ],
        check=True,
    )

    segment = transcode.ensure_segment(
        501, hevc_path, remux_audio=False, index=0, reencode_video=True,
        video_width=3840, video_height=2160,
    )

    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=p=0", str(segment),
        ],
        capture_output=True, text=True, check=True,
    )
    # .ts (MPEG-TS) containers can report stream info more than once (PMT/PID
    # quirk) -- the first non-empty line is enough to confirm the cap applied.
    first_line = next(line for line in probe.stdout.splitlines() if line.strip())
    width, height = first_line.split(",")
    assert int(width) == 1920
    assert int(height) == 1080
