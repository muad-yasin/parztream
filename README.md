# parztream

A lightweight, self-hosted media server. Scans one or more folders for
music and video files, stores metadata in SQLite, and serves a web
interface so any device on the local network can browse and stream
the files in a browser.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate   # .venv\Scripts\activate on Windows
pip install -r requirements.txt
```

Video duration lookups use `ffprobe` (from ffmpeg) if it's on your
`PATH`; it's optional — video files still work without it, just
without a known duration.

## Running

```bash
export PARZTREAM_MEDIA_DIRS=/path/to/music:/path/to/videos   # ; separated on Windows
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open `http://<host>:8000/` from any device on the LAN. Click
"Scan library" to (re)index the configured folders — scanning runs
in the background, so the UI stays responsive while it works.

## Configuration

Set via environment variables:

- `PARZTREAM_MEDIA_DIRS` — folders to scan, separated by `os.pathsep`
  (`:` on Linux/macOS, `;` on Windows).
- `PARZTREAM_DB_PATH` — SQLite file location (defaults to
  `parztream.db` in the project root).
- `PARZTREAM_PASSWORD` — if set, the whole app (UI, API, streaming)
  requires HTTP Basic Auth with this password. If unset, the server
  has **no authentication** — anyone who can reach the port can
  browse and stream. Recommended for anything beyond local testing.
- `PARZTREAM_USERNAME` — Basic Auth username (defaults to
  `parztream`), only relevant when `PARZTREAM_PASSWORD` is set.
