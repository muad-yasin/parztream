import secrets
from http.cookies import SimpleCookie
from urllib.parse import quote

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.datastructures import Headers
from starlette.responses import JSONResponse, RedirectResponse

from .config import AUTH_PASSWORD, AUTH_USERNAME, SECRET_KEY, SESSION_MAX_AGE

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


def check_credentials(username: str, password: str) -> bool:
    return secrets.compare_digest(username, AUTH_USERNAME) and secrets.compare_digest(
        password, AUTH_PASSWORD or ""
    )


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
        if scope["type"] != "http" or not AUTH_PASSWORD:
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
