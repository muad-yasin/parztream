import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import mdns
from .auth import SessionAuthMiddleware
from .config import AUTH_PASSWORD
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
    if not AUTH_PASSWORD:
        logger.warning(
            "PARZTREAM_PASSWORD is not set — the server is reachable with no authentication."
        )
    mdns.start_mdns()
    yield
    mdns.stop_mdns()


app = FastAPI(title="parztream", lifespan=lifespan)
app.add_middleware(SessionAuthMiddleware)

app.include_router(library.router)
app.include_router(stream.router)
app.include_router(setup.router)
app.include_router(login.router)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
