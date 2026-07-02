import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..db import get_connection

router = APIRouter(prefix="/api", tags=["stream"])

CHUNK_SIZE = 1024 * 1024
RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)")


@router.get("/stream/{media_id}")
def stream_media(media_id: int, request: Request):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Media not found")

    path = Path(row["path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File missing on disk")

    file_size = path.stat().st_size
    content_type = "audio/mpeg" if row["media_type"] == "audio" else "video/mp4"

    range_header = request.headers.get("range")
    if range_header is None:
        return StreamingResponse(
            _iter_file(path, 0, file_size - 1),
            media_type=content_type,
            headers={"Accept-Ranges": "bytes", "Content-Length": str(file_size)},
        )

    match = RANGE_RE.match(range_header)
    if not match:
        raise HTTPException(status_code=416, detail="Invalid range header")

    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else file_size - 1
    end = min(end, file_size - 1)

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
    }
    return StreamingResponse(
        _iter_file(path, start, end),
        status_code=206,
        media_type=content_type,
        headers=headers,
    )


def _iter_file(path: Path, start: int, end: int):
    with open(path, "rb") as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = f.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
