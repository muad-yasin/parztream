# parztream

## What is this?

parztream turns a folder of your movies, TV shows, and music into
your own private streaming website — like a Netflix or Spotify, but
for files you already own, and only visible to devices on your own
home network. Nothing gets uploaded to the internet, there's no
monthly fee, and no company can see what you're watching. Once it's
running on one computer in your house, you (and anyone else on your
Wi-Fi) can open a web browser on a phone, tablet, laptop, or smart TV
and watch or listen to whatever's in that folder.

This guide assumes you've never used a "terminal" (also called a
command line or command prompt) before, and walks through every step.

## Windows: the easy way (no installation needed)

If you're on Windows, you can skip everything below and just run a
single file — no Python, no terminal, nothing to install.

1. Go to the [Releases page](https://github.com/muad-yasin/parztream/releases)
   and download the latest **`parztream-windows.exe`**.
2. Double-click it. A window opens showing some startup text, and
   your web browser opens automatically to your library a moment
   later.
3. That's it — parztream is running. To stop it, just close that
   window.

Windows may show a blue "Windows protected your PC" warning the first
time you run it — that's expected for a new app like this one, not a
sign anything's wrong; see [Troubleshooting](#troubleshooting) below
for why, and how to get past it.

Your settings, library database, and cache are stored in
`%APPDATA%\parztream`, so they survive being closed and reopened.
Want it to start automatically when your computer turns on, run as a
full background service, or set a password? See
[`ADVANCED.md`](ADVANCED.md).

## Linux: the easy way (no installation needed)

If you're on Linux, you can also skip everything below and run a
single file — no Python, no virtual environment, nothing to install.
This works on essentially any 64-bit Linux desktop or server.

1. Go to the [Releases page](https://github.com/muad-yasin/parztream/releases)
   and download the latest **`parztream-linux-x86_64.AppImage`**.
2. Make it runnable (most file managers offer this as a right-click
   "Properties → Permissions" checkbox; from a terminal it's
   `chmod +x parztream-linux-x86_64.AppImage`).
3. Run it — double-click from a file manager, or run it from a
   terminal: `./parztream-linux-x86_64.AppImage`. A window opens
   showing some startup text, and your browser opens automatically to
   your library (on a desktop, that is — see the note below for
   headless servers).
4. That's it — parztream is running. To stop it, press **Ctrl+C** in
   that window (or close it).

If you're running this on a **headless server** (no desktop/monitor —
a common way to run a home media server), the browser obviously won't
open automatically; the startup text tells you the address to open
from another device instead, e.g. `http://<server's address>:8000`.

Your settings, library database, and cache are stored in
`~/.local/share/parztream`, so they survive being closed and reopened.
Want it to start automatically on boot, run as a full background
service, or set a password? See [`ADVANCED.md`](ADVANCED.md).

## macOS: the easy way (no installation needed)

If you're on an Apple Silicon Mac (M1 or newer — the vast majority of
Macs sold since late 2020), you can also skip everything below.

1. Go to the [Releases page](https://github.com/muad-yasin/parztream/releases)
   and download the latest **`parztream-macos-arm64.dmg`**.
2. Double-click the `.dmg` — a window opens; drag the **parztream**
   icon into your **Applications** folder, the way most Mac software
   installs.
3. Open parztream from Applications (or Launchpad). The first time,
   macOS will likely refuse with a warning — that's expected, not a
   sign anything's wrong; see [Troubleshooting](#troubleshooting)
   below for exactly what to click.
4. A Terminal window opens showing some startup text, and your
   browser opens automatically to your library a moment later. To
   stop parztream, close that Terminal window (or press Ctrl+C in it).

This build isn't code-signed (that requires a paid Apple Developer
account), so macOS's Gatekeeper will block it on first launch — see
[Troubleshooting](#troubleshooting) for the one-time steps to get past
that.

Your settings, library database, and cache are stored in
`~/Library/Application Support/parztream`, so they survive being
closed and reopened. Want it to start automatically at login, or set
a password? See [`ADVANCED.md`](ADVANCED.md).

On an older, Intel-based Mac? This build won't run — use the
from-source steps below instead.

**Everyone else** (an Intel Mac, or anyone who'd rather run it from
source) — follow the steps below instead.

## What you'll need before starting

- **A computer to run it on.** This computer becomes the "server" —
  the one that stores your media files and needs to stay turned on
  while you (or anyone else) want to watch or listen. It can run
  Windows, macOS, or Linux.
- **Python, version 3.10 or newer.** Python is the programming
  language parztream is written in — think of it as a required piece
  of plumbing, similar to how you need a PDF reader installed to open
  a PDF. Download it for free from
  [python.org/downloads](https://www.python.org/downloads/).
  - **On Windows:** during installation, make sure to tick the
    checkbox that says **"Add Python to PATH"** — it's easy to miss,
    and without it, none of the steps below will work.
  - **On macOS:** the installer from python.org is the easiest route;
    macOS ships with an older Python that won't work here.
  - **On Linux:** most distributions already have Python installed —
    check with the command in Step 1 below before installing anything.
- **(Optional, but recommended) ffmpeg.** A free tool that lets
  parztream generate video thumbnails, show video length, and play a
  wider range of video files. parztream works without it — you'll
  just get plainer video listings and slightly less compatibility.
  Install instructions: [ffmpeg.org/download.html](https://ffmpeg.org/download.html).
  You can always come back and install this later.
- **About 10 minutes**, and a willingness to copy and paste a few
  lines of text into a black window. You don't need to understand
  what they do — this guide explains each one.

## Step-by-step installation

### Step 1: Get the project files onto your computer

You have two options — pick whichever sounds easier.

**Option A — Download the ZIP file (simplest, no extra tools needed)**

1. Go to the project's page: <https://github.com/muad-yasin/parztream>
2. Click the green **"Code"** button, then click **"Download ZIP"**.
3. Find the downloaded file (usually in your Downloads folder) and
   unzip it — on Windows, right-click it and choose "Extract All"; on
   macOS, just double-click it. Put the resulting folder somewhere
   you'll remember, e.g. your Documents folder.

**Option B — Clone it with git (only if you already have git installed)**

```bash
git clone https://github.com/muad-yasin/parztream.git
```

This downloads the same project files using `git`, a tool many
programmers use for tracking code changes. If you don't already have
`git`, use Option A instead — there's no need to install it just for
this.

### Step 2: Open a terminal in the project folder

The "terminal" (or "command prompt") is a plain-text window where you
type commands instead of clicking things. Every step from here on
happens inside it.

- **Windows:** Open the folder you just created (File Explorer),
  click once in the address bar at the top, type `cmd`, and press
  Enter. A black window opens already pointed at that folder.
- **macOS:** Open the folder in Finder, then go to
  **Finder → Services → New Terminal at Folder** (or open the
  Terminal app from Applications → Utilities, then type `cd ` followed
  by dragging the folder into the window, then press Enter).
- **Linux:** Most file managers have a "Open Terminal Here" option in
  the right-click menu for a folder.

To double check you're in the right place and Python is installed,
type this and press Enter:

```bash
python3 --version
```

*(On Windows, if that says it's not recognized, try `python --version`
instead — Windows sometimes uses `python` without the `3`.)*

You should see something like `Python 3.12.4`. If instead you see an
error, see [Troubleshooting](#troubleshooting) below.

### Step 3: Create a private space for parztream's files

This step creates what's called a "virtual environment" — think of it
as giving parztream its own private toolbox, so the pieces it needs
don't get mixed up with anything else on your computer.

```bash
python3 -m venv .venv
```

*(Use `python` instead of `python3` here too if that's what worked in
Step 2.)*

Nothing visible happens — that's normal, it just created a new hidden
folder called `.venv`. Now "activate" it (turn that toolbox on for
this terminal window):

- **Windows:**
  ```bash
  .venv\Scripts\activate
  ```
- **macOS / Linux:**
  ```bash
  source .venv/bin/activate
  ```

You'll know it worked because you'll see `(.venv)` appear at the
start of the line in your terminal. You'll need to repeat this
"activate" step (only this step, not Steps 1–3 as a whole) every time
you come back to run parztream later.

### Step 4: Install what parztream needs to run

```bash
pip install -r requirements.txt
```

This tells Python to download and set up all the pieces parztream
depends on. You'll see a bunch of text scroll by — that's normal.
Depending on your internet connection, this can take anywhere from a
few seconds to a couple of minutes.

*(If this fails with something like "pip: command not found," try
`python3 -m pip install -r requirements.txt` instead.)*

### Step 5: Start the server

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

This starts parztream itself. You'll see a few lines of text appear
and then the terminal will just sit there — that's correct, it means
it's running and waiting. **Leave this window open** — closing it
stops parztream (see [Stopping the server](#stopping-the-server--restarting-later)
below for how to do that on purpose).

## How to use it

1. On the same computer, open a web browser and go to:
   **`http://localhost:8000`**
2. The first time you visit, you'll land on a setup page with a
   built-in folder browser. Click through your folders to find where
   your movies, shows, or music live — you never need to type a file
   path. Select one or more folders and save.
3. parztream will scan those folders right away. Once it's done,
   you'll see your library — thumbnails, titles, and a search box.
   Click anything to play it.
4. **From another device** (a phone, tablet, another computer) on the
   same Wi-Fi network, open a browser and go to:
   **`http://parztream.local:8000`**
   This works automatically most of the time. If that address doesn't
   load, you can instead use the server computer's network address —
   see [Troubleshooting](#troubleshooting) for how to find it.
5. TV episodes named like `Show Name S01E02...` are automatically
   grouped by show and put in the right order. Subtitle files
   (`.srt`/`.vtt`) with the same name as a video are picked up
   automatically too.
6. On a phone, you can add parztream to your home screen (via your
   browser's menu, usually "Add to Home Screen") for a shortcut that
   looks and feels like a real app.

Want a password so random people on your Wi-Fi can't see your library
(useful if you live with roommates, or your network isn't fully
private)? That, along with a few other optional settings, is covered
in [`ADVANCED.md`](ADVANCED.md).

## Stopping the server / restarting later

**To stop parztream:** click into the terminal window where it's
running, and press **Ctrl+C** (this is the same on Windows, macOS,
and Linux). The server shuts down; your media files and library
information aren't affected.

**To start it again later** (you only need to do this — not the whole
installation again):

1. Open a terminal in the project folder (Step 2 above).
2. Activate the private toolbox from Step 3:
   - Windows: `.venv\Scripts\activate`
   - macOS/Linux: `source .venv/bin/activate`
3. Start it again:
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```

That's it — no need to reinstall anything. If you'd rather parztream
start automatically whenever your computer turns on, without you
opening a terminal each time, see [`ADVANCED.md`](ADVANCED.md).

## Troubleshooting

**"Windows protected your PC" / "Unknown publisher" when running `parztream-windows.exe`**
This is normal for a new app and doesn't mean anything's wrong with
the file. It appears because getting an app "recognized" by Windows
requires a paid code-signing certificate, which this project doesn't
have. Click **"More info"**, then **"Run anyway."** If you'd rather
avoid that warning entirely, you can run parztream from source instead
using the steps below.

**"parztream.app is damaged and can't be opened" / "Apple could not verify... malware" on macOS**
This is Gatekeeper, macOS's version of the Windows warning above —
also expected for a new, unsigned app, not a sign anything's actually
wrong or damaged. Try, in order:
1. In Finder, **Control-click (or right-click) the app → Open**, then
   confirm **"Open"** in the dialog that appears. This works on most
   macOS versions for a first launch.
2. If that still refuses, open **Terminal** (Spotlight search →
   "Terminal") and run:
   ```bash
   xattr -cr /Applications/parztream.app
   ```
   (adjust the path if you put it somewhere other than Applications),
   then try opening it again normally.

**"Permission denied" when running the `.AppImage` on Linux**
It needs to be marked as runnable first — either tick the
"executable" checkbox in your file manager's Properties/Permissions
for the file, or run `chmod +x parztream-linux-x86_64.AppImage` in a
terminal, then try again.

**"dlopen(): error loading libfuse.so.2" or similar when running the `.AppImage`**
Some newer Linux distributions (recent Ubuntu, Fedora) don't ship
FUSE by default, which AppImages normally need to run. Either install
it (e.g. `sudo apt install libfuse2` on Ubuntu/Debian, or the
equivalent for your distro), or run the AppImage without FUSE by
adding a flag: `./parztream-linux-x86_64.AppImage --appimage-extract-and-run`.

**"python3 is not recognized" / "python: command not found"**
Python either isn't installed, or wasn't added to your system's PATH
during installation. Reinstall from
[python.org/downloads](https://www.python.org/downloads/) and, on
Windows, make sure to tick "Add Python to PATH." Also try the other
name (`python` instead of `python3`, or vice versa).

**"pip is not recognized" / "pip: command not found"**
Use `python3 -m pip install -r requirements.txt` (or
`python -m pip install -r requirements.txt` on Windows) instead of
plain `pip`.

**"Address already in use" / the server won't start**
Something else on your computer is already using port 8000 (a common
default for lots of software). Either close that other program, or
start parztream on a different port:
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8001
```
and then visit `http://localhost:8001` instead.

**Nothing loads in the browser**
Double-check the terminal window from Step 5 is still open and
running (no error message, no "Ctrl+C" pressed). Try
`http://127.0.0.1:8000` as an alternative to `localhost`.

**"No module named ..." error when starting the server**
Your private toolbox from Step 3 probably isn't turned on for this
terminal window. Re-run the "activate" command
(`source .venv/bin/activate` or `.venv\Scripts\activate`) and try
Step 5 again.

**Other devices (phone, tablet) can't reach it**
Make sure that device is on the *same* Wi-Fi network as the server
computer. If `http://parztream.local:8000` doesn't load, find the
server's network address instead: on the server computer, run
`ipconfig` (Windows) or `ifconfig`/`ip addr` (macOS/Linux) in a
terminal, look for something like `192.168.1.42`, and try
`http://192.168.1.42:8000` from the other device.

**A video won't play, or shows a "can't play in browser" message**
Some video files use a format your browser can't decode. Installing
`ffmpeg` (see "What you'll need," above) fixes most cases
automatically. For the ones it can't fix, parztream offers a
download link so you can still get the file.

Still stuck? See the next section.

## Where to get help

If something's not working, or you think you've found a bug, please
open an issue on GitHub:

**<https://github.com/muad-yasin/parztream/issues>**

Click "New issue," and include:
- What you were trying to do
- What you expected to happen
- What happened instead (copy-paste any error message you saw)
- Your operating system (Windows/macOS/Linux)

There are no silly questions — if the instructions above were
confusing anywhere, that's useful to know too.

---

Looking for more advanced options — password protection, running
parztream automatically in the background, changing where files are
stored, or other configuration? See [`ADVANCED.md`](ADVANCED.md).
