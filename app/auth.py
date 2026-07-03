import secrets
import time
from http.cookies import SimpleCookie
from urllib.parse import quote

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.datastructures import Headers
from starlette.responses import JSONResponse, RedirectResponse

from .config import AUTH_PIN, SECRET_KEY, SESSION_MAX_AGE

SESSION_COOKIE_NAME = "parztream_session"
_SESSION_VALUE = "authenticated"

# Paths reachable with no session at all, since they're what makes logging
# in possible in the first place. Deliberately minimal: login.html is fully
# self-contained (inline CSS/JS) specifically so nothing else -- style.css,
# app.js, /api/setup/* -- needs to be added here. The icon/manifest files
# are the one deliberate exception: login.html links to them (favicon,
# apple-touch-icon, manifest.json) so the tab icon and "Add to Home Screen"
# work correctly even before logging in, and there's nothing sensitive in a
# handful of static branding images to justify gating them.
PUBLIC_PATHS = {
    "/login.html",
    "/api/login",
    "/manifest.json",
    "/icon-192.png",
    "/icon-512.png",
    "/favicon-32.png",
}

_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="parztream-session")

# A 4-digit PIN only has 10,000 possibilities, so unlike a real password it
# needs throttling to not be trivially brute-forceable over a fast LAN
# connection. In-process only, like app/scanner.py's scan lock -- resets on
# restart and only meaningful because the app always runs as a single
# process (see CLAUDE.md). Keyed by client IP, not the (nonexistent) session,
# since a lockout has to apply *before* anyone's proven who they are.
_MAX_ATTEMPTS = 5
_LOCKOUT_SECONDS = 30
# Doubled on every lockout for a given client (30s, 60s, 120s, ...), capped
# here -- a flat 30s lockout still lets a steady attacker cover the entire
# 10,000-PIN keyspace in well under a day from one IP, since nothing
# actually gets harder for them over time. Reset to the base duration on
# any successful login (see register_successful_attempt), since a real
# user who just mistyped a few times shouldn't inherit an escalated
# lockout on some future actual mistake.
_MAX_LOCKOUT_SECONDS = 60 * 60
_login_attempts: dict[str, dict] = {}


def check_pin(pin: str) -> bool:
    # secrets.compare_digest raises TypeError on non-ASCII str input --
    # comparing UTF-8 bytes instead avoids that entirely (and is still a
    # timing-safe comparison), so a PIN submission containing non-ASCII
    # characters gets a normal 401 rather than a 500.
    return secrets.compare_digest(pin.encode("utf-8"), (AUTH_PIN or "").encode("utf-8"))


def register_failed_attempt(client_id: str) -> None:
    record = _login_attempts.setdefault(client_id, {"count": 0, "locked_until": 0.0, "lockouts": 0})
    record["count"] += 1
    if record["count"] >= _MAX_ATTEMPTS:
        record["lockouts"] += 1
        duration = min(_LOCKOUT_SECONDS * (2 ** (record["lockouts"] - 1)), _MAX_LOCKOUT_SECONDS)
        record["locked_until"] = time.monotonic() + duration
        record["count"] = 0


def register_successful_attempt(client_id: str) -> None:
    _login_attempts.pop(client_id, None)


def seconds_until_unlocked(client_id: str) -> int:
    record = _login_attempts.get(client_id)
    if not record:
        return 0
    remaining = record["locked_until"] - time.monotonic()
    return max(0, int(remaining) + 1 if remaining > 0 else 0)


def create_session_cookie_value() -> str:
    return _serializer.dumps(_SESSION_VALUE)


def verify_session_cookie_value(value: str) -> bool:
    try:
        return _serializer.loads(value, max_age=SESSION_MAX_AGE) == _SESSION_VALUE
    except (BadSignature, SignatureExpired):
        return False


class SessionAuthMiddleware:
    """Pure ASGI middleware (not BaseHTTPMiddleware) so it doesn't buffer
    StreamingResponse bodies — that matters here since /api/stream serves
    large files. Replaces the old HTTP Basic Auth (which gave every visitor
    the browser's native, unbranded credential popup) with a signed session
    cookie set by a real login page."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not AUTH_PIN:
            await self.app(scope, receive, send)
            return

        if scope["path"] in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        cookie_value = _get_cookie(headers, SESSION_COOKIE_NAME)
        if cookie_value and verify_session_cookie_value(cookie_value):
            await self.app(scope, receive, send)
            return

        if "text/html" in headers.get("accept", ""):
            # A real page navigation (not a fetch()/<img>/<video> request,
            # which don't send this) -- send it to the login page, and back
            # to wherever it came from afterward.
            query_string = scope.get("query_string", b"").decode()
            requested = scope["path"] + (f"?{query_string}" if query_string else "")
            response = RedirectResponse(url=f"/login.html?next={quote(requested)}", status_code=302)
        else:
            response = JSONResponse({"detail": "Not authenticated"}, status_code=401)
        await response(scope, receive, send)


def _get_cookie(headers: Headers, name: str):
    cookie_header = headers.get("cookie")
    if not cookie_header:
        return None
    jar = SimpleCookie()
    jar.load(cookie_header)
    morsel = jar.get(name)
    return morsel.value if morsel else None
