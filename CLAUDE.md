# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A lightweight, self-hosted media server (home Plex alternative). It
scans configured folders for music/video files, stores metadata in
SQLite, and serves a plain HTML/JS web UI so any device on the LAN
can browse and stream files via a browser.

Stack: Python + FastAPI backend, SQLite (stdlib `sqlite3`, no ORM),
static HTML/JS/CSS frontend served directly by FastAPI (no build
step, no framework). Developed on Linux; must also run on Windows.

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate   # .venv\Scripts\activate on Windows
pip install -r requirements.txt

# Run the dev server (reload on change)
export PARZTREAM_MEDIA_DIRS=/path/to/media   # : separated on Linux, ; on Windows
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Tests
pip install -r requirements-dev.txt
pytest                       # whole suite
pytest tests/test_stream.py  # one file
pytest -k out_of_bounds      # by name
```

There is no linter or build step yet.

Tests live in `tests/`, run against tmp-path DB/media dirs via an
autouse fixture in `tests/conftest.py` (see below), never your real
config. A couple of `test_scanner.py` cases need real audio and are
skipped automatically when `ffmpeg` isn't on `PATH`.

## Architecture

- `app/config.py` — all configuration is read from environment
  variables here (`PARZTREAM_MEDIA_DIRS`, `PARZTREAM_DB_PATH`,
  `PARZTREAM_CACHE_DIR`/`PARZTREAM_CACHE_MAX_BYTES` for
  `app/transcode.py`'s remux cache and its optional size cap).
  Nothing else in the app should read `os.environ` directly. Also
  owns `AUDIO_EXTENSIONS`/`VIDEO_EXTENSIONS` — if you add a new
  extension here, check whether `mimetypes.guess_type()` actually
  knows it (many don't, e.g. `.m4b`); if not, register an override
  with `mimetypes.add_type()` right here too, otherwise streaming
  falls back to `application/octet-stream` and browsers won't play
  it even though the file scans in fine.
- `app/db.py` — raw `sqlite3` access via a `get_connection()`
  context manager (opens, commits on success, always closes). One
  table, `media`. No migrations system yet — schema changes mean
  editing `SCHEMA` in this file (existing dev DBs need to be deleted
  and rescanned).
- `app/scanner.py` — walks `MEDIA_DIRS`, classifies files as
  audio/video by extension, extracts metadata (`mutagen` for audio
  tags; a single `ffprobe -show_entries format=duration:stream=...`
  JSON call for video duration *and* the first video/audio stream's
  codec name, stored as `video_codec`/`audio_codec` — both degrade
  gracefully to `None`/filename if ffprobe is unavailable), and
  upserts into `media` by path. Also deletes DB rows for files no
  longer found on disk. This is the only place file-metadata
  extraction happens.
  Scans run in the background (see below), coordinated by a
  module-level `threading.Lock` plus a `_scan_state` dict
  (`get_scan_status`/`start_scan`/`run_claimed_scan`) — `start_scan()`
  claims the lock synchronously so a second concurrent trigger fails
  fast with 409 instead of racing. This state is in-process memory,
  so the app must always run as a single process/worker (see
  `deploy/`) — multiple workers would each have their own lock and
  could scan concurrently without ever seeing the 409.
- `app/routers/library.py` — CRUD-ish read endpoints over the
  `media` table plus `POST /api/scan` (claims the scan lock, then
  hands the actual scan to FastAPI `BackgroundTasks` — returns
  immediately) and `GET /api/scan/status` for the frontend to poll.
  `GET /api/library` is paginated — it returns
  `{items, total, limit, offset}`, not a bare array; `limit` is
  clamped to `[1, MAX_PAGE_SIZE]`. `GET /api/library/{id}/art` serves
  embedded cover art (extracted on-demand via `app/artwork.py`, not
  cached anywhere), 404s if the file has none.
- `app/artwork.py` — pulls embedded cover art out of audio files
  (ID3 `APIC` for mp3, `covr` for MP4-family containers, FLAC
  `pictures`) via `mutagen`, re-reading the file fresh on every
  request rather than caching — simplest option, and no place yet
  where that's shown to be a bottleneck. Video thumbnails
  (would need decoding a frame via `ffmpeg`) aren't implemented;
  `get_cover_art` returns `None` immediately for `media_type ==
  "video"`.
- `app/routers/stream.py` — calls `transcode.resolve_playable_path(row)`
  to get the path to actually serve (original or cached remux; a
  `UnsupportedVideoCodec` becomes a `415`), then serves file bytes
  with manual HTTP Range header parsing/`206 Partial Content` support,
  including suffix ranges (`bytes=-500`) and a proper `416` for
  out-of-bounds requests. This is hand-rolled (not `FileResponse`)
  specifically so seeking works in `<video>`/`<audio>` players — that
  includes seeking within a remuxed file, since it's a real cached
  file on disk, not a live stream. Any change here should preserve
  Range support. `Content-Type` is derived via `mimetypes.guess_type()`
  on whichever path actually gets served — don't hardcode it,
  different containers need different MIME types for the browser to
  play them at all.
- `app/transcode.py` — `resolve_playable_path(row)` decides whether a
  video's original file can be played directly (mp4/webm container +
  h264/vp8/vp9/av1 video + aac/mp3/opus/vorbis audio, or no codec info
  yet — see below), or needs a one-time `ffmpeg -c:v copy` remux
  (only re-encoding audio, via `-c:a aac`, if the audio codec itself
  is the problem — e.g. AC3/DTS) cached to `CACHE_DIR/{id}.mp4`. This
  is deliberately *not* full transcoding: video is always copied, never
  re-encoded, so a genuinely incompatible video codec (e.g. HEVC)
  raises `UnsupportedVideoCodec` instead of silently failing or trying
  to fake support. Audio files always direct-play (never routed
  through this). If `video_codec` is `None` (ffprobe unavailable, or
  the row predates this feature and hasn't been rescanned), it falls
  back to direct play rather than guessing wrong. The remux runs
  **synchronously in the request** on a cache miss — no background
  job/polling like scanning has — since it's normally fast (stream
  copy, not re-encode); an audio-only transcode of a long file is the
  one case that can take noticeably longer. The frontend's `playMedia`
  absorbs this by probing with a tiny ranged request before handing
  the URL to `<video>`/`<audio>`, so the cache is already warm by the
  time real playback starts. Cache eviction (`_prune_cache`) runs
  right after a new file is written, if `CACHE_MAX_BYTES` is set:
  oldest-by-mtime files are deleted until back under budget. It
  always excludes the file that was *just* created from eviction —
  without that, a single cache miss on a small `CACHE_MAX_BYTES`
  could delete the very file the current request is about to serve.
  An evicted file isn't a loss, just a cache miss on next play (cheap
  to re-derive, unlike the original scan metadata).
- `app/auth.py` — `BasicAuthMiddleware`, a pure ASGI middleware (not
  `BaseHTTPMiddleware`, which buffers `StreamingResponse` bodies —
  that would hurt streaming large files). Gates the entire app,
  including the static UI and streaming, uniformly. No-ops entirely
  if `PARZTREAM_PASSWORD` isn't set.
- `app/main.py` — wires routers and mounts `static/` at `/`. Route
  registration order matters: API routers are included *before* the
  `StaticFiles` mount, since the static mount is a catch-all at `/`.
  `BasicAuthMiddleware` is added at app level so it covers everything
  behind it.
- `static/` — plain JS, no bundler. `app.js` fetches `/api/library`
  (with `limit`/`offset`, tracked in a module-level `offset`
  variable, reset to 0 on filter change or after a scan), renders a
  clickable list with a lazy-loaded `<img src="/api/library/{id}/art">`
  per row (hidden via `onerror` if 404). `playMedia` probes
  `/api/stream/{id}` with a tiny `Range: bytes=0-1` request first —
  this both warms the transcode cache before real playback starts and
  lets a `415` (unsupported video codec) show a "download instead"
  message rather than a silent `<video>` failure — before pointing an
  `<audio>`/`<video>` element at the same URL. Also polls
  `/api/scan/status` after triggering a scan (the trigger endpoint
  returns immediately, it doesn't wait for the scan to finish).
- `deploy/` — templates for running as a persistent background
  service (systemd unit + env-file template for Linux, a batch
  script + env-file template for Windows), documented in the
  README's "Running as a service" section. Not installed/enabled
  anywhere by default — these are files to copy onto a target
  machine, not something the app or its tests touch. Real env files
  with actual passwords belong outside the repo (`/etc/parztream/`
  or `C:\ProgramData\parztream\`), never committed — only the
  `.example` templates live in `deploy/`.

Test isolation relies on a quirk worth knowing: `config.py` reads env
vars into module-level constants at import time, and `db.py`/
`scanner.py`/`auth.py` import those by name (`from .config import
DB_PATH`, etc.), so patching env vars after startup does nothing.
`tests/conftest.py` instead monkeypatches the *consuming* module's
attribute directly (e.g. `monkeypatch.setattr(db, "DB_PATH", ...)`,
`monkeypatch.setattr(scanner, "MEDIA_DIRS", ...)`,
`monkeypatch.setattr(transcode, "CACHE_DIR", ...)`) — that works
because each function looks up the name in its own module's globals
at call time. If you add a new config value, follow the same pattern
rather than trying to override the environment mid-test.

## Conventions

- Cross-platform paths: use `pathlib.Path` and `os.pathsep`
  everywhere (not hardcoded `:`/`;`) since Windows support is a
  target, not just Linux.
- Keep metadata extraction failures non-fatal — a single unreadable
  or corrupt media file should not abort the whole scan, and one bad
  tag field (e.g. malformed title) shouldn't discard sibling fields
  (e.g. duration) — see the per-field try/except in
  `_extract_metadata`/`_first_tag`.
- Auth is intentionally minimal (single shared password, no
  per-user accounts) — this is a home-LAN tool, not multi-tenant.
