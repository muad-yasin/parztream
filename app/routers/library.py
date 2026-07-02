from typing import Optional

from fastapi import APIRouter, HTTPException

from ..db import get_connection
from ..scanner import scan_media_dirs

router = APIRouter(prefix="/api", tags=["library"])


@router.get("/library")
def list_media(media_type: Optional[str] = None):
    query = "SELECT * FROM media"
    params = ()
    if media_type:
        query += " WHERE media_type = ?"
        params = (media_type,)
    query += " ORDER BY title"
    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


@router.get("/library/{media_id}")
def get_media(media_id: int):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Media not found")
        return dict(row)


@router.post("/scan")
def trigger_scan():
    scan_media_dirs()
    return {"status": "ok"}
