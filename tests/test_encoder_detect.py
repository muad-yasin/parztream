import glob
import shutil
import subprocess
import threading
import time

import pytest

from app import encoder_detect

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)

requires_vaapi_render_node = pytest.mark.skipif(
    not glob.glob("/dev/dri/renderD*"), reason="no VAAPI render node on this machine"
)


@pytest.fixture(autouse=True)
def reset_detection_cache():
    encoder_detect._detected_encoder = encoder_detect._UNSET
    encoder_detect._auto_capable = encoder_detect._UNSET_CAPABLE
    yield
    encoder_detect._detected_encoder = encoder_detect._UNSET
    encoder_detect._auto_capable = encoder_detect._UNSET_CAPABLE


def test_first_working_candidate_wins_and_stops_trying_further_ones(monkeypatch):
    monkeypatch.setattr(encoder_detect, "CANDIDATES_BY_PLATFORM", {"linux": ["a", "b", "c"]})
    monkeypatch.setattr(encoder_detect.sys, "platform", "linux")
    monkeypatch.setattr(encoder_detect, "_list_encoders", lambda: {"a", "b", "c", encoder_detect.SOFTWARE_FALLBACK})
    tried = []

    def fake_try(name):
        tried.append(name)
        return name == "b"

    monkeypatch.setattr(encoder_detect, "_try_encode", fake_try)

    assert encoder_detect.get_encoder() == "b"
    assert tried == ["a", "b"]  # "c" never tried once "b" succeeds


def test_falls_through_to_software_when_no_hardware_candidate_works(monkeypatch):
    monkeypatch.setattr(encoder_detect, "CANDIDATES_BY_PLATFORM", {"linux": ["a", "b"]})
    monkeypatch.setattr(encoder_detect.sys, "platform", "linux")
    monkeypatch.setattr(encoder_detect, "_list_encoders", lambda: {"a", "b", encoder_detect.SOFTWARE_FALLBACK})
    monkeypatch.setattr(encoder_detect, "_try_encode", lambda name: name == encoder_detect.SOFTWARE_FALLBACK)

    assert encoder_detect.get_encoder() == encoder_detect.SOFTWARE_FALLBACK


def test_returns_none_when_nothing_works(monkeypatch):
    monkeypatch.setattr(encoder_detect, "CANDIDATES_BY_PLATFORM", {"linux": ["a"]})
    monkeypatch.setattr(encoder_detect.sys, "platform", "linux")
    monkeypatch.setattr(encoder_detect, "_list_encoders", lambda: {"a", encoder_detect.SOFTWARE_FALLBACK})
    monkeypatch.setattr(encoder_detect, "_try_encode", lambda name: False)

    assert encoder_detect.get_encoder() is None


def test_candidate_not_listed_is_never_probed(monkeypatch):
    # A candidate could be a real ffmpeg encoder name but simply not
    # compiled into this particular build -- must not waste a real encode
    # attempt on something guaranteed to fail.
    monkeypatch.setattr(encoder_detect, "CANDIDATES_BY_PLATFORM", {"linux": ["a", "b"]})
    monkeypatch.setattr(encoder_detect.sys, "platform", "linux")
    monkeypatch.setattr(encoder_detect, "_list_encoders", lambda: {"b"})  # "a" not compiled in
    tried = []
    monkeypatch.setattr(encoder_detect, "_try_encode", lambda name: tried.append(name) or True)

    assert encoder_detect.get_encoder() == "b"
    assert tried == ["b"]


def test_result_is_cached_second_call_does_not_reprobe(monkeypatch):
    call_count = {"n": 0}

    def fake_list():
        call_count["n"] += 1
        return {encoder_detect.SOFTWARE_FALLBACK}

    monkeypatch.setattr(encoder_detect, "CANDIDATES_BY_PLATFORM", {})
    monkeypatch.setattr(encoder_detect, "_list_encoders", fake_list)
    monkeypatch.setattr(encoder_detect, "_try_encode", lambda name: True)

    first = encoder_detect.get_encoder()
    second = encoder_detect.get_encoder()

    assert first == second == encoder_detect.SOFTWARE_FALLBACK
    assert call_count["n"] == 1


def test_concurrent_first_callers_only_trigger_one_probe_round(monkeypatch):
    call_count = {"n": 0}
    call_count_lock = threading.Lock()

    def fake_list():
        with call_count_lock:
            call_count["n"] += 1
        time.sleep(0.05)  # widen the race window so the bug would reproduce
        return {encoder_detect.SOFTWARE_FALLBACK}

    monkeypatch.setattr(encoder_detect, "CANDIDATES_BY_PLATFORM", {})
    monkeypatch.setattr(encoder_detect, "_list_encoders", fake_list)
    monkeypatch.setattr(encoder_detect, "_try_encode", lambda name: True)

    results = []
    threads = [
        threading.Thread(target=lambda: results.append(encoder_detect.get_encoder()))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert call_count["n"] == 1
    assert all(r == encoder_detect.SOFTWARE_FALLBACK for r in results)


@requires_ffmpeg
def test_real_detection_finds_a_working_encoder():
    # Confirms the real subprocess plumbing (ffmpeg -encoders parsing, the
    # synthetic test-encode) works end to end, not just the mocked logic
    # above -- whichever encoder actually wins depends on this machine's
    # real hardware (a dev box with no GPU falls through to the software
    # fallback; one with a working VAAPI/NVENC/etc. path may not).
    #
    # Only meaningful when libopenh264 is compiled in: it's the sole
    # candidate guaranteed to work without a GPU, so on a build without it
    # (GPL ffmpeg, e.g. CI's or most distros') a GPU-less machine
    # legitimately detects nothing and None is the *correct* answer, not a
    # plumbing failure.
    listed = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True
    ).stdout
    if "libopenh264" not in listed:
        pytest.skip("this ffmpeg lacks libopenh264, the only no-hardware candidate")
    assert encoder_detect.get_encoder() is not None


def test_hwaccel_pre_input_args_is_empty_for_plain_encoders():
    # NVENC/AMF/VideoToolbox accept normal software frames directly --
    # no device init needed before -i, unlike VAAPI/QSV below.
    assert encoder_detect._hwaccel_pre_input_args("h264_nvenc") == []
    assert encoder_detect._hwaccel_pre_input_args("libopenh264") == []


def test_hwaccel_pre_input_args_for_qsv_is_always_present():
    assert encoder_detect._hwaccel_pre_input_args("h264_qsv") == [
        "-init_hw_device", "qsv=hw", "-filter_hw_device", "hw",
    ]


def test_hwaccel_pre_input_args_for_vaapi_uses_the_detected_device(monkeypatch):
    monkeypatch.setattr(encoder_detect, "_vaapi_device_path", lambda: "/dev/dri/renderD128")
    assert encoder_detect._hwaccel_pre_input_args("h264_vaapi") == [
        "-vaapi_device", "/dev/dri/renderD128",
    ]


def test_hwaccel_pre_input_args_for_vaapi_is_none_without_a_render_node(monkeypatch):
    # None (not []) signals "can't even attempt this candidate here" --
    # distinct from "attempted and failed", so callers skip straight past
    # it instead of spawning a doomed ffmpeg process.
    monkeypatch.setattr(encoder_detect, "_vaapi_device_path", lambda: None)
    assert encoder_detect._hwaccel_pre_input_args("h264_vaapi") is None


def test_hwaccel_upload_filter_empty_for_plain_encoders():
    assert encoder_detect._hwaccel_upload_filter("h264_nvenc") == ""
    assert encoder_detect._hwaccel_upload_filter("libopenh264") == ""


def test_encode_video_args_for_plain_encoder_with_and_without_scale():
    assert encoder_detect.encode_video_args("libopenh264", None, None, "") == (
        [], ["-c:v", "libopenh264"],
    )
    assert encoder_detect.encode_video_args("libopenh264", None, None, "scale=640:-1") == (
        [], ["-c:v", "libopenh264", "-vf", "scale=640:-1"],
    )


def test_encode_video_args_for_vaapi_combines_scale_and_upload_filter(monkeypatch):
    monkeypatch.setattr(encoder_detect, "_vaapi_device_path", lambda: "/dev/dri/renderD128")

    pre_input, video_args = encoder_detect.encode_video_args("h264_vaapi", None, None, "scale=640:-1")

    assert pre_input == ["-vaapi_device", "/dev/dri/renderD128"]
    assert video_args == ["-c:v", "h264_vaapi", "-vf", "scale=640:-1,format=nv12,hwupload"]


def test_encode_video_args_for_vaapi_without_scale_still_uploads(monkeypatch):
    monkeypatch.setattr(encoder_detect, "_vaapi_device_path", lambda: "/dev/dri/renderD128")

    pre_input, video_args = encoder_detect.encode_video_args("h264_vaapi", None, None, "")

    assert video_args == ["-c:v", "h264_vaapi", "-vf", "format=nv12,hwupload"]


def test_encode_video_args_returns_none_when_vaapi_device_unavailable(monkeypatch):
    monkeypatch.setattr(encoder_detect, "_vaapi_device_path", lambda: None)
    assert encoder_detect.encode_video_args("h264_vaapi", None, None, "") == (None, None)


def test_try_encode_skips_vaapi_without_spawning_ffmpeg_when_no_render_node(monkeypatch):
    monkeypatch.setattr(encoder_detect, "_vaapi_device_path", lambda: None)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("should never spawn ffmpeg when there's no render node to target")

    monkeypatch.setattr(subprocess, "run", fail_if_called)

    assert encoder_detect._try_encode("h264_vaapi") is False


@requires_ffmpeg
@requires_vaapi_render_node
def test_real_vaapi_probe_gets_past_pixel_format_negotiation():
    # Regression test for the actual bug this was written to fix: without
    # -vaapi_device + a format=nv12,hwupload filter, ffmpeg fails
    # immediately with a pixel-format negotiation error ("Impossible to
    # convert between the formats supported by the filter...") before ever
    # reaching real hardware capability checks -- confirmed by manually
    # reproducing that exact failure against this same real render node
    # before this fix existed. This test can't assert the probe *succeeds*
    # (that depends on whether this machine's GPU/driver actually exposes
    # an H.264 encode profile via VAAPI, which varies by hardware -- this
    # sandbox's own GPU does not), only that it no longer fails at the
    # wiring stage.
    pre_input_args, video_args = encoder_detect.encode_video_args("h264_vaapi", None, None, "")
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            *pre_input_args,
            "-f", "lavfi", "-i", "color=c=black:size=64x64:rate=1:duration=1",
            "-frames:v", "1",
            *video_args,
            "-f", "null", "-",
        ],
        capture_output=True, text=True, timeout=10,
    )
    assert "Impossible to convert between the formats" not in result.stderr


def test_capable_is_false_when_no_encoder_detected_at_all(monkeypatch):
    monkeypatch.setattr(encoder_detect, "get_encoder", lambda: None)
    called = []
    monkeypatch.setattr(encoder_detect, "_measure_encode_seconds", lambda *a: called.append(True))

    assert encoder_detect.is_transcode_capable() is False
    assert called == []  # never even attempts a benchmark with nothing to benchmark


def test_capable_is_true_for_fast_software_fallback(monkeypatch):
    # Software is held to a lower bar than hardware (SOFTWARE_MIN_REALTIME_FACTOR
    # vs. MIN_REALTIME_FACTOR) but does get benchmarked and can auto-enable --
    # unlike the original all-or-nothing "never" behavior.
    monkeypatch.setattr(encoder_detect, "get_encoder", lambda: encoder_detect.SOFTWARE_FALLBACK)
    monkeypatch.setattr(encoder_detect, "_measure_encode_seconds", lambda *a: 1.5)

    assert encoder_detect.is_transcode_capable() is True


def test_capable_is_false_for_too_slow_software_fallback(monkeypatch):
    monkeypatch.setattr(encoder_detect, "get_encoder", lambda: encoder_detect.SOFTWARE_FALLBACK)
    # Clears the bare 1.0x line but not SOFTWARE_MIN_REALTIME_FACTOR (1.2x).
    monkeypatch.setattr(encoder_detect, "_measure_encode_seconds", lambda *a: encoder_detect._BENCHMARK_CLIP_SECONDS / 1.05)

    assert encoder_detect.is_transcode_capable() is False


def test_capable_is_true_for_fast_hardware_encoder(monkeypatch):
    monkeypatch.setattr(encoder_detect, "get_encoder", lambda: "h264_nvenc")
    # Encodes _BENCHMARK_CLIP_SECONDS of content well within one second --
    # comfortably above MIN_REALTIME_FACTOR.
    monkeypatch.setattr(encoder_detect, "_measure_encode_seconds", lambda *a: 0.5)

    assert encoder_detect.is_transcode_capable() is True


def test_capable_is_false_for_too_slow_hardware_encoder(monkeypatch):
    monkeypatch.setattr(encoder_detect, "get_encoder", lambda: "h264_vaapi")
    monkeypatch.setattr(encoder_detect, "encode_video_args", lambda *a: (["-vaapi_device", "/dev/dri/renderD128"], ["-c:v", "h264_vaapi"]))
    # Takes longer than real time to encode the clip -- well under
    # MIN_REALTIME_FACTOR.
    monkeypatch.setattr(encoder_detect, "_measure_encode_seconds", lambda *a: encoder_detect._BENCHMARK_CLIP_SECONDS * 2)

    assert encoder_detect.is_transcode_capable() is False


def test_capable_is_false_when_benchmark_fails_or_times_out(monkeypatch):
    monkeypatch.setattr(encoder_detect, "get_encoder", lambda: "h264_nvenc")
    monkeypatch.setattr(encoder_detect, "_measure_encode_seconds", lambda *a: None)

    assert encoder_detect.is_transcode_capable() is False


def test_capable_result_is_cached_benchmark_runs_once(monkeypatch):
    monkeypatch.setattr(encoder_detect, "get_encoder", lambda: "h264_nvenc")
    call_count = {"n": 0}

    def fake_measure(*args):
        call_count["n"] += 1
        return 0.1

    monkeypatch.setattr(encoder_detect, "_measure_encode_seconds", fake_measure)

    first = encoder_detect.is_transcode_capable()
    second = encoder_detect.is_transcode_capable()

    assert first is second is True
    assert call_count["n"] == 1
