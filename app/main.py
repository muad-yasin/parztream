import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .auth import BasicAuthMiddleware
from .config import AUTH_PASSWORD
from .db import init_db
from .routers import library, setup, stream

logger = logging.getLogger("parztream")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if not AUTH_PASSWORD:
        logger.warning(
            "PARZTREAM_PASSWORD is not set — the server is reachable with no authentication."
        )
    yield


app = FastAPI(title="parztream", lifespan=lifespan)
app.add_middleware(BasicAuthMiddleware)

app.include_router(library.router)
app.include_router(stream.router)
app.include_router(setup.router)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
