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

## Download and run

No Python, no terminal, nothing to install — pick your platform,
download one file from the
[Releases page](https://github.com/muad-yasin/parztream/releases),
and run it.

### Windows

1. Download **`parztream-windows.exe`**.
2. Double-click it. A window opens with some startup text, and your
   browser opens automatically to your library a moment later.
3. To stop parztream, close that window.

Windows will likely show a blue "Windows protected your PC" warning
the first time — expected for a new app, not a sign anything's wrong;
see [Troubleshooting](#troubleshooting).

Settings and your library are stored in `%APPDATA%\parztream`.

### Linux

1. Download **`parztream-linux-x86_64.AppImage`**.
2. Make it runnable: `chmod +x parztream-linux-x86_64.AppImage` (or
   tick "executable" in your file manager's permissions).
3. Run it — double-click it, or `./parztream-linux-x86_64.AppImage`
   from a terminal. A window opens with startup text, and your
   browser opens automatically (unless you're on a headless server —
   the startup text prints the address to use from another device
   instead).
4. To stop parztream, press Ctrl+C in that window (or close it).

Settings and your library are stored in `~/.local/share/parztream`.

### macOS (Apple Silicon only)

1. Download **`parztream-macos-arm64.dmg`**.
2. Double-click it, then drag **parztream** into your Applications
   folder.
3. Open it from Applications. macOS will likely refuse the first
   time with a Gatekeeper warning — expected for a new, unsigned app;
   see [Troubleshooting](#troubleshooting).
4. A Terminal window opens with startup text, and your browser opens
   automatically. To stop parztream, close that window (or press
   Ctrl+C).

Settings and your library are stored in
`~/Library/Application Support/parztream`. On an older, Intel-based
Mac, this build won't run — see [`ADVANCED.md`](ADVANCED.md) for
running from source instead.

---

Want to run parztream from source (e.g. an Intel Mac, or to
contribute to the code), set a password, or have it start
automatically as a background service? See [`ADVANCED.md`](ADVANCED.md).

## How to use it

- The first time, you'll land on a setup page with a built-in folder
  browser — click through to your movies/shows/music folders and
  save. You never need to type a path.
- parztream scans those folders right away. Once done, you'll see
  your library with thumbnails and a search box — click anything to
  play it. "Scan library" in the header re-scans any time (e.g. after
  adding new files).
- **From another device** (phone, tablet, another computer) on the
  same Wi-Fi, open a browser and go to `http://parztream.local:8000`
  — this works automatically most of the time. If it doesn't, see
  [Troubleshooting](#troubleshooting).
- TV episodes named like `Show Name S01E02...` are grouped
  automatically by show. Subtitle files (`.srt`/`.vtt`) with the same
  name as a video are picked up automatically too.
- On a phone, add parztream to your home screen (browser menu → "Add
  to Home Screen") for a shortcut that looks and feels like a real
  app.

Want a password so only people you trust can see your library? See
[`ADVANCED.md`](ADVANCED.md).

## Running it again later

Just repeat the last step for your platform above — double-click the
same `.exe`/`.AppImage`/`.app` again. Nothing needs reinstalling;
your library and settings are exactly as you left them.

## Troubleshooting

**"Windows protected your PC" / "Unknown publisher" (Windows)**
Expected for a new, unsigned app — not a sign anything's wrong. Click
**"More info"**, then **"Run anyway."**

**"...is damaged and can't be opened" / "Apple could not verify..." (macOS)**
Also expected, for the same reason (an unsigned app). Try, in order:
1. **Control-click (or right-click) the app → Open**, then confirm
   **"Open"** in the dialog that appears.
2. If that still refuses, open **Terminal** (Spotlight search →
   "Terminal") and run:
   ```bash
   xattr -cr /Applications/parztream.app
   ```
   (adjust the path if you put it somewhere other than Applications),
   then try opening it again.

**"Permission denied" running the `.AppImage` (Linux)**
It needs to be marked as runnable first: tick "executable" in your
file manager's Properties/Permissions for the file, or run
`chmod +x parztream-linux-x86_64.AppImage` in a terminal.

**"dlopen(): error loading libfuse.so.2" or similar (Linux)**
Some newer distributions (recent Ubuntu, Fedora) don't ship FUSE by
default, which AppImages normally need. Either install it (e.g.
`sudo apt install libfuse2`, or your distro's equivalent), or run
without it: `./parztream-linux-x86_64.AppImage --appimage-extract-and-run`.

**"Address already in use" / the server won't start**
Something else on your computer is already using port 8000. Close
that other program, then try again.

**Nothing loads in the browser**
Make sure the app's window is still open, with no error message
shown. Try `http://127.0.0.1:8000` as an alternative to `localhost`.

**Other devices (phone, tablet) can't reach it**
Make sure that device is on the *same* Wi-Fi network as the computer
running parztream. If `http://parztream.local:8000` doesn't load,
find that computer's network address instead: run `ipconfig`
(Windows) or `ifconfig`/`ip addr` (macOS/Linux) in a terminal, look
for something like `192.168.1.42`, and try
`http://192.168.1.42:8000` from the other device.

**A video won't play, or shows a "can't play in browser" message**
Some video files use a format your browser can't decode. parztream
offers a download link so you can still get the file either way.

Running from source and hit a Python-specific error instead (like
`command not found` or `No module named`)? See
[`ADVANCED.md`](ADVANCED.md)'s troubleshooting notes for that.

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

Looking for more advanced options — running from source, password
protection, running parztream automatically in the background,
changing where files are stored, or other configuration? See
[`ADVANCED.md`](ADVANCED.md).
