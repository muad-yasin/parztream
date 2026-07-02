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
