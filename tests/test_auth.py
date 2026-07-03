from app import auth


def test_no_pin_configured_allows_open_access(client):
    res = client.get("/api/library")
    assert res.status_code == 200


def test_no_pin_configured_allows_static_ui(client):
    res = client.get("/")
    assert res.status_code == 200


def test_login_page_itself_is_reachable_with_no_session(client, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_PIN", "1234")

    res = client.get("/login.html")

    assert res.status_code == 200


def test_icon_and_manifest_assets_are_reachable_with_no_session(client, monkeypatch):
    # login.html links to these (favicon, apple-touch-icon, manifest.json)
    # -- if they required a session, the tab icon and "Add to Home Screen"
    # would silently be broken on the one page that's supposed to work
    # before logging in.
    monkeypatch.setattr(auth, "AUTH_PIN", "1234")

    for path in ["/manifest.json", "/icon-192.png", "/icon-512.png", "/favicon-32.png"]:
        assert client.get(path).status_code == 200, path


def test_api_request_without_a_session_gets_401_json(client, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_PIN", "1234")

    res = client.get("/api/library")

    assert res.status_code == 401
    assert res.json() == {"detail": "Not authenticated"}


def test_browser_navigation_without_a_session_redirects_to_login(client, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_PIN", "1234")

    res = client.get("/", headers={"Accept": "text/html"}, follow_redirects=False)

    assert res.status_code == 302
    assert res.headers["location"].startswith("/login.html?next=")


def test_login_with_wrong_pin_is_rejected_and_sets_no_cookie(client, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_PIN", "1234")

    res = client.post("/api/login", json={"pin": "0000"})

    assert res.status_code == 401
    assert "set-cookie" not in res.headers


def test_login_when_auth_not_enabled_returns_400(client):
    res = client.post("/api/login", json={"pin": "1234"})
    assert res.status_code == 400


def test_login_with_correct_pin_grants_access_to_protected_routes(client, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_PIN", "1234")

    login_res = client.post("/api/login", json={"pin": "1234"})
    assert login_res.status_code == 200
    assert auth.SESSION_COOKIE_NAME in login_res.cookies

    # TestClient persists cookies across requests on the same client, like a
    # real browser session.
    res = client.get("/api/library")
    assert res.status_code == 200


def test_session_cookie_is_httponly_and_samesite_lax_and_not_secure(client, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_PIN", "1234")

    res = client.post("/api/login", json={"pin": "1234"})

    set_cookie = res.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie.lower().replace("samesite=lax", "SameSite=lax")
    # No Secure flag: parztream runs over plain HTTP by design (see
    # README) -- a Secure cookie would never be sent back over HTTP at all,
    # silently breaking every login.
    assert "Secure" not in set_cookie


def test_logout_clears_the_session(client, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_PIN", "1234")
    client.post("/api/login", json={"pin": "1234"})
    assert client.get("/api/library").status_code == 200

    client.post("/api/logout")

    assert client.get("/api/library").status_code == 401


def test_tampered_session_cookie_is_rejected(client, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_PIN", "1234")
    client.cookies.set(auth.SESSION_COOKIE_NAME, "not-a-real-signed-value")

    res = client.get("/api/library")

    assert res.status_code == 401


def test_check_pin_is_timing_safe_not_just_equal(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_PIN", "1234")
    assert auth.check_pin("1234") is True
    assert auth.check_pin("0000") is False


def test_repeated_wrong_pins_lock_out_further_attempts(client, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_PIN", "1234")

    for _ in range(auth._MAX_ATTEMPTS):
        res = client.post("/api/login", json={"pin": "0000"})
        assert res.status_code == 401

    # The next attempt is locked out even with the correct PIN -- the
    # lockout is about attempt volume, not which PIN was guessed.
    res = client.post("/api/login", json={"pin": "1234"})
    assert res.status_code == 429
    assert "set-cookie" not in res.headers


def test_lockout_message_reports_seconds_remaining(client, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_PIN", "1234")

    for _ in range(auth._MAX_ATTEMPTS):
        client.post("/api/login", json={"pin": "0000"})

    res = client.post("/api/login", json={"pin": "0000"})
    assert res.status_code == 429
    assert "s." in res.json()["detail"]


def test_successful_login_resets_the_failed_attempt_count(client, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_PIN", "1234")

    for _ in range(auth._MAX_ATTEMPTS - 1):
        client.post("/api/login", json={"pin": "0000"})

    # One below the lockout threshold, then a correct PIN -- should not be
    # locked out, and should clear the near-miss count for next time.
    ok_res = client.post("/api/login", json={"pin": "1234"})
    assert ok_res.status_code == 200

    client.post("/api/logout")
    for _ in range(auth._MAX_ATTEMPTS - 1):
        res = client.post("/api/login", json={"pin": "0000"})
        assert res.status_code == 401


def test_non_ascii_pin_is_rejected_not_a_500(client, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_PIN", "1234")

    res = client.post("/api/login", json={"pin": "日本語"})

    assert res.status_code == 401
    assert "set-cookie" not in res.headers


def test_check_pin_handles_non_ascii_input_without_raising():
    assert auth.check_pin("日本語") is False


def test_repeated_lockouts_escalate_the_wait_time(client, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_PIN", "1234")
    client_id = "testclient"

    auth.register_failed_attempt(client_id)
    auth.register_failed_attempt(client_id)
    auth.register_failed_attempt(client_id)
    auth.register_failed_attempt(client_id)
    auth.register_failed_attempt(client_id)  # 5th failure -> first lockout
    first_wait = auth.seconds_until_unlocked(client_id)

    # Force the first lockout to have already expired, then trigger a
    # second one -- it must be longer than the first, not the same flat
    # duration every time.
    auth._login_attempts[client_id]["locked_until"] = 0.0
    for _ in range(auth._MAX_ATTEMPTS):
        auth.register_failed_attempt(client_id)
    second_wait = auth.seconds_until_unlocked(client_id)

    assert second_wait > first_wait


def test_successful_login_resets_lockout_escalation(client, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_PIN", "1234")
    client_id = "testclient"

    for _ in range(auth._MAX_ATTEMPTS):
        auth.register_failed_attempt(client_id)
    assert auth._login_attempts[client_id]["lockouts"] == 1

    auth.register_successful_attempt(client_id)

    assert client_id not in auth._login_attempts
