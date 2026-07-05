import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from app import artwork, auth, cache, config, db, main, scanner, transcode

_H264_ENCODER = "unprobed"


def _available_h264_encoder():
    """The H.264 encoder this machine's ffmpeg actually ships, or None.

    Test-media synthesis can't hardcode one: GPL ffmpeg builds (distro
    packages, CI) bundle libx264, while the LGPL builds parztream vendors
    bundle libopenh264 -- each is missing from the other family, and both
    families are realistic dev environments here. Probed once per session.
    """
    global _H264_ENCODER
    if _H264_ENCODER == "unprobed":
        if shutil.which("ffmpeg") is None:
            _H264_ENCODER = None
        else:
            listed = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"],
                capture_output=True, text=True,
            ).stdout
            _H264_ENCODER = next(
                (e for e in ("libx264", "libopenh264") if e in listed), None
            )
    return _H264_ENCODER


@pytest.fixture
def h264_encoder():
    encoder = _available_h264_encoder()
    if encoder is None:
        pytest.skip("this ffmpeg build has no H.264 encoder (libx264/libopenh264)")
    return encoder


@pytest.fixture
def media_dir(tmp_path):
    d = tmp_path / "media"
    d.mkdir()
    return d


@pytest.fixture
def make_file(media_dir):
    def _make(name, content=b"hello world"):
        path = media_dir / name
        # `name` may contain subdirectories (e.g. "Show/Season 1/ep.mkv") --
        # a no-op for existing flat-file callers, since media_dir itself
        # already exists.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    return _make


@pytest.fixture(autouse=True)
def isolated_app_state(tmp_path, media_dir, monkeypatch):
    """Point config at per-test tmp paths and reset the scanner's global
    lock/status so tests can't see state left over by a previous test."""
    # settings.get_media_dirs() falls back to config.MEDIA_DIRS when no
    # settings row exists yet (true for every test, since each gets a fresh
    # tmp DB) -- patching the fallback here, not settings.get_media_dirs
    # itself, keeps the real settings.py logic exercised by the whole suite.
    monkeypatch.setattr(config, "MEDIA_DIRS", [media_dir])
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(auth, "AUTH_PIN", None)
    # Rate-limit state is in-process/module-level (see app/auth.py), so
    # without clearing it a lockout triggered by one test's failed-login
    # attempts would leak into the next test using the same TestClient IP.
    auth._login_attempts.clear()
    auth._cast_token_requests.clear()
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(artwork, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(cache, "CACHE_MAX_BYTES", None)
    # Real mDNS registration on every test using the `client` fixture would
    # be slow and, in a sandboxed/CI-like environment, potentially flaky --
    # app/mdns.py is tested directly (mocked) in tests/test_mdns.py instead.
    monkeypatch.setattr(config, "MDNS_ENABLED", False)
    db.init_db()

    if scanner._scan_lock.locked():
        scanner._scan_lock.release()
    scanner._scan_state.update(
        status="idle", error=None, last_scan_at=None,
        scanned_count=0, failed_count=0, failed_examples=[],
        incomplete_count=0, incomplete_examples=[],
    )

    yield


@pytest.fixture
def client():
    with TestClient(main.app, base_url="http://localhost") as c:
        yield c
