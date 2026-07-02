import pytest
from fastapi.testclient import TestClient

from app import artwork, auth, cache, config, db, main, scanner, transcode


@pytest.fixture
def media_dir(tmp_path):
    d = tmp_path / "media"
    d.mkdir()
    return d


@pytest.fixture
def make_file(media_dir):
    def _make(name, content=b"hello world"):
        path = media_dir / name
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
    monkeypatch.setattr(auth, "AUTH_PASSWORD", None)
    monkeypatch.setattr(auth, "AUTH_USERNAME", "parztream")
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
    scanner._scan_state.update(status="idle", error=None, last_scan_at=None)

    yield


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c
