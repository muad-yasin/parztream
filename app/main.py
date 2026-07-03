import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import mdns, transcode
from .auth import SessionAuthMiddleware
from .config import AUTH_PIN
from .db import init_db
from .routers import library, login, setup, stream

logger = logging.getLogger("parztream")
# Without an actual handler attached, Python's logging module only ever
# shows WARNING+ (via its built-in "handler of last resort" fallback) no
# matter what level is set on the logger itself -- confirmed live: our
# warning() calls appeared in the console, but info() calls (like mDNS's
# successful-registration message) silently didn't, making it look like
# mDNS had failed even when it hadn't. propagate=False keeps this isolated
# to our own logger rather than also going through the root logger (and
# potentially printing twice if something else ever configures that).
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler())
logger.propagate = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if not AUTH_PIN:
        logger.warning(
            "PARZTREAM_PIN is not set — the server is reachable with no authentication."
        )
    elif not (len(AUTH_PIN) == 4 and AUTH_PIN.isdigit()):
        logger.warning(
            "PARZTREAM_PIN is set but isn't a 4-digit PIN — login will still work, "
            "but the login page expects exactly 4 digits."
        )
    mdns.start_mdns()
    yield
    mdns.stop_mdns()
    # Without this, an ffmpeg process generating HLS segments (see
    # app/transcode.py) at the moment the server stops/restarts would be
    # left running in the background indefinitely instead of being cleaned
    # up with the server that spawned it.
    transcode.terminate_all_jobs()


app = FastAPI(title="parztream", lifespan=lifespan)
app.add_middleware(SessionAuthMiddleware)

app.include_router(library.router)
app.include_router(stream.router)
app.include_router(setup.router)
app.include_router(login.router)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
