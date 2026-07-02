import os
import string
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from .. import settings
from ..scanner import run_claimed_scan, start_scan

router = APIRouter(prefix="/api/setup", tags=["setup"])


@router.get("/status")
def setup_status():
    return {"configured": settings.is_configured()}


@router.get("/browse")
def browse(path: str = ""):
    target = Path(path) if path else _default_start_path()

    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Not a directory")

    try:
        directories = sorted(
            (entry.name for entry in target.iterdir() if entry.is_dir() and not entry.name.startswith(".")),
            key=str.lower,
        )
    except PermissionError:
        directories = []

    parent = target.parent
    return {
        "path": str(target),
        "parent": str(parent) if parent != target else None,
        "directories": directories,
    }


class SetupPayload(BaseModel):
    media_dirs: list[str]


@router.post("")
def save_setup(payload: SetupPayload, background_tasks: BackgroundTasks):
    dirs = [Path(p) for p in payload.media_dirs if p.strip()]
    if not dirs:
        raise HTTPException(status_code=400, detail="At least one folder is required")
    for d in dirs:
        if not d.is_dir():
            raise HTTPException(status_code=400, detail=f"Not a directory: {d}")

    settings.set_media_dirs(dirs)
    if start_scan():
        background_tasks.add_task(run_claimed_scan)
    return {"status": "ok"}


def _default_start_path() -> Path:
    if os.name == "nt":
        drives = [Path(f"{letter}:\\") for letter in string.ascii_uppercase if Path(f"{letter}:\\").exists()]
        return drives[0] if drives else Path("C:\\")
    return Path.home()
