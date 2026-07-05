# Advanced usage

This page covers everything that didn't fit in the beginner-friendly
[`README.md`](README.md): running from source, network details,
configuration options, running parztream as a background service,
accessibility notes, testing, and how the three downloadable builds
themselves are put together.

## Contents

- [Running from source](#running-from-source)
- [Finding parztream on your network](#finding-parztream-on-your-network)
- [How playback compatibility ("Direct Stream") works](#how-playback-compatibility-direct-stream-works)
- [Mobile/PWA details](#mobilepwa-details)
- [How login sessions work](#how-login-sessions-work)
- [Configuration reference](#configuration-reference)
- [Real video transcoding](#real-video-transcoding)
- [Running as a background service](#running-as-a-background-service)
- [Building the Windows .exe](#building-the-windows-exe)
- [Building the Linux AppImage](#building-the-linux-appimage)
- [Building the macOS app](#building-the-macos-app)
- [Accessibility](#accessibility)
- [Testing](#testing)

## Running from source

The [README](README.md) covers the download-and-run builds for
Windows, Linux (x86_64), and macOS (Apple Silicon). Running from
source is for anyone those don't cover — an Intel Mac, a different
CPU architecture — or who wants to contribute to the code.

### What you'll need

- **Python, version 3.10 or newer.** Download it for free from
  [python.org/downloads](https://www.python.org/downloads/).
  - **On Windows:** tick **"Add Python to PATH"** during
    installation — easy to miss, and without it none of the steps
    below will work.
  - **On macOS:** use the python.org installer; macOS's built-in
    Python is too old.
  - **On Linux:** most distributions already have Python installed.
- **(Optional, but recommended) ffmpeg** — lets parztream generate
  video thumbnails, show video length, and play a wider range of
  video files. The downloadable builds already include this;
  building from source doesn't. Get it from
  [ffmpeg.org/download.html](https://ffmpeg.org/download.html);
  parztream works without it, just with a bit less compatibility.

### Step-by-step

1. **Get the project files.**
   - **Option A — download the ZIP**: on the
     [project page](https://github.com/muad-yasin/parztream), click
     **Code → Download ZIP**, then unzip it.
   - **Option B — clone with git** (if you already have it):
     ```bash
     git clone https://github.com/muad-yasin/parztream.git
     ```
2. **Open a terminal in that folder** — File Explorer's address bar,
   type `cmd`, Enter (Windows); Finder → Services → New Terminal at
   Folder (macOS); most Linux file managers have "Open Terminal Here."
3. **Create and activate a virtual environment** (a private space for
   parztream's dependencies, so they don't mix with anything else on
   your system):
   ```bash
   python3 -m venv .venv
   ```
   Then activate it — you'll see `(.venv)` appear at the start of
   your terminal prompt:
   - **Windows:** `.venv\Scripts\activate`
   - **macOS/Linux:** `source .venv/bin/activate`

   You'll need to repeat just this activation step every time you
   come back to run parztream later — not the whole setup.
4. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
5. **Start it:**
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```
   Leave this window open — closing it stops parztream. Open
   `http://localhost:8000` in a browser; see the README's
   [How to use it](README.md#how-to-use-it) from there.

To run it again later: open a terminal in the project folder,
activate the virtual environment (step 3's activation command), then
run the `uvicorn` command from step 5 again.

### Troubleshooting (source installs)

**"python3 is not recognized" / "python: command not found"**
Python either isn't installed, or wasn't added to PATH. Reinstall
from [python.org/downloads](https://www.python.org/downloads/),
ticking "Add Python to PATH" on Windows. Also try the other name
(`python` instead of `python3`, or vice versa).

**"pip is not recognized" / "pip: command not found"**
Use `python3 -m pip install -r requirements.txt` (or
`python -m pip install -r requirements.txt` on Windows) instead of
plain `pip`.

**"No module named ..." error when starting the server**
Your virtual environment probably isn't activated for this terminal
window. Re-run the activation command (step 3 above) and try again.

**"Address already in use"**
Something else is using port 8000. Close that program, or start on a
different port instead:
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8001
```
then visit `http://localhost:8001`.

## Finding parztream on your network

By default, other devices on the same network can reach parztream at
`http://parztream.local:8000/`, thanks to an automatic network
announcement (mDNS/Bonjour) — no setup needed, and it keeps working
even if the server's IP address changes later.

Support for `.local` names varies by platform:

- **macOS, iOS, Linux** — works reliably out of the box.
- **Windows** — inconsistent; some versions resolve `.local` names
  fine, others need Apple's Bonjour component installed (it ships
  with iTunes, or can be installed standalone).
- **Android browsers** — the weakest link; Android supports mDNS at
  the OS level, but browsers resolving `.local` names in the address
  bar is unreliable across versions.

To turn this announcement off (e.g. on a network where multicast
traffic is filtered), set `PARZTREAM_MDNS_ENABLED=false`.

A second, independent option: set the server machine's actual OS
hostname to `parztream` (`sudo hostnamectl set-hostname parztream` on
Linux; System Properties → Computer Name on Windows). Many home
routers automatically register a device's DHCP hostname into their
own DNS, so on networks where that's true, plain
`http://parztream:8000/` (no `.local`) works everywhere, including the
Windows/Android cases where mDNS is weakest. This depends on your
router's firmware and isn't guaranteed, but costs nothing extra to
also set up, and covers different networks than mDNS does.

Neither option is a 100% guarantee on every network — the server's IP
address always works too, as a reliable fallback. Two related
environment variables: `PARZTREAM_MDNS_HOSTNAME` (defaults to
`parztream`) to advertise a different name, and `PARZTREAM_PORT`
(defaults to `8000`) which must be kept in sync with whatever
`--port` you actually start uvicorn with.

## How playback compatibility ("Direct Stream") works

Most files just play. If a video's *container* or *audio track* would
stop a browser from playing it (the most common real case: an MKV
with ordinary H.264 video but AC3/DTS surround audio, which browsers
can't decode), parztream transparently repackages it into an MP4 —
copying the video as-is and only re-encoding the audio if needed —
and caches the result so it only happens once per file. This needs
`ffmpeg` on `PATH`.

What this *doesn't* do: re-encode video. If the video codec itself
isn't one a browser supports (e.g. HEVC), playback shows a clear
"can't play in browser" message with a link to download the original
file instead — the video quality/resolution never changes, and a
genuinely incompatible video codec stays incompatible for in-browser
playback specifically, not unplayable everywhere.

## Mobile/PWA details

- The header controls reflow onto their own rows below ~640px wide
  instead of overflowing sideways.
- Fullscreen-on-tap uses the standard Fullscreen API, falling back to
  iOS Safari's own fullscreen video API where the standard one isn't
  supported. It never blocks playback if fullscreen is denied.
- "Add to Home Screen" uses a web app manifest (`display: standalone`)
  so the browser's address bar is hidden, closer to a real app than a
  bookmark. This is a "PWA" (Progressive Web App), not an app-store
  app.

None of the above has been verified on a real phone — no device was
available while building this. It's implemented correctly against the
relevant web platform APIs and reviewed carefully, but that's a
different (weaker) claim than "tested." If something doesn't behave
as described, this is the first place to look.

## How login sessions work

Setting `PARZTREAM_PIN` (see [Configuration reference](#configuration-reference))
makes the app require signing in with a 4-digit PIN through a login
page before anything is accessible. Without it, anyone who can reach
the server's address can browse and stream — fine on a fully trusted
home network, not recommended otherwise. A PIN is deliberately not a
full password: it's faster to type on a phone or TV remote, which
matters more here than resisting a determined remote attacker, since
the realistic risk on a home LAN is someone already on your network,
not an internet-wide brute-force campaign. To keep even that
narrower risk in check, 5 incorrect attempts from the same address
lock out further attempts for 30 seconds.

A successful login sets a signed session cookie good for 90 days, so
you're not asked again on every visit; "Log out" in the header clears
it. Sessions are self-contained signed cookies, not tracked
server-side, so logging out only tells your *browser* to stop sending
the cookie — a copied cookie value stays valid until it expires on
its own. If you ever suspect a session leaked, set/rotate
`PARZTREAM_SECRET_KEY` and restart — that invalidates every existing
session at once, which changing `PARZTREAM_PIN` alone does not do.

## Configuration reference

Everything below is optional — parztream runs with sensible defaults
and a guided setup page. Set these as environment variables if you
want to configure it another way (e.g. for the service setups below).

| Variable | What it does | Default |
|---|---|---|
| `PARZTREAM_MEDIA_DIRS` | Folders to scan, separated by `os.pathsep` (`:` on Linux/macOS, `;` on Windows). Only used as a starting default — once folders are saved through the setup page, that takes over. | none (setup page prompts) |
| `PARZTREAM_DB_PATH` | SQLite database file location. | `parztream.db` |
| `PARZTREAM_PIN` | Enables login. Should be a 4-digit PIN. See [How login sessions work](#how-login-sessions-work). | unset (no login) |
| `PARZTREAM_SECRET_KEY` | Signs session cookies. If unset, a random key is generated on every restart, meaning everyone's logged out each time. Set a fixed value (`python3 -c "import secrets; print(secrets.token_hex(32))"`) to keep people logged in across restarts. | random per-restart |
| `PARZTREAM_CACHE_DIR` | Where repackaged videos and video thumbnails are cached. | `cache/` |
| `PARZTREAM_CACHE_MAX_BYTES` | Caps the cache folder's total size — oldest files are deleted once a new one pushes it over the limit. An evicted file just gets cheaply re-derived next time it's played. | unset (no limit) |
| `PARZTREAM_ENABLE_TRANSCODE` | Controls real video re-encoding for videos whose codec itself can't play in a browser (e.g. HEVC). `1`/`true`/`yes` always turns it on; `0`/`false`/`no` always turns it off; unset/empty/`auto` (the default) automatically enables it only if a real hardware encoder is detected and benchmarks fast enough for real-time playback. See [Real video transcoding](#real-video-transcoding). | unset (auto-detect) |
| `PARZTREAM_MAX_CONCURRENT_TRANSCODES` | Caps how many videos can be re-encoded at once (separate from the existing per-video job dedup). | `1` |
| `PARZTREAM_MDNS_ENABLED` | Set to `false` to turn off the `parztream.local` network announcement. | `true` |
| `PARZTREAM_MDNS_HOSTNAME` | Name advertised on the network. Change this if running more than one instance on the same LAN. | `parztream` |
| `PARZTREAM_PORT` | Must match whatever `--port` you start uvicorn with — purely informational, only affects what's advertised on the network. | `8000` |

**Security note:** symlinks are deliberately not followed when
scanning. A symlink inside a scanned folder can point anywhere on
disk regardless of its own filename, so following them would let
anything writable into a scanned folder (a compromised download
client, another OS account with folder access, plain
misconfiguration) expose arbitrary files through the
streaming/download endpoints. This is intentional and not something
to "fix" by following symlinks.

## Real video transcoding

By default, a video whose codec itself can't be decoded by a browser
(HEVC being the common case) is handled based on `PARZTREAM_ENABLE_TRANSCODE`,
which has three modes:

- **Auto-detect (the default, unset or `auto`).** The first time such a
  file is actually played, parztream checks for a real hardware encoder
  (Intel Quick Sync, NVIDIA NVENC, AMD AMF/VCE, VAAPI, or Apple
  VideoToolbox, depending on platform) and benchmarks it against a
  representative clip. If it encodes comfortably faster than real time,
  transcoding turns itself on automatically — no configuration needed. If
  no hardware encoder is found, or the one found isn't fast enough, the
  file falls back to download-only, exactly as if transcoding were off.
  The software-only fallback (`libopenh264`) is deliberately **never**
  auto-enabled, however fast it benchmarks — it's pure CPU load with no
  hardware offload, which is exactly the risk this auto-detection exists
  to protect weaker hardware (NAS boxes, old laptops, Raspberry Pi via the
  Linux build) from.
- **Always on (`1`/`true`/`yes`).** Skips the speed benchmark entirely —
  if *any* working encoder is found (hardware or the software fallback),
  it's used, exactly like this project's original opt-in-only behavior
  before auto-detection existed. Use this if you know your hardware can
  keep up (including a software-only setup) and don't want the benchmark's
  speed threshold to potentially decide against it.
- **Always off (`0`/`false`/`no`).** Guarantees the old default: those
  files always fall back to download-only, and nothing in
  `app/encoder_detect.py` is ever even called.

Once transcoding is happening (auto-detected or forced on), parztream
verifies the chosen encoder with a real test encode, not just what ffmpeg
claims to support — a hardware encoder can be compiled in but still fail
at runtime with no GPU present or a missing driver. Re-encoded video is
always capped at 1080p (downscaled, never upscaled) to bound worst-case
cost regardless of source resolution, and `PARZTREAM_MAX_CONCURRENT_TRANSCODES`
(default `1`) limits how many videos can be re-encoding at once so one
weak CPU/GPU doesn't get asked to do several at the same time.

**Why `libopenh264`, not `libx264`:** the vendored ffmpeg for the
Windows/Linux builds is deliberately LGPL-licensed (see below), which
excludes GPL-licensed `libx264`/`libx265` entirely. `libopenh264` is
the only software H.264 encoder available in that build — it's
noticeably lower quality-per-bit than `libx264`, a direct trade-off of
staying LGPL-only rather than switching those two platforms to a GPL
ffmpeg build. macOS's Homebrew-sourced ffmpeg already includes
`libx264`/`libx265` (see the macOS packaging section), but this
feature doesn't rely on that, to keep encoder detection uniform across
platforms.

**Honesty about what's actually been verified:** hardware-encoder
success has not been confirmed on real hardware — development and
testing happened in an environment with no GPU/hardware encode path
available at all, so only "candidate compiled in but fails at
runtime" and "falls through to software" have actually been
exercised. Likewise, the exact set of encoders compiled into the real
vendored BtbN binaries (as opposed to a generic system ffmpeg) hasn't
been spot-checked. The auto-detection speed benchmark inherits this
exact same caveat — it's logic-tested (mocked encode timings), never
run against a real GPU, so its 2x-real-time threshold hasn't been
validated against actual hardware encoder throughput. If you enable
this and a hardware encoder you expect to work isn't being picked up
(or auto-detection didn't turn transcoding on when you expected it
to), check the server logs for a "Transcode auto-detection: ..." line
explaining what was measured, and that's the first place to look.

## Running as a background service

The main README's install command runs parztream in the foreground —
it stops when you close the terminal. Templates for running it as a
persistent background service (auto-start, restart on crash) are in
`deploy/`.

### Linux (systemd)

1. Put the project at a stable location (e.g. `/opt/parztream`),
   including its `.venv`, and create a system user to run it as:
   ```bash
   sudo useradd --system --home-dir /opt/parztream --shell /usr/sbin/nologin parztream
   sudo chown -R parztream:parztream /opt/parztream
   ```
2. Copy the env template and fill in real values — keep it outside
   the project checkout since it holds a PIN:
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

### Windows

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

## Building the Windows .exe

The Windows executable (`parztream-windows.exe`, offered at the top
of the main README as the easiest way to get started) is built
automatically by GitHub Actions
(`.github/workflows/build-windows-exe.yml`) whenever a version tag
(e.g. `v1.2.0`) is pushed, and attached to that GitHub Release. You
can also trigger a build manually from the Actions tab (without a
tag) to test changes before actually cutting a release — it just
uploads the exe as a workflow artifact instead.

What the build does:

1. Downloads a static, LGPL-licensed build of `ffmpeg`/`ffprobe` for
   Windows (from [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds))
   into `packaging/windows/vendor/ffmpeg/` — gitignored, never
   committed, re-downloaded fresh on every build.
2. Runs PyInstaller against `packaging/windows/parztream.spec`, which
   bundles the Python interpreter, all of parztream's dependencies,
   the `static/` folder, and the two ffmpeg binaries into one
   `parztream.exe`.
3. Uploads the result as a workflow artifact, and — for tag-triggered
   runs only — attaches it to the matching GitHub Release.

`packaging/windows/launcher.py` is the actual entry point (not
`app/main.py` directly) — it exists specifically to solve problems
that only exist in a frozen, double-click .exe: it points the
database and cache at `%APPDATA%\parztream` (a onefile PyInstaller
build unpacks into a temporary folder that's deleted after every run,
so the app's own defaults would silently lose the whole library on
every restart if used as-is), persists a random `PARZTREAM_SECRET_KEY`
to a file there too (otherwise every launch would log everyone out —
see [How login sessions work](#how-login-sessions-work)), adds the
bundled ffmpeg to `PATH`, and opens the default browser automatically
once the server's actually ready to accept connections rather than
immediately (which would show a connection-refused page).

The .exe reads the same `PARZTREAM_*` environment variables as
running from source (see [Configuration reference](#configuration-reference))
— there's no in-app settings screen for them yet. Set them in Windows
before double-clicking (e.g. `setx PARZTREAM_PIN 1234`
in a terminal, then reopen the exe) if you want a PIN or other
non-default config.

To build it yourself on a real Windows machine instead of relying on
CI:

```bash
pip install -r requirements-build.txt
rem put ffmpeg.exe and ffprobe.exe in packaging\windows\vendor\ffmpeg\ first
pyinstaller packaging\windows\parztream.spec --distpath dist
```

The result is `dist\parztream.exe`.

**Licensing note:** the bundled ffmpeg/ffprobe binaries are
LGPL-licensed builds, deliberately chosen over a GPL build because
parztream never needs GPL-only encoders — video is only ever copied,
never re-encoded (see
[How playback compatibility works](#how-playback-compatibility-direct-stream-works)).
If that ever changes, revisit this before switching to a GPL build.

**Not code-signed:** producing a code-signed .exe (so Windows doesn't
show a "Windows protected your PC" warning) requires a paid
certificate this project doesn't have. The warning is expected and
harmless — the README's Troubleshooting section tells users what to
do about it.

**Verification status:** this whole pipeline (the spec file's
hidden-imports list, the launcher's PATH/data-dir handling, the CI
workflow) was written and reviewed without access to a real Windows
machine — based on documented PyInstaller/uvicorn/zeroconf behavior,
not hands-on testing. The `v0.1.1` release build did succeed in CI —
PyInstaller completed without error and produced `parztream-windows.exe`
— which confirms the spec/hidden-imports list are at least correct
enough to build. That is **not** the same as confirming the exe
actually launches and works on a real Windows machine — a green CI
build only proves PyInstaller didn't error, not that the resulting exe
runs correctly. Treat that as still unverified until someone actually
downloads and runs it on Windows.

## Building the Linux AppImage

The Linux build (`parztream-linux-x86_64.AppImage`, offered in the
main README as the easiest way for non-technical Linux users to get
started) follows the same overall approach as the Windows `.exe`:
`packaging/linux/launcher.py` (not `app/main.py`) is the PyInstaller
entry point, for the same reasons as the Windows launcher — it points
`PARZTREAM_DB_PATH`/`PARZTREAM_CACHE_DIR` at a persistent location
instead of wherever PyInstaller happens to unpack itself
(`sys._MEIPASS`, deleted after every run), following the XDG Base
Directory spec (`$XDG_DATA_HOME`, or `~/.local/share/parztream`) since
that's the Linux-native equivalent of Windows' `%APPDATA%`; persists a
generated `PARZTREAM_SECRET_KEY` there too; adds the bundled
`ffmpeg`/`ffprobe` to `PATH`; and opens the default browser once the
server's ready — except on Linux that's expected to silently do
nothing on a headless server (a genuinely common way this specific app
gets run), which the launcher accounts for rather than treating as an
error. Same `setdefault`-not-override behavior for all of these: set
your own environment variables first (e.g. `export
PARZTREAM_PIN=1234` before running it) for a PIN or other
non-default config.

What's specific to Linux/AppImage rather than shared with Windows:

- **AppImage is a packaging format wrapped around the same kind of
  PyInstaller onefile binary**, not a different build approach.
  `packaging/linux/parztream.spec` produces a plain self-contained
  Linux binary first; `packaging/linux/AppRun` (a tiny shell script)
  and `packaging/linux/parztream.desktop` (name/icon/categories) are
  what turn that binary plus `static/icon-512.png` into a proper
  AppDir, which `appimagetool` then packages into the final
  `.AppImage`. `Terminal=true` in the `.desktop` file is deliberate —
  it's what makes double-clicking from a file manager still show the
  startup banner and allow Ctrl+C to stop it, consistent with how the
  README describes stopping it.
- **glibc compatibility**: `.github/workflows/build-linux-appimage.yml`
  builds the PyInstaller binary inside a `python:3.12-slim-bullseye`
  Docker container (Debian 11, glibc 2.31) rather than directly on the
  `ubuntu-latest` runner. A binary built against a bleeding-edge
  runner's glibc can fail to even start on an older/stabler distro
  (Debian stable, Ubuntu 22.04, a NAS's Linux, etc.) with a
  `GLIBC_x.xx not found` error — building against an older baseline
  avoids that. This used to be a `manylinux_2_28` image instead, which
  seemed like the more obvious choice (it's specifically built for
  producing broadly-compatible Linux binaries) — but manylinux images
  build Python *without* a shared library (`libpythonX.Y.so`), since
  they're meant for building wheels, not standalone executables, and
  PyInstaller hard-requires one ("Python was built without a shared
  library"). Confirmed live in the very first real release build — not
  a hypothetical. The official `python:*-slim` images are built with
  `--enable-shared` and don't have this problem. If a future
  PyInstaller/Python bump needs a newer glibc baseline, move to a newer
  Debian-based `python:*-slim` tag, not back to manylinux. One more
  gap the same live build surfaced: PyInstaller also needs `objdump`
  (from `binutils`), which the `slim` image doesn't include either —
  the workflow installs it via `apt-get` before running PyInstaller.
  Between manylinux's missing shared library and slim's missing
  `binutils`, neither "obvious" base image worked without a fix; don't
  assume a third swap won't have its own gap too.
- **FUSE**: running an AppImage normally needs FUSE, which several
  modern distros no longer ship by default — this is a real rough edge
  `.exe` doesn't have. README's Troubleshooting section covers both
  fixes (install `libfuse2`, or run with
  `--appimage-extract-and-run`). The build itself sidesteps needing
  FUSE *in CI* the same way (`appimagetool --appimage-extract-and-run`)
  — that flag is about building the AppImage in a
  FUSE-less container, unrelated to whether an end user's machine has
  FUSE to run the *output* file.

To build the raw binary yourself (without wrapping it into an
AppImage) on a real Linux machine instead of relying on CI:

```bash
pip install -r requirements-build.txt
# put ffmpeg and ffprobe in packaging/linux/vendor/ffmpeg/ first
pyinstaller packaging/linux/parztream.spec --distpath dist
```

The result is `dist/parztream` — a single executable file, runnable
directly (`./dist/parztream`) without needing AppImage at all if
you don't care about desktop-icon integration.

**Licensing note:** same as the Windows build — the bundled
ffmpeg/ffprobe are LGPL, not GPL, deliberately, because parztream
never needs GPL-only encoders. See
[Building the Windows .exe](#building-the-windows-exe) for the full
reasoning.

**Verification status: fully verified, unlike Windows/macOS.** The raw
PyInstaller binary was built and run locally during development,
confirmed to serve the full app correctly. The CI pipeline itself
needed three real fixes before it worked (all found by actually
running it, not by inspection): the original `manylinux_2_28` base
image doesn't build Python with a shared library, which PyInstaller
requires; the replacement `python:3.12-slim-bullseye` image is missing
`binutils` (needed for `objdump`); and files written by the
containerized build step come out root-owned on the host, which broke
the later `appimagetool` step until that step's output directory was
`chown`'d back to the runner user. After those fixes, the actual
`v0.1.1` release asset — the real downloaded `.AppImage`, not just the
raw binary — was run end-to-end and confirmed to correctly serve the
setup wizard, static assets, and icons. This is the one build of the
three with real evidence behind it, not just "CI went green."

## Building the macOS app

The macOS build (`parztream-macos-arm64.dmg`, offered in the main
README as the easiest way for Apple Silicon Mac users to get started)
follows the same overall shape as Windows/Linux, with two
macOS-specific differences worth understanding before touching any of
this.

`packaging/macos/launcher.py` is the PyInstaller entry point, for the
same reasons as the other two launchers — it points
`PARZTREAM_DB_PATH`/`PARZTREAM_CACHE_DIR` at
`~/Library/Application Support/parztream` (the standard macOS location
for this, instead of `%APPDATA%` or the XDG dirs), persists a
generated `PARZTREAM_SECRET_KEY` there too, adds the bundled
`ffmpeg`/`ffprobe` to `PATH`, and opens the default browser once the
server's ready. Same `setdefault`-not-override behavior: set your own
environment variables first for a PIN or other non-default
config.

**Difference 1 — no console by default.** Double-clicking a `.app` on
macOS gives it no visible Terminal window, unlike Windows' `.exe`
console or the AppImage's `Terminal=true` `.desktop` entry. Since this
app's entire "how do I stop it" story is "close the window (or
Ctrl+C)," running invisibly in the background with no window at all
would be a real regression — the only way to stop it would be Activity
Monitor. `packaging/macos/parztream.spec` names the actual compiled
binary `parztream-bin` and sets `CFBundleExecutable` to `parztream`;
the build workflow then copies `packaging/macos/parztream-wrapper.sh`
into `Contents/MacOS/parztream` — a small script that uses `osascript`
to open a real Terminal window and run `parztream-bin` inside it. This
is why there are two binaries inside the `.app`, and why the wrapper
script is a committed file rather than something PyInstaller generates.

**Difference 2 — no LGPL ffmpeg source for macOS.** The Windows/Linux
builds bundle a specifically LGPL static ffmpeg build (see
[Building the Windows .exe](#building-the-windows-exe)) so parztream
never distributes GPL-only encoders it doesn't need. No equivalent
reliably-available LGPL-only macOS static build exists the same way,
so `.github/workflows/build-macos-app.yml` installs ffmpeg via
Homebrew instead — which, by default, is very likely a **GPL** build
(Homebrew's ffmpeg formula includes x264/x265 and other GPL-licensed
components unless built with nonstandard flags). This is a
**deliberate, documented inconsistency** with the Windows/Linux
policy, not an oversight — if this project ever cares about being
strictly GPL-clean across all three platforms (e.g. before wider
public distribution), this needs real attention: either verify exactly
what Homebrew's ffmpeg formula links against at build time, or source/
build an LGPL-only macOS ffmpeg deliberately.

**Not code-signed or notarized:** producing a signed, notarized `.app`
(so Gatekeeper doesn't block it on first launch) requires an Apple
Developer Program membership ($99/year) plus a notarization step in CI
using Apple credentials. This project doesn't have that, by explicit
choice — the Gatekeeper block is expected and documented in the
README's Troubleshooting section (Control-click → Open, or
`xattr -cr`) rather than something to silently work around.

**Apple Silicon only:** `target_arch="arm64"` in the spec file and the
`macos-14` GitHub Actions runner (natively arm64) both reflect this —
an Intel Mac cannot run this build at all. Revisit if Intel support
ever becomes worth the doubled build matrix.

**Verification status:** no macOS environment was available at all
while writing this, so the `BUNDLE()` Info.plist keys, the
`parztream-wrapper.sh` osascript approach, and the `create-dmg`
invocation were all based on documented behavior only. The `v0.1.1`
release build did succeed in CI on a real `macos-14` runner —
PyInstaller, the wrapper-script swap, and `create-dmg` all completed
without error and produced `parztream-macos-arm64.dmg` — which is
real signal that the mechanics are basically sound (this exact
pipeline failed on its first attempt for an unrelated reason — a
missing `contents: write` permission on the release-attach step — and
that got caught and fixed the same way). What CI success does **not**
confirm: whether the `.dmg` actually mounts correctly, whether the
`.app` actually launches past Gatekeeper as described, or whether the
osascript wrapper actually opens a working Terminal window on real
hardware — none of that runs during a CI build. Treat this as
"builds successfully" rather than "works," until someone actually
opens it on a real Apple Silicon Mac.

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

**Known gaps:** touch targets meet WCAG 2.2's AA minimum (24×24px)
but not the stricter AAA guideline (44×44px) — fixing that properly
needs a broader spacing/layout pass with real-device testing, which
wasn't available while building this. None of the above was verified
with a real screen reader or an automated tool like axe-core either
(no browser automation was available in the environment this was
built in) — it's built correctly per the relevant WCAG success
criteria and carefully reviewed, not screen-reader-tested.

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
