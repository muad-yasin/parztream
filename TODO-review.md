# Code review findings & feature roadmap — 2026-07-03

Three parts: (1) full-codebase review findings (bugs/security/edge cases),
prioritized High/Medium/Low; (2) feature gaps vs. Jellyfin/Plex/Kodi
(F-items), ranked by effort vs. impact; (3) playback diagnosis (PB-/PP-
items + action plan) for "many files don't play" and "poor performance".
Delete entries as they're resolved.

**Resolved this session** (implemented + tested, see git log): H1, H2, H3,
M2, M3, M4, M5, L2, L6, L7, L8, L11, L12, L13, PB2, PB5 (same defect as
H2), PB6, PP1 (same defect as H3), PP3.

## Medium

- [ ] **M1. Entire scan runs as one SQLite write transaction**
  (`app/scanner.py`). With 10s ffprobe and 240s packet-scan timeouts per
  file, a large scan holds the write lock for hours: `POST /api/setup`
  fails with "database is locked" (5s busy timeout), and a crash loses all
  scan progress. Commit incrementally (per directory) or enable WAL in
  `app/db.py`.

- [ ] **M6. HLS cache has no freshness check; orphaned cache dirs leak.**
  Thumbnails check mtime (`app/artwork.py`); `ensure_segment`
  (`app/transcode.py`) trusts any leftover segment — replace a file in
  place (same path ⇒ same id) and playback serves stale/mixed segments.
  `{id}_hls/` dirs and thumbs for deleted media are never cleaned unless
  `CACHE_MAX_BYTES` is set (unset by default).

- [ ] **M7. No Host-header validation → DNS rebinding crosses the LAN
  boundary.** A remote page can reach `http://<lan-ip>:8000` as
  same-origin via rebinding; with no PIN (the default) that includes
  `/api/setup/browse` (whole-filesystem listing) and `/api/setup`
  (repointing media dirs). Even with a PIN, cross-site "simple" POSTs
  (text/plain form bodies) reach JSON endpoints. Cheap mitigation: reject
  unexpected `Host` values in `SessionAuthMiddleware`.

## Low

- [ ] **L1.** Scan-lock leak: if the claimed BackgroundTask never runs
  (crash between claim and execution), `_scan_lock` is held until restart
  → every scan 409s (`app/routers/library.py`).
- [ ] **L3.** Range nits (`app/routers/stream.py`): multi-range requests
  silently serve only the first range; syntactically invalid `Range` gets
  416 where RFC 9110 says ignore-and-200. Browsers don't care in practice.
- [ ] **L4.** `get_scan_status()` shallow-copies `_scan_state`
  (`app/scanner.py`) — the example lists stay shared with the scanning
  thread; a status poll can serialize them mid-append.
- [ ] **L5.** Subtitles: strict `utf-8-sig` decode (`app/subtitles.py`)
  404s CP1252/Latin-1 `.srt` files (very common); 1-digit-hour timestamps
  (`0:00:01,000`) aren't converted so those cues get dropped.
- [ ] **L9.** `static/setup.js` joins Windows paths with `/` (cosmetic
  mixed separators end up in the DB/UI); `/api/setup/browse` 500s on
  non-permission `OSError`s, e.g. dir deleted between `is_dir` and
  `iterdir` (`app/routers/setup.py`).
- [ ] **L10.** Show pages silently cap at 500 episodes — `limit: 500` with
  no pager (`static/app.js`, `MAX_PAGE_SIZE`).

## Not re-flagged (documented deliberate trade-offs)

No session revocation / PIN change not invalidating sessions, no Secure
cookie over HTTP, unpaginated `/api/shows`, unrestricted `/browse` behind
auth, unescaped LIKE wildcards in `q`.

---

# Feature gaps vs. Jellyfin / Plex / Kodi — 2026-07-03

From a feature-level comparison against Jellyfin 10.11/12.x, Plex
(2025–2026 state), and Kodi 22 "Piers", based on their public release
notes/docs only — **no source code from any of them was read or copied**
(Jellyfin and Kodi are GPL; everything below builds on parztream's own
modules). Filtered to parztream's scope: home LAN, one household,
non-technical users, SQLite + plain JS, single process.

Ranked by effort vs. user-facing impact. F1/F4's original ordering note
("do H1 first") no longer applies — H1 (rescan wiping rows for an
unavailable media dir) is fixed.

- [ ] **F1. Server-side watch state + Continue Watching** *(effort S,
  impact XL — best value on this list)*. Their concept: clients heartbeat
  playback position to the server; it derives resume points, watched
  flags, and a "Continue Watching"/"Next Up" shelf (Kodi needs
  user-managed MySQL for this; Jellyfin/Plex need user accounts). Ours: a
  `playback_state` table **keyed by media path, not id** (survives rescan
  id churn), one throttled `POST /api/library/{id}/progress` wired into
  the `timeupdate` handler that already exists (`static/app.js`,
  currently localStorage-only), and a Continue Watching row on the home
  view. No accounts — one shared household profile matches the single-PIN
  model. Better than theirs: shared across devices by default because
  playback already flows through the server; no MySQL, no user system.

- [ ] **F2. Embedded subtitle tracks (mkv)** *(effort M, impact XL for
  mkv libraries — the biggest "this file doesn't work right" gap)*.
  Their concept: enumerate streams in the container, extract a chosen
  subtitle track on demand, convert to WebVTT. Ours: extend the scanner's
  existing single ffprobe call to also record subtitle/audio streams
  (language, codec) via the `_extract_metadata` dict pattern; add
  `?track=n` to the existing subtitles endpoint running an ffmpeg
  text-sub extraction to WebVTT, cached under `cache.py`'s existing
  `lock_for` + `prune` budget; one `<track>` per stream in the player.
  Scope cut: *audio* track selection needs remux changes — ship subtitles
  first, audio later.

- [ ] **F3. Local metadata conventions (NFO + poster.jpg), no scraping**
  *(effort M, impact L)*. Their concept: Jellyfin/Plex scrape TMDB/TVDB,
  but all three also honor the Kodi-originated local conventions —
  `movie.nfo`/`tvshow.nfo` sidecars and `poster.jpg`/`folder.jpg` — which
  media managers (tinyMediaManager, Radarr/Sonarr) write. Ours: scanner
  reads the sidecar XML for title/year/plot; `artwork.py` prefers a
  local `poster.jpg` over the ffmpeg frame-grab. Zero network calls, API
  keys, or rate limits; curated libraries get Plex-grade presentation
  instantly and parztream stays offline-capable. Optional TMDB fetcher
  later behind an env var, gated the same way transcoding was.

- [ ] **F4. Automatic library freshness** *(effort S–M, impact M-L)*.
  Their concept: watch library folders via OS filesystem events
  (inotify/ReadDirectoryChangesW) plus scheduled scans. Ours: skip OS
  watchers — a periodic background incremental scan reusing the existing
  scan lock/status machinery, short-circuiting directories whose mtime
  hasn't changed (the packet-scan duration cache already makes rescans
  cheap). Better than theirs: inotify famously doesn't work on SMB/NFS
  mounts — exactly where home media lives, and a recurring Jellyfin
  support headache — while mtime polling is boring, cross-platform
  (Windows target), and dependency-free.

- [ ] **F5. Trickplay-style seek previews** *(effort M-L, impact M —
  defer; worst effort-to-impact here)*. Their concept: pre-generate a
  sprite sheet of frames at fixed intervals, show the nearest frame while
  scrubbing. Ours: generation is easy (one ffmpeg tile-filter job into
  the existing cache, same pattern as `get_video_thumbnail`) — but native
  `<video>` controls expose no scrub-hover hook, so this needs a custom
  control bar, dragging in fullscreen/mobile/accessibility work so far
  avoided by using native controls.

**Deliberately not pursuing** (flagship features elsewhere, poor fit
here): intro-skip / media segments (per-episode audio fingerprinting is
heavy compute for NAS-class hardware), multi-user profiles (contradicts
the one-PIN household model), DLNA serving (large protocol surface,
dying client support — the PWA is the better bet), SyncPlay/watch-
together, Live TV/DVR, remote access (deliberately LAN-only; note Plex
paywalled remote streaming of personal media in 2025, which is exactly
the wind at parztream's back).

---

# Playback diagnosis — 2026-07-03

Investigation of two reported problems: (a) many video files fail to
play at all, (b) poor playback performance (buffering, stuttering, slow
starts). PB2/PB5/PB6 and PP1/PP3 are fixed this session (see top of file)
-- remaining items below.

Pipeline recap: play click → probe `GET /api/stream/{id}` (Range 0-1)
→ `resolve_playable_path` routes to (1) direct play with hand-rolled
Range/206, (2) on-demand HLS (static VOD playlist + per-segment ffmpeg
into MPEG-TS, hls.js client), or (3) 415 for incompatible video codecs
with transcoding off (default) or a codec/container combo this module
genuinely can't fix (see TS_SAFE_VIDEO_CODECS).

## Files that don't play (ranked by likelihood)

- [ ] **PB1. HEVC → 415 by design.** `PARZTREAM_ENABLE_TRANSCODE` is off
  by default, so every HEVC/mpeg2/etc. file is a hard "can't play" with a
  download link (now a clearer, actionable message pointing at the env
  var). This is a deliberate opt-in, not a bug -- an operational decision
  for the user to make, not something to silently flip.
- [ ] **PB3. `video_codec IS NULL` rows direct-play blind.** ffprobe
  missing or timing out at scan (slow NAS) leaves codec NULL →
  `resolve_playable_path` falls back to direct play → browser gets raw
  mkv/avi it can't decode → now at least a readable player-side error
  (PB6 fixed this session), but the underlying scan gap remains. Repair:
  fix ffmpeg/ffprobe availability, rescan.
- [ ] **PB4. `duration IS NULL` → HLS playlist 500s**
  (`app/routers/stream.py`). Same ffprobe-failure files; flagged in scan
  diagnostics as "incomplete metadata." Meaningfully less likely now that
  the packet-scan duration fallback is cached across rescans (see git
  log), but still a real gap if ffprobe genuinely can't read a file at
  all.
- [ ] **PB7. `canPlayType("application/vnd.apple.mpegurl")` truthiness
  no longer reliably means "Safari, use native HLS" — confirmed to
  misfire on a real, current Chromium build.** `static/app.js`'s
  `playMedia()` branches on `el.canPlayType(...)` being truthy to decide
  between native HLS (`el.src = hlsPlaylistUrl`, meant for Safari) and
  hls.js (`new Hls(); hls.loadSource(...); hls.attachMedia(...)`).
  Confirmed live: a recent Chromium build also returns `"maybe"` for that
  MIME type (Chromium's own experimental/partial native HLS rollout), so
  it takes the Safari branch too and skips hls.js's JS-based MPEG-TS
  transmuxing entirely. Chromium's native path doesn't handle this app's
  legacy MPEG-TS segments correctly on that browser — real playback
  failure (`MEDIA_ERR_DECODE`) for a file that hls.js decodes perfectly:
  identical playlist/segments played flawlessly the moment hls.js was
  used explicitly instead of letting `canPlayType` bypass it. Found while
  browser-verifying the multichannel-AAC fix (see git log) — the
  channel/mapping fix itself is confirmed correct via hls.js; this is a
  separate, pre-existing bug in the player's engine-selection logic that
  was just masking that verification. Fix direction: don't trust
  `canPlayType` truthiness alone to select the native-HLS branch — e.g.
  prefer `Hls.isSupported()` whenever it's true and only fall back to
  native `el.src` when hls.js genuinely isn't available, inverting
  today's priority order.

## Poor performance

- [ ] **PP2. Threadpool exhaustion under modest concurrency.** Each
  in-flight segment request still parks a sync-pool thread in a 0.1s poll
  loop (up to 30s, `ensure_segment`); hls.js prefetches several. Partially
  mitigated this session (idle jobs no longer run/hold threads
  indefinitely once abandoned, and a busy transcode slot now fails fast
  with 503 instead of hanging), but the core "one thread per in-flight
  segment request" architecture is unchanged -- 2-3 concurrent viewers can
  still pressure the thread pool. Real fix: event-driven segment waits
  (a per-segment `threading.Event` signaled by the job watcher instead of
  polling).
- [ ] **PP4. Slow starts are structural.** probe → playlist → spawn
  ffmpeg → wait for segment 0 *and* 1 (completion heuristic needs N+1)
  → several seconds to first frame even on fast disks.
- [ ] **PP5. Post-seek A/V glitches.** `-ss N*6` + `-c:v copy` +
  `-reset_timestamps 1` starts at the nearest keyframe while the
  playlist promises an exact 6s grid → timing drift at job-splice
  points, felt as jump/stutter right after seeking.

Ruled out: hls.js version, chunk size, MIME handling (guess_type on the
served path; `.m4b` already special-cased), Range parsing (compliant
enough for real browsers).

## Remaining action items

1. Config: `PARZTREAM_ENABLE_TRANSCODE=1` for HEVC (PB1) — the user's
   own call, encoder detection is now fixed for VAAPI/QSV (see git log)
   but still worth verifying on real target hardware.
2. Rescan after confirming ffprobe health (PB3/PB4).
3. fMP4 HLS segments instead of TS: the properly durable fix for any
   future codec-routing edge case (carries vp9/av1/opus natively), modern
   hls.js path. Needs real-browser testing incl. Safari native HLS. Not
   urgent now that PB2's specific vp8/vp9/av1 failure mode is fixed by
   routing those to a clear 415 instead.
4. Event-driven segment waits (PP2) — biggest remaining performance item.
5. Bound re-encode/-copy jobs with `-t` to stop exactly before the next
   job's range (PP5), instead of (or in addition to) the current
   terminate-the-old-job approach, to smooth the splice point itself.

**Open questions / user testing needed before implementing**
- Which failure class dominates this library:
  `sqlite3 parztream.db "SELECT video_codec, audio_codec, COUNT(*)
  FROM media WHERE media_type='video' GROUP BY 1,2 ORDER BY 3 DESC;"`
- Symptom specifics: which browser; media on local disk vs NAS/SMB;
  does stutter affect direct-play mp4s too (→ I/O) or only HLS-routed
  files (→ job machinery); correlated with seeking (PP5)?
- If transcoding gets enabled: check the "Transcode encoder detection:
  using ..." log line and whether the first HEVC play keeps up in real
  time on the user's actual hardware (VAAPI/QSV wiring is now fixed and
  unit-tested, but only verified against one real AMD GPU in a sandbox
  that turned out to lack an H.264 encode profile -- still unverified on
  hardware that can actually encode).
- fMP4 migration must be verified in hls.js *and* Safari native HLS
  before committing.
