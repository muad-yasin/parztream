import pytest
from fastapi.testclient import TestClient

from app import auth, db, main, scanner, transcode


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
    monkeypatch.setattr(scanner, "MEDIA_DIRS", [media_dir])
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(auth, "AUTH_PASSWORD", None)
    monkeypatch.setattr(auth, "AUTH_USERNAME", "parztream")
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(transcode, "CACHE_MAX_BYTES", None)
    db.init_db()

    if scanner._scan_lock.locked():
        scanner._scan_lock.release()
    scanner._scan_state.update(status="idle", error=None, last_scan_at=None)

    yield


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c
