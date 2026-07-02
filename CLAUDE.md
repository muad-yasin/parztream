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
```

There is no test suite, linter, or build step yet.

To sanity-check the backend after changes: start the server, then
`curl http://127.0.0.1:8000/api/library` and
`curl -X POST http://127.0.0.1:8000/api/scan`.

## Architecture

- `app/config.py` — all configuration is read from environment
  variables here (`PARZTREAM_MEDIA_DIRS`, `PARZTREAM_DB_PATH`).
  Nothing else in the app should read `os.environ` directly.
- `app/db.py` — raw `sqlite3` access via a `get_connection()`
  context manager (opens, commits on success, always closes). One
  table, `media`. No migrations system yet — schema changes mean
  editing `SCHEMA` in this file (existing dev DBs need to be deleted
  and rescanned).
- `app/scanner.py` — walks `MEDIA_DIRS`, classifies files as
  audio/video by extension, extracts metadata (`mutagen` for audio
  tags, `ffprobe` subprocess for video duration — both degrade
  gracefully to `None`/filename if unavailable), and upserts into
  `media` by path. Also deletes DB rows for files no longer found on
  disk. This is the only place file-metadata extraction happens.
  Scans run in the background (see below), coordinated by a
  module-level `threading.Lock` plus a `_scan_state` dict
  (`get_scan_status`/`start_scan`/`run_claimed_scan`) — `start_scan()`
  claims the lock synchronously so a second concurrent trigger fails
  fast with 409 instead of racing.
- `app/routers/library.py` — CRUD-ish read endpoints over the
  `media` table plus `POST /api/scan` (claims the scan lock, then
  hands the actual scan to FastAPI `BackgroundTasks` — returns
  immediately) and `GET /api/scan/status` for the frontend to poll.
- `app/routers/stream.py` — serves file bytes with manual HTTP
  Range header parsing/`206 Partial Content` support, including
  suffix ranges (`bytes=-500`) and a proper `416` for out-of-bounds
  requests. This is hand-rolled (not `FileResponse`) specifically so
  seeking works in `<video>`/`<audio>` players. Any change here
  should preserve Range support — without it, scrubbing/seeking
  breaks. `Content-Type` is derived via `mimetypes.guess_type()` —
  don't hardcode it, different containers need different MIME types
  for the browser to play them at all.
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
- `static/` — plain JS, no bundler. `app.js` fetches `/api/library`,
  renders a clickable list, points an `<audio>`/`<video>` element at
  `/api/stream/{id}` on click, and polls `/api/scan/status` after
  triggering a scan (the trigger endpoint returns immediately, it
  doesn't wait for the scan to finish).

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
