import logging

from fastapi.testclient import TestClient

from app import config, main
from app.routers import library


def test_secret_key_warning_logged_when_unset(monkeypatch, caplog):
    monkeypatch.setattr(config, "SECRET_KEY_IS_EPHEMERAL", True)
    monkeypatch.setattr(main, "SECRET_KEY_IS_EPHEMERAL", True)

    with caplog.at_level(logging.WARNING, logger="parztream"):
        with TestClient(main.app, base_url="http://localhost"):
            pass

    assert any("PARZTREAM_SECRET_KEY" in record.message for record in caplog.records)


def test_no_secret_key_warning_when_key_is_set(monkeypatch, caplog):
    monkeypatch.setattr(config, "SECRET_KEY_IS_EPHEMERAL", False)
    monkeypatch.setattr(main, "SECRET_KEY_IS_EPHEMERAL", False)

    with caplog.at_level(logging.WARNING, logger="parztream"):
        with TestClient(main.app, base_url="http://localhost"):
            pass

    assert not any("PARZTREAM_SECRET_KEY" in record.message for record in caplog.records)


def test_unhandled_exception_returns_generic_500_not_a_traceback(monkeypatch):
    def _boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(library, "get_connection", _boom)

    # The default TestClient re-raises the server-side exception into the
    # test itself (handy for catching real bugs) rather than exercising
    # main.py's exception handler the way a real deployed client would see
    # it -- raise_server_exceptions=False is what actually exercises that
    # handler here.
    with TestClient(main.app, base_url="http://localhost", raise_server_exceptions=False) as client:
        res = client.get("/api/shows")

    assert res.status_code == 500
    assert res.json() == {"detail": "Internal server error"}
    assert "kaboom" not in res.text
