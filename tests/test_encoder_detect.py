import shutil
import threading
import time

import pytest

from app import encoder_detect

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)


@pytest.fixture(autouse=True)
def reset_detection_cache():
    encoder_detect._detected_encoder = encoder_detect._UNSET
    yield
    encoder_detect._detected_encoder = encoder_detect._UNSET


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
    # This dev environment has no GPU/hardware encode path available, so
    # detection is expected to fall through every hardware candidate and
    # land on the software fallback -- this confirms the real subprocess
    # plumbing (ffmpeg -encoders parsing, the synthetic test-encode) works
    # end to end, not just the mocked logic above.
    assert encoder_detect.get_encoder() is not None
