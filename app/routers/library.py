from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import Response

from ..artwork import get_cover_art
from ..db import get_connection
from ..scanner import get_scan_status, run_claimed_scan, start_scan

router = APIRouter(prefix="/api", tags=["library"])

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500


@router.get("/library")
def list_media(media_type: Optional[str] = None, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0):
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    offset = max(0, offset)

    where = ""
    params = {}
    if media_type:
        where = " WHERE media_type = :media_type"
        params["media_type"] = media_type

    with get_connection() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM media{where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM media{where} ORDER BY title LIMIT :limit OFFSET :offset",
            {**params, "limit": limit, "offset": offset},
        ).fetchall()

    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/library/{media_id}")
def get_media(media_id: int):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Media not found")
        return dict(row)


@router.get("/library/{media_id}/art")
def get_art(media_id: int):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Media not found")

    path = Path(row["path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File missing on disk")

    art = get_cover_art(path, row["media_type"])
    if art is None:
        raise HTTPException(status_code=404, detail="No embedded artwork")

    data, mime = art
    return Response(content=data, media_type=mime)


@router.post("/scan")
def trigger_scan(background_tasks: BackgroundTasks):
    if not start_scan():
        raise HTTPException(status_code=409, detail="Scan already in progress")
    background_tasks.add_task(run_claimed_scan)
    return {"status": "started"}


@router.get("/scan/status")
def scan_status():
    return get_scan_status()
