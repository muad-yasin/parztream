from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from .. import auth

router = APIRouter(prefix="/api", tags=["auth"])


class LoginPayload(BaseModel):
    pin: str


@router.post("/login")
def login(payload: LoginPayload, request: Request, response: Response):
    # Referenced via the auth module (not `from ..auth import AUTH_PIN`) so
    # tests monkeypatching auth.AUTH_PIN are actually seen here -- a separate
    # `from` import would bind its own independent copy at import time. Same
    # reasoning as documented in CLAUDE.md for config.py's other consumers.
    if not auth.AUTH_PIN:
        raise HTTPException(status_code=400, detail="Authentication is not enabled")

    # request.client can be None for some ASGI transports (e.g. certain unix
    # socket setups) -- fall back to a shared bucket rather than crashing;
    # worst case everyone shares one lockout counter in that rare setup.
    client_id = request.client.host if request.client else "unknown"

    remaining = auth.seconds_until_unlocked(client_id)
    if remaining > 0:
        raise HTTPException(
            status_code=429,
            detail=f"Too many incorrect attempts. Try again in {remaining}s.",
        )

    if not auth.check_pin(payload.pin):
        auth.register_failed_attempt(client_id)
        raise HTTPException(status_code=401, detail="Incorrect PIN")

    auth.register_successful_attempt(client_id)
    response.set_cookie(
        key=auth.SESSION_COOKIE_NAME,
        value=auth.create_session_cookie_value(),
        max_age=auth.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        # No secure=True: parztream deliberately runs over plain HTTP on a
        # trusted LAN (see README) -- a Secure cookie is never sent back at
        # all over a non-HTTPS connection, which would silently break login.
    )
    return {"status": "ok"}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(auth.SESSION_COOKIE_NAME)
    return {"status": "ok"}
