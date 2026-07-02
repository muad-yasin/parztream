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

Scanning skips symlinks entirely — neither symlinked files nor
symlinked subdirectories are followed. This is deliberate: a symlink
inside a scanned folder can point anywhere on disk regardless of its
own filename, so following them would let anything writable into
`PARZTREAM_MEDIA_DIRS` (a compromised download client, another OS
account with folder access, plain misconfiguration) expose arbitrary
files on the server through the streaming/download endpoints.

## Running

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Finding the server without hunting for its IP

parztream advertises itself over mDNS on startup, so **`http://parztream.local:8000/`** works from any device on the LAN whose OS/browser supports it — no need to look up or remember an IP address, and it keeps working even if that IP changes later (a router reboot, a new DHCP lease, etc.). This is on by default and needs nothing configured; if you ever need to turn it off (e.g. a network where multicast traffic is filtered or unwelcome), set `PARZTREAM_MDNS_ENABLED=false`.

Real support varies by platform, worth knowing honestly rather than assuming it "just works" everywhere:
- **macOS, iOS, Linux** — reliable out of the box (this is what Apple calls Bonjour; Avahi covers Linux).
- **Windows** — inconsistent; some versions/configurations resolve `.local` names fine, others need Apple's Bonjour component installed (it comes bundled with iTunes, or can be installed standalone) to work reliably.
- **Android browsers** — the weakest link; Android supports mDNS at the OS level, but browser resolution of `.local` names in the address bar is unreliable across versions.

**A second, independent path that covers different ground**: set the server machine's actual OS hostname to `parztream` (e.g. `sudo hostnamectl set-hostname parztream` on Linux; the equivalent System Properties change on Windows). Many home routers (anything running `dnsmasq` under the hood — common on ISP-provided routers and virtually all OpenWrt/DD-WRT setups) automatically register a device's DHCP hostname into the router's own local DNS resolver. On networks where that's true, **plain `http://parztream:8000/` — no `.local` suffix — works identically on every device**, including the Windows/Android cases where mDNS is weakest, because it's resolved as ordinary DNS through the router your devices already use, not through anything client-side. This depends on your specific router's firmware, which parztream has no way to detect or guarantee — but it costs nothing extra to also enable, and the two mechanisms cover different networks, so setting both gives the broadest realistic coverage.

Neither is a 100% guarantee on every possible network with zero setup — that's not achievable purely through the browser address bar, not a gap in this app specifically. The server's IP address always works too, as the reliable fallback.

Two related environment variables, if you ever need them: `PARZTREAM_MDNS_HOSTNAME` (defaults to `parztream`) to advertise a different name, and `PARZTREAM_PORT` (defaults to `8000`) which **must match** whatever `--port` you actually start uvicorn with — the app has no way to introspect that itself, it only affects what port number gets included in the mDNS advertisement.

Open `http://<host>:8000/` from any device on the LAN. If no media
folders are configured yet, you'll land on a setup page with a
built-in folder browser — no need to know a folder's exact path or
touch a config file. Pick one or more folders and save; parztream
scans them right away and remembers the choice (in its database, not
just for this run), so it survives restarts without needing
`PARZTREAM_MEDIA_DIRS` set at all. Revisit folder selection any time
via the "Settings" link in the header. `PARZTREAM_MEDIA_DIRS` still
works exactly as before if you'd rather configure it that way (e.g.
for the systemd/service setups below) — it's used as the starting
default, and anything saved through the setup page takes precedence
over it.

Click "Scan library" to re-index the configured folders — scanning
runs in the background, so the UI stays responsive while it works.
The library list shows thumbnails for every item — embedded cover art
for audio (mp3/FLAC/m4a/m4b), an extracted video frame for video,
both generated the first time they're requested and cached after — is
searchable (title/artist/album/show name, case-insensitive substring
match), and is paginated 50 items at a time.

### On a phone

- The header controls (search, filters, buttons) reflow onto their
  own rows below ~640px instead of overflowing sideways.
- Tapping a video on a touch device requests fullscreen automatically
  (falls back to iOS Safari's own native fullscreen video API, which
  doesn't support the standard Fullscreen API for `<video>`). Never
  blocks playback if it fails — some browsers/contexts don't grant it.
- "Add to Home Screen" gets a real app icon and, with `display:
  standalone` in the manifest, hides the browser's address bar —
  closer to a real app than a bookmark.

Honest caveat: none of the above (the header reflow, the fullscreen
request, autoplay after tapping a row) has been verified on a real
phone — no device was available while building this. It's written to
the relevant web platform APIs correctly and reviewed carefully, not
device-tested. If something doesn't behave as described, that's the
first place to look.

### TV show grouping

Video files named like `Show Name S01E02...` (dots/underscores/spaces
all work, e.g. `The.Chosen.S01E02.1080p.mp4`) are automatically
recognized as episodes: a "shows" dropdown lets you browse by show,
listing episodes in season/episode order instead of alphabetically.
Anything that doesn't match that pattern is just left as a regular,
ungrouped video — there's no attempt to guess at other naming
conventions (`1x02`, absolute numbering, etc.).

### Subtitles

If a video has a same-name `.srt` or `.vtt` file next to it (e.g.
`Movie.mp4` + `Movie.srt`), it's picked up automatically and shown as
a subtitle track during playback — `.srt` gets converted to WebVTT on
the fly (browsers only support WebVTT natively), `.vtt` is served
as-is. Only one subtitle file per video is supported — no multi-
language track selection.

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
file instead (`/api/stream/{id}?original=1`, which always serves the
untouched source file regardless of codec — that's what makes the
download link actually work even though in-browser playback is
blocked) — the video quality/resolution never changes, and a
genuinely incompatible video codec stays incompatible for in-browser
playback specifically, not unplayable everywhere.

### Login

If `PARZTREAM_PASSWORD` is set, the whole app requires signing in
first through a proper login page (`/login.html`) — not the browser's
native, unbranded Basic Auth popup. A successful login sets a signed
session cookie good for 90 days, so you're not asked again on every
visit; "Log out" in the header (or `POST /api/logout`) clears it.

Worth knowing: sessions are self-contained signed cookies, not
tracked server-side, so logging out only works by telling the
*browser* to stop sending the cookie — it doesn't invalidate that
specific cookie value on the server. A copied cookie stays valid
until it expires on its own. If you ever suspect a session leaked,
set/rotate `PARZTREAM_SECRET_KEY` (below) and restart — that
invalidates every existing session at once, which changing
`PARZTREAM_PASSWORD` alone does **not** do.

If `PARZTREAM_PASSWORD` is unset, the server has **no
authentication** — anyone who can reach the port can browse and
stream. Recommended for anything beyond a fully trusted LAN.

### Accessibility

- Every media/folder row is a real `<button>`, reachable and
  activatable with just a keyboard (Tab + Enter/Space) — not a click
  handler on a non-interactive element.
- A "Skip to content" link (visible on keyboard focus) lets keyboard
  users bypass the header controls and jump straight to the library.
- Search, filter, and login fields have real (if visually hidden)
  `<label>`s — placeholder text alone isn't a substitute for screen
  reader users.
- Scan progress, search result counts, and playback state changes are
  announced to screen readers via a live region — previously these
  were silent unless you happened to be focused on the exact element
  that changed.
- Text/background color contrast throughout meets WCAG AA (verified
  by calculating actual contrast ratios for every color pair in the
  palette, not eyeballed).

Known gap, left as a deliberate scope cut: touch targets meet WCAG
2.2's AA minimum (24×24px) but not the stricter AAA guideline
(44×44px) — fixing that properly means a broader spacing/layout pass
that needs real-device testing to get right, which wasn't available
while building this. None of the above was verified with a real
screen reader or an automated tool like axe-core either (no browser
automation was available in the environment this was built in) — it's
built correctly per the relevant WCAG success criteria and carefully
reviewed, not screen-reader-tested.

## Configuration

Set via environment variables:

- `PARZTREAM_MEDIA_DIRS` — folders to scan, separated by `os.pathsep`
  (`:` on Linux/macOS, `;` on Windows). Only used as the *default*
  before anything's been configured through the setup page — once
  folders are saved there (stored in the database), that takes over
  and this env var is ignored, even if it's still set.
- `PARZTREAM_DB_PATH` — SQLite file location (defaults to
  `parztream.db` in the project root).
- `PARZTREAM_PASSWORD` — enables login (see "Login" above). Unset by
  default, meaning no authentication at all.
- `PARZTREAM_USERNAME` — login username (defaults to `parztream`),
  only relevant when `PARZTREAM_PASSWORD` is set. The login page only
  asks for a password, not a username — this only matters if you're
  calling `POST /api/login` directly rather than through the page.
- `PARZTREAM_SECRET_KEY` — signs session cookies. If unset, a random
  key is generated every time the process starts, which means
  **everyone's logged out on every restart**. Set this to a fixed
  random value (e.g. `python3 -c "import secrets; print(secrets.token_hex(32))"`)
  to keep people logged in across restarts — or deliberately leave it
  unset if you'd rather every restart force a fresh login.
- `PARZTREAM_CACHE_DIR` — where repackaged videos (see "Playback
  compatibility" below) and generated video thumbnails are cached
  (defaults to `cache/` in the project root). Grows roughly
  proportional to how much of your library needs a container/audio
  fix, since video is copied rather than re-encoded; thumbnails
  themselves are small.
- `PARZTREAM_CACHE_MAX_BYTES` — caps `PARZTREAM_CACHE_DIR`'s total
  size; once a new file pushes it over this limit, the oldest cached
  files are deleted to make room (the file just created is never
  evicted, even if it alone exceeds the cap). Unset by default, i.e.
  **no limit, no automatic pruning** — deleting cached files nobody
  asked to be capped isn't a sane default. An evicted file isn't
  lost, just re-derived (cheaply) the next time it's played.
- `PARZTREAM_MDNS_ENABLED` — set to `false` to turn off the
  `parztream.local` mDNS advertisement described above. On by
  default.
- `PARZTREAM_MDNS_HOSTNAME` — the name advertised over mDNS (defaults
  to `parztream`, i.e. `parztream.local`). Change this if you're
  running more than one parztream instance on the same LAN.
- `PARZTREAM_PORT` — **must match** whatever `--port` you actually
  start `uvicorn` with (defaults to `8000`, matching every example in
  this README). Purely informational: the app can't introspect its
  own actual port, so this only controls what port number gets
  included in the mDNS advertisement — a mismatch doesn't break
  anything else, it just means `parztream.local` would point at the
  wrong port.

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

Optional but recommended, for the reasons in "Finding the server"
above: set the machine's actual OS hostname so plain
`http://parztream:8000/` has a chance of working too, on top of the
`parztream.local` mDNS name the app advertises on its own:
```bash
sudo hostnamectl set-hostname parztream
```

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

Optional but recommended, same reasoning as the Linux step above: set
the machine's actual computer name to `parztream` (System Properties
→ Computer Name → Change), so plain `http://parztream:8000/` has a
chance of working too, on top of the `parztream.local` mDNS name.

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
