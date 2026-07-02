# parztream

Your own personal streaming service, running on a computer you own.
parztream scans folders of movies, shows, and music and turns them
into a simple website you can open from your phone, tablet, or any
other device on your home network — no subscription, no uploading
your files anywhere.

## Contents

- [Features](#features)
- [Quick start](#quick-start)
- [Finding parztream from other devices](#finding-parztream-from-other-devices)
- [Using parztream](#using-parztream)
- [Configuration](#configuration)
- [Running as a background service](#running-as-a-background-service)
- [Accessibility](#accessibility)
- [Testing](#testing)

## Features

- Stream video and music to any device with a web browser
- Guided, click-through setup — no config files or typing folder paths
- Automatic cover art and video thumbnails
- TV episodes grouped by show and season
- Subtitle support (`.srt` / `.vtt`)
- Fast search across your whole library
- Works well on phones: installable as an app, full-screen video
- Optional password protection
- Find the server by name on your network — no IP address to remember

## Quick start

You'll need Python 3.10+. `ffmpeg` is optional but recommended — it
adds video thumbnails, video length, and lets otherwise-incompatible
video files still play (see [Playback compatibility](#playback-compatibility)).

```bash
python3 -m venv .venv
source .venv/bin/activate        # on Windows: .venv\Scripts\activate
pip install -r requirements.txt

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Then open **`http://localhost:8000`** (or the server's LAN address —
see the next section) in a browser. The first time you visit, a setup
page walks you through picking folders to scan with a built-in
folder browser — you never need to know or type an exact path.

## Finding parztream from other devices

From any other device on the same Wi-Fi/network, try:

**`http://parztream.local:8000/`**

parztream announces itself under that name automatically — no setup
needed, and it keeps working even if the server's IP address changes
later. If it doesn't resolve on a particular device, use the
server's IP address instead (e.g. `http://192.168.1.42:8000/`).

<details>
<summary>Why this sometimes doesn't work, and other options</summary>

Support for `.local` names (mDNS/Bonjour) varies by platform:

- **macOS, iOS, Linux** — works reliably out of the box.
- **Windows** — inconsistent; some versions resolve `.local` names
  fine, others need Apple's Bonjour component installed (it ships
  with iTunes, or can be installed standalone).
- **Android browsers** — the weakest link; Android supports mDNS at
  the OS level, but browsers resolving `.local` names in the address
  bar is unreliable across versions.

This is on by default and needs nothing configured. To turn it off
(e.g. on a network where multicast traffic is filtered), set
`PARZTREAM_MDNS_ENABLED=false`.

A second, independent option: set the server machine's actual OS
hostname to `parztream` (`sudo hostnamectl set-hostname parztream` on
Linux; System Properties → Computer Name on Windows). Many home
routers automatically register a device's DHCP hostname into their
own DNS, so on networks where that's true, plain
**`http://parztream:8000/`** (no `.local`) works everywhere, including
the Windows/Android cases where mDNS is weakest. This depends on your
router's firmware and isn't guaranteed, but costs nothing extra to
also set up, and covers different networks than mDNS does.

Neither option is a 100% guarantee on every network — the server's IP
address always works too, as a reliable fallback. Two related
environment variables: `PARZTREAM_MDNS_HOSTNAME` (defaults to
`parztream`) to advertise a different name, and `PARZTREAM_PORT`
(defaults to `8000`) which must be kept in sync with whatever
`--port` you actually start uvicorn with.

</details>

## Using parztream

**Library & search** — Click "Scan library" any time to re-index
your folders (runs in the background, so the page stays usable while
it works). Every item gets a thumbnail — embedded cover art for
music, an extracted frame for video. Search by title, artist, album,
or show name; revisit folder selection any time via "Settings" in
the header.

**TV shows** — Files named like `Show Name S01E02...` (dots,
underscores, or spaces all work, e.g. `The.Chosen.S01E02.1080p.mp4`)
are automatically grouped into a "shows" dropdown, listed in episode
order. Anything named differently is left as a regular, ungrouped
video rather than guessed at.

**Subtitles** — Drop a same-named `.srt` or `.vtt` file next to a
video (e.g. `Movie.mp4` + `Movie.srt`) and it's picked up
automatically during playback. Only one subtitle file per video is
supported.

**On your phone** — The page reflows for narrow screens, video goes
full-screen automatically when you tap play, and you can "Add to
Home Screen" for a real app icon and a browser-chrome-free window.

<details>
<summary>Mobile details, and an honest caveat</summary>

- The header controls reflow onto their own rows below ~640px wide
  instead of overflowing sideways.
- Fullscreen-on-tap uses the standard Fullscreen API, falling back to
  iOS Safari's own fullscreen video API where the standard one isn't
  supported. It never blocks playback if fullscreen is denied.
- "Add to Home Screen" uses a web app manifest (`display: standalone`)
  so the browser's address bar is hidden, closer to a real app than a
  bookmark.

None of the above has been verified on a real phone — no device was
available while building this. It's implemented correctly against the
relevant web platform APIs and reviewed carefully, but that's a
different (weaker) claim than "tested." If something doesn't behave
as described, this is the first place to look.

</details>

**Playback compatibility** — Most files just play. If a video's file
type would otherwise stop it from playing in a browser (most common
case: surround-sound audio a browser can't decode), parztream fixes
that automatically the first time it's played and remembers the fix
for next time. Video quality/resolution is never changed by this.

<details>
<summary>What "Direct Stream" actually does</summary>

If a video's *container* or *audio track* would stop a browser from
playing it (the most common real case: an MKV with ordinary H.264
video but AC3/DTS surround audio, which browsers can't decode),
parztream transparently repackages it into an MP4 — copying the video
as-is and only re-encoding the audio if needed — and caches the
result so it only happens once per file. This needs `ffmpeg` on
`PATH`.

What this *doesn't* do: re-encode video. If the video codec itself
isn't one a browser supports (e.g. HEVC), playback shows a clear
"can't play in browser" message with a link to download the original
file instead — the video quality/resolution never changes, and a
genuinely incompatible video codec stays incompatible for in-browser
playback specifically, not unplayable everywhere.

</details>

**Password protection** — Optional. Set `PARZTREAM_PASSWORD` (see
[Configuration](#configuration)) and the app requires signing in
through a proper login page before anything is accessible. Without
it, anyone who can reach the server's address can browse and stream —
fine on a fully trusted home network, not recommended otherwise.

<details>
<summary>How login sessions work</summary>

A successful login sets a signed session cookie good for 90 days, so
you're not asked again on every visit; "Log out" in the header clears
it. Sessions are self-contained signed cookies, not tracked
server-side, so logging out only tells your *browser* to stop sending
the cookie — a copied cookie value stays valid until it expires on
its own. If you ever suspect a session leaked, set/rotate
`PARZTREAM_SECRET_KEY` and restart — that invalidates every existing
session at once, which changing `PARZTREAM_PASSWORD` alone does not
do.

</details>

## Configuration

Everything below is optional — parztream runs with sensible defaults
and a guided setup page. Set these as environment variables if you
want to configure it another way (e.g. for the service setups below).

| Variable | What it does | Default |
|---|---|---|
| `PARZTREAM_MEDIA_DIRS` | Folders to scan, separated by `os.pathsep` (`:` on Linux/macOS, `;` on Windows). Only used as a starting default — once folders are saved through the setup page, that takes over. | none (setup page prompts) |
| `PARZTREAM_DB_PATH` | SQLite database file location. | `parztream.db` |
| `PARZTREAM_PASSWORD` | Enables login. See [Password protection](#using-parztream). | unset (no login) |
| `PARZTREAM_USERNAME` | Login username. Only relevant if you're calling the login API directly — the login page itself only asks for a password. | `parztream` |
| `PARZTREAM_SECRET_KEY` | Signs session cookies. If unset, a random key is generated on every restart, meaning everyone's logged out each time. Set a fixed value (`python3 -c "import secrets; print(secrets.token_hex(32))"`) to keep people logged in across restarts. | random per-restart |
| `PARZTREAM_CACHE_DIR` | Where repackaged videos and video thumbnails are cached. | `cache/` |
| `PARZTREAM_CACHE_MAX_BYTES` | Caps the cache folder's total size — oldest files are deleted once a new one pushes it over the limit. An evicted file just gets cheaply re-derived next time it's played. | unset (no limit) |
| `PARZTREAM_MDNS_ENABLED` | Set to `false` to turn off the `parztream.local` network announcement. | `true` |
| `PARZTREAM_MDNS_HOSTNAME` | Name advertised on the network. Change this if running more than one instance on the same LAN. | `parztream` |
| `PARZTREAM_PORT` | Must match whatever `--port` you start uvicorn with — purely informational, only affects what's advertised on the network. | `8000` |

<details>
<summary>Security note: symlinks are deliberately not followed when scanning</summary>

A symlink inside a scanned folder can point anywhere on disk
regardless of its own filename, so following them would let anything
writable into a scanned folder (a compromised download client,
another OS account with folder access, plain misconfiguration) expose
arbitrary files through the streaming/download endpoints. This is
intentional and not something to "fix" by following symlinks.

</details>

## Running as a background service

The Quick Start command runs parztream in the foreground — it stops
when you close the terminal. Templates for running it as a persistent
background service (auto-start, restart on crash) are in `deploy/`.

<details>
<summary>Linux (systemd)</summary>

1. Put the project at a stable location (e.g. `/opt/parztream`),
   including its `.venv`, and create a system user to run it as:
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
4. Check status/logs: `systemctl status parztream`,
   `journalctl -u parztream -f`.

Optional but recommended: set the machine's actual OS hostname so
plain `http://parztream:8000/` has a chance of working too, on top of
the `parztream.local` name the app advertises on its own:
```bash
sudo hostnamectl set-hostname parztream
```

Edit `User`/`WorkingDirectory`/`ExecStart` in the unit file first if
your paths or username differ from the example. Don't add
`--workers` to `ExecStart` — scan status/locking lives in one
process's memory, so multiple worker processes would silently break
the concurrent-scan-rejects-with-409 behavior.

</details>

<details>
<summary>Windows</summary>

There's no systemd equivalent; `deploy/windows/run-parztream.bat`
plus an env file (`deploy/windows/parztream.env.bat.example`, copy to
`C:\ProgramData\parztream\parztream.env.bat` and fill in real values)
gets you a runnable script. To make it persistent, either:

- **Task Scheduler** — create a task that runs `run-parztream.bat` at
  log-on/startup. Simplest, but it runs as a visible background
  process tied to a login session, not a true Windows service.
- **[NSSM](https://nssm.cc/)** — wraps the batch script as an actual
  Windows service with restart-on-failure, closer to the systemd
  setup above.

Optional but recommended, same reasoning as the Linux step above: set
the machine's actual computer name to `parztream` (System Properties
→ Computer Name → Change), so plain `http://parztream:8000/` has a
chance of working too.

These Windows steps are untested — they're written from documented
behavior, not verified on an actual Windows machine.

</details>

## Accessibility

- Every media/folder row is a real button, reachable and activatable
  with just a keyboard (Tab + Enter/Space).
- A "Skip to content" link (visible on keyboard focus) lets keyboard
  users bypass the header controls and jump straight to the library.
- Search, filter, and login fields have real (if visually hidden)
  labels — placeholder text alone isn't a substitute for screen
  reader users.
- Scan progress, search result counts, and playback state changes are
  announced to screen readers via a live region.
- Text/background color contrast throughout meets WCAG AA (verified
  by calculating actual contrast ratios for every color pair, not
  eyeballed).

<details>
<summary>Known gaps</summary>

Touch targets meet WCAG 2.2's AA minimum (24×24px) but not the
stricter AAA guideline (44×44px) — fixing that properly needs a
broader spacing/layout pass with real-device testing, which wasn't
available while building this. None of the above was verified with a
real screen reader or an automated tool like axe-core either (no
browser automation was available in the environment this was built
in) — it's built correctly per the relevant WCAG success criteria and
carefully reviewed, not screen-reader-tested.

</details>

## Testing

```bash
pip install -r requirements-dev.txt
pytest
```

Tests run against isolated temporary directories/databases (see
`tests/conftest.py`), never your real media folders or database. A
couple of scanner tests that need real audio metadata are skipped
automatically if `ffmpeg` isn't on `PATH`.

For architecture notes and conventions, see `CLAUDE.md`.
