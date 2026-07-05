import json
import logging
import mimetypes
import re
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from .. import auth, cache, scanner, transcode
from ..db import get_connection

router = APIRouter(prefix="/api", tags=["stream"])

logger = logging.getLogger("parztream")

CHUNK_SIZE = 1024 * 1024
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
SEGMENT_NAME_RE = re.compile(r"^segment_(\d{5})\.ts$")


def _get_media_row(media_id: int):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Media not found")
    return row


@router.post("/cast-token/{media_id}")
def create_cast_token_route(media_id: int, request: Request):
    # Only reachable by an already-authenticated browser session (or
    # unconditionally when no PIN is configured, same as every other route
    # here) -- this is the bridge that lets that authenticated sender hand a
    # Cast/Google TV receiver, which has no cookie jar, a URL it can fetch on
    # its own. See app/auth.py's CAST_STREAM_PATH_RE for where the token is
    # actually validated.
    _get_media_row(media_id)  # 404s for an unknown id, same as every other route here

    client_id = request.client.host if request.client else "unknown"
    if not auth.check_cast_token_rate_limit(client_id):
        raise HTTPException(status_code=429, detail="Too many cast requests. Try again shortly.")

    return {"token": auth.create_cast_token(media_id)}


@router.get("/stream/{media_id}")
def stream_media(media_id: int, request: Request, original: bool = False):
    row = _get_media_row(media_id)

    original_path = Path(row["path"])
    if not original_path.is_file():
        raise HTTPException(status_code=404, detail="File missing on disk")

    # ?original=1 bypasses the browser-compatibility check entirely and
    # serves the source file's raw bytes -- the only way to get bytes at
    # all for a codec resolve_playable_path can't fix (e.g. HEVC), meant
    # for downloading to play in VLC/another device, not in-browser
    # playback. Without this, "download instead" would 415 exactly like
    # in-browser playback does, since it'd hit the same compatibility check.
    if original:
        path = original_path
    else:
        try:
            path = transcode.resolve_playable_path(row)
        except transcode.UnsupportedVideoCodec as exc:
            raise HTTPException(status_code=415, detail=exc.user_message())
        except transcode.NeedsHlsRemux:
            # Tells the frontend to switch to HLS playback instead of
            # treating this endpoint as a directly-streamable file -- the
            # actual conversion work happens lazily as the playlist/segment
            # endpoints below are requested, not here.
            return JSONResponse({"hls_playlist": f"/api/stream/{media_id}/hls/playlist.m3u8"})

    file_size = path.stat().st_size
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    base_headers = {"Accept-Ranges": "bytes"}
    if original:
        base_headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(path.name)}"

    range_header = request.headers.get("range")
    if range_header is None:
        return StreamingResponse(
            _iter_file(path, 0, file_size - 1),
            media_type=content_type,
            headers={**base_headers, "Content-Length": str(file_size)},
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
        **base_headers,
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Content-Length": str(end - start + 1),
    }
    return StreamingResponse(
        _iter_file(path, start, end),
        status_code=206,
        media_type=content_type,
        headers=headers,
    )


def _require_hls(row):
    """Returns (remux_audio, reencode_video) if this row genuinely needs
    HLS remuxing; raises a clean HTTPException otherwise. Hitting these
    routes for a file that doesn't need remuxing at all would be a frontend
    bug, not a normal case -- fail with a real error rather than silently
    doing the wrong thing."""
    try:
        transcode.resolve_playable_path(row)
    except transcode.UnsupportedVideoCodec as exc:
        raise HTTPException(status_code=415, detail=exc.user_message())
    except transcode.NeedsHlsRemux as exc:
        return exc.remux_audio, exc.reencode_video
    raise HTTPException(status_code=400, detail="This file doesn't need HLS remuxing")


def _segment_boundaries(row):
    """This row's keyframe-accurate HLS segment boundaries, backfilling
    lazily for a legacy row scanned before the segment_boundaries column
    existed (or one the scanner skipped, e.g. transcoding was off then and
    is on now): probe the keyframes now, persist the result so this only
    ever happens once per file, and invalidate any segments cached on the
    old fixed 6s grid -- they don't line up with the boundary-derived
    playlist about to be served. Returns None (callers fall back to the
    old fixed-grid behavior) when the probe finds nothing to work from;
    deliberately not persisted as a failure marker, so a later request or
    rescan retries rather than wedging the file on the degraded path
    forever. The lock collapses concurrent first requests (playlist +
    first segments arrive nearly together) into one packet walk -- same
    dedup-by-key pattern as the remux/thumbnail caches."""
    if row["segment_boundaries"]:
        return json.loads(row["segment_boundaries"])
    if row["duration"] is None:
        return None
    with cache.lock_for(f"segment_boundaries:{row['id']}"):
        # A request that waited on the lock finds the winner's result here
        # instead of re-paying for the probe.
        with get_connection() as conn:
            fresh = conn.execute(
                "SELECT segment_boundaries FROM media WHERE id = ?", (row["id"],)
            ).fetchone()
        if fresh is not None and fresh["segment_boundaries"]:
            return json.loads(fresh["segment_boundaries"])
        boundaries = transcode.compute_segment_boundaries(
            scanner.probe_keyframes(Path(row["path"])), row["duration"]
        )
        if boundaries is None:
            return None
        with get_connection() as conn:
            conn.execute(
                "UPDATE media SET segment_boundaries = ? WHERE id = ?",
                (json.dumps(boundaries), row["id"]),
            )
        transcode.invalidate_segments(row["id"])
        return boundaries


@router.get("/stream/{media_id}/hls/playlist.m3u8")
def stream_hls_playlist(media_id: int):
    row = _get_media_row(media_id)
    _require_hls(row)  # only need the raise-or-not here, not the returned flags

    if row["duration"] is None:
        raise HTTPException(
            status_code=500,
            detail="Video duration unknown; try rescanning the library.",
        )

    playlist = transcode.build_playlist(row["duration"], _segment_boundaries(row))
    return Response(content=playlist, media_type="application/vnd.apple.mpegurl")


@router.get("/stream/{media_id}/hls/{segment_name}")
def stream_hls_segment(media_id: int, segment_name: str):
    row = _get_media_row(media_id)
    remux_audio, reencode_video = _require_hls(row)

    match = SEGMENT_NAME_RE.fullmatch(segment_name)
    if not match:
        raise HTTPException(status_code=404, detail="Not found")
    index = int(match.group(1))

    original_path = Path(row["path"])
    if not original_path.is_file():
        raise HTTPException(status_code=404, detail="File missing on disk")

    try:
        segment_path = transcode.ensure_segment(
            media_id, original_path, remux_audio, index,
            reencode_video, row["video_width"], row["video_height"],
            row["audio_stream_index"], _segment_boundaries(row),
        )
    except transcode.RemuxFailed as exc:
        logger.error("Remux failed for media %s: %s", media_id, exc)
        raise HTTPException(
            status_code=500,
            detail="Couldn't prepare this video for playback (conversion failed).",
        )
    except transcode.TranscodeUnavailable:
        raise HTTPException(
            status_code=503,
            detail="Server is busy transcoding other videos right now -- try again shortly.",
        )
    except (FileNotFoundError, TimeoutError) as exc:
        logger.warning("Segment %s for media %s unavailable: %s", index, media_id, exc)
        raise HTTPException(status_code=404, detail=str(exc))

    return FileResponse(segment_path, media_type="video/mp2t")


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
