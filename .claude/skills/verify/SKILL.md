---
name: verify
description: Drive a real parztream server (and real Chromium) to verify changes end-to-end, beyond the unit/e2e suites.
---

# Verifying parztream changes against the real app

## Launch a real server (the tests/e2e/conftest.py recipe)

Configuration is env vars only (`app/config.py` reads them at import, so
a subprocess is the only way to change them without monkeypatching):

```bash
source .venv/bin/activate
PARZTREAM_DB_PATH=/tmp/verify/db.sqlite \
PARZTREAM_CACHE_DIR=/tmp/verify/cache \
PARZTREAM_MDNS_ENABLED=false \
PARZTREAM_MEDIA_DIRS=/tmp/verify/media \
python -m uvicorn app.main:app --host 127.0.0.1 --port <free port>
```

Wait for `GET /api/setup/status` to answer, `POST /api/scan`, poll
`GET /api/scan/status` until not `scanning`. The DB is plain sqlite —
inspect/mutate it directly from the driver process between requests
(the server opens a connection per request, no lock held).

Browser: Playwright Chromium is installed in the venv. Launch with
`--autoplay-policy=no-user-gesture-required` or currentTime never
advances headless. Selectors: `#movies-grid .poster-tile`,
`#media-list .row-btn`, `#player-container video`.

## Synthesizing test media that behaves like real media

- **Irregular keyframes or the test proves nothing**: with uniform
  keyframe spacing, a mis-seeked/mis-cut segment has the wrong content
  at the *right duration*, so duration assertions pass on broken
  output (a real bug slipped through exactly this way). Use explicit
  times: `-force_key_frames 0,4,9,15,22,30,37,41`.
- **Use `testsrc`, not `color`**: it burns a running clock into the
  frames, so a screenshot/extracted frame proves *which* source time
  a segment or seek actually landed on.
- mkv + h264 + `-c:a ac3` routes through HLS with audio transcode;
  mp4 + h264 + aac direct-plays (control case).

## Sharp edges found by doing this (don't rediscover)

- `ffmpeg -ss` on mkv lands ~0.13s short of the target for B-frame
  video (CLI dts heuristic) — `ffprobe -read_intervals` does NOT do
  this, so it cannot predict ffmpeg's landing. Probe landings with
  `ffmpeg -ss T -i f -map 0:v:0 -c:v copy -copyts -frames:v 1 -f framecrc -`.
- `-segment_times` are measured from the first packet the muxer sees,
  not absolute even under `-copyts`.
- `ffprobe -read_intervals %+#1` reads the first packet of the FILE
  (often audio in MPEG-TS), regardless of `-select_streams`.
- A 404 on `/api/library/{id}/subtitles` during playback is by-design
  (`<track>` pre-check deliberately skipped) — allowlist it before
  asserting "no failed requests".

## Suites (CI's job, but for completeness)

`pytest` (unit), `pytest tests/e2e -o addopts=""` (real uvicorn +
Chromium smoke). The deepest standalone harness pattern from the HLS
boundary work: synthesize oracle boundaries from the source file with
raw ffprobe, then check DB/playlist/served-segment-bytes against that
oracle over HTTP — never against the app's own functions.
