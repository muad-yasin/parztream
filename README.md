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
in the background, so the UI stays responsive while it works. The
library list shows embedded cover art where available (mp3/FLAC/
m4a/m4b) and is paginated 50 items at a time.

### Playback compatibility ("Direct Stream")

Most files play directly with no processing. If a video's *container*
or *audio track* would stop a browser from playing it (the most
common real case: an MKV with ordinary H.264 video but AC3/DTS
surround audio, which browsers can't decode), parztream transparently
repackages it into an MP4 — copying the video as-is and only
re-encoding the audio if needed — and caches the result so it only
happens once per file. This needs `ffmpeg` on `PATH`.

What this *doesn't* do: re-encode video. If the video codec itself
isn't one a browser supports (e.g. HEVC), playback returns a clear
"can't play in browser" message with a link to download the original
file instead, rather than a full transcode — the video quality/
resolution never changes, and a genuinely incompatible video codec
stays incompatible.

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
- `PARZTREAM_CACHE_DIR` — where repackaged videos are cached (see
  "Playback compatibility" above; defaults to `cache/` in the project
  root). Grows roughly proportional to how much of your library needs
  fixing up, since video is copied rather than re-encoded — nothing
  prunes it automatically, so keep an eye on disk usage if space is
  tight.

## Running as a service

The commands above run parztream in the foreground; it stops when you
close the terminal and won't restart on crash or reboot. Templates
for running it as a persistent background service are in `deploy/`.

### Linux (systemd)

1. Put the project at a stable location (e.g. `/opt/parztream`),
   including its `.venv` (see Setup above), and create a system user
   to run it as:
   ```bash
   sudo useradd --system --home-dir /opt/parztream --shell /usr/sbin/nologin parztream
   sudo chown -R parztream:parztream /opt/parztream
   ```
2. Copy the env template and fill in real values — keep it outside
   the project checkout since it holds a password:
   ```bash
   sudo mkdir -p /etc/parztream
   sudo cp deploy/systemd/parztream.env.example /etc/parztream/parztream.env
   sudo chmod 600 /etc/parztream/parztream.env
   sudo $EDITOR /etc/parztream/parztream.env
   ```
3. Install and start the unit:
   ```bash
   sudo cp deploy/systemd/parztream.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now parztream
   ```
4. Check status/logs: `systemctl status parztream`, `journalctl -u parztream -f`.

Edit `User`/`WorkingDirectory`/`ExecStart` in the unit file first if
your paths or username differ from the example. Don't add
`--workers` to `ExecStart` — scan status/locking lives in one
process's memory (see `app/scanner.py`), so multiple worker
processes would silently break the concurrent-scan-rejects-with-409
behavior.

### Windows

There's no systemd equivalent; `deploy/windows/run-parztream.bat`
plus an env file (`deploy/windows/parztream.env.bat.example`, copy to
`C:\ProgramData\parztream\parztream.env.bat` and fill in real values)
gets you a runnable script. To make it persistent, either:

- **Task Scheduler** — create a task that runs
  `run-parztream.bat` at log-on/startup. Simplest, but it runs as a
  visible background process tied to a login session, not a true
  Windows service.
- **[NSSM](https://nssm.cc/)** — wraps the batch script as an actual
  Windows service with restart-on-failure, closer to the systemd
  setup above.

These Windows steps are untested — they're written from documented
behavior, not verified on an actual Windows machine.

## Testing

```bash
pip install -r requirements-dev.txt
pytest
```

Tests run against isolated tmp directories/databases (see
`tests/conftest.py`), never your real media folders or DB. A couple
of scanner tests that need real audio metadata are skipped
automatically if `ffmpeg` isn't on `PATH`.
