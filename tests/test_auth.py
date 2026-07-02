import base64

from app import auth


def _basic_header(username, password):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_no_password_configured_allows_open_access(client):
    res = client.get("/api/library")
    assert res.status_code == 200


def test_no_password_configured_allows_static_ui(client):
    res = client.get("/")
    assert res.status_code == 200


def test_password_configured_rejects_missing_credentials(client, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_PASSWORD", "secret")

    res = client.get("/api/library")

    assert res.status_code == 401
    assert res.headers["www-authenticate"] == 'Basic realm="parztream"'


def test_password_configured_rejects_wrong_credentials(client, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_PASSWORD", "secret")

    res = client.get("/api/library", headers=_basic_header("parztream", "wrong"))

    assert res.status_code == 401


def test_password_configured_accepts_correct_credentials(client, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_PASSWORD", "secret")

    res = client.get("/api/library", headers=_basic_header("parztream", "secret"))

    assert res.status_code == 200


def test_password_configured_protects_static_ui_too(client, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_PASSWORD", "secret")

    res = client.get("/")

    assert res.status_code == 401


def test_custom_username_is_respected(client, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_PASSWORD", "secret")
    monkeypatch.setattr(auth, "AUTH_USERNAME", "admin")

    res = client.get("/api/library", headers=_basic_header("admin", "secret"))

    assert res.status_code == 200
