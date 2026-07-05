import os
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
        "audio_channels": None,
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


class _TerminableFakeProcess:
    """Like _FakeProcess, but terminate() has a real, observable effect --
    for tests of _terminate_stale_jobs/_reap_idle_jobs_locked, which call
    process.terminate() and need communicate() to actually unblock and
    poll() to eventually reflect death, the way a real killed process
    would (rather than running "forever" with no way to end it)."""

    def __init__(self):
        self._terminate_event = threading.Event()
        self.terminate_called = threading.Event()
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_called.set()
        self._terminate_event.set()

    def communicate(self):
        self._terminate_event.wait(timeout=5)
        self.returncode = -15
        return b"", b""


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


def test_incompatible_video_codec_raises(tmp_path, monkeypatch):
    # TRANSCODE_MODE defaults to "auto", whose outcome now genuinely depends
    # on this machine's real hardware/software encoder speed (see
    # SOFTWARE_MIN_REALTIME_FACTOR) -- pin it to "off" so this test stays
    # portable across ffmpeg builds/platforms instead of depending on
    # whatever encoder happens to be available in the environment it runs in.
    monkeypatch.setattr(config, "TRANSCODE_MODE", "off")
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


def test_multichannel_aac_is_not_treated_as_direct_playable(tmp_path):
    # Regression test: an *already*-AAC track that's multichannel (e.g. a
    # real "AAC 5.1" release) used to be treated as "compatible codec,
    # just copy it" purely by codec name -- but Chromium's MediaSource AAC
    # decoder rejects multichannel AAC outright (confirmed live), so this
    # must route through the real audio-transcode path (which downmixes),
    # not a blind stream-copy.
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x")
    row = _row(path=str(f), video_codec="h264", audio_codec="aac", audio_channels=6)

    with pytest.raises(transcode.NeedsHlsRemux) as exc_info:
        transcode.resolve_playable_path(row)
    assert exc_info.value.remux_audio is True


def test_stereo_aac_still_direct_plays(tmp_path):
    # Regression guard: the multichannel check above must not affect the
    # common, already-fine case.
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x")
    row = _row(path=str(f), video_codec="h264", audio_codec="aac", audio_channels=2)

    assert transcode.resolve_playable_path(row) == f


def test_aac_with_unknown_channel_count_still_direct_plays(tmp_path):
    # audio_channels=None (a row scanned before this column existed, or a
    # file ffprobe couldn't determine channels for) must not newly block
    # something that used to work -- same "don't guess wrong" reasoning as
    # video_codec is None elsewhere in this function.
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x")
    row = _row(path=str(f), video_codec="h264", audio_codec="aac", audio_channels=None)

    assert transcode.resolve_playable_path(row) == f


@pytest.mark.parametrize("codec", ["vp9", "av1", "vp8"])
def test_ts_unsafe_codec_in_wrong_container_is_unsupported_not_a_broken_remux(tmp_path, codec):
    # Regression test: vp8/vp9/av1 are browser-playable codecs (video_ok is
    # True), but ffmpeg's mpegts muxer this module's HLS path relies on has
    # no mapping for them -- routing one into "-c:v copy -f mpegts" is a
    # guaranteed failure, not a degraded fallback, so it must be treated as
    # unsupported instead of guaranteeing a broken playback attempt.
    f = tmp_path / "clip.mkv"
    f.write_bytes(b"x")
    row = _row(path=str(f), video_codec=codec, audio_codec="opus")

    with pytest.raises(transcode.UnsupportedVideoCodec) as exc_info:
        transcode.resolve_playable_path(row)
    assert exc_info.value.reason == "container"
    message = exc_info.value.user_message().lower()
    assert "download" in message
    assert "parztream_enable_transcode" not in message  # not an encoder problem


def test_h264_in_wrong_container_still_gets_the_working_ts_remux(tmp_path):
    # h264 is the one codec TS_SAFE_VIDEO_CODECS allows -- confirms the fix
    # above didn't overcorrect and break the one case that actually works.
    f = tmp_path / "clip.mkv"
    f.write_bytes(b"x")
    row = _row(path=str(f), video_codec="h264", audio_codec="opus")

    with pytest.raises(transcode.NeedsHlsRemux) as exc_info:
        transcode.resolve_playable_path(row)
    assert exc_info.value.reencode_video is False


def test_build_playlist_lists_one_segment_per_chunk_and_ends_the_list():
    # No boundaries -- the fixed-grid fallback for a file whose keyframes
    # couldn't be probed, kept behaving exactly as it always has.
    playlist = transcode.build_playlist(duration=14.0)

    assert "#EXTM3U" in playlist
    assert "#EXT-X-ENDLIST" in playlist
    # 14s / 6s-per-segment -> 3 segments (0, 1, 2), last one shorter.
    assert "segment_00000.ts" in playlist
    assert "segment_00001.ts" in playlist
    assert "segment_00002.ts" in playlist
    assert "segment_00003.ts" not in playlist


def _extinf_values(playlist: str):
    return [
        float(line[len("#EXTINF:"):].rstrip(","))
        for line in playlist.splitlines() if line.startswith("#EXTINF:")
    ]


def test_compute_segment_boundaries_picks_first_keyframe_past_each_minimum():
    # Keyframes every 2s: boundaries land on the first keyframe at least
    # SEGMENT_SECONDS past the previous boundary. The 28s keyframe is only
    # 4s past the last boundary, so the tail folds into the final segment.
    keyframes = [float(t) for t in range(0, 30, 2)]

    boundaries = transcode.compute_segment_boundaries(keyframes, duration=30.0)

    assert boundaries == [0.0, 6.0, 12.0, 18.0, 24.0]


def test_compute_segment_boundaries_reflects_sparse_keyframes():
    # A 10s keyframe interval (sparse GOPs, common in high-quality rips):
    # segments come out 10s long because that's where cuts are physically
    # possible with -c:v copy -- the playlist must say so, not claim 6s.
    keyframes = [0.0, 10.0, 20.0, 30.0]

    boundaries = transcode.compute_segment_boundaries(keyframes, duration=40.0)

    assert boundaries == [0.0, 10.0, 20.0, 30.0]


def test_compute_segment_boundaries_normalizes_a_nonzero_start_clock():
    # MPEG-TS sources often start their timeline at a nonzero pts -- -ss
    # and the playlist both count from the start of the file, so boundaries
    # must be rebased to keyframes[0].
    keyframes = [1.4, 7.4, 13.4]

    boundaries = transcode.compute_segment_boundaries(keyframes, duration=18.0)

    assert boundaries == [0.0, 6.0, 12.0]


def test_compute_segment_boundaries_never_leaves_a_sliver_final_segment():
    # A keyframe right at the end must not become a boundary -- the final
    # "segment" would be a sub-second sliver (or empty if duration is
    # slightly under-reported).
    keyframes = [0.0, 6.0, 11.7]

    boundaries = transcode.compute_segment_boundaries(keyframes, duration=12.0)

    assert boundaries == [0.0, 6.0]


def test_compute_segment_boundaries_returns_none_without_keyframes():
    assert transcode.compute_segment_boundaries([], duration=30.0) is None
    assert transcode.compute_segment_boundaries(None, duration=30.0) is None


def test_build_playlist_extinf_values_match_the_boundaries():
    boundaries = [0.0, 6.5, 14.2, 20.2]

    playlist = transcode.build_playlist(duration=23.0, boundaries=boundaries)

    values = _extinf_values(playlist)
    assert values == pytest.approx([6.5, 7.7, 6.0, 2.8], abs=0.001)
    # TARGETDURATION is a spec MUST: >= every real segment duration, so the
    # 7.7s segment forces 8, not the old hardcoded SEGMENT_SECONDS.
    assert "#EXT-X-TARGETDURATION:8" in playlist
    assert "segment_00003.ts" in playlist
    assert "segment_00004.ts" not in playlist


def test_build_playlist_clamps_final_segment_when_duration_disagrees():
    # duration and the last boundary come from different ffprobe calls -- a
    # slightly-short duration must never produce a zero/negative EXTINF.
    playlist = transcode.build_playlist(duration=11.9, boundaries=[0.0, 6.0, 12.0])

    assert all(v > 0 for v in _extinf_values(playlist))


def _fake_landings(monkeypatch, landings):
    """Replace the real (subprocess-spawning) seek-landing probe with a
    fixed target->landing map, recording every probed target. `landings`
    maps a rounded target to where the fake demuxer 'lands'; a callable
    value is applied to the target."""
    probed = []

    def fake_probe(src_path, seconds):
        probed.append(round(seconds, 3))
        value = landings.get(round(seconds, 3), landings.get("default"))
        return value(seconds) if callable(value) else value

    monkeypatch.setattr(transcode, "_probe_seek_landing", fake_probe)
    return probed


def test_start_job_with_boundaries_cuts_at_the_stored_times(monkeypatch):
    captured_cmd = {}

    def fake_popen(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        return _FakeProcess(lambda: (b"", b"", 0))

    boundaries = [0.0, 6.5, 14.2, 20.2]
    # The demuxer lands exactly where aimed (the mp4-family case).
    _fake_landings(monkeypatch, {0.0: 0.0, 6.501: 6.5})
    with patch("subprocess.Popen", side_effect=fake_popen):
        transcode._start_job(
            Path("/tmp/some_hls_dir"), Path("/media/clip.mkv"), remux_audio=False,
            start_index=1, boundaries=boundaries,
        )

    cmd = captured_cmd["cmd"]
    # Seeks to the segment's exact stored boundary (plus the rounding
    # guard), never to a fixed start_index * 6s grid position.
    assert float(cmd[cmd.index("-ss") + 1]) == pytest.approx(
        6.5 + transcode.KEYFRAME_TIME_GUARD, abs=1e-6
    )
    # The split times are measured by the muxer from the first packet (the
    # landing), and audio must flow from the landing too, on a pinned
    # timeline -- see _start_job.
    assert "-noaccurate_seek" in cmd
    assert "-copyts" in cmd
    # Continuous timestamps only -- per-segment resets audibly broke audio
    # in real browsers (the PP6 bug, see _start_job's -copyts comment).
    assert "-reset_timestamps" not in cmd
    assert cmd[cmd.index("-segment_start_number") + 1] == "1"
    assert "-segment_time" not in cmd
    split_values = [float(v) for v in cmd[cmd.index("-segment_times") + 1].split(",")]
    expected = [
        14.2 - 6.5 - transcode.KEYFRAME_TIME_GUARD,
        20.2 - 6.5 - transcode.KEYFRAME_TIME_GUARD,
    ]
    assert split_values == pytest.approx(expected, abs=1e-6)


def test_start_job_seek_landing_short_starts_at_the_landed_boundary(monkeypatch):
    # Regression test for a confirmed live bug: on mkv, ffmpeg's -ss lands
    # ~0.13s short of the requested time (its internal dts heuristic), so
    # a seek aimed at boundary 22.0 landed on the *previous* seek point --
    # and split times computed against 22.0 then cut every segment in the
    # wrong place (a 15s segment where the playlist promised 8s). The job
    # must anchor to where the seek provably lands: same -ss target, but
    # numbering/splits from the landed boundary.
    captured_cmd = {}

    def fake_popen(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        return _FakeProcess(lambda: (b"", b"", 0))

    boundaries = [0.0, 9.0, 15.0, 22.0, 30.0, 37.0]
    _fake_landings(monkeypatch, {0.0: 0.0, 22.001: 15.0})
    with patch("subprocess.Popen", side_effect=fake_popen):
        job = transcode._start_job(
            Path("/tmp/some_hls_dir"), Path("/media/clip.mkv"), remux_audio=False,
            start_index=3, boundaries=boundaries,
        )

    cmd = captured_cmd["cmd"]
    assert float(cmd[cmd.index("-ss") + 1]) == pytest.approx(22.001, abs=1e-6)
    assert cmd[cmd.index("-segment_start_number") + 1] == "2"
    assert job.start_index == 2  # coverage bookkeeping must match the files
    split_values = [float(v) for v in cmd[cmd.index("-segment_times") + 1].split(",")]
    guard = transcode.KEYFRAME_TIME_GUARD
    assert split_values == pytest.approx([22.0 - 15.0 - guard, 30.0 - 15.0 - guard, 37.0 - 15.0 - guard], abs=1e-6)


def test_start_job_walks_down_when_the_landing_is_not_a_boundary(monkeypatch):
    # A landing on a non-boundary keyframe can't start a job (the first
    # file written must begin exactly at its own boundary) -- re-aim at
    # the landing and probe again until one IS a boundary.
    captured_cmd = {}

    def fake_popen(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        return _FakeProcess(lambda: (b"", b"", 0))

    boundaries = [0.0, 9.0, 15.0, 22.0, 30.0, 37.0]
    probed = _fake_landings(monkeypatch, {0.0: 0.0, 22.001: 17.0, 17.001: 15.0})
    with patch("subprocess.Popen", side_effect=fake_popen):
        transcode._start_job(
            Path("/tmp/some_hls_dir"), Path("/media/clip.mkv"), remux_audio=False,
            start_index=3, boundaries=boundaries,
        )

    cmd = captured_cmd["cmd"]
    assert probed == [0.0, 22.001, 17.001]
    assert float(cmd[cmd.index("-ss") + 1]) == pytest.approx(17.001, abs=1e-6)
    assert cmd[cmd.index("-segment_start_number") + 1] == "2"


def test_start_job_gives_up_walking_and_starts_from_zero(monkeypatch):
    # Pathological seek-point layout: no probe ever lands on a boundary.
    # Starting from the top of the file is slower but always correct.
    captured_cmd = {}

    def fake_popen(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        return _FakeProcess(lambda: (b"", b"", 0))

    boundaries = [0.0, 9.0, 15.0, 22.0, 30.0, 37.0]
    _fake_landings(monkeypatch, {0.0: 0.0, "default": lambda t: t - 1.5})
    with patch("subprocess.Popen", side_effect=fake_popen):
        transcode._start_job(
            Path("/tmp/some_hls_dir"), Path("/media/clip.mkv"), remux_audio=False,
            start_index=3, boundaries=boundaries,
        )

    cmd = captured_cmd["cmd"]
    assert "-ss" not in cmd
    assert cmd[cmd.index("-segment_start_number") + 1] == "0"
    split_values = [float(v) for v in cmd[cmd.index("-segment_times") + 1].split(",")]
    assert len(split_values) == len(boundaries) - 1


def test_start_job_probe_failure_falls_back_to_trusting_the_target(monkeypatch):
    # No ffmpeg / unreadable file: behave exactly as before the probe
    # existed (exact for mp4-family sources) rather than failing the job
    # before it even spawns.
    captured_cmd = {}

    def fake_popen(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        return _FakeProcess(lambda: (b"", b"", 0))

    boundaries = [0.0, 9.0, 15.0, 22.0, 30.0, 37.0]
    monkeypatch.setattr(transcode, "_probe_seek_landing", lambda *a: None)
    with patch("subprocess.Popen", side_effect=fake_popen):
        transcode._start_job(
            Path("/tmp/some_hls_dir"), Path("/media/clip.mkv"), remux_audio=False,
            start_index=3, boundaries=boundaries,
        )

    cmd = captured_cmd["cmd"]
    assert float(cmd[cmd.index("-ss") + 1]) == pytest.approx(22.001, abs=1e-6)
    # Even the degraded no-probe path keeps the one continuous timeline --
    # a plain -ss without it would emit near-zero timestamps that can't be
    # mixed with any other job's segments.
    assert "-copyts" in cmd
    assert cmd[cmd.index("-segment_start_number") + 1] == "3"
    split_values = [float(v) for v in cmd[cmd.index("-segment_times") + 1].split(",")]
    guard = transcode.KEYFRAME_TIME_GUARD
    assert split_values == pytest.approx([30.0 - 22.0 - 2 * guard, 37.0 - 22.0 - 2 * guard], abs=1e-6)


def test_seeked_reencode_job_never_probes_the_demuxer_landing(monkeypatch):
    # Re-encode decodes the video, so accurate_seek already makes output
    # frame-exact at the requested boundary -- probing would be wasted
    # subprocesses on the path that can least afford extra latency.
    monkeypatch.setattr(encoder_detect, "get_encoder", lambda: "libopenh264")

    def exploding_probe(*args):
        raise AssertionError("re-encode jobs must not probe seek landings")

    monkeypatch.setattr(transcode, "_probe_seek_landing", exploding_probe)
    captured_cmd = {}

    def fake_popen(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        return _FakeProcess(lambda: (b"", b"", 0))

    with patch("subprocess.Popen", side_effect=fake_popen):
        transcode._start_job(
            Path("/tmp/some_hls_dir"), Path("/media/clip.mkv"), remux_audio=False,
            start_index=2, reencode_video=True, boundaries=[0.0, 6.0, 12.0, 18.0],
        )

    cmd = captured_cmd["cmd"]
    # Boundary MINUS the guard: accurate_seek keeps frames at-or-after the
    # target, so aiming past the boundary would drop the boundary frame
    # itself and skew every split (see _start_job's re-encode branch).
    assert float(cmd[cmd.index("-ss") + 1]) == pytest.approx(11.999, abs=1e-6)
    assert "-copyts" in cmd
    assert cmd[cmd.index("-segment_start_number") + 1] == "2"


def test_start_job_for_the_final_segment_never_splits_again(monkeypatch):
    captured_cmd = {}

    def fake_popen(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        return _FakeProcess(lambda: (b"", b"", 0))

    _fake_landings(monkeypatch, {0.0: 0.0, 12.001: 12.0})
    with patch("subprocess.Popen", side_effect=fake_popen):
        transcode._start_job(
            Path("/tmp/some_hls_dir"), Path("/media/clip.mkv"), remux_audio=False,
            start_index=2, boundaries=[0.0, 6.0, 12.0],
        )

    cmd = captured_cmd["cmd"]
    assert "-segment_times" not in cmd
    # Without an explicit value the segment muxer's default is 2s -- the
    # remainder must stay one single segment.
    assert float(cmd[cmd.index("-segment_time") + 1]) > 10_000


def test_start_job_without_boundaries_keeps_the_fixed_grid(monkeypatch):
    captured_cmd = {}

    def fake_popen(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        return _FakeProcess(lambda: (b"", b"", 0))

    with patch("subprocess.Popen", side_effect=fake_popen):
        transcode._start_job(
            Path("/tmp/some_hls_dir"), Path("/media/clip.mkv"), remux_audio=False,
            start_index=3, boundaries=None,
        )

    cmd = captured_cmd["cmd"]
    assert cmd[cmd.index("-ss") + 1] == str(3 * transcode.SEGMENT_SECONDS)
    assert cmd[cmd.index("-segment_time") + 1] == str(transcode.SEGMENT_SECONDS)


def test_reencode_job_forces_keyframes_at_every_boundary(monkeypatch):
    monkeypatch.setattr(encoder_detect, "get_encoder", lambda: "libopenh264")
    captured_cmd = {}

    def fake_popen(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        return _FakeProcess(lambda: (b"", b"", 0))

    with patch("subprocess.Popen", side_effect=fake_popen):
        transcode._start_job(
            Path("/tmp/some_hls_dir"), Path("/media/clip.mkv"), remux_audio=False,
            start_index=0, reencode_video=True, boundaries=[0.0, 6.0, 12.5],
        )

    cmd = captured_cmd["cmd"]
    forced = [float(v) for v in cmd[cmd.index("-force_key_frames") + 1].split(",")]
    # The encoder must emit an IDR frame exactly where the muxer will cut,
    # so every re-encoded segment starts decodable -- stream copy gets this
    # for free from the source's own keyframes, a re-encode has to ask.
    assert forced == pytest.approx([6.0, 12.5], abs=1e-6)

    # Seeked: still the boundaries' ABSOLUTE times -- under -copyts the
    # encoder compares against the source's own clock, and a seek-relative
    # time simply never fires (verified against a real encode).
    with patch("subprocess.Popen", side_effect=fake_popen):
        transcode._start_job(
            Path("/tmp/some_hls_dir"), Path("/media/clip.mkv"), remux_audio=False,
            start_index=1, reencode_video=True, boundaries=[0.0, 6.0, 12.5],
        )
    cmd = captured_cmd["cmd"]
    forced = [float(v) for v in cmd[cmd.index("-force_key_frames") + 1].split(",")]
    assert forced == pytest.approx([12.5], abs=1e-6)


def test_stale_reset_format_segments_are_wiped_once_on_first_request(tmp_path, monkeypatch):
    # Segments cached before the continuous-timestamp format change each
    # restart near pts 0 -- serving one of those next to a new-format
    # segment reproduces exactly the timestamp chaos the change removed,
    # so a directory without the format marker gets wiped once.
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")
    hls_dir = transcode.hls_dir_for(9)
    hls_dir.mkdir(parents=True)
    stale = hls_dir / "segment_00000.ts"
    stale.write_bytes(b"pre-format-change segment")

    transcode._ensure_segment_format(9, hls_dir)

    assert not stale.exists()
    assert (hls_dir / transcode._FORMAT_MARKER).is_file()

    # Second touch is a no-op: marked directories keep their segments.
    keep = hls_dir / "segment_00000.ts"
    keep.write_bytes(b"new-format segment")
    transcode._ensure_segment_format(9, hls_dir)
    assert keep.read_bytes() == b"new-format segment"


def test_prune_never_evicts_the_segment_format_marker(tmp_path, monkeypatch):
    from app import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(cache, "CACHE_MAX_BYTES", 1)  # evict everything
    hls_dir = tmp_path / "9_hls"
    hls_dir.mkdir()
    (hls_dir / "segment_00000.ts").write_bytes(b"x" * 100)
    marker = hls_dir / transcode._FORMAT_MARKER
    marker.touch()

    cache.prune()

    assert not (hls_dir / "segment_00000.ts").exists()
    # Losing the marker would wipe the directory again on the next
    # request even though its remaining segments are the right format.
    assert marker.is_file()


def test_segment_request_past_the_playlist_end_fails_without_spawning_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")
    f = tmp_path / "clip.mkv"
    f.write_bytes(b"source bytes")

    with patch("subprocess.Popen") as mock_popen:
        with pytest.raises(FileNotFoundError):
            transcode.ensure_segment(
                125, f, remux_audio=False, index=3, boundaries=[0.0, 6.0, 12.0],
            )
    mock_popen.assert_not_called()


def test_invalidate_segments_removes_cached_segments_and_stops_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")
    f = tmp_path / "clip.mkv"
    f.write_bytes(b"source bytes")
    hls_dir = transcode.hls_dir_for(601)

    created = []

    def fake_popen(cmd, **kwargs):
        p = _TerminableFakeProcess()
        created.append(p)
        return p

    try:
        with patch("subprocess.Popen", side_effect=fake_popen):
            transcode._find_or_start_job(hls_dir, f, False, 0)
        stale = hls_dir / "segment_00000.ts"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"cut on the old fixed grid")

        transcode.invalidate_segments(601)

        assert not stale.exists()
        assert created[0].terminate_called.is_set()
    finally:
        for p in created:
            p.terminate()


@requires_ffmpeg
def test_boundary_cut_segments_really_match_the_playlist_durations(tmp_path, monkeypatch, h264_encoder):
    # End-to-end through real ffmpeg: probe real keyframes, compute
    # boundaries, cut a segment, and confirm its actual duration is the
    # same number the playlist advertises for it -- the exact property
    # whose violation (fixed-grid EXTINF vs. keyframe-cut segments) caused
    # the stutter/desync this design fixed.
    from app import scanner

    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")
    mkv_path = tmp_path / "clip.mkv"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=64x64:rate=25:duration=14",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=14",
            # Keyframe every 2s so boundaries land on a real 6s cadence --
            # a lavfi still image otherwise gets exactly one keyframe.
            "-force_key_frames", "expr:gte(t,n_forced*2)",
            "-c:v", h264_encoder, "-c:a", "aac", "-shortest",
            str(mkv_path),
        ],
        check=True,
    )

    keyframes = scanner.probe_keyframes(mkv_path)
    assert keyframes, "keyframe probe found nothing in a freshly-encoded file"
    boundaries = transcode.compute_segment_boundaries(keyframes, duration=14.0)
    assert boundaries is not None and len(boundaries) >= 2
    assert boundaries[0] == 0.0

    segment = transcode.ensure_segment(
        603, mkv_path, remux_audio=False, index=0, boundaries=boundaries,
    )

    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "csv=p=0", str(segment),
        ],
        capture_output=True, text=True, check=True,
    )
    actual = float(probe.stdout.strip())
    advertised = _extinf_values(transcode.build_playlist(14.0, boundaries))[0]
    assert actual == pytest.approx(advertised, abs=0.3)


@requires_ffmpeg
def test_seeked_job_produces_the_same_segment_as_a_sequential_one(tmp_path, monkeypatch, h264_encoder):
    # The old fixed-grid seek was non-deterministic: "-ss N*6" snapped to
    # whatever keyframe was nearest, so a job seeked to segment N cut
    # different content than a sequential job passing through N. With
    # stored boundaries plus the seek-landing probe, both must produce a
    # segment starting at the same keyframe with the same duration.
    #
    # The keyframes here are deliberately IRREGULAR (so every segment has
    # a different length): an earlier version of this test used a uniform
    # 2s cadence, and a confirmed live bug slipped straight through it --
    # ffmpeg's -ss landed one seek point early on mkv (its ~0.13s dts
    # heuristic), producing a segment of *wrong content* whose duration
    # happened to match because every GOP was the same size. Duration
    # assertions only mean something when the expected lengths are
    # distinguishable.
    from app import scanner

    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")
    mkv_path = tmp_path / "clip.mkv"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=64x64:rate=25:duration=20",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
            "-force_key_frames", "0,4,9,15",
            "-c:v", h264_encoder, "-c:a", "aac", "-shortest",
            str(mkv_path),
        ],
        check=True,
    )

    boundaries = transcode.compute_segment_boundaries(
        scanner.probe_keyframes(mkv_path), duration=20.0
    )
    assert boundaries == pytest.approx([0.0, 9.0, 15.0], abs=0.05)

    def segment_duration(path):
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "csv=p=0", str(path),
            ],
            capture_output=True, text=True, check=True,
        )
        return float(probe.stdout.strip())

    # Sequential: a job starting at 0 passes through segment 1 on its way.
    sequential = transcode.ensure_segment(604, mkv_path, remux_audio=False, index=1, boundaries=boundaries)
    sequential_duration = segment_duration(sequential)

    # Seeked: a fresh media id (fresh hls dir) asked directly for the
    # final segment -- the landing probe may legitimately start the job at
    # an earlier boundary (mkv seeks land short), but the file served for
    # index 2 must still be exactly [15, 20).
    seeked = transcode.ensure_segment(605, mkv_path, remux_audio=False, index=2, boundaries=boundaries)
    seeked_duration = segment_duration(seeked)

    assert sequential_duration == pytest.approx(15.0 - 9.0, abs=0.3)
    assert seeked_duration == pytest.approx(20.0 - 15.0, abs=0.3)

    # And a seeked request for the same middle segment the sequential job
    # produced must match it.
    seeked_middle = transcode.ensure_segment(606, mkv_path, remux_audio=False, index=1, boundaries=boundaries)
    assert segment_duration(seeked_middle) == pytest.approx(sequential_duration, abs=0.15)


@requires_ffmpeg
def test_seeked_job_segments_carry_the_same_timestamps_as_sequential_ones(tmp_path, monkeypatch, h264_encoder):
    # PP6 regression, confirmed live against a real library file: the
    # duration/content test above passed while segment TIMESTAMPS were
    # broken two ways -- every segment reset to pts ~0 (which hls.js's
    # audio remuxer can't stitch: it drops the "overlapping" audio, so
    # sound starves/desyncs on every HLS-routed file), and a seeked job's
    # first segment kept absolute timestamps while the rest reset, so the
    # same index could differ by minutes depending on which job wrote it.
    # The fix is one continuous absolute timeline (see _start_job's
    # -copyts comment), which this test pins down in both directions:
    # same index => same timestamps regardless of job, and consecutive
    # segments => contiguous timestamps exactly one EXTINF apart. Checked
    # for the audio stream too, since the desyncing real files all route
    # through remux_audio=True (hence the ac3 source track here).
    from app import scanner

    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")
    mkv_path = tmp_path / "clip.mkv"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=64x64:rate=25:duration=20",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
            # Irregular on purpose -- see the sibling test above.
            "-force_key_frames", "0,4,9,15",
            "-c:v", h264_encoder, "-c:a", "ac3", "-shortest",
            str(mkv_path),
        ],
        check=True,
    )

    boundaries = transcode.compute_segment_boundaries(
        scanner.probe_keyframes(mkv_path), duration=20.0
    )
    assert boundaries == pytest.approx([0.0, 9.0, 15.0], abs=0.05)

    def stream_starts(path):
        # First packet pts per stream, read from the packets themselves --
        # ffprobe's stream-level start_time is a heuristic that can sit a
        # B-frame reorder delay off for a segment whose first dts precedes
        # its first pts, which is exactly the precision under test here.
        starts = {}
        for selector, codec_type in (("v:0", "video"), ("a:0", "audio")):
            probe = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-select_streams", selector,
                    "-show_entries", "packet=pts_time",
                    "-of", "csv=p=0", str(path),
                ],
                capture_output=True, text=True, check=True,
            )
            first = next((l for l in probe.stdout.splitlines() if l.strip(",")), None)
            if first is not None:
                starts[codec_type] = float(first.strip(","))
        return starts

    # Sequential job: starts from the top, then a second request for the
    # final index rides the same job -- and a final-index request only
    # returns once the job has fully exited (see ensure_segment's wait
    # loop), so every file compared below is complete, not mid-write.
    transcode.ensure_segment(607, mkv_path, remux_audio=True, index=0, boundaries=boundaries)
    transcode.ensure_segment(607, mkv_path, remux_audio=True, index=2, boundaries=boundaries)
    # Seeked job under a fresh media id: asked directly for a later
    # segment, so it anchors at a probed landing and runs with
    # -copyts (see _start_job); same final-index completion guarantee.
    seeked = transcode.ensure_segment(608, mkv_path, remux_audio=True, index=2, boundaries=boundaries)
    assert seeked.is_file()

    sequential_dir = transcode.hls_dir_for(607)
    for segment in sorted(transcode.hls_dir_for(608).glob("segment_*.ts")):
        twin = sequential_dir / segment.name
        assert twin.is_file(), f"sequential job never produced {segment.name}"
        seeked_starts = stream_starts(segment)
        sequential_starts = stream_starts(twin)
        assert set(seeked_starts) == {"video", "audio"}
        for codec_type, start in sequential_starts.items():
            assert seeked_starts[codec_type] == pytest.approx(start, abs=0.05), (
                f"{segment.name} {codec_type} starts at {seeked_starts[codec_type]}"
                f" from the seeked job but {start} from the sequential one"
            )

    # Continuity: within one job's output, each segment's video timeline
    # picks up exactly where the previous one left off (one boundary
    # further along) -- per-segment resets would make every delta ~0.
    # Segment 0 alone may sit up to one B-frame reorder delay high: its
    # first dts is negative on a B-frame source, and each segment's own
    # mpegts muxer context compensates negative starts individually.
    # That bump is deterministic (any job producing segment 0 starts from
    # the top and gets the identical shift -- the cross-job assertions
    # above already prove it), one-time, and far inside hls.js's default
    # fragment-placement tolerance, so it's tolerated rather than fought.
    sequential_video_starts = [
        stream_starts(sequential_dir / f"segment_{i:05d}.ts")["video"]
        for i in range(len(boundaries))
    ]
    deltas = [b - a for a, b in zip(sequential_video_starts, sequential_video_starts[1:])]
    expected = [b - a for a, b in zip(boundaries, boundaries[1:])]
    assert deltas[0] == pytest.approx(expected[0], abs=0.2)
    assert deltas[1:] == pytest.approx(expected[1:], abs=0.05)


@requires_ffmpeg
def test_mkv_with_compatible_codecs_gets_remuxed_into_a_playable_segment(tmp_path, monkeypatch, h264_encoder):
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")

    mkv_path = tmp_path / "clip.mkv"
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
def test_incompatible_audio_gets_transcoded_while_video_is_copied(tmp_path, monkeypatch, h264_encoder):
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")

    mkv_path = tmp_path / "clip.mkv"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=blue:size=64x64:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", h264_encoder, "-c:a", "ac3", "-shortest",
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
def test_multichannel_audio_is_downmixed_to_stereo_when_transcoded(tmp_path, monkeypatch, h264_encoder):
    # Regression test for the real playback bug this fixed: Chromium's
    # MediaSource AAC decoder rejects multichannel (>2 channel) AAC
    # outright (confirmed live against a real browser -- "
    # CHUNK_DEMUXER_ERROR_APPEND_FAILED"), even though the exact same
    # segment is completely valid per ffprobe/ffmpeg's own decoder. A
    # straight "-c:v copy -c:a aac" on a 5.1 source stays 6-channel AAC and
    # silently produces a segment that looks fine here but never actually
    # plays in a real browser.
    mkv_path = tmp_path / "clip_51.mkv"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=blue:size=64x64:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-filter_complex", "[1:a]pan=5.1|FL=c0|FR=c0|FC=c0|LFE=c0|BL=c0|BR=c0[a51]",
            "-map", "0:v", "-map", "[a51]",
            "-c:v", h264_encoder, "-c:a", "ac3", "-shortest",
            str(mkv_path),
        ],
        check=True,
    )

    segment = transcode.ensure_segment(8, mkv_path, remux_audio=True, index=0, audio_stream_index=0)

    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=codec_name,channels",
            "-of", "csv=p=0", str(segment),
        ],
        capture_output=True, text=True, check=True,
    )
    # .ts (MPEG-TS) containers can report stream info more than once (PMT/PID
    # quirk) -- the first non-empty line is enough to confirm the downmix.
    first_line = next(line for line in probe.stdout.splitlines() if line.strip())
    codec, channels = first_line.split(",")
    assert codec == "aac"
    assert int(channels) <= transcode.MAX_DIRECT_PLAY_AUDIO_CHANNELS


def test_start_job_maps_explicit_video_and_audio_streams_when_index_known(monkeypatch):
    captured_cmd = {}

    def fake_popen(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        return _FakeProcess(lambda: (b"", b"", 0))

    with patch("subprocess.Popen", side_effect=fake_popen):
        transcode._start_job(
            Path("/tmp/some_hls_dir"), Path("/media/clip.mkv"), remux_audio=True,
            start_index=0, audio_stream_index=2,
        )

    cmd = captured_cmd["cmd"]
    assert "-map" in cmd
    map_positions = [i for i, arg in enumerate(cmd) if arg == "-map"]
    mapped_values = [cmd[i + 1] for i in map_positions]
    assert mapped_values == ["0:v:0", "0:a:2"]
    assert "-ac" in cmd
    assert cmd[cmd.index("-ac") + 1] == "2"


def test_start_job_omits_map_entirely_when_audio_stream_index_unknown(monkeypatch):
    # Regression test: a bare "-map 0:v:0" with no matching audio -map
    # would restrict the output to video only and silently drop audio --
    # -map must either cover both streams or be omitted entirely so
    # ffmpeg's own default auto-selection picks both, exactly as before
    # this feature existed (e.g. for a row scanned before this column was
    # added).
    captured_cmd = {}

    def fake_popen(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        return _FakeProcess(lambda: (b"", b"", 0))

    with patch("subprocess.Popen", side_effect=fake_popen):
        transcode._start_job(
            Path("/tmp/some_hls_dir"), Path("/media/clip.mkv"), remux_audio=True,
            start_index=0, audio_stream_index=None,
        )

    assert "-map" not in captured_cmd["cmd"]


@requires_ffmpeg
def test_seeking_ahead_of_generated_segments_triggers_a_new_job_from_that_point(tmp_path, monkeypatch, h264_encoder):
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")

    mkv_path = tmp_path / "clip.mkv"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=blue:size=64x64:duration=30",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=30",
            "-c:v", h264_encoder, "-c:a", "aac", "-shortest",
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


def test_stale_cached_segments_are_invalidated_when_source_file_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")

    src_path = tmp_path / "clip.mkv"
    src_path.write_bytes(b"original")

    hls_dir = transcode.hls_dir_for(99)
    hls_dir.mkdir(parents=True)
    stale_segment = hls_dir / "segment_00000.ts"
    stale_segment.write_bytes(b"stale segment content")

    # Establish the marker against the file's current mtime -- not stale yet.
    transcode._invalidate_if_source_changed(99, hls_dir, src_path)
    assert stale_segment.is_file()

    # Replace the source in place with a newer mtime -- same path, so same
    # media_id/hls_dir, exactly the scenario M6 describes.
    time.sleep(0.01)
    src_path.write_bytes(b"replaced content")
    os.utime(src_path, None)

    transcode._invalidate_if_source_changed(99, hls_dir, src_path)

    assert not stale_segment.is_file()


def test_unchanged_source_file_keeps_cached_segments(tmp_path, monkeypatch):
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")

    src_path = tmp_path / "clip.mkv"
    src_path.write_bytes(b"original")

    hls_dir = transcode.hls_dir_for(100)
    hls_dir.mkdir(parents=True)
    segment = hls_dir / "segment_00000.ts"
    segment.write_bytes(b"segment content")

    transcode._invalidate_if_source_changed(100, hls_dir, src_path)
    transcode._invalidate_if_source_changed(100, hls_dir, src_path)

    assert segment.is_file()


@requires_ffmpeg
def test_creating_a_new_segment_prunes_older_ones_once_over_budget(tmp_path, monkeypatch, h264_encoder):
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
                "-c:v", h264_encoder, "-c:a", "aac", "-shortest",
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


def test_ensure_segment_wakes_promptly_once_the_segment_becomes_valid(tmp_path, monkeypatch):
    # Proves the event-driven wait actually wakes faster than the old
    # 100ms poll interval would have -- generous margin to avoid flakiness
    # on a loaded CI box, but well under what a second full poll cycle
    # plus scheduling slop would need.
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")

    f = tmp_path / "clip.mkv"
    f.write_bytes(b"source bytes")

    def fake_popen(cmd, **kwargs):
        segment_path = Path(cmd[-1].replace("%05d", "00000"))

        def on_communicate():
            time.sleep(0.3)
            segment_path.parent.mkdir(parents=True, exist_ok=True)
            segment_path.write_bytes(b"fake segment")
            Path(cmd[-1].replace("%05d", "00001")).write_bytes(b"fake next segment")
            return b"", b"", 0

        return _FakeProcess(on_communicate)

    with patch("subprocess.Popen", side_effect=fake_popen):
        start = time.monotonic()
        transcode.ensure_segment(200, f, remux_audio=False, index=0)
        elapsed = time.monotonic() - start

    # The segment isn't written until ~0.3s in; a prompt wake should return
    # well before a second 100ms poll tick would add much slop on top.
    assert elapsed < 0.5


def test_many_waiters_share_a_single_progress_poller(tmp_path, monkeypatch):
    # Before this fix, every ensure_segment caller ran its own independent
    # sleep(0.1)-poll loop against _highest_contiguous_segment -- N waiters
    # meant N threads churning through filesystem checks. Now there should
    # be exactly one poller per job, so the call count is bounded by
    # elapsed time / poll interval, not by the number of waiting threads.
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")

    f = tmp_path / "clip.mkv"
    f.write_bytes(b"source bytes")

    call_count = 0
    call_count_lock = threading.Lock()
    real_highest_contiguous_segment = transcode._highest_contiguous_segment

    def counting_wrapper(hls_dir, start):
        nonlocal call_count
        with call_count_lock:
            call_count += 1
        return real_highest_contiguous_segment(hls_dir, start)

    monkeypatch.setattr(transcode, "_highest_contiguous_segment", counting_wrapper)

    def fake_popen(cmd, **kwargs):
        segment_path = Path(cmd[-1].replace("%05d", "00000"))

        def on_communicate():
            time.sleep(0.3)
            segment_path.parent.mkdir(parents=True, exist_ok=True)
            segment_path.write_bytes(b"fake segment")
            Path(cmd[-1].replace("%05d", "00001")).write_bytes(b"fake next segment")
            return b"", b"", 0

        return _FakeProcess(on_communicate)

    results = []
    with patch("subprocess.Popen", side_effect=fake_popen):
        threads = [
            threading.Thread(
                target=lambda: results.append(transcode.ensure_segment(201, f, remux_audio=False, index=0))
            )
            for _ in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert len(results) == 8
    # ~0.3s of polling at a 0.1s interval is roughly 3-4 calls from the one
    # poller thread; _check_jobs_locked also calls this per _find_or_start_job
    # invocation (once per waiter, i.e. up to 8 more) -- bounded well below
    # what 8 independent 100ms-poll waiter loops over 0.3s+ would produce.
    assert call_count < 30


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


def test_incompatible_codec_with_transcode_off_raises_unsupported(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRANSCODE_MODE", "off")
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


def test_incompatible_codec_with_transcode_on_but_no_encoder_raises_unsupported(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRANSCODE_MODE", "on")
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


def test_incompatible_codec_with_transcode_on_and_encoder_found_needs_hls_remux(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRANSCODE_MODE", "on")
    monkeypatch.setattr(encoder_detect, "get_encoder", lambda: "libopenh264")
    f = tmp_path / "clip.mkv"
    f.write_bytes(b"x")
    row = _row(path=str(f), video_codec="hevc", audio_codec="ac3")

    with pytest.raises(transcode.NeedsHlsRemux) as exc_info:
        transcode.resolve_playable_path(row)
    assert exc_info.value.reencode_video is True
    assert exc_info.value.remux_audio is True  # ac3 is also incompatible


def test_transcode_off_never_calls_encoder_detection(tmp_path, monkeypatch):
    # Proves the mode short-circuits before encoder_detect is even touched
    # -- zero new code runs when the feature is off, exactly today's
    # behavior.
    monkeypatch.setattr(config, "TRANSCODE_MODE", "off")
    called = []
    monkeypatch.setattr(encoder_detect, "get_encoder", lambda: called.append(True))
    f = tmp_path / "clip.mkv"
    f.write_bytes(b"x")
    row = _row(path=str(f), video_codec="hevc", audio_codec="aac")

    with pytest.raises(transcode.UnsupportedVideoCodec):
        transcode.resolve_playable_path(row)
    assert called == []


def test_transcode_on_never_calls_capability_benchmark(tmp_path, monkeypatch):
    # "on" is an explicit, unconditional opt-in -- it must never be
    # second-guessed by the auto-detection benchmark, only the plain
    # existence check "on" always used before auto-detection existed.
    monkeypatch.setattr(config, "TRANSCODE_MODE", "on")
    monkeypatch.setattr(encoder_detect, "get_encoder", lambda: "libopenh264")
    called = []
    monkeypatch.setattr(encoder_detect, "is_transcode_capable", lambda: called.append(True))
    f = tmp_path / "clip.mkv"
    f.write_bytes(b"x")
    row = _row(path=str(f), video_codec="hevc", audio_codec="aac")

    with pytest.raises(transcode.NeedsHlsRemux):
        transcode.resolve_playable_path(row)
    assert called == []


def test_incompatible_codec_with_transcode_auto_and_capable_hardware_needs_hls_remux(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRANSCODE_MODE", "auto")
    monkeypatch.setattr(encoder_detect, "is_transcode_capable", lambda: True)
    f = tmp_path / "clip.mkv"
    f.write_bytes(b"x")
    row = _row(path=str(f), video_codec="hevc", audio_codec="ac3")

    with pytest.raises(transcode.NeedsHlsRemux) as exc_info:
        transcode.resolve_playable_path(row)
    assert exc_info.value.reencode_video is True
    assert exc_info.value.remux_audio is True


def test_incompatible_codec_with_transcode_auto_and_incapable_hardware_raises_unsupported(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRANSCODE_MODE", "auto")
    monkeypatch.setattr(encoder_detect, "is_transcode_capable", lambda: False)
    f = tmp_path / "clip.mkv"
    f.write_bytes(b"x")
    row = _row(path=str(f), video_codec="hevc", audio_codec="aac")

    with pytest.raises(transcode.UnsupportedVideoCodec) as exc_info:
        transcode.resolve_playable_path(row)
    assert exc_info.value.transcode_enabled is False


def test_transcode_auto_never_calls_plain_get_encoder(tmp_path, monkeypatch):
    # "auto" must route entirely through is_transcode_capable(),
    # never the plain existence-only get_encoder() -- otherwise a slow
    # hardware encoder (or the software fallback) could get auto-enabled
    # without ever being benchmarked.
    monkeypatch.setattr(config, "TRANSCODE_MODE", "auto")
    monkeypatch.setattr(encoder_detect, "is_transcode_capable", lambda: False)
    called = []
    monkeypatch.setattr(encoder_detect, "get_encoder", lambda: called.append(True))
    f = tmp_path / "clip.mkv"
    f.write_bytes(b"x")
    row = _row(path=str(f), video_codec="hevc", audio_codec="aac")

    with pytest.raises(transcode.UnsupportedVideoCodec):
        transcode.resolve_playable_path(row)
    assert called == []


def test_needs_segment_boundaries_true_for_reencode_when_mode_on(monkeypatch):
    monkeypatch.setattr(config, "TRANSCODE_MODE", "on")
    assert transcode.needs_segment_boundaries(Path("clip.mkv"), "hevc", "aac", 2) is True


def test_needs_segment_boundaries_false_for_reencode_when_mode_auto(monkeypatch):
    # Deliberately treated like "off" here -- see needs_segment_boundaries's
    # docstring: a scan must never trigger encoder_detect's probing
    # subprocesses, even indirectly via the auto-detection benchmark.
    monkeypatch.setattr(config, "TRANSCODE_MODE", "auto")
    assert transcode.needs_segment_boundaries(Path("clip.mkv"), "hevc", "aac", 2) is False


def test_needs_segment_boundaries_false_for_reencode_when_mode_off(monkeypatch):
    monkeypatch.setattr(config, "TRANSCODE_MODE", "off")
    assert transcode.needs_segment_boundaries(Path("clip.mkv"), "hevc", "aac", 2) is False


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
def test_hevc_source_is_transcoded_to_h264_via_software_fallback(tmp_path, monkeypatch, h264_encoder):
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")
    # This one runs a real re-encode, so the pinned encoder must be one
    # this machine's ffmpeg actually has -- unlike the mocked-Popen tests
    # elsewhere in this file, where "libopenh264" is just a string.
    monkeypatch.setattr(encoder_detect, "get_encoder", lambda: h264_encoder)

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
def test_resolution_cap_applied_during_reencode(tmp_path, monkeypatch, h264_encoder):
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")
    # Real re-encode -- see test_hevc_source_is_transcoded_to_h264_via_
    # software_fallback for why this can't hardcode "libopenh264".
    monkeypatch.setattr(encoder_detect, "get_encoder", lambda: h264_encoder)

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


def test_starting_a_new_job_terminates_the_old_one_for_the_same_hls_dir(tmp_path, monkeypatch):
    # Regression test: an old job left running after a far seek used to
    # never get stopped, letting it race a newer job on the same
    # segment_%05d.ts filenames (two -c:v copy jobs cut segments
    # differently, so "segment N" from each can have different content).
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")
    f = tmp_path / "clip.mkv"
    f.write_bytes(b"source bytes")
    hls_dir = transcode.hls_dir_for(701)

    created = []

    def fake_popen(cmd, **kwargs):
        p = _TerminableFakeProcess()
        created.append(p)
        return p

    try:
        with patch("subprocess.Popen", side_effect=fake_popen):
            job1 = transcode._find_or_start_job(hls_dir, f, False, 0)
            assert len(created) == 1

            # Far beyond LOOKAHEAD_SEGMENTS -- job1 can't cover this, so a
            # genuinely new job must start.
            job2 = transcode._find_or_start_job(hls_dir, f, False, 500)
            assert len(created) == 2
            assert job2 is not job1

        assert created[0].terminate_called.is_set()
        assert job1.superseded is True
    finally:
        for p in created:
            p.terminate()


def test_idle_job_is_reaped_when_a_different_video_is_requested(tmp_path, monkeypatch):
    # Regression test: an abandoned job (viewer navigated away) used to run
    # to end-of-file, holding the transcode semaphore/CPU/disk the whole
    # time. Reaping is opportunistic (piggybacks on any segment request,
    # not a timer), so this simulates that by making a completely
    # unrelated video's request trigger the sweep.
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")
    f = tmp_path / "clip.mkv"
    f.write_bytes(b"source bytes")

    created = []

    def fake_popen(cmd, **kwargs):
        p = _TerminableFakeProcess()
        created.append(p)
        return p

    try:
        with patch("subprocess.Popen", side_effect=fake_popen):
            job_a = transcode._find_or_start_job(transcode.hls_dir_for(801), f, False, 0)
            job_a.last_requested = time.monotonic() - (transcode.JOB_IDLE_TIMEOUT + 1)

            transcode._find_or_start_job(transcode.hls_dir_for(802), f, False, 0)

        assert job_a.superseded is True
        assert created[0].terminate_called.is_set()
    finally:
        for p in created:
            p.terminate()


def test_transcode_unavailable_raised_when_no_slot_frees_up_in_time(tmp_path, monkeypatch):
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(transcode, "_transcode_semaphore", threading.Semaphore(1))
    monkeypatch.setattr(transcode, "TRANSCODE_SLOT_TIMEOUT", 0.2)
    monkeypatch.setattr(encoder_detect, "get_encoder", lambda: "libopenh264")

    f = tmp_path / "clip.mkv"
    f.write_bytes(b"source bytes")
    held_process = _TerminableFakeProcess()

    try:
        with patch("subprocess.Popen", return_value=held_process):
            job1 = transcode._find_or_start_job(
                transcode.hls_dir_for(901), f, False, 0, True, None, None,
            )
            with pytest.raises(transcode.TranscodeUnavailable):
                transcode._find_or_start_job(
                    transcode.hls_dir_for(902), f, False, 0, True, None, None,
                )
        assert job1 is not None
    finally:
        held_process.terminate()


def test_reencode_with_disappeared_encoder_device_fails_cleanly_and_releases_slot(tmp_path, monkeypatch):
    # Regression test: get_encoder() can cache e.g. "h264_vaapi" earlier in
    # the process's life, then the render node it needs disappears (device
    # unplugged, permissions changed) -- encode_video_args then returns
    # (None, None), which used to get splatted straight into the ffmpeg
    # command list, raising an obscure TypeError instead of a clean error,
    # and never releasing the semaphore slot it had already acquired.
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(transcode, "_transcode_semaphore", threading.Semaphore(1))
    monkeypatch.setattr(encoder_detect, "get_encoder", lambda: "h264_vaapi")
    monkeypatch.setattr(encoder_detect, "encode_video_args", lambda *a, **k: (None, None))

    f = tmp_path / "clip.mkv"
    f.write_bytes(b"source bytes")

    with pytest.raises(transcode.RemuxFailed):
        transcode._find_or_start_job(transcode.hls_dir_for(1001), f, False, 0, True, None, None)

    # The slot must have been released despite the failure -- a bounded
    # acquire proves it's actually free, not just "eventually" free.
    assert transcode._transcode_semaphore.acquire(timeout=1)
