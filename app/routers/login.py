from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from .. import auth

router = APIRouter(prefix="/api", tags=["auth"])


class LoginPayload(BaseModel):
    password: str


@router.post("/login")
def login(payload: LoginPayload, response: Response):
    # Referenced via the auth module (not `from ..auth import AUTH_PASSWORD`)
    # so tests monkeypatching auth.AUTH_PASSWORD/AUTH_USERNAME are actually
    # seen here -- a separate `from` import would bind its own independent
    # copy at import time. Same reasoning as documented in CLAUDE.md for
    # config.py's other consumers.
    if not auth.AUTH_PASSWORD:
        raise HTTPException(status_code=400, detail="Authentication is not enabled")

    if not auth.check_credentials(auth.AUTH_USERNAME, payload.password):
        raise HTTPException(status_code=401, detail="Incorrect password")

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
