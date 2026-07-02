import base64
import binascii
import secrets

from starlette.datastructures import Headers
from starlette.responses import Response

from .config import AUTH_PASSWORD, AUTH_USERNAME


class BasicAuthMiddleware:
    """Pure ASGI middleware (not BaseHTTPMiddleware) so it doesn't buffer
    StreamingResponse bodies — that matters here since /api/stream serves
    large files."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not AUTH_PASSWORD:
            await self.app(scope, receive, send)
            return

        header = Headers(scope=scope).get("authorization")
        if header and _check_credentials(header):
            await self.app(scope, receive, send)
            return

        response = Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="parztream"'},
        )
        await response(scope, receive, send)


def _check_credentials(header: str) -> bool:
    try:
        scheme, _, encoded = header.partition(" ")
        if scheme.lower() != "basic":
            return False
        decoded = base64.b64decode(encoded).decode("utf-8")
        username, _, password = decoded.partition(":")
    except (ValueError, binascii.Error):
        return False
    return secrets.compare_digest(username, AUTH_USERNAME) and secrets.compare_digest(
        password, AUTH_PASSWORD
    )
