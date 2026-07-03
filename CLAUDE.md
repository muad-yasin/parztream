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

# Run the dev server (reload on change) -- PARZTREAM_MEDIA_DIRS is now
# optional; without it you land on /setup.html to pick folders via a
# built-in browser instead, and the choice persists in the DB
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
  `app/transcode.py`'s remux cache and its optional size cap;
  `PARZTREAM_MDNS_ENABLED`/`PARZTREAM_MDNS_HOSTNAME`/`PARZTREAM_PORT`
  for `app/mdns.py` — `PORT` is purely informational, since the app
  has no way to introspect what port `uvicorn` was actually started
  with, it just needs to be kept in sync manually with `--port`).
  Nothing else in the app should read `os.environ` directly. Also
  owns `AUDIO_EXTENSIONS`/`VIDEO_EXTENSIONS` — if you add a new
  extension here, check whether `mimetypes.guess_type()` actually
  knows it (many don't, e.g. `.m4b`); if not, register an override
  with `mimetypes.add_type()` right here too, otherwise streaming
  falls back to `application/octet-stream` and browsers won't play
  it even though the file scans in fine. `MEDIA_DIRS` here is only
  ever a *fallback default* now — see `app/settings.py`.
- `app/settings.py` — mutable, DB-backed settings (currently just
  `media_dirs`), for things a non-technical user configures through
  the web UI (`/setup.html`) instead of environment variables.
  `get_media_dirs()` reads the `settings` table, falling back to
  `config.MEDIA_DIRS` if nothing's been saved there yet; `set_media_dirs()`
  upserts it. This is a *live* lookup, not a module-level constant like
  `config.MEDIA_DIRS` — a folder change through `/setup.html` takes
  effect on the next scan without restarting the process. Anything
  that needs the current media directories should call
  `settings.get_media_dirs()`, never import `config.MEDIA_DIRS`
  directly (that would silently ignore anything saved via setup).
- `app/db.py` — raw `sqlite3` access via a `get_connection()`
  context manager (opens, commits on success, always closes). Two
  tables: `media` and `settings` (a plain key/value store, currently
  just holding `media_dirs` as a JSON-encoded list). No migrations
  system yet — schema changes mean editing `SCHEMA` in this file
  (existing dev DBs need to be deleted and rescanned).
- `app/scanner.py` — walks `settings.get_media_dirs()` via
  `os.walk(..., followlinks=False)` (not `Path.rglob`, deliberately: a
  plain glob would follow symlinks), and explicitly skips any entry
  where `path.is_symlink()` is true.
  Confirmed live before this existed: a symlink named e.g. `song.mp3`
  inside a scanned folder, pointing anywhere on disk, got scanned and
  fully served through the streaming endpoint regardless of what it
  actually pointed to. Don't reintroduce `rglob`/`glob` here without
  re-adding an equivalent symlink check — both the file-level check and
  `followlinks=False` are load-bearing, not redundant (one blocks
  symlinked *files*, the other blocks descending into symlinked
  *directories*). Classifies files as audio/video by extension,
  extracts metadata (`mutagen` for audio tags; a single `ffprobe
  -show_entries format=duration:stream=...` JSON call for video
  duration *and* the first video/audio stream's codec name, stored as
  `video_codec`/`audio_codec` — both degrade gracefully to
  `None`/filename if ffprobe is unavailable; a regex,
  `_parse_show_episode`, against the filename stem for `show_name`/
  `season_number`/`episode_number` — only recognizes the "Show Name
  S01E02" convention, anything else stays ungrouped rather than
  guessing), and upserts into `media` by path. Also deletes DB rows
  for files no longer found on disk. This is the only place
  file-metadata extraction happens. `_extract_metadata` returns a
  dict, not a positional tuple — it kept growing fields (this is its
  3rd extension) and a dict is far less fragile to extend/mock in
  tests than a positional tuple; follow that pattern rather than
  reverting to positional if you add another field.
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
  clamped to `[1, MAX_PAGE_SIZE]`. It also takes an optional
  `show_name` filter, which switches ordering from alphabetical
  `title` to `season_number, episode_number` — episode order only
  makes sense once you've already filtered to one show — and an
  optional `q` search param, a plain `LIKE '%q%'` across
  title/artist/album/show_name (SQLite's `LIKE` is case-insensitive
  for ASCII by default; not worth FTS5 for a personal media library's
  row counts). Not escaping literal `%`/`_` in `q` is a deliberate cut
  — worst case is an overly broad match, never a correctness/security
  issue. `GET
  /api/shows` is a separate, deliberately *un*paginated endpoint
  (grouped/aggregated via `GROUP BY show_name`, and the number of
  distinct shows is inherently much smaller than the episode count —
  pagination would be overkill there). `GET /api/library/{id}/art`
  serves art: `get_cover_art` (audio, uncached) or
  `get_video_thumbnail` (video, cached — see below), 404s if neither
  finds anything. `GET /api/library/{id}/subtitles` serves
  `app/subtitles.py`'s WebVTT conversion, 404s for audio or if there's
  no sidecar file.
- `app/routers/setup.py` — `GET /api/setup/status` (`{"configured": bool}`,
  backed by `settings.is_configured()`), `GET /api/setup/browse?path=`
  (lists subdirectories of `path`, or a platform-appropriate default —
  `Path.home()` on POSIX, first available drive letter on Windows —
  when `path` is omitted; hidden dirs and non-directories are filtered
  out), and `POST /api/setup` (body: `{"media_dirs": [...]}, validates
  each path is a real directory, calls `settings.set_media_dirs()`,
  then triggers a background scan the same way `POST /api/scan` does).
  No path restrictions on what `/browse` can list beyond "must be a
  real directory" — once past auth (if configured), the setup UI can
  see the whole filesystem, same trust boundary as the rest of the
  app; this is a deliberate choice, not an oversight, consistent with
  the "you're the admin" model everywhere else here. Worth remembering:
  `/setup.html`/`/api/setup/*` are reachable with **no auth at all**
  during the specific window where `PARZTREAM_PIN` isn't set yet
  and folders haven't been configured — that's the existing
  no-auth-by-default behavior extended to setup, not a new gap.
- `app/artwork.py` — two independent functions, kept separate rather
  than unified because their cost profiles are opposite. `get_cover_art`
  pulls embedded art out of audio files (ID3 `APIC` for mp3, `covr`
  for MP4-family containers, FLAC `pictures`) via `mutagen`, re-reading
  the file fresh on every request -- cheap (just tag parsing), so
  there's no reason to cache it. `get_video_thumbnail` grabs a real
  frame via `ffmpeg` (seeking to `min(10s, 10% of duration)`, since
  frame 0 is often a black/blank intro) — genuinely expensive compared
  to tag-reading, so unlike cover art it *is* cached, to
  `CACHE_DIR/{id}_thumb.jpg`, sharing the same cache directory and
  pruning as `app/transcode.py`'s remuxed videos. Its whole body runs
  inside `cache.lock_for(str(thumb_path))` for the same reason
  `_get_or_create_remux` does — see `app/cache.py`.
- `app/cache.py` — `prune(protect)`, extracted out of
  `app/transcode.py` once `app/artwork.py`'s video thumbnails became
  a second thing writing into `CACHE_DIR` — both now share one budget
  rather than each tracking their own. Same protect-the-just-created-
  file behavior as before extraction. Tolerant of files disappearing
  mid-run (`FileNotFoundError` on `.stat()` during listing is caught;
  `.unlink()` uses `missing_ok=True`) — two different resources finishing
  and pruning around the same time, each unaware of the other, is a real
  scenario, not a hypothetical one, now that cache creation is locked
  per-resource (see `lock_for` below) rather than globally.
  `lock_for(key)` returns a `threading.Lock` for a given key (a cache
  path, as a string), creating one on first use and never removing it.
  `app/transcode.py`'s `_get_or_create_remux` and `app/artwork.py`'s
  `get_video_thumbnail` each wrap their *entire* check-cache/create-if-
  missing body in `with cache.lock_for(str(output_path)):`. This closes
  a confirmed live bug: without it, concurrent requests for the same
  not-yet-cached resource (e.g. two devices pressing play around the
  same time) each spawned their own `ffmpeg` process racing to write the
  identical output path, and different clients received measurably
  different byte content (different checksums) for what should have
  been one canonical file — not just wasted CPU, an actual correctness
  bug. `tests/test_transcode.py`/`test_artwork.py` have a regression
  test for this using real `threading` (not mocks pretending to be
  concurrent) that fails with `call_count == 8` if the lock is removed —
  keep that test if you ever touch this locking.
- `app/subtitles.py` — looks for a same-stem `.vtt`/`.srt` sidecar
  file next to the video (`find_subtitle_path`; `.vtt` preferred, no
  conversion needed). `.srt` gets converted to WebVTT via a regex
  scoped specifically to `HH:MM:SS,mmm` timestamps (`_srt_to_vtt`) —
  deliberately narrow so it never touches a comma inside actual
  dialogue text. No caching here, unlike thumbnails/remux: it's plain
  text regex substitution, cheap enough to redo on every request.
  Only one subtitle track is supported (no per-language selection) —
  a real scope cut, not an oversight.
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
  play them at all. `?original=1` bypasses `resolve_playable_path`
  entirely and serves the source file's raw bytes with
  `Content-Disposition: attachment` (RFC 5987-encoded filename via
  `urllib.parse.quote`, not a naive f-string into the header — avoids
  header injection from unusual filenames). This exists because the
  frontend's "download instead" link for a `415` hits this *same*
  endpoint — without a bypass it would 415 too, since it'd re-run the
  same compatibility check that just failed, leaving no way to get an
  unsupported-codec file's bytes out of parztream at all. Found live
  against a real 29GB HEVC file before this existed: in-browser
  playback correctly blocked, but the "download instead" fallback was
  actually broken.
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
  time real playback starts. Calls `cache.prune()` right after writing
  a new file (see `app/cache.py`) — an evicted file isn't a loss, just
  a cache miss on next play (cheap to re-derive, unlike the original
  scan metadata).
- `app/auth.py` — `SessionAuthMiddleware`, a pure ASGI middleware (not
  `BaseHTTPMiddleware`, which buffers `StreamingResponse` bodies —
  that would hurt streaming large files). Replaced `BasicAuthMiddleware`
  (HTTP Basic Auth, which gave every visitor the browser's native,
  unbranded credential popup) with a signed session cookie set by a
  real login page — non-technical-user UX was the whole motivation, see
  git history around "real login page" for the fuller reasoning.
  Gates the entire app uniformly except `PUBLIC_PATHS`
  (`/login.html`, `/api/login`, plus `/manifest.json`/`/icon-192.png`/
  `/icon-512.png`/`/favicon-32.png` — deliberately minimal otherwise;
  `login.html` is fully self-contained with inline CSS/JS specifically
  so nothing else needs to be added here. The icon/manifest files are
  the one deliberate exception: `login.html` links to them so the tab
  icon and "Add to Home Screen" work even before logging in, and
  there's nothing sensitive in static branding images to justify
  gating them. Confirmed live: without this, those links 401'd on the
  one page that's supposed to work pre-auth — regression test in
  `tests/test_auth.py`). No-ops entirely if `PARZTREAM_PIN`
  isn't set, same as before. Distinguishes a real browser navigation
  from a `fetch()`/`<img>`/`<video>` request via the `Accept` header
  (`text/html` → `302` redirect to `/login.html?next=<original path>`;
  anything else → `401` JSON) — don't "simplify" this to always redirect
  or always 401, both branches are load-bearing: a JS `fetch()` getting
  an HTML redirect body where it expects JSON would break silently, and
  a top-level page load getting a bare `401` would show raw JSON
  instead of the login page.
  Sessions are itsdangerous-signed cookies (`URLSafeTimedSerializer`,
  keyed by `config.SECRET_KEY`) carrying no real payload beyond a fixed
  marker string + timestamp — verified via signature + `SESSION_MAX_AGE`
  (90 days), not looked up against any server-side store. That means
  logout (`app/routers/login.py`) only works by telling the *client* to
  stop sending the cookie (`Response.delete_cookie`) — there's no
  revocation list, so a copied cookie value stays valid until it
  expires on its own regardless of logout, and changing
  `PARZTREAM_PIN` does **not** invalidate already-issued sessions
  (only rotating `SECRET_KEY` does, since that's what sessions are
  signed against, not the PIN). This is a known, accepted
  trade-off for a stateless design, not an oversight — document it if
  you touch this, don't silently "fix" it into a stateful store without
  discussing the trade-off first.
  No `Secure` flag on the cookie: parztream runs over plain HTTP by
  design (see README), and a `Secure` cookie is never sent back at all
  over a non-HTTPS connection — setting it would silently break every
  login.
  Login is gated by a 4-digit **PIN** (`config.AUTH_PIN`,
  `PARZTREAM_PIN`), not an arbitrary-length password — chosen
  deliberately for faster entry on a phone/TV remote, since the
  realistic threat model here is someone already on the home LAN, not
  a remote attacker. Because a PIN's keyspace (10,000 combinations) is
  small enough to brute-force quickly without a password's entropy,
  `auth.py` also tracks failed attempts per client IP
  (`_login_attempts`, in-process/module-level like `app/scanner.py`'s
  scan lock — same single-process caveat applies) and locks out further
  attempts for `_LOCKOUT_SECONDS` (30s) after `_MAX_ATTEMPTS` (5)
  consecutive failures from the same address; a successful login clears
  that address's count. This is throttling, not real brute-force
  protection (it resets on restart, and doesn't survive a distributed
  attempt from multiple addresses) — proportionate to a home-LAN threat
  model, not meant to be bulletproof.
- `app/routers/login.py` — `POST /api/login` (checks the lockout via
  `auth.seconds_until_unlocked` before even looking at the submitted
  PIN, then `auth.check_pin`, then sets the session cookie),
  `POST /api/logout` (clears it). References `auth.AUTH_PIN` via the
  `auth` module object (`from .. import auth`, then `auth.AUTH_PIN` at
  call time) rather than `from ..auth import AUTH_PIN` — the latter
  would bind its own independent copy at import time that tests
  monkeypatching `auth.AUTH_PIN` wouldn't reach. Same "quirk" as
  `config.py`'s other consumers, documented further down. The lockout
  key is `request.client.host`, not the session (there isn't one yet
  at login time) — falls back to a shared `"unknown"` bucket if
  `request.client` is `None` (some ASGI transports don't set it),
  which just means everyone on such a setup shares one lockout counter
  rather than the endpoint crashing.
- `app/network.py` — `get_local_ip()`, the classic "connect a UDP
  socket to an external address and read what local address the OS
  picked" trick for guessing this machine's LAN-facing IP. UDP
  `connect()` never actually sends a packet (connectionless), so this
  doesn't need real connectivity to work, just a configured route.
- `app/mdns.py` — advertises `http://<MDNS_HOSTNAME>.local:<PORT>/`
  over mDNS via the `zeroconf` package, so LAN devices that support it
  can reach the server without knowing its IP (see README's "Finding
  the server" for the real per-platform support picture — it's
  genuinely inconsistent on Windows/Android, not just theoretically).
  **`start_mdns()` spins up a background `threading.Thread` rather
  than registering inline — this is load-bearing, not a style choice.**
  Confirmed live: calling `zeroconf`'s sync `register_service()`
  directly from inside the FastAPI lifespan reliably raises
  `zeroconf._exceptions.EventLoopBlocked`, because zeroconf's sync API
  schedules work on its own internal event loop via
  `run_coroutine_threadsafe` and waits for it, and doing that from a
  thread that's already running *another* event loop (uvicorn's, which
  the lifespan runs on) contends and times out. A plain background
  thread sidesteps it entirely. If you ever refactor this, re-verify
  against a real running server (`tests/test_mdns.py`'s mocked tests
  wouldn't have caught this — only the real round-trip test would, and
  only if it were run against a genuinely async-hosted app; `tests/`
  uses `TestClient`, which never triggered this because it runs
  its own event loop differently than uvicorn does under `--reload`).
  Registration failure of any kind is always non-fatal — logs a
  warning, never raises — the server works fine by IP either way.
  `stop_mdns()` unregisters + closes on app shutdown; also safe to
  call if `start_mdns()` never successfully registered.
- `app/main.py` — wires routers and mounts `static/` at `/`. Route
  registration order matters: API routers are included *before* the
  `StaticFiles` mount, since the static mount is a catch-all at `/`.
  `SessionAuthMiddleware` is added at app level so it covers everything
  behind it. Explicitly attaches a `StreamHandler` to the `"parztream"`
  logger and sets `propagate = False` — without an actual handler,
  Python's logging module only surfaces `WARNING`+ via its built-in
  fallback ("handler of last resort"), regardless of what level is set
  on the logger itself. Confirmed live: the `PARZTREAM_PIN` unset
  warning showed up in the console fine, but `app/mdns.py`'s
  successful-registration `info()` log line silently didn't, which
  looked exactly like mDNS had failed even though it hadn't. If you
  add more `logger.info()` calls elsewhere expecting them to be
  visible, they'll work now that this is fixed — don't rediscover this
  the hard way.
- `static/` — plain JS, no bundler. `app.js` fetches `/api/library`
  (with `limit`/`offset`/`q`, tracked in module-level `offset`/search-
  input state, reset to 0 on filter change, show-select change,
  search input, or after a scan), renders each row as `<li><button
  class="row-btn">...` — a real `<button>`, not a click handler on the
  `<li>` (see "Accessibility" below for why that distinction matters)
  — with a lazy-loaded, `alt=""` (decorative — the adjacent text label
  already names the item) `<img src="/api/library/{id}/art">` (hidden
  via `onerror` if 404), prefixed with `S{season}E{episode}` when
  `show_name` is set, or an "empty" message (different text for "no
  media scanned yet" vs. "no search results") when the list is empty.
  The search `<input>` is debounced 300ms (`searchDebounceTimer`) so
  it doesn't fire a request per keystroke. The shows `<select>` is
  repopulated from `GET /api/shows` on load and after each scan.
  `playMedia` probes
  `/api/stream/{id}` with a tiny `Range: bytes=0-1` request first —
  this both warms the transcode cache before real playback starts and
  lets a `415` (unsupported video codec) show a "download instead"
  message (link uses `?original=1`, *not* the bare stream URL — see
  `app/routers/stream.py`) rather than a silent `<video>` failure —
  before pointing an `<audio>`/`<video>` element at the same URL,
  plus a `<track>` for
  video pointed at `/api/library/{id}/subtitles` (no pre-check needed
  here — a 404 on a `<track src>` just gets ignored by the browser,
  unlike a `<video src>` 415). Also polls `/api/scan/status` after
  triggering a scan (the trigger endpoint returns immediately, it
  doesn't wait for the scan to finish). `init()` checks
  `GET /api/setup/status` before anything else and redirects to
  `/setup.html` if unconfigured — every other function in this file
  assumes at least one media dir already exists, so don't reorder this
  check after the rest of startup.
  `setup.html`/`setup.js` are a self-contained folder-browser page
  (separate from `index.html`, not a view inside the main SPA-ish
  page) — click a name in `#folder-list` to descend, "Add this folder"
  to select the *currently browsed* path (these are different actions:
  clicking navigates, the button selects), remove selected folders
  individually, "Save & start scanning" `POST`s to `/api/setup` and
  redirects to `/` on success. Deliberately no path-typing fallback —
  browse-only, since the whole point is not requiring the user to know
  an exact filesystem path.
  `login.html` is standalone, not a view inside `index.html` — inline
  `<style>`/`<script>`, no dependency on `style.css`/`app.js`, so it
  never needs to be in `auth.PUBLIC_PATHS` beyond itself. PIN-only
  form: a single `type="password"` input constrained to 4 digits
  (`inputmode="numeric"`, digit-stripping on `input` so a physical
  keyboard can't type non-digits into it) that auto-submits
  (`form.requestSubmit()`) once 4 digits are entered — no separate
  submit tap needed, matching the phone-lock-screen convention this is
  deliberately modeled on. A `429` from `/api/login` (rate-limited, see
  `app/auth.py`) is shown with the server's own message, which includes
  seconds-remaining. Reads `?next=` from its own URL to return you to whatever page
  triggered the redirect, but only if it's a relative path
  (`next.startsWith("/")`) — guards against an open-redirect via a
  crafted link with `?next=https://evil.example`. The header's
  "Log out" button `POST`s `/api/logout` then sends you to
  `/login.html` directly.

  **Accessibility conventions, apply these to any new interactive UI:**
  - Any clickable row/list item must be a real `<button>` wrapping the
    row's content (class `row-btn`, styled in `style.css` to look like
    a plain row — no default button chrome), never a `click` handler
    on a non-interactive element like `<li>`/`<div>`. This is a
    confirmed-real fix, not theoretical: before this, list rows were
    entirely unreachable by keyboard.
  - Icon-only controls (no visible text, e.g. `setup.html`'s `⬆` up
    button) need an explicit `aria-label` — a `title` attribute alone
    isn't reliably exposed to screen readers and isn't visible until
    hover for sighted users either.
  - Any `<input>`/`<select>` needs a real `<label>`, even if it's
    visually hidden via the `.sr-only` class (defined in `style.css`
    and, since `login.html` doesn't link that stylesheet, duplicated
    inline there too) — a placeholder is not an accessible-name
    substitute, and disappears once the user starts typing anyway.
  - Content that changes without a page navigation (scan status,
    search result counts, player state) should call `app.js`'s
    `announce(message)` helper, which writes into the hidden
    `#status-announcer` (`aria-live="polite"`) — otherwise screen
    reader users get no feedback that anything happened. `setup.html`
    has its own local live regions instead (`#current-path` as
    `aria-live="polite"`, `#setup-error` as `role="alert"`) since it
    doesn't share `app.js`.
  - `<img>` elements need an explicit `alt` — `alt=""` when the image
    is purely decorative/redundant with adjacent text (e.g. the
    library thumbnails, since the title label right next to them
    already identifies the item), a real description otherwise. Never
    leave `alt` unset.
  - Color contrast in the existing dark palette (`#14161a`/`#1e2127`/
    `#23262c` backgrounds, `#e6e6e6`/`#9aa0aa` text, `#7aa2f7` links,
    `#f78c8c` errors) was verified by calculating actual WCAG contrast
    ratios, not eyeballed — all pass AA, most pass AAA. If you add a
    new color, check it the same way rather than assuming a dark theme
    is automatically fine.
  - None of this was tested with a real screen reader or an automated
    tool (axe-core, Lighthouse) — no browser automation was available
    in the environment this was built in. It's built correct per the
    relevant WCAG success criteria and carefully code-reviewed, that's
    a different (weaker) claim than "verified" — don't oversell it as
    tested if you're asked about it later.

  **Mobile/PWA conventions:**
  - The header (`header`/`.controls` in `style.css`) reflows to a
    single column below 640px via a `@media (max-width: 640px)` block
    — the flex layout stays row-based above that width. `.row-label`
    (library row titles) gets `text-overflow: ellipsis` so a long
    title/show name can't blow out the row width on a narrow screen.
  - Fullscreen-on-play (`app.js`'s `requestVideoFullscreen`) is gated
    on `window.matchMedia("(pointer: coarse)").matches`
    (`isTouchDevice`), not a viewport-width check — a narrow desktop
    window shouldn't trigger phone-style fullscreen, and a large
    touchscreen tablet should. Tries the standard `el.requestFullscreen()`
    first, falls back to `el.webkitEnterFullscreen()` for iOS Safari
    (which doesn't implement the standard Fullscreen API for
    `<video>` at all). Both paths are wrapped so a rejection/exception
    never blocks playback — fullscreen is a nice-to-have, not a
    requirement for the video to work.
  - `static/manifest.json` + `static/icon-192.png`/`icon-512.png`/
    `favicon-32.png` (dark rounded-square background, accent-blue
    play-triangle, matching the app's existing palette) enable "Add to
    Home Screen" with `display: standalone` (hides the browser chrome)
    and a real icon instead of a generic globe. Linked from
    `index.html`/`setup.html`/`login.html` (`<link rel="icon">`,
    `<link rel="apple-touch-icon">`, `<link rel="manifest">`, plus a
    `theme-color` meta tag). The icons were generated with a
    locally-installed Pillow, used only as a one-off image-generation
    tool — it is **not** a project dependency and isn't in either
    requirements file.
  - None of the above (header reflow, fullscreen-on-tap, "Add to Home
    Screen" behavior) has been verified on a real phone — same caveat
    as the accessibility work above, built correct against the
    relevant web platform APIs and reviewed, not device-tested.
- `deploy/` — templates for running as a persistent background
  service (systemd unit + env-file template for Linux, a batch
  script + env-file template for Windows), documented in the
  README's "Running as a service" section. Not installed/enabled
  anywhere by default — these are files to copy onto a target
  machine, not something the app or its tests touch. Real env files
  with actual passwords belong outside the repo (`/etc/parztream/`
  or `C:\ProgramData\parztream\`), never committed — only the
  `.example` templates live in `deploy/`.
- `packaging/windows/` — the Windows `.exe` build (offered in the
  README as the easiest way for non-technical users to get started,
  no Python/terminal needed). `launcher.py`, not `app/main.py`, is the
  actual PyInstaller entry point (`parztream.spec` builds it) — it
  exists to handle things that only matter once parztream is a frozen,
  double-clicked exe: pointing `PARZTREAM_DB_PATH`/`PARZTREAM_CACHE_DIR`
  at a persistent `%APPDATA%\parztream` folder (a PyInstaller onefile
  build's own temp dir, `sys._MEIPASS`, is deleted after every run —
  app/config.py's defaults would silently lose the whole library every
  restart if used as-is here), persisting a generated
  `PARZTREAM_SECRET_KEY` to a file there too (otherwise every launch
  would sign everyone out), adding the bundled `ffmpeg`/`ffprobe` to
  `PATH` (everything in `app/` just calls `"ffmpeg"`/`"ffprobe"` and
  relies on PATH lookup — no code there needed to change), and opening
  the default browser once the server's actually accepting connections
  rather than immediately. These env vars are `setdefault`, not a hard
  override — set them yourself first (e.g. via `setx`) for a password
  or other non-default config, same as running from source.
  `packaging/windows/vendor/ffmpeg/` (gitignored, never committed) is
  where `.github/workflows/build-windows-exe.yml` puts a downloaded
  LGPL static ffmpeg build before invoking PyInstaller — deliberately
  LGPL, not GPL, chosen because parztream never needs GPL-only
  encoders (video is only ever copied, never re-encoded — see
  `app/transcode.py`). The exe is unsigned (no code-signing
  certificate), so Windows SmartScreen shows a warning on first run —
  expected, documented in the README's Troubleshooting section, not a
  bug to silently "fix" by suppressing the warning some other way.
  **This entire pipeline was written without access to a real Windows
  machine.** The `v0.1.1` release build did succeed in CI (PyInstaller
  produced `parztream-windows.exe` with no errors), which confirms the
  spec/hidden-imports list are at least correct enough to build — but
  that's still not the same as confirming the exe actually launches
  and works on a real Windows machine, which nothing in CI checks. The
  first attempt at this build (the `v0.1` tag) failed outright on a
  missing `contents: write` permission for the release-attach step —
  fixed, but a reminder that "should work" and "did work" are
  different claims here. Treat Windows as still unverified end-to-end,
  and re-verify here if you touch the spec file, the hidden-imports
  list, or launcher.py's import-order-dependent env var setup.
- `packaging/linux/` — the Linux build (`parztream-linux-x86_64.AppImage`,
  the same "easiest way to get started" pitch as the Windows exe, for
  Linux users). Same architecture as `packaging/windows/`:
  `launcher.py` is the PyInstaller entry point, not `app/main.py`,
  solving the same class of problems — except it uses the XDG Base
  Directory spec (`$XDG_DATA_HOME`, falling back to
  `~/.local/share/parztream`) instead of `%APPDATA%` for the
  persistent data dir, and its browser-open step is expected to
  silently no-op on a headless server (a genuinely common way this
  specific app gets run on Linux) rather than being treated as a
  failure. AppImage itself is just a packaging format wrapped around
  that same kind of onefile PyInstaller binary — `AppRun` (a one-line
  shell script exec-ing the bundled binary) and `parztream.desktop`
  (`Terminal=true` deliberately, so double-clicking from a file
  manager still shows the startup banner and allows Ctrl+C, matching
  what the README tells users) turn the binary plus
  `static/icon-512.png` into an AppDir, which `appimagetool` packages
  into the final `.AppImage`.
  `.github/workflows/build-linux-appimage.yml` builds the PyInstaller
  binary inside a `python:3.12-slim-bullseye` Docker container (Debian
  11, glibc 2.31) rather than directly on the `ubuntu-latest` runner —
  a binary built against a bleeding-edge runner's glibc can fail to
  even start on an older/stabler distro with a `GLIBC_x.xx not found`
  error. **Not** a `manylinux` image, despite that seeming like the
  obvious choice for "broadly compatible Linux binary" — manylinux
  images build Python without a shared library (they're meant for
  building wheels, not standalone executables), which makes
  PyInstaller fail outright ("Python was built without a shared
  library"). Confirmed live: this broke the actual first release build.
  If a future PyInstaller/Python bump needs a newer glibc baseline,
  move to a newer Debian-based `python:*-slim` tag, not back to
  manylinux. `packaging/linux/vendor/ffmpeg/`
  (gitignored) is where that workflow puts a downloaded LGPL static
  ffmpeg build before invoking PyInstaller, same licensing reasoning
  as `packaging/windows/`.
  Deliberately **no `.deb`/`.rpm`** — decided against them in favor of
  the portable AppImage, since distro-native packaging only benefits
  one distro family at a time and mostly helps people already
  comfortable with a terminal, which cuts against why the exe/AppImage
  exist in the first place (see README's "the easy way" sections).
  Running an AppImage normally needs FUSE, which some modern distros
  (recent Ubuntu, Fedora) don't ship by default — a real rough edge
  the `.exe` doesn't have, documented in the README's Troubleshooting
  section (`libfuse2`, or `--appimage-extract-and-run`) rather than
  something to "fix" by bundling FUSE itself.
  **Fully verified, unlike Windows/macOS**: both the raw local
  PyInstaller binary during development *and* the actual downloaded
  `v0.1.1` release `.AppImage` were run for real and confirmed to
  correctly serve the full app (setup wizard, static assets, icons)
  and write its database/secret key to the right persistent XDG
  folder. Getting the CI build working took three real, only-found-by-
  running-it fixes, in order: `manylinux_2_28` doesn't build Python
  with a shared library (PyInstaller requires one) →  switched to
  `python:3.12-slim-bullseye`; that image is missing `binutils`
  (PyInstaller needs `objdump`) → installed via `apt-get` in the same
  step; files written by the containerized build step come out
  root-owned on the host, which broke the later, non-containerized
  `appimagetool` step with a permission error → `chown -R $(id
  -u):$(id -g)` back to the runner user as the last thing inside the
  container. If you touch the Docker base image here, expect a similar
  class of surprise and actually run the workflow rather than trusting
  it by inspection.
- `packaging/macos/` — the macOS build (`parztream-macos-arm64.dmg`),
  same "easiest way to get started" pitch as Windows/Linux, for
  Apple Silicon Mac users specifically (Intel isn't built at all —
  `target_arch="arm64"` in the spec, matching the `macos-14` GitHub
  Actions runner). `launcher.py` follows the same pattern as the other
  two — persistent data dir at `~/Library/Application Support/parztream`
  (macOS's equivalent of `%APPDATA%`/XDG dirs), persisted secret key,
  bundled ffmpeg added to `PATH`, browser opened once ready.
  Two things that make this build meaningfully different from
  Windows/Linux, not just "the macOS version of the same thing":
  1. **A double-clicked `.app` has no console by default** — unlike
     the `.exe`'s console window or the AppImage's `Terminal=true`
     `.desktop` entry. Since "close the window / Ctrl+C" is this app's
     entire stop mechanism, running with no visible window at all
     would be a real regression (Activity Monitor would be the only
     way to stop it). The spec names the actual PyInstaller binary
     `parztream-bin` and sets `CFBundleExecutable` to `parztream`; the
     build workflow copies the committed
     `packaging/macos/parztream-wrapper.sh` into
     `Contents/MacOS/parztream` — an `osascript`-based script that
     opens a real Terminal window and runs `parztream-bin` inside it.
     Don't "simplify" this by pointing `CFBundleExecutable` straight
     at the compiled binary — that silently removes the only way
     non-technical users have to stop the server.
  2. **No LGPL ffmpeg source exists for macOS the way BtbN provides
     for Windows/Linux**, so `.github/workflows/build-macos-app.yml`
     installs ffmpeg via Homebrew instead, which is very likely a
     **GPL** build by default (x264/x265 included). This is a known,
     deliberate inconsistency with the LGPL policy used for
     Windows/Linux (see `packaging/windows/`), not an oversight — flag
     it if this project ever needs to be strictly GPL-clean across all
     three platforms.
  Also unsigned/unnotarized (no Apple Developer account — that's a
  real $99/year recurring cost, an explicit decision, not an
  oversight), so Gatekeeper blocks first launch; documented in the
  README's Troubleshooting section (Control-click → Open, or
  `xattr -cr`), not something to route around some other way.
  **Builds successfully, but "builds" ≠ "works"**: no macOS
  environment was available at all while writing this, so the
  `BUNDLE()` Info.plist keys, the wrapper script's `osascript` syntax,
  and the `create-dmg` invocation were all based on documented
  behavior only. The `v0.1.1` release build did succeed on a real
  `macos-14` CI runner — PyInstaller, the wrapper-script swap, and
  `create-dmg` all completed without error, producing an actual
  `parztream-macos-arm64.dmg` (this same pipeline's first attempt
  failed for an unrelated reason — a missing `contents: write`
  permission — caught and fixed the same way as Windows/Linux). CI
  success here only proves the build mechanics are sound; it doesn't
  run the `.dmg`, open the `.app`, or exercise Gatekeeper/the osascript
  wrapper — none of that happens in a headless CI job. Don't treat
  this as working until someone actually opens it on real Apple
  Silicon hardware, and re-verify here if you touch the spec file, the
  wrapper script, or the workflow.

Test isolation relies on a quirk worth knowing: `config.py` reads env
vars into module-level constants at import time, and `db.py`/
`auth.py`/etc. import those by name (`from .config import DB_PATH`,
etc.), so patching env vars after startup does nothing.
`tests/conftest.py` instead monkeypatches the *consuming* module's
attribute directly (e.g. `monkeypatch.setattr(db, "DB_PATH", ...)`,
`monkeypatch.setattr(config, "MEDIA_DIRS", ...)`) — that works because
each function looks up the name in its own module's globals at call
time. `CACHE_DIR` in particular is imported separately by *three*
modules (`transcode.py`, `artwork.py`, `cache.py`), all of which need
patching to the same tmp path for a test to see one consistent cache
dir — `isolated_app_state` does all three.

This same quirk is why `app/routers/login.py` deliberately imports
`from .. import auth` and writes `auth.AUTH_PIN` instead of
`from ..auth import AUTH_PIN` — the latter binds its own
independent copy at import time, which `monkeypatch.setattr(auth,
"AUTH_PIN", ...)` in tests would never reach. When a module needs
a value another module already imported from `config` (rather than
importing straight from `config` itself), prefer referencing the
other module's attribute at call time over re-importing from `config`
directly — keeps there being one obvious place tests need to patch.
`app/mdns.py` follows the same rule from the start (`from . import
config`, then `config.MDNS_ENABLED`/etc. throughout) rather than
`from .config import MDNS_ENABLED` — same reasoning, applied
proactively rather than fixed after the fact.

If you add a new config value, follow the same pattern rather than
trying to override the environment mid-test.

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
