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

# Browser end-to-end tests (real uvicorn subprocess + Playwright
# Chromium; excluded from plain `pytest` by pytest.ini's addopts, so
# blank that out to run them)
playwright install chromium  # one-time browser download
pytest tests/e2e -o addopts=""
```

There is no linter or build step yet.

Tests live in `tests/`, run against tmp-path DB/media dirs via an
autouse fixture in `tests/conftest.py` (see below), never your real
config. A couple of `test_scanner.py` cases need real audio and are
skipped automatically when `ffmpeg` isn't on `PATH`.

`tests/e2e/` is a separate, opt-in layer: smoke tests driving the real
web UI in a real Chromium via Playwright, against a genuine `uvicorn`
subprocess configured through actual env vars (its own
`tests/e2e/conftest.py`, which deliberately does *not* reuse
`tests/conftest.py`'s in-process monkeypatching — see the module
docstring). It exists to catch the "passes under TestClient, breaks in
a real browser" class — and did so immediately: on its first-ever run
it caught Chromium 149 claiming native HLS support via `canPlayType()`
that it doesn't actually have (see the `static/` section below). The
synthetic test video forces a keyframe every second — a static
`lavfi`-generated clip otherwise has exactly one keyframe, which makes
`app/transcode.py`'s segmenter produce one giant segment plus a
byte-identical duplicate for index 1 (segment splits and `-ss` seeks
both land on keyframes), and real browsers reject that with a decode
error that has nothing to do with the code under test.
`.github/workflows/test.yml` runs the unit suite on every push/PR on
Ubuntu **and** Windows (with real ffmpeg installed, so the
`requires_ffmpeg` integration tests actually run in CI rather than
skipping) plus the e2e suite on Ubuntu.

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
  just holding `media_dirs` as a JSON-encoded list). Schema changes
  mean editing `SCHEMA` in this file, plus — since real installs now
  have DBs that also hold user configuration, so "delete and rescan"
  stopped being an acceptable upgrade path — adding an `ALTER TABLE
  ... ADD COLUMN` entry to `_MEDIA_COLUMN_MIGRATIONS`, which
  `init_db()` applies to any existing DB missing the column (the
  project's entire migrations system so far; `segment_boundaries` was
  the first column added this way). `media` has
  since grown `is_movie`/`is_extra` columns (both computed at scan
  time, see `app/scanner.py`) for the Movies/TV-Shows poster-grid UI —
  if you read further down and hit a note claiming a feature "needed
  no DB schema change," that was true only at the point that specific
  feature was written, not a claim about the schema's current state;
  check `SCHEMA` itself, not prose, for what columns actually exist
  today.
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
  `None`/filename if ffprobe is unavailable), and upserts into `media`
  by path. Also deletes DB rows for files no longer found on disk.
  This is the only place file-metadata extraction happens.
  `_extract_metadata` returns a dict, not a positional tuple — it kept
  growing fields (this is its 3rd extension) and a dict is far less
  fragile to extend/mock in tests than a positional tuple; follow that
  pattern rather than reverting to positional if you add another
  field.
  `show_name`/`season_number`/`episode_number` come from two layered
  heuristics, tried in this order, never mixed: (1) **folder-based**,
  `_parse_folder_show_episode` — recognizes the Plex/Jellyfin
  convention `<Show>/<Season Folder>/<episode file>` by checking
  *only* `path.parent` against `_SEASON_FOLDER_RE` (full match:
  "Season 1", "Season 01", "S01", "S1", "Season 00" for specials;
  trailing junk like "Season 1 (2013)" is rejected). If it matches,
  the show name is `path.parent.parent.name`, the season comes from
  the folder, and the episode number comes from the filename via
  `_parse_episode_in_stem` (tries an `S##E##` tag anywhere in the
  name, then a leading "Episode N", then a bare leading number like
  "01 - Uno.mkv" — a 4-digit filename like `1984.mkv` can never match
  as an episode number, since `\d{1,3}` plus the required trailing
  separator/end-of-string can't consume all 4 digits). A season
  folder sitting directly under a configured library root (no real
  show folder above it) is deliberately rejected rather than using the
  library root's own name ("TV", "Media") as the show. Only ever
  checks the immediate parent, so an `Extras` folder nested inside a
  season folder is correctly left ungrouped, not misread as an
  episode. If the filename's own season marker disagrees with the
  folder's (e.g. `S01E01.mkv` sitting in a `Season 2` folder), **the
  folder wins** for the season number — only the episode digits are
  taken from the filename. (2) **Filename-only fallback**, the
  original `_parse_show_episode` against the filename stem — only
  recognizes the "Show Name S01E02" convention, anything else stays
  ungrouped rather than guessing. This is what still handles flat
  libraries with no season subfolders at all, completely unchanged.
  Movie titles also get a folder-based improvement, decided per
  directory in `scan_media_dirs` *before* iterating files in it: if a
  directory has no season-named subdirectory and, after excluding
  trailer/sample files (see below), contains exactly one real video,
  that video's `title` becomes the containing folder's name instead of
  its (often messy, scene-release-style) filename — e.g.
  `Inception (2010)/Inception.2010.1080p.BluRay.x264-GROUP.mkv` titles
  as "Inception (2010)". This only ever applies to a file that didn't
  already resolve to a show via either heuristic above — a season
  folder with only one episode ripped so far also technically looks
  like "one real video, no season subfolder inside it," but since its
  show/season/episode are already resolved before this check runs, it
  is never mistaken for a movie folder and retitled to the season
  folder's own name. A folder with 2+ real videos and no season
  structure is left ambiguous on purpose (existing filenames keep
  their titles) rather than guessing which one "is" the movie.
  Video files named to look like a trailer or sample clip —
  `_TRAILER_SAMPLE_RE`, matching when "trailer"/"sample" (optionally
  pluralized or suffixed with digits/punctuation: "trailer1",
  "Inception-trailer", "samples") is the *trailing* token of the
  filename stem — are excluded from the library entirely, not just
  from the movie-folder video count. End-anchored deliberately, not a
  substring search, so a real title like `"Trailer Park Boys.mkv"`
  isn't falsely excluded (trailer isn't the trailing word there).
  **Behavior note**: a file previously in the library that matches
  this pattern (e.g. it gets renamed to end in "-trailer") disappears
  from the library on the next scan — the file itself is untouched on
  disk, it's just no longer surfaced, via the same `_remove_missing`
  path used for genuinely deleted files. This is the intended
  decluttering effect for large collections, not a bug.
  None of this needed any DB schema, API, or frontend change — the
  `show_name`/`season_number`/`episode_number`/`title` columns and
  every consumer of them (`GET /api/shows`, `GET
  /api/library?show_name=`, the frontend's show dropdown and row
  labels) were already generic to where the values came from.
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
  yet — falls back to direct play rather than guessing wrong), or
  raises one of two exceptions the caller (`app/routers/stream.py`)
  routes on: `NeedsHlsRemux` (container/audio needs fixing, or — see
  below — the video codec itself does too) or `UnsupportedVideoCodec`
  (video codec incompatible and no re-encode is available). Audio
  files always direct-play, never routed through any of this.

  **Container/audio-only fix (the common case)**: on-demand HLS, not a
  single blocking file. A confirmed-real bug drove this design: an
  earlier synchronous "write one whole remuxed .mp4 file, then serve
  it" approach meant a request for a large file blocked for however
  long the *entire* remux took (minutes, for a large file) before any
  bytes could be served, and two viewers of the same uncached file
  serialized on each other. Instead, `build_playlist(duration,
  boundaries)` returns a static, complete VOD `.m3u8` computed once
  from the file's known duration and its **keyframe-accurate segment
  boundaries** (every segment index listed upfront, even though most
  don't exist as files yet), and `ensure_segment(media_id, src_path,
  remux_audio, index, ..., boundaries)` generates one segment at a
  time, on demand, into `CACHE_DIR/{media_id}_hls/segment_NNNNN.ts`,
  cutting at exactly those boundaries via `ffmpeg -f segment
  -segment_times t1,t2,... -segment_start_number N`. The boundaries
  (`compute_segment_boundaries`, a greedy "first keyframe at least
  `SEGMENT_SECONDS` past the previous boundary" walk over the
  keyframe timestamps `app/scanner.py`'s `probe_keyframes` extracts)
  fixed a confirmed-real stutter/A/V-desync bug: `-c:v copy` can only
  cut at keyframes, so the old fixed-6s-grid playlist's EXTINF values
  lied about real segment durations. Segments carry the source's own
  **continuous absolute timestamps** (`-copyts -avoid_negative_ts
  disabled` on every job; deliberately no `-reset_timestamps`) — like
  a normal pre-segmented HLS VOD. An earlier design reset every
  segment's timestamps to ~0 and trusted EXTINF for placement; that
  turned out to be the root cause of audio being audibly
  broken/desynced on every HLS-routed file (PP6): per-segment resets
  are non-compliant HLS, and hls.js only *appears* to cope — its
  video remuxer re-anchors on the PTS jump each fragment, but its
  audio remuxer drops the "overlapping" reset audio, leaving the
  audio SourceBuffer starving ~1s ahead of the playhead while video
  buffered minutes ahead (observed against a real library file via
  MediaSource instrumentation in Chromium; plain sequential playback,
  no seeking needed). Continuous timestamps also make any two jobs'
  output for "segment N" identical *by construction*. Cache dirs
  holding pre-format-change segments are wiped once on first touch
  via a `.timestamps_continuous` marker file in each `{id}_hls/` dir
  (`_ensure_segment_format`; `cache.py`'s prune deliberately never
  evicts dotfiles so a budget prune can't re-trigger the wipe). One
  known cosmetic quirk, deterministic and covered by tests: segment 0
  alone sits up to one B-frame reorder delay (~80ms) high, because
  its first dts is negative and each segment's own mpegts context
  compensates that individually — within hls.js's placement
  tolerance, not worth fighting. Boundaries are computed at scan time (only
  for files that would actually route through HLS — the keyframe
  probe walks every packet, so it's cached by path+size like the
  packet-scan duration fallback and never run for direct-play files),
  stored as JSON in `media.segment_boundaries`, lazily backfilled by
  `app/routers/stream.py`'s `_segment_boundaries` for legacy/skipped
  rows (persist + `invalidate_segments()` to drop old fixed-grid
  segments), with a `boundaries=None` fixed-grid fallback kept only
  for files whose keyframes genuinely can't be probed.
  `KEYFRAME_TIME_GUARD` (1ms) nudges `-ss` just past and split times
  just before their keyframe so float/timebase rounding can't snap a
  cut to a neighboring keyframe — see its comment before touching any
  of the time math. Seeking works correctly even mid-conversion: a
  segment request either finds it already cached, joins a job already
  headed there (`_find_or_start_job`, deduped per `hls_dir` via
  `_jobs`/`_jobs_guard`, with `LOOKAHEAD_SEGMENTS` deciding "close
  enough to just wait" vs. spawning a new job), or starts a fresh
  `ffmpeg -ss <boundary>` job seeked directly to that point — and
  because any two jobs asked for "segment N" now cut at the same
  stored boundary, seeks are deterministic (the old grid's `-ss N*6`
  snapped to whatever keyframe was nearest, so two jobs could produce
  different content for the same index). A seeked **stream-copy** job
  never trusts `-ss` blindly: confirmed live, ffmpeg's CLI subtracts
  an internal ~0.13s "dts heuristic" (`3*AV_TIME_BASE/23`) from the
  requested seek target whenever the video has B-frame reordering and
  the demuxer doesn't seek by pts — true for mkv, the main container
  routed through here (mp4-family is exempt) — so a seek aimed just
  past a boundary keyframe lands one seek point *short*, which
  mis-cut every following segment (observed: a 15s segment where the
  playlist promised 8s, containing the wrong content). Instead,
  `_probe_seek_landing` asks the same ffmpeg binary to do the same
  seek and reports where it actually lands (one packet via
  `-f framecrc`, demux-only), `_resolve_copy_seek` walks down until
  the landing provably IS a boundary (worst case: start from 0,
  always correct, capped by `SEEK_PROBE_LIMIT`), and the job anchors
  `-segment_start_number` and its split times to that landed boundary
  — the segment muxer measures `-segment_times` from the first packet
  it receives (also confirmed empirically; neither absolute times nor
  times relative to the *requested* seek align when the landing
  differs). `-noaccurate_seek` keeps a transcoded audio track
  flowing from the landing alongside the copied video (accurate_seek
  would decode-drop audio up to the requested time, leaving the first
  regenerated segments silent). Re-encode jobs skip the landing probe
  — decoding plus accurate_seek is frame-exact — but seek to
  boundary-MINUS-guard, not plus: accurate_seek keeps frames
  at-or-after the target, so aiming past the boundary would drop the
  boundary frame itself and (since splits are measured from the first
  packet) skew every split target past its forced keyframe, missing
  every cut. Their `-force_key_frames` times are the boundaries'
  ABSOLUTE times — under `-copyts` the encoder compares against the
  source's own clock, and a seek-relative time simply never fires
  (both verified against real encodes). If you touch any of this, the uniform-keyframe
  trap matters for tests too: a clip with evenly spaced keyframes can
  produce the *wrong content at the right duration*, so duration
  assertions only prove correctness when the expected segment lengths
  are irregular. A segment file existing
  on disk is *not* proof it's finished being written (ffmpeg's segment
  muxer keeps the current one open) — `ensure_segment`'s wait loop
  only trusts a segment once the *next* segment has appeared or the
  job has fully exited, otherwise a reader could get a truncated file.
  All spawned processes are tracked in `_all_processes` and killed by
  `terminate_all_jobs()` on server shutdown (`app/main.py`'s lifespan)
  so a restart never leaves an orphaned ffmpeg running.

  **Video codec itself incompatible (e.g. HEVC) — real transcoding,
  gated by `config.TRANSCODE_MODE`** (`PARZTREAM_ENABLE_TRANSCODE`, a
  tri-state: `"on"`/`"off"`/`"auto"`, default `"auto"`): in `"on"` mode,
  `resolve_playable_path` checks `app/encoder_detect.py`'s `get_encoder()`
  before deciding — if it finds a working encoder (hardware or the
  software fallback), `NeedsHlsRemux(reencode_video=True)` is raised
  instead of `UnsupportedVideoCodec`, exactly this project's original
  opt-in-only behavior before auto-detection existed, never
  second-guessed by a speed check. In `"auto"` mode (the default), the
  same branch instead calls `encoder_detect.is_hardware_transcode_capable()`
  — a *hardware* encoder must both be detected and benchmark fast enough
  for real-time re-encoding before auto-enabling; the software fallback
  never auto-enables regardless of benchmarked speed (see
  `app/encoder_detect.py`, below). In `"off"` mode, neither function is
  ever called. Once re-encoding is confirmed either way, `_start_job`
  swaps `-c:v copy` for `-c:v <encoder> -vf scale=...` plus
  `-force_key_frames` at every segment boundary — the encoder must
  emit an IDR frame exactly where the muxer will cut so every
  re-encoded segment starts decodable, which stream-copy gets for free
  from the source's own keyframes (see `_scale_args`,
  which caps re-encodes at 1080p, never upscales, and is a no-op —
  same "don't guess" pattern as elsewhere — when `video_width`/
  `video_height` are unknown). This is deliberately gated behind both
  the mode AND actual runtime detection, never assumed: real encoding
  is meaningfully CPU/GPU-intensive in a way stream-copy never is, and
  parztream's realistic hardware (NAS boxes, old laptops, Raspberry
  Pi) is exactly where that could make things *worse* than today's
  download-link fallback if it ran unconditionally -- which is the
  entire reason `"auto"` mode benchmarks before enabling, rather than
  just checking whether *any* encoder exists the way `"on"` mode does.
  A `threading.Semaphore(config.MAX_CONCURRENT_TRANSCODES)` (default 1)
  caps concurrent re-encode jobs specifically — stream-copy jobs never
  touch it, staying as cheap and uncapped as before. That semaphore is
  deliberately acquired *outside* `_jobs_guard` in `_find_or_start_job`
  (a double-checked-locking pattern, re-verifying nothing changed while
  waiting for a slot) — acquiring a potentially-blocking semaphore
  while holding `_jobs_guard` would stall every other media id's
  segment requests too, since that lock is the single serialization
  point across the whole module, not just the caller's own video.
  See `app/encoder_detect.py` for why the software fallback is
  `libopenh264` and not `libx264`. Calls `cache.prune()` after each
  job completes successfully (see `app/cache.py`, which recurses into
  `*_hls/` directories) — an evicted segment isn't a loss, just a
  cache miss regenerated on next request, same philosophy as the
  original single-file cache.
- `app/encoder_detect.py` — `get_encoder()` answers "what `-c:v` value
  should a real re-encode use on this machine," cached for the life of
  the process after the first call (thread-safe, lock-guarded so
  concurrent first-callers only trigger one probing round — same
  dedup philosophy as `app/transcode.py`'s `_jobs_guard`). Tries
  hardware candidates in a platform-specific order (`h264_videotoolbox`
  on macOS; `h264_qsv`/`h264_vaapi`/`h264_nvenc`/`h264_amf` on
  Windows/Linux, in that order) before falling back to `libopenh264`
  (software). Each candidate is verified with a genuine one-frame
  synthetic encode (`-f lavfi -i color=...` to `-f null -`), not just
  "is it listed in `ffmpeg -encoders`" — a hardware encoder can be
  compiled in but still fail at runtime (no GPU, missing driver, no
  permissions), and listing alone would silently produce a broken
  first transcode instead of a clean fallback. `libopenh264` (BSD) is
  the fallback specifically because the vendored Windows/Linux ffmpeg
  is deliberately LGPL-licensed (see `ADVANCED.md`), which excludes
  GPL-licensed `libx264`/`libx265` entirely — hardware encoders don't
  have this problem since they call OS/vendor APIs at runtime rather
  than bundling GPL code, which is exactly why they're tried first
  regardless of licensing. Detection is lazy (first real-transcode
  request), not run at server startup, since the whole feature is
  opt-in and most installs/requests never touch this path at all.
  **Unverified**: hardware-encoder success has not been confirmed on
  real hardware (development happened with no GPU/hardware encode path
  available), and the real vendored BtbN binaries' actual encoder
  inventory hasn't been spot-checked against what this module assumes
  — flag this if you touch it, don't quietly treat it as confirmed.

  **`is_hardware_transcode_capable()`** — the auto-detection layer on top
  of `get_encoder()`, used only by `config.TRANSCODE_MODE == "auto"`
  (`app/transcode.py`). `get_encoder()` only proves an encoder *works* (a
  trivial 64×64, 1-frame synthetic encode, pass/fail on `returncode`) —
  this proves it's *fast enough*, by benchmarking a representative 1080p,
  3-second synthetic clip (`_measure_encode_seconds`, built via the same
  `encode_video_args()` a real segment job uses, so the benchmark measures
  the identical ffmpeg command shape) and requiring `MIN_REALTIME_FACTOR`
  (2.0×, a starting judgment call, not a measured constant — a live HLS
  segment job has real overhead beyond pure encode throughput that a bare
  1.0× benchmark leaves no room for at all: segment muxing, a concurrent
  seek spinning up a second job, another device's thumbnail work). Returns
  `False` immediately, without ever spawning a benchmark subprocess, when
  `get_encoder()` found nothing or found only `SOFTWARE_FALLBACK` —
  software encoding never auto-enables regardless of how fast it
  benchmarks, since it's pure CPU load with no hardware offload, exactly
  the resource-exhaustion risk (NAS boxes, old laptops, Raspberry Pi) this
  whole feature exists to protect against; it stays available only via
  explicit `"on"` mode. Cached for the process lifetime with the same
  double-checked-locking pattern as `get_encoder()` (a separate lock/
  sentinel — reuses `get_encoder()`'s own cache rather than re-probing
  existence). Same lazy timing as `get_encoder()` and for the same reason
  (`app/transcode.py`'s `needs_segment_boundaries` deliberately treats
  `"auto"` the same as `"off"`, so a scan can never trigger this
  benchmark either — see that function's docstring). Logs its outcome via
  `logger.info` the first time it runs (visible in server logs whenever
  the first incompatible file is actually played, not at startup, since
  there's no equivalent of `app/main.py`'s `AUTH_PIN`/`SECRET_KEY`
  startup-warning pattern here — that's intentional, not an oversight).
  **Unverified, same caveat as `get_encoder()` above**: this benchmark has
  only ever been exercised with mocked encode timings (no GPU available
  in this project's dev environment), so `MIN_REALTIME_FACTOR`'s 2.0×
  threshold hasn't been validated against real hardware-encoder
  throughput — flag this if you touch it or tune the threshold, don't
  quietly treat it as confirmed.
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
  the hard way. Also warns at startup if `config.SECRET_KEY_IS_EPHEMERAL`
  is true (no `PARZTREAM_SECRET_KEY` set) — otherwise a forgotten env var
  in a `deploy/` setup silently signs everyone out on every restart with
  no diagnostic trail, unlike the analogous `AUTH_PIN` warnings right next
  to it. A single `@app.exception_handler(Exception)` logs and turns any
  *uncaught* exception (as opposed to the `HTTPException`s every route
  already raises deliberately) into a generic `{"detail": "Internal
  server error"}` 500 — closes the gap where an unexpected bug (a corrupt
  DB row, an unexpected data shape) would otherwise fall through to
  Starlette's bare default with no server-side log line pointing at what
  broke.
- `static/` — plain JS, no bundler. `app.js` was a single ~950-line file;
  it's now split into native ES modules (`<script type="module" src="/app.js">`
  in `index.html` — modules are natively deferred, so this also closes an
  unrelated startup-ordering issue, see below) rather than pulled into a
  build step, since the project's whole point is no bundler/no framework
  and `import`/`export` is a language feature every target browser already
  supports, not a new dependency. Layout, by responsibility: `state.js`
  (the shared `playerState` object — `activePlayingId`/`activeRowBtn`/
  `activeHls` — mutated directly by both `player.js` and `rows.js`, since
  ES module bindings can't be reassigned from an importing module, only
  read; sharing one mutable object is less boilerplate than a getter/setter
  pair per field), `dom.js` (`announce()`, and `showMessage()` — a generic
  "clear this container and show one centered message" helper used for
  loading/error/empty states everywhere, list and grids alike), `resume.js`
  (client-side-only resume position, unchanged), `cast.js` (Cast SDK setup
  + `castMedia`/`isCastAvailable`), `rows.js` (`createMediaRow`/
  `createPosterTile`/`renderPager`, plus `attachThumbnailFallback` and
  `markActiveIfCurrent` — extracted once there were two real call sites
  duplicating the same logic, not before), `player.js` (`playMedia`/
  `stopPlayer`, the lazy hls.js loader — see below), `views.js` (routing:
  `currentRoute`/`setActiveView`/`render`, and the three data loaders:
  `loadLibrary`/`loadMoviesGrid`/`loadShowsGrid`/`loadShowView`), `scan.js`
  (scan banner/diagnostics UI + `pollScanStatus` + the scan button's click
  handler), and `app.js` itself (just wiring: the logout button and
  `init()`). Dependency direction is one-way and acyclic: `player.js`
  never imports `rows.js` or `views.js`, so there's no cycle to reason
  about — check this still holds before adding a new cross-module call.

  **hls.js is no longer loaded unconditionally.** `player.js`'s
  `ensureHls()` injects `/hls.min.js` (532KB) as a `<script>` tag only the
  first time `playMedia` actually hits an HLS-routed file, caching the
  in-flight load as a promise so two HLS files clicked back-to-back share
  one script load instead of injecting it twice — most playback is direct-
  play and never touches this at all, so there's no reason to pay that
  download/parse cost on every page view. A failed/offline load resolves
  to `null` and falls through to the native-`canPlayType` branch exactly
  as if `window.Hls` had never been present.

  **The Cast SDK script is `async`, not a plain blocking `<script src>`.**
  Without it, a classic (non-async/non-deferred) `<script src>` blocks the
  *next* script tag from even starting to load until it finishes — so a
  slow or blocked `gstatic.com` request (no internet, an ad-blocker, a
  genuinely LAN-only/offline setup, which this app explicitly supports)
  used to stall the entire app's boot behind an external CDN dependency
  that has nothing to do with local playback. `async` is safe here
  specifically because the Cast Web Sender SDK is designed around a
  `window.__onGCastApiAvailable` callback (set in `cast.js`) that the SDK
  polls for once it's ready, regardless of load order — this is Google's
  own recommended integration pattern, not a workaround.

  **Loading/error states for the Movies/TV Shows grids** (`loadMoviesGrid`/
  `loadShowsGrid` in `views.js`) now match the flat list's: a "Loading…"
  message via `showMessage()` before the fetch, and a distinct network-vs-
  server-error message plus `announce()` call on failure — previously
  these two functions rendered nothing while loading and failed
  completely silently (`catch (err) { return; }`), so a slow connection or
  a network hiccup on first load looked identical to "this library is
  empty," which is the very first thing a new user sees. `.empty-message`
  in `style.css` was generalized from `ul#media-list li.empty-message` to
  a bare class selector for this reason — the grids' `<p class="empty-message">`
  elements previously matched no rule at all and rendered unstyled.

  **Stale-response guards**: `views.js`'s `loadShowView` and `setup.js`'s
  `browse()` each track a request-generation counter and bail before
  touching the DOM if a newer call has since started — otherwise a slower,
  superseded request (clicking between two shows, or two folders, in quick
  succession) could resolve after the newer one and overwrite the screen
  with stale content. `playMedia`'s pre-play probe already had the
  equivalent protection via `AbortController`; this extends the same
  principle to the other two places a fast double-click could race.

  `views.js`'s `loadLibrary` fetches `/api/library`
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
  `player.js`'s `playMedia` probes
  `/api/stream/{id}` with a tiny `Range: bytes=0-1` request first —
  this both warms the transcode cache before real playback starts and
  lets a `415` (unsupported video codec) show a "download instead"
  message (link uses `?original=1`, *not* the bare stream URL — see
  `app/routers/stream.py`) rather than a silent `<video>` failure —
  before pointing an `<audio>`/`<video>` element at the same URL,
  plus a `<track>` for
  video pointed at `/api/library/{id}/subtitles` (no pre-check needed
  here — a 404 on a `<track src>` just gets ignored by the browser,
  unlike a `<video src>` 415). When the probe says a file needs HLS,
  the attach order is **hls.js first, native
  `canPlayType("application/vnd.apple.mpegurl")` second** — the order
  hls.js's own docs prescribe, and load-bearing, not stylistic:
  Chromium 149 answers `"maybe"` to that `canPlayType` while being
  unable to actually demux HLS, so the old native-first order sent it
  down the native path and every HLS playback died with a decode error
  (caught by `tests/e2e` on its first run, confirmed against a real
  browser). iOS Safari — no MSE, so hls.js can't run there — still
  reaches its genuinely-native support through the fallback branch.
  Don't flip this back. Also polls `/api/scan/status` after
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
    search result counts, player state) should call `dom.js`'s
    `announce(message)` helper, which writes into the hidden
    `#status-announcer` (`aria-live="polite"`) — otherwise screen
    reader users get no feedback that anything happened. `setup.html`
    has its own local live regions instead (`#current-path` as
    `aria-live="polite"`, `#setup-error` as `role="alert"`) since it's a
    separate non-module script, not part of `app.js`'s module graph.
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
  - Fullscreen-on-play (`player.js`'s `requestVideoFullscreen`) is gated
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

  **TV casting (Google Cast + AirPlay):** a Cast button (`static/cast.js`'s
  `castMedia`) lets `playMedia`'s existing sender-side player act as a
  remote control for a Chromecast/Google TV/Android TV device — the
  browser tab stays the sender, the TV's own default media receiver
  fetches the stream URL and plays it directly, using Google's
  Cast Web Sender SDK (`static/index.html` loads
  `https://www.gstatic.com/cv/js/sender/v1/cast_sender.js` — the one
  deliberate exception to this project's "everything vendored, no CDN"
  rule, since Google's terms don't allow self-hosting it; only the
  sender's browser tab needs internet access for this, the actual media
  bytes still flow LAN-only once casting starts). Since a Cast receiver
  has no cookie jar and can't present the session cookie
  `SessionAuthMiddleware` normally requires, `app/auth.py` mints a
  short-lived, single-media signed token (`create_cast_token`/
  `verify_cast_token`, a separate `itsdangerous` serializer/salt from the
  session cookie's, so one can never be replayed as the other) via
  `POST /api/cast-token/{id}` — reachable only by an already-authenticated
  sender — and the middleware accepts `?cast_token=` as an alternative to
  the cookie, but only for the exact stream/HLS path shapes matched by
  `CAST_STREAM_PATH_RE`, never any other route. `POST /api/cast-token/{id}`
  is also the one endpoint besides `/api/login` with any rate limiting:
  `auth.check_cast_token_rate_limit` caps it at `CAST_TOKEN_RATE_LIMIT`
  (20) mints per rolling `CAST_TOKEN_RATE_WINDOW_SECONDS` (60) per client
  IP, a plain fixed-window counter rather than the login lockout's
  escalating-lockout logic — minting a token isn't a guessing attack (the
  token itself is still an unforgeable signed value), this only exists to
  cap a script minting tokens in a tight loop; normal casting use never
  comes close. Chromecast's default
  receiver doesn't support the Matroska container at all (unlike this
  app's own `<video>` player, which can direct-play many `.mkv` files), so
  `castMedia` forces the HLS remux path for casting whenever the source
  is `.mkv`, regardless of what the in-browser compatibility probe
  decided for local playback.
  **AirPlay needed no new code**: nothing in this codebase sets
  `disableRemotePlayback`, so Safari's native AirPlay picker on
  `<video>`/`<audio>` and Chrome/Android's Remote Playback API picker
  should already work today.
  **Unverified**: no real Chromecast/Google TV/Android TV/Apple TV
  hardware was available while building this — the token minting/
  validation round-trips correctly under test, and the Cast SDK
  script/button load without breaking any existing flow (confirmed via
  `tests/e2e`), but actual on-device Cast session negotiation and
  receiver playback have not been verified end-to-end, matching this
  project's existing disclosure convention for platform work built
  without access to the real hardware (see the Windows/macOS packaging
  sections above). Re-verify here if real Cast/AirPlay hardware becomes
  available, rather than assuming this works as built.
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

## Non-goals

- **No "Continue Watching" / server-side watch state.** Resume position
  stays client-side only (`static/resume.js`'s `localStorage`-based
  `resumeKey`/`saveResumePosition`/`getResumePosition`), even though it
  doesn't survive a device switch. This has been proposed before (it
  scored well on effort-vs-impact) but is a deliberate scope cut, not an
  oversight — don't add a `playback_state` table, a progress-reporting
  endpoint, or a "Continue Watching" shelf without the user explicitly
  asking for it first.
