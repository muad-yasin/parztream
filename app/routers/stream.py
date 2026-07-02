import mimetypes
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..db import get_connection

router = APIRouter(prefix="/api", tags=["stream"])

CHUNK_SIZE = 1024 * 1024
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


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
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    range_header = request.headers.get("range")
    if range_header is None:
        return StreamingResponse(
            _iter_file(path, 0, file_size - 1),
            media_type=content_type,
            headers={"Accept-Ranges": "bytes", "Content-Length": str(file_size)},
        )

    match = RANGE_RE.match(range_header)
    if not match or not (match.group(1) or match.group(2)):
        raise HTTPException(
            status_code=416,
            detail="Invalid range header",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    start_str, end_str = match.groups()
    if start_str == "":
        # Suffix range, e.g. "bytes=-500" meaning the last 500 bytes.
        suffix_length = int(end_str)
        start = max(file_size - suffix_length, 0)
        end = file_size - 1
    else:
        start = int(start_str)
        end = min(int(end_str), file_size - 1) if end_str else file_size - 1

    if start >= file_size or start > end:
        raise HTTPException(
            status_code=416,
            detail="Range not satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

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
