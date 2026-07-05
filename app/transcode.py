import logging
import math
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import cache, config, encoder_detect
from .config import CACHE_DIR

logger = logging.getLogger("parztream")

# Codecs essentially every modern browser can decode natively.
COMPATIBLE_VIDEO_CODECS = {"h264", "vp8", "vp9", "av1"}
COMPATIBLE_AUDIO_CODECS = {"aac", "mp3", "opus", "vorbis"}

# Chromium's <video>/MediaSource AAC decoder rejects multichannel (>2
# channel) AAC outright -- confirmed live: a 5.1 AAC track appended via
# hls.js fails with "PipelineStatus::CHUNK_DEMUXER_ERROR_APPEND_FAILED:
# RunSegmentParserLoop: stream parsing failed", even though the exact same
# .ts segment is completely valid per ffprobe/ffmpeg's own (much more
# permissive) decoder -- this is a real browser limitation, not a bug in
# the segment. A codec name alone being in COMPATIBLE_AUDIO_CODECS isn't
# enough for AAC specifically; it also has to be within this channel cap,
# or it needs routing through the real transcode path (see
# resolve_playable_path/_start_job), which downmixes to stereo.
MAX_DIRECT_PLAY_AUDIO_CHANNELS = 2

# Containers browsers can open directly, independent of what's inside.
DIRECT_PLAY_CONTAINERS = {".mp4", ".webm"}

# Of the browser-compatible video codecs above, only h264 can actually be
# muxed into the MPEG-TS segments this module's on-demand HLS path produces
# -- ffmpeg's mpegts muxer has no standard mapping for vp8/vp9/av1.
# Confirmed real: routing one of those into a "-c:v copy -f mpegts" remux
# job fails immediately (the ffmpeg process errors out, every segment
# request for that file 500s/404s), not a degraded-but-working fallback.
# Until fMP4 HLS segments (which can carry any of these) replace MPEG-TS
# here, a vp8/vp9/av1 file inside a non-direct-play container (e.g. .mkv)
# has no working playback path in this app at all -- see resolve_playable_path.
TS_SAFE_VIDEO_CODECS = {"h264"}

# Minimum length of each on-demand HLS segment, in seconds. Short enough
# that a forward seek into not-yet-generated territory only waits a few
# seconds for one segment (stream-copy is fast), long enough not to spawn
# an unreasonable number of tiny ffmpeg-adjacent files for a long video.
# "Minimum", not "exactly": with -c:v copy, segments can only be cut at the
# source's keyframes, so real segments run from one keyframe-accurate
# boundary (see compute_segment_boundaries) to the next -- at least this
# long, longer when the source's keyframes are sparse.
SEGMENT_SECONDS = 6

# Padding added to a keyframe-exact -ss value, and subtracted from
# keyframe-exact -segment_times split points. ffprobe reports keyframe
# timestamps to microsecond precision, but ffmpeg's own parsing/timebase
# rescaling of a "-ss 6.006000" argument can round to a timestamp one tick
# *below* the keyframe's real pts -- and input seeking snaps backward to the
# nearest keyframe at-or-before the target, so an exact-looking value can
# land a whole GOP early. Nudging the seek target just past the keyframe
# (backward snap then lands exactly on it) and the split points just before
# it (the segment muxer splits at the first keyframe at-or-after a split
# point) makes both deterministic. 1ms is orders of magnitude larger than
# the rounding error being guarded against and well under one frame
# duration even at 120fps (~8.3ms), so it can never skip to a neighboring
# frame, let alone a neighboring keyframe.
KEYFRAME_TIME_GUARD = 0.001

# How many _probe_seek_landing calls a single seeked stream-copy job may
# spend walking down to a boundary it can provably land on (see
# _resolve_copy_seek) before giving up and starting from the top of the
# file instead -- which is always correct, just does more (fast,
# stream-copy) work. Realistic containers resolve in 1-2 probes; only a
# pathological seek-point layout (e.g. an all-intra file with an index
# entry on every frame, none of them lining up with boundaries) burns the
# whole budget.
SEEK_PROBE_LIMIT = 8

# How close (seconds) a probed seek landing has to be to a stored boundary
# to count as *being* that boundary. Landings and boundaries both
# originate from the same packet timestamps, so real matches differ only
# by container-timebase rounding (mkv stores milliseconds) -- far below
# this -- while a genuine miss is at least one whole frame away.
_LANDING_MATCH_TOLERANCE = 0.01

# If a running job's on-disk progress is within this many segments of a
# requested index, a request just waits for it rather than spawning a
# redundant second ffmpeg process seeked to nearly the same place.
LOOKAHEAD_SEGMENTS = 3

# How long a segment request will wait for it to appear before giving up.
# Generous because stream-copy is normally much faster than real-time, but
# bounded so a genuinely stuck/hung ffmpeg doesn't hang a request forever.
SEGMENT_WAIT_TIMEOUT = 30

# A job with no segment requested from it in this long is considered
# abandoned -- the viewer navigated away or closed the tab -- and gets
# terminated rather than running to end-of-file. Reaped opportunistically
# (see _reap_idle_jobs_locked) whenever any segment request anywhere
# touches _jobs_guard, not on a separate timer/thread.
JOB_IDLE_TIMEOUT = 60

# How long a request will wait for a free re-encode slot before giving up
# (see TranscodeUnavailable). Without this, an abandoned re-encode job
# holding the single default slot could block every other re-encode
# request's thread indefinitely -- JOB_IDLE_TIMEOUT above is what actually
# frees the slot in that case, this is just the bound on how long a still-
# waiting request sits before being told to retry instead of hanging.
TRANSCODE_SLOT_TIMEOUT = 30


class UnsupportedVideoCodec(Exception):
    """Raised when a video can't be played in a browser and this module has
    no way to fix it. Two distinct reasons share this one exception (same
    415 contract for callers) but need different messages:
    reason="codec" -- the video codec itself needs a real re-encode, which
    is either not enabled or not possible on this machine (see
    transcode_enabled). reason="container" -- the video codec (vp8/vp9/av1)
    is itself perfectly browser-playable, but this module's on-demand HLS
    path can only mux h264 into the MPEG-TS segments it produces (see
    TS_SAFE_VIDEO_CODECS) -- routing one of those into that remux path is a
    guaranteed failure, not a degraded-but-working fallback, so it's
    treated as unfixable here rather than attempted. transcode_enabled is
    irrelevant to that case (it's a muxer limitation, not a missing
    encoder), so its message never mentions the env var."""

    def __init__(self, codec: str, transcode_enabled: bool = False, reason: str = "codec"):
        self.codec = codec
        self.transcode_enabled = transcode_enabled
        self.reason = reason
        super().__init__(codec)

    def user_message(self) -> str:
        if self.reason == "container":
            return (
                f"This file's video codec ('{self.codec}') is browser-playable, but its "
                "container can't be repackaged into a working stream by this server yet "
                "(only H.264 video can be) -- download it to play in another app instead."
            )
        if self.transcode_enabled:
            return (
                f"Video codec '{self.codec}' can't be played in a browser, and no working "
                "video transcoder was found on this server (checked hardware and software "
                "encoders) -- see the server logs for what was tried."
            )
        return (
            f"Video codec '{self.codec}' can't be played in a browser yet. Setting "
            "PARZTREAM_ENABLE_TRANSCODE=1 lets the server convert it automatically during "
            "playback -- this uses more CPU/GPU and may not keep up in real time on modest "
            "hardware, so it's opt-in rather than always on."
        )


class NeedsHlsRemux(Exception):
    """Raised by resolve_playable_path when the file's container or audio
    track needs fixing before it can play in a browser. Callers (see
    app/routers/stream.py) should route to the HLS playlist/segment
    endpoints -- build_playlist()/ensure_segment() -- instead of treating
    this row as a plain streamable file. reencode_video is True only when
    the video codec itself is incompatible AND config.TRANSCODE_MODE allows
    it (either "on", or "auto" with a hardware encoder that benchmarks fast
    enough -- see resolve_playable_path and app/encoder_detect.py) --
    otherwise that case still raises UnsupportedVideoCodec exactly as
    before either of these existed."""

    def __init__(self, remux_audio: bool, reencode_video: bool = False):
        self.remux_audio = remux_audio
        self.reencode_video = reencode_video
        super().__init__(f"remux_audio={remux_audio} reencode_video={reencode_video}")


class RemuxFailed(Exception):
    """Raised/stored when an HLS segment-generation ffmpeg process exits
    non-zero. Carries ffmpeg's stderr output for diagnostics."""


class TranscodeUnavailable(Exception):
    """Raised when no re-encode slot freed up within TRANSCODE_SLOT_TIMEOUT
    -- surfaced by app/routers/stream.py as a 503 so an overloaded server
    tells the client to retry shortly instead of a request (and the sync
    threadpool thread handling it) blocking indefinitely on the semaphore,
    potentially for as long as an abandoned re-encode job takes to either
    finish or get reaped by _reap_idle_jobs_locked."""


def resolve_playable_path(row) -> Path:
    """Return the path that should actually be streamed for this media row
    if it's directly playable as-is. Raises UnsupportedVideoCodec if the
    video codec itself can't be fixed, or NeedsHlsRemux if only the
    container/audio track needs fixing (caller should use the HLS
    endpoints, not this function's return value, in that case)."""
    path = Path(row["path"])
    if row["media_type"] != "video":
        return path

    video_codec = row["video_codec"]
    audio_codec = row["audio_codec"]
    audio_channels = row["audio_channels"]

    # No codec info yet (ffprobe unavailable at scan time, or this row was
    # scanned before this feature existed) -- don't guess, just direct play
    # as before.
    if video_codec is None:
        return path

    video_ok = video_codec.lower() in COMPATIBLE_VIDEO_CODECS
    # audio_channels is None either for a row scanned before this column
    # existed, or a genuinely unknown channel count -- same "don't newly
    # block something that used to work" reasoning as video_codec is None
    # above, so it's treated as fine rather than forcing an unnecessary
    # remux.
    audio_ok = audio_codec is None or (
        audio_codec.lower() in COMPATIBLE_AUDIO_CODECS
        and (audio_channels is None or audio_channels <= MAX_DIRECT_PLAY_AUDIO_CHANNELS)
    )
    container_ok = path.suffix.lower() in DIRECT_PLAY_CONTAINERS

    if video_ok and audio_ok and container_ok:
        return path

    if not video_ok:
        # Real re-encoding has three modes (config.TRANSCODE_MODE -- see its
        # definition in app/config.py for the full reasoning):
        #   "on"   -- always attempt it if any working encoder is detected
        #             (hardware or the libopenh264 software fallback),
        #             exactly this project's original opt-in-only behavior.
        #   "auto" -- only attempt it if whatever encoder was detected
        #             (hardware or software) benchmarks fast enough for
        #             real-time HLS re-encoding (encoder_detect.is_transcode_capable)
        #             -- software is held to a lower bar than hardware
        #             (SOFTWARE_MIN_REALTIME_FACTOR vs. MIN_REALTIME_FACTOR),
        #             see that constant's docstring for why.
        #   "off"  -- never call app/encoder_detect.py at all (no probing
        #             subprocesses spawned) -- exactly today's dead end
        #             (download-only), unchanged from before auto-detection
        #             existed.
        # Either way this never guesses: a codec this box genuinely can't
        # (or, in "auto", isn't fast enough to) encode stays a clean
        # UnsupportedVideoCodec, never a slow/broken re-encode attempt.
        if config.TRANSCODE_MODE == "on":
            if encoder_detect.get_encoder() is not None:
                raise NeedsHlsRemux(remux_audio=not audio_ok, reencode_video=True)
            raise UnsupportedVideoCodec(video_codec, transcode_enabled=True)
        if config.TRANSCODE_MODE == "auto":
            if encoder_detect.is_transcode_capable():
                raise NeedsHlsRemux(remux_audio=not audio_ok, reencode_video=True)
            raise UnsupportedVideoCodec(video_codec, transcode_enabled=False)
        raise UnsupportedVideoCodec(video_codec, transcode_enabled=False)

    # video_ok is True here, but the container isn't -- only a codec this
    # module's MPEG-TS-based remux can actually carry (see
    # TS_SAFE_VIDEO_CODECS) can be fixed by that path. vp8/vp9/av1 in the
    # wrong container has no working remux today -- treat it as unfixable
    # rather than guaranteeing a broken playback attempt (see PB2/H2 in the
    # code review this fixed).
    if video_codec.lower() not in TS_SAFE_VIDEO_CODECS:
        raise UnsupportedVideoCodec(video_codec, reason="container")

    raise NeedsHlsRemux(remux_audio=not audio_ok)


def hls_dir_for(media_id: int) -> Path:
    return CACHE_DIR / f"{media_id}_hls"


# Marks an HLS cache directory as holding continuous-timestamp segments
# (see _start_job's -copyts comment). Directories from before that format
# change hold segments that each restart near pts 0 -- mixing the two in
# one playback session reproduces exactly the timestamp chaos the change
# removed, so leftovers are wiped once instead. A dotfile deliberately:
# cache.prune skips dotfiles, so budget eviction can never delete the
# marker out from under a full directory (which would wipe it all again).
_FORMAT_MARKER = ".timestamps_continuous"


def _ensure_segment_format(media_id: int, hls_dir: Path):
    marker = hls_dir / _FORMAT_MARKER
    if marker.exists():
        return
    if any(hls_dir.glob("segment_*.ts")):
        invalidate_segments(media_id)
        hls_dir.mkdir(parents=True, exist_ok=True)
    marker.touch()


def needs_segment_boundaries(path: Path, video_codec, audio_codec, audio_channels) -> bool:
    """Whether this video would route through the HLS path at all, i.e.
    whether paying the (packet-walk-priced, see app/scanner.py's
    probe_keyframes) cost of extracting its keyframe boundaries at scan
    time is worth anything. Mirrors resolve_playable_path's routing with one
    deliberate difference: the re-encode branch only ever returns True for
    config.TRANSCODE_MODE == "on", never "auto" (even though "auto" might
    end up enabling re-encoding too) -- both encoder_detect.get_encoder()
    and is_transcode_capable() spawn probing subprocesses and are
    deliberately lazy (first real transcode request, see
    app/encoder_detect.py), and a scan must not be the thing that triggers
    either. Worst case of treating "auto" like "off" here is one wasted
    keyframe walk skipped for a file that later turns out to auto-enable
    re-encoding anyway -- covered by the *existing* lazy backfill in
    app/routers/stream.py (same path legacy rows already rely on), just
    paid on that file's first real HLS request instead of at scan time.
    Direct-play files and files with no working HLS route (vp9/av1 in the
    wrong container, incompatible codec with transcoding off/not-yet-known)
    return False -- boundaries for those would never be read."""
    if video_codec is None:
        return False
    video_ok = video_codec.lower() in COMPATIBLE_VIDEO_CODECS
    audio_ok = audio_codec is None or (
        audio_codec.lower() in COMPATIBLE_AUDIO_CODECS
        and (audio_channels is None or audio_channels <= MAX_DIRECT_PLAY_AUDIO_CHANNELS)
    )
    container_ok = path.suffix.lower() in DIRECT_PLAY_CONTAINERS
    if video_ok and audio_ok and container_ok:
        return False
    if not video_ok:
        return config.TRANSCODE_MODE == "on"
    return video_codec.lower() in TS_SAFE_VIDEO_CODECS


def compute_segment_boundaries(keyframes: list, duration: float):
    """Turn a video's raw keyframe timestamps (see app/scanner.py's
    probe_keyframes) into the list of segment start times the playlist and
    segment jobs both work from -- segment i runs from boundaries[i] to
    boundaries[i+1] (or to `duration` for the last one). Greedy: walk the
    keyframes and start a new segment at the first keyframe at least
    SEGMENT_SECONDS past the previous boundary, so every boundary is an
    actual keyframe and -c:v copy can cut exactly there. This is the whole
    fix for the fixed-6s-grid playlist lying about segment durations:
    stream copy can only cut at keyframes, so pretending segments are
    exactly 6s long left hls.js placing variable-length segments on a
    fixed-length timeline, drifting further out of sync every segment.

    Timestamps are normalized so boundaries[0] is always 0.0 -- some
    containers (MPEG-TS notably) start their timeline at a nonzero pts,
    but ffmpeg's -ss and the playlist both count from the start of the
    file, not the stream's raw clock.

    Returns None (caller falls back to the old fixed grid) rather than
    guessing when there are no keyframes to work from."""
    if not keyframes:
        return None
    origin = keyframes[0]
    boundaries = [0.0]
    for kf in keyframes:
        rel = kf - origin
        # A boundary this close to the end would make the final segment a
        # sliver (or, if duration is slightly under-reported, empty) --
        # fold that tail into the previous segment instead.
        if rel >= duration - 1.0:
            break
        if rel - boundaries[-1] >= SEGMENT_SECONDS:
            boundaries.append(rel)
    return boundaries


def invalidate_segments(media_id: int):
    """Delete every cached HLS segment for this media, stopping any job
    still writing into its directory first. Called when segment boundaries
    are first computed (or change) for a file -- segments cut on the old
    fixed 6s grid don't line up with a boundary-derived playlist, so
    serving a stale one would splice mismatched content into the stream."""
    hls_dir = hls_dir_for(media_id)
    with _jobs_guard:
        _terminate_stale_jobs(hls_dir)
        _jobs.pop(hls_dir, None)
    shutil.rmtree(hls_dir, ignore_errors=True)


def build_playlist(duration: float, boundaries: list = None) -> str:
    """A complete, static VOD playlist computed once from the file's known
    duration and keyframe-derived segment boundaries (both from ffprobe, see
    app/scanner.py) -- not ffmpeg's own growing "event" playlist. Since the
    total duration is already known upfront, there's no need for
    live-playlist semantics: every segment index is listed immediately, and
    each segment's actual bytes are generated on demand (see ensure_segment)
    whenever a player first requests it, whether that's sequential playback
    or a seek.

    Each EXTINF is the real distance between consecutive boundaries (the
    exact points ensure_segment's jobs cut at), so hls.js's timeline matches
    the bytes it actually receives -- the mismatch between a fixed-6s-grid
    playlist and keyframe-cut segments was the root cause of stutter and
    progressive A/V desync. boundaries=None keeps the old fixed-grid
    playlist as a degraded fallback for the rare file whose keyframes
    couldn't be probed at all (see app/routers/stream.py's backfill)."""
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]
    if boundaries:
        seg_lengths = [
            end - start for start, end in zip(boundaries, boundaries[1:] + [duration])
        ]
        # duration and the last boundary both come from ffprobe, but via
        # different probes -- clamp so a slightly-short duration can never
        # produce a zero/negative final EXTINF.
        seg_lengths[-1] = max(seg_lengths[-1], 0.1)
        # TARGETDURATION must be >= every real segment length, or players
        # legitimately mistrust the playlist (it's a spec MUST).
        lines.insert(1, f"#EXT-X-TARGETDURATION:{math.ceil(max(seg_lengths))}")
    else:
        lines.insert(1, f"#EXT-X-TARGETDURATION:{SEGMENT_SECONDS}")
        total_segments = max(1, math.ceil(duration / SEGMENT_SECONDS))
        remaining = duration
        seg_lengths = []
        for _ in range(total_segments):
            seg_lengths.append(min(SEGMENT_SECONDS, remaining) if remaining > 0 else SEGMENT_SECONDS)
            remaining -= SEGMENT_SECONDS
    for i, seg_len in enumerate(seg_lengths):
        lines.append(f"#EXTINF:{seg_len:.3f},")
        lines.append(f"segment_{i:05d}.ts")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def _scale_args(width, height) -> list:
    """ffmpeg -vf args to cap a re-encode at 1080p, preserving aspect ratio
    and never upscaling. Pure function of the source's known dimensions so
    it's trivially testable without ffmpeg. Returns [] (no-op) when
    dimensions are unknown (old rows scanned before this feature existed,
    until rescanned -- same "don't guess" pattern as video_codec is None
    elsewhere in this module) or already at/under the cap.
    `force_original_aspect_ratio=decrease` combined with the min(...) bounds
    lets ffmpeg compute whichever dimension is actually constraining
    (landscape or portrait) without branching here; the second scale stage
    forces even width/height, required for H.264. The no-op check below
    must match what the filter itself would compute as a no-op (both
    bounds already satisfied), not just "the longer side is under 1080" --
    those aren't the same thing for a 1920-wide source, which the filter's
    own min(1920, iw)/min(1080, ih) bounds already leave untouched."""
    if not width or not height or (width <= 1920 and height <= 1080):
        return []
    return [
        "-vf",
        "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease,"
        "scale=trunc(iw/2)*2:trunc(ih/2)*2",
    ]


class _Job:
    def __init__(self, process: subprocess.Popen, start_index: int, reencode_video: bool = False):
        self.process = process
        self.start_index = start_index
        self.reencode_video = reencode_video
        self.done = threading.Event()
        # Set whenever _poll_job_progress notices a new segment has
        # appeared (see below) -- lets ensure_segment's waiters block on an
        # Event instead of each independently sleep-polling the filesystem
        # every 100ms, which is what actually pressures the FastAPI
        # threadpool when hls.js prefetches several segments from several
        # concurrent viewers. Cleared by each waiter after waking (not by
        # the poller), so a pulse can't be lost between two waiters that
        # both had it set and haven't rechecked yet.
        self.progress_event = threading.Event()
        self.error: RemuxFailed | None = None
        # Bumped every time a segment request is routed to this job (see
        # _check_jobs_locked) -- used by _reap_idle_jobs_locked to find jobs
        # nobody's actually waiting on anymore (the viewer navigated away or
        # closed the tab) so they don't keep running/holding a transcode
        # slot for the rest of the file.
        self.last_requested = time.monotonic()
        # Set just before this job is deliberately terminated to make way
        # for a newer one (see _terminate_stale_jobs) or reaped as idle --
        # tells _watch_job this isn't a real failure, so it doesn't log an
        # error or set job.error for a kill this module itself initiated.
        self.superseded = False


_jobs_guard = threading.Lock()
_jobs: dict[Path, list] = {}  # hls_dir -> [_Job, ...] currently producing segments in it

_all_processes_guard = threading.Lock()
_all_processes: set = set()

# Caps how many *re-encoding* jobs run at once, system-wide -- separate from
# the per-hls_dir dedup above, which already prevents redundant jobs for the
# *same* video. This instead prevents N different incompatible-codec videos
# each spawning their own encode job and overwhelming a weak CPU/GPU.
# Stream-copy jobs (the existing container/audio-only remux) never touch
# this semaphore at all -- they stay as cheap and uncapped as before.
_transcode_semaphore = threading.Semaphore(config.MAX_CONCURRENT_TRANSCODES)


def _reap_idle_jobs_locked():
    """Must be called with _jobs_guard held. Terminates any job across ANY
    hls_dir that hasn't had a segment requested from it in JOB_IDLE_TIMEOUT
    -- called opportunistically at the top of every _find_or_start_job call
    (i.e. on every real segment request, for any video), rather than on a
    dedicated background thread/timer, matching this module's existing
    request-driven style. Real consequence of not doing this: an abandoned
    re-encode job runs to end-of-file, holding the transcode semaphore for
    however much of the file is left and starving every other re-encode
    request in the meantime."""
    now = time.monotonic()
    for jobs in _jobs.values():
        for job in jobs:
            if job.process.poll() is None and now - job.last_requested > JOB_IDLE_TIMEOUT:
                job.superseded = True
                job.process.terminate()


def _terminate_stale_jobs(hls_dir: Path):
    """Must be called with _jobs_guard held, right before starting a
    genuinely new job for hls_dir (see _find_or_start_job) -- stops every
    other still-running job for this same hls_dir first. Without this, an
    old job (still seeking through content nobody's watching anymore after
    a seek elsewhere) keeps running indefinitely and can race the new job
    writing the same segment_%05d.ts paths: confirmed real that two -c:v
    copy jobs started at different positions cut keyframe-aligned segments
    differently, so "segment N" from each can have different byte content,
    and ensure_segment's "next segment exists" completion check can then be
    satisfied by the *other* job's file mid-write, serving a truncated
    segment. A waiter still blocked on a job terminated this way gets a
    clean FileNotFoundError/404 (see _watch_job) rather than a corrupted
    file -- an accepted trade-off for a single-viewer-reseeks pattern,
    which is what actually triggers this path in practice."""
    for job in _jobs.get(hls_dir, []):
        if job.process.poll() is None:
            job.superseded = True
            job.process.terminate()


def _segment_path(hls_dir: Path, index: int) -> Path:
    return hls_dir / f"segment_{index:05d}.ts"


def _highest_contiguous_segment(hls_dir: Path, start: int) -> int:
    """How far a job starting at `start` has actually gotten, judged by
    which segment files already exist in an unbroken run from `start`.
    Checking the filesystem directly avoids needing any progress signal
    out of the ffmpeg process itself."""
    idx = start
    while _segment_path(hls_dir, idx).is_file():
        idx += 1
    return idx - 1


_SOURCE_MTIME_MARKER = ".source_mtime"


def _invalidate_if_source_changed(media_id: int, hls_dir: Path, src_path: Path) -> None:
    """Mirrors app/artwork.py's get_video_thumbnail mtime check: a cached
    segment existing on disk isn't proof it's still valid if the source file
    at this path was replaced in-place since the segment was cut (same path
    => same media_id => same hls_dir, so nothing else would ever notice).
    Deliberately a separate marker from _FORMAT_MARKER above, which is a
    one-time migration flag that's never revisited once set -- this one is
    checked on every call."""
    marker = hls_dir / _SOURCE_MTIME_MARKER
    try:
        source_mtime = src_path.stat().st_mtime
    except FileNotFoundError:
        return
    try:
        stale = marker.stat().st_mtime < source_mtime
    except FileNotFoundError:
        stale = False
    if stale:
        invalidate_segments(media_id)
        hls_dir.mkdir(parents=True, exist_ok=True)
    marker.touch()


def ensure_segment(
    media_id: int, src_path: Path, remux_audio: bool, index: int,
    reencode_video: bool = False, video_width=None, video_height=None,
    audio_stream_index=None, boundaries: list = None,
) -> Path:
    """Block until segment `index` is fully written to disk for this media
    (starting or reusing an ffmpeg job that produces it), then return its
    path. This is what makes seeking work during an in-progress conversion:
    a request for any segment index -- sequential or a forward/backward
    jump -- either finds it already cached, joins a job already headed
    there, or kicks off a new one seeked directly to that point.
    audio_stream_index (the scanner's chosen audio track, see
    app/scanner.py's _choose_audio_stream) is passed through to _start_job
    so the track actually served is guaranteed to be the same one
    resolve_playable_path validated -- not whatever ffmpeg's own default
    stream-selection heuristic would otherwise pick. boundaries (see
    compute_segment_boundaries) maps `index` to its exact keyframe start
    time -- any two jobs asked for "segment N" therefore cut identical
    content, unlike the old fixed-grid seek where -ss snapped to whatever
    keyframe was nearest N*6s. None falls back to the old fixed grid."""
    if boundaries is not None and index >= len(boundaries):
        # The playlist lists exactly len(boundaries) segments, so this is a
        # request for something that can never exist -- fail it before
        # spawning an ffmpeg job seeked past the end of the file.
        raise FileNotFoundError(f"segment {index} is past the end of the playlist")
    hls_dir = hls_dir_for(media_id)
    hls_dir.mkdir(parents=True, exist_ok=True)
    _ensure_segment_format(media_id, hls_dir)
    _invalidate_if_source_changed(media_id, hls_dir, src_path)
    target = _segment_path(hls_dir, index)

    job = _find_or_start_job(
        hls_dir, src_path, remux_audio, index, reencode_video, video_width, video_height,
        audio_stream_index, boundaries,
    )
    if job is None:
        # No active job could still be writing this file -- either a past
        # job finished it, or it's a leftover from a previous server run.
        # Safe to trust as complete.
        return target

    deadline = time.monotonic() + SEGMENT_WAIT_TIMEOUT
    while True:
        # A segment file existing isn't proof it's finished -- ffmpeg's
        # segment muxer keeps a file open for writing until it moves on to
        # the next one. Only trust it once we can prove ffmpeg has moved
        # past it (the next segment has appeared) or the job has exited
        # entirely (so every file it touched is necessarily closed).
        if target.is_file() and (_segment_path(hls_dir, index + 1).is_file() or job.done.is_set()):
            return target
        if job.done.is_set():
            # job.error is set (in the watcher thread) strictly before
            # job.done, so checking it here -- only once we know the job
            # has actually finished -- can never race and misreport a real
            # failure as "never produced" (checking job.error earlier in
            # this loop, before job.done, could observe it as still-None
            # even though it's set moments later in this same iteration).
            if job.error is not None:
                raise job.error
            # Job finished successfully without ever producing this index
            # -- most likely a seek past the end of the video.
            raise FileNotFoundError(f"segment {index} was never produced")
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out waiting for segment {index}")
        # Block on the job's progress event instead of an unconditional
        # sleep(0.1) -- wakes as soon as _poll_job_progress notices new
        # segment data, rather than up to 100ms late every iteration. Still
        # re-checks the real filesystem predicate above on every wake (the
        # event alone can't tell "my segment" from "some other segment"),
        # and clears the event itself (not the poller) so a pulse can't be
        # lost between two waiters that were both woken by the same set().
        job.progress_event.wait(timeout=min(1.0, max(0.0, deadline - time.monotonic())))
        job.progress_event.clear()


def _find_or_start_job(
    hls_dir: Path, src_path: Path, remux_audio: bool, index: int,
    reencode_video: bool = False, video_width=None, video_height=None,
    audio_stream_index=None, boundaries: list = None,
):
    """Returns the _Job that will (eventually) produce `index`, or None if
    the segment is already complete on disk with no active job that could
    still be writing to it (safe to serve immediately). Raises
    TranscodeUnavailable if reencode_video and no slot frees up within
    TRANSCODE_SLOT_TIMEOUT."""
    while True:
        with _jobs_guard:
            _reap_idle_jobs_locked()
            job_or_done = _check_jobs_locked(hls_dir, index)
            if job_or_done is not _NEED_NEW_JOB:
                return job_or_done
            if not reencode_video:
                _terminate_stale_jobs(hls_dir)
                job = _start_job(
                    hls_dir, src_path, remux_audio, index, False, video_width, video_height,
                    audio_stream_index, boundaries,
                )
                _jobs[hls_dir].append(job)
                return job
            # Don't acquire the (possibly-blocking) transcode semaphore
            # while holding _jobs_guard -- it's the single serialization
            # point across every media id's segment requests, so blocking
            # here would stall unrelated stream-copy requests too, not just
            # other re-encode ones.

        if not _transcode_semaphore.acquire(timeout=TRANSCODE_SLOT_TIMEOUT):
            raise TranscodeUnavailable()
        with _jobs_guard:
            # Re-check: another thread may have started a covering job (or
            # the segment may now exist) while we were waiting for a slot.
            job_or_done = _check_jobs_locked(hls_dir, index)
            if job_or_done is not _NEED_NEW_JOB:
                _transcode_semaphore.release()  # didn't end up needing it
                return job_or_done
            _terminate_stale_jobs(hls_dir)
            try:
                job = _start_job(
                    hls_dir, src_path, remux_audio, index, True, video_width, video_height,
                    audio_stream_index, boundaries,
                )
            except Exception:
                # _start_job can fail before ever spawning a process (see
                # its encode_video_args None check) -- if it does, nothing
                # will ever call _watch_job to release this slot, so it
                # must be released right here instead of leaking forever.
                _transcode_semaphore.release()
                raise
            _jobs[hls_dir].append(job)
            return job


_NEED_NEW_JOB = object()


def _check_jobs_locked(hls_dir: Path, index: int):
    """Must be called with _jobs_guard held. Returns an existing _Job that
    covers `index`, None if the segment is already complete on disk with
    nothing active that could still be writing it, or the _NEED_NEW_JOB
    sentinel if a new job needs to be started."""
    alive = [job for job in _jobs.get(hls_dir, []) if job.process.poll() is None]
    _jobs[hls_dir] = alive
    for job in alive:
        progress = _highest_contiguous_segment(hls_dir, job.start_index)
        if job.start_index <= index <= progress + 1 + LOOKAHEAD_SEGMENTS:
            job.last_requested = time.monotonic()
            return job
    if _segment_path(hls_dir, index).is_file():
        return None
    return _NEED_NEW_JOB


def _probe_seek_landing(src_path: Path, seconds: float):
    """The raw source-clock pts of the first video packet ffmpeg actually
    emits for `-ss <seconds> -i <src> -c:v copy` on THIS machine, or None
    if that couldn't be determined. This is the ground truth a seeked
    stream-copy job's construction has to be built on, because `-ss` does
    NOT reliably land on the keyframe at-or-before the requested time:
    confirmed live, ffmpeg's CLI subtracts an internal ~0.13s "dts
    heuristic" (3*AV_TIME_BASE/23) from the requested seek target whenever
    the video stream has B-frame reordering and the demuxer doesn't seek
    by pts -- true for mkv, the single most common container routed
    through this module -- so a seek aimed just past a keyframe snaps to
    the *previous* seek point instead. mp4-family inputs are exempt (their
    demuxer seeks by pts, so the heuristic is skipped and landings are
    exact). Rather than replicating that version-dependent constant, this
    asks the same ffmpeg binary the job will run to do the same seek and
    read out where it landed: one packet through -f framecrc, which
    prints "stream, dts, pts, ..." lines in stream-timebase units --
    demux-only, no decode, tens of milliseconds even on large files."""
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-v", "error",
                "-ss", f"{max(seconds, 0.0):.6f}", "-i", str(src_path),
                "-map", "0:v:0", "-c:v", "copy", "-copyts",
                "-frames:v", "1", "-f", "framecrc", "-",
            ],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None

    timebase = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("#tb 0:"):
            numerator, _, denominator = line.split()[-1].partition("/")
            try:
                timebase = int(numerator) / int(denominator)
            except (ValueError, ZeroDivisionError):
                return None
        elif line and not line.startswith("#"):
            parts = [p.strip() for p in line.split(",")]
            if timebase is None or len(parts) < 3:
                return None
            try:
                return float(parts[2]) * timebase  # stream, dts, PTS, ...
            except ValueError:
                return None
    return None


def _resolve_copy_seek(src_path: Path, boundaries: list, start_index: int):
    """Find a seek target for a stream-copy job that provably lands the
    demuxer on a segment boundary, by probing where candidate targets
    actually land (see _probe_seek_landing) instead of assuming. Returns
    (effective_start_index, seek_target_seconds_or_None) -- the job then
    starts at that (possibly earlier) boundary, regenerating a few real
    segments on its way to the requested one, which stream-copy chews
    through far faster than real time. seek_target None means "start from
    the top of the file, no -ss at all", which is always exact by
    definition. Returns None if probing itself failed (no ffmpeg, weird
    file) -- the caller falls back to trusting the requested target, which
    is what this module did before the probe existed and is exact for
    mp4-family sources.

    The walk: probe the wanted boundary's target; if the landing IS a
    boundary (any boundary -- earlier is fine), done. Otherwise re-aim at
    the landing itself and probe again; each landing is strictly earlier,
    so this terminates, and SEEK_PROBE_LIMIT caps the pathological case.
    A landing at/before the first boundary just means "start from 0"."""
    origin = _probe_seek_landing(src_path, 0.0)
    if origin is None:
        return None

    # Boundaries are stored file-relative (normalized to the first
    # keyframe, see compute_segment_boundaries); probes speak the raw
    # source clock, which for some containers (MPEG-TS notably) starts
    # past zero -- `origin` converts between the two.
    target = boundaries[start_index] + origin + KEYFRAME_TIME_GUARD
    for _ in range(SEEK_PROBE_LIMIT):
        landing = _probe_seek_landing(src_path, target)
        if landing is None:
            return None
        rel = landing - origin
        if rel <= boundaries[0] + _LANDING_MATCH_TOLERANCE:
            return 0, None
        matched = None
        for i, boundary in enumerate(boundaries):
            if abs(boundary - rel) <= _LANDING_MATCH_TOLERANCE:
                matched = i
                break
        if matched is not None:
            # The job re-runs this exact seek: same binary, same file,
            # same target -- same landing.
            return matched, target
        target = landing + KEYFRAME_TIME_GUARD
    return 0, None


def _start_job(
    hls_dir: Path, src_path: Path, remux_audio: bool, start_index: int,
    reencode_video: bool = False, video_width=None, video_height=None,
    audio_stream_index=None, boundaries: list = None,
) -> "_Job":
    """Caller is responsible for holding a transcode-semaphore slot already
    (see _find_or_start_job) when reencode_video is True -- this function
    only spawns the process and never blocks."""
    # -ac 2: whenever audio is actually transcoded, always downmix to
    # stereo -- Chromium's MSE AAC decoder rejects anything above 2
    # channels outright (confirmed real, see MAX_DIRECT_PLAY_AUDIO_CHANNELS
    # above), so a straight "-c:a aac" on a 5.1/7.1 source would produce a
    # multichannel AAC track that's valid per ffprobe but unplayable in a
    # real browser. A no-op for a source that's already stereo/mono.
    audio_args = ["-c:a", "aac", "-ac", "2"] if remux_audio else ["-c:a", "copy"]
    segment_pattern = str(hls_dir / "segment_%05d.ts")

    # Every job runs under -copyts: segments carry the source's own
    # CONTINUOUS timestamps (segment N's internal clock starts at boundary
    # N, plus the mpegts muxer's fixed startup offset), exactly like a
    # normal pre-segmented HLS VOD stream. This replaced -reset_timestamps
    # 1 (every segment restarting near 0), which turned out to be the PP6
    # audio-desync root cause: per-segment resets are non-compliant HLS,
    # and hls.js only *appears* to cope -- its video remuxer re-anchors on
    # the big PTS jump every fragment, but its audio remuxer treats the
    # reset audio as overlapping already-buffered content and drops it.
    # Observed against a real library file (Chromium, hls.js 1.6,
    # SourceBuffer instrumentation): video buffered 250s ahead while
    # audio starved along ~1s ahead of the playhead, hls.js re-fetching
    # early fragments over and over to squeeze out slivers of audio --
    # audibly broken sound on every HLS-routed file. With continuous
    # timestamps both buffers advance in lockstep (verified the same
    # way). A second bug this kills by construction: a seeked -copyts job
    # under reset_timestamps kept absolute timestamps in its FIRST
    # segment only (the reset only kicks in from the first split), so the
    # same index could carry wildly different timestamps depending on
    # which job wrote it; now every job emits the one absolute timeline,
    # so any two jobs' output for segment N is identical. hls.js maps the
    # constant mux offset out via initPTS from whichever fragment it
    # loads first -- correct even when playback starts mid-file.
    # -avoid_negative_ts disabled: on a B-frame source the very first
    # packet's dts is negative (one reorder delay below pts 0), and the
    # default "auto" quietly shifts a from-zero job's entire timeline up
    # by that amount to compensate -- a shift no seeked job (starting at
    # a positive dts) ever gets, leaving the same index up to one reorder
    # delay apart across jobs (caught by the timestamp-equality
    # regression test). Disabling it keeps every job on the source's
    # exact clock; the mpegts muxer's own fixed startup offset (1.4s)
    # comfortably absorbs the small negative input dts.
    copyts_args = ["-copyts", "-avoid_negative_ts", "disabled"]
    effective_index = start_index
    if boundaries is not None:
        # Keyframe-accurate cutting: start the job at a stored boundary and
        # split at every subsequent one, so the segments this job writes
        # match build_playlist's EXTINF values exactly. The segment muxer
        # measures -segment_times against the first packet it receives
        # (confirmed empirically, -copyts or not -- absolute times don't
        # align if the demuxer lands anywhere other than assumed), so
        # everything below is anchored to where the seek provably lands,
        # not to where it was aimed. KEYFRAME_TIME_GUARD (see its comment)
        # keeps float/timebase rounding from snapping the seek to the
        # previous keyframe or a split to the next one.
        if start_index and not reencode_video:
            resolved = _resolve_copy_seek(src_path, boundaries, start_index)
            if resolved is not None:
                # Anchor the job to the probed landing: it may be an
                # earlier boundary than requested (mkv seeks land short,
                # see _probe_seek_landing), in which case the job starts
                # there and regenerates a few real segments on the way.
                # -noaccurate_seek keeps a transcoded audio track aligned
                # with the copied video: accurate_seek would decode-drop
                # audio up to the *requested* time while copied video
                # packets flow from the *landing*, leaving the first
                # segments silent.
                effective_index, seek_target = resolved
                if seek_target is not None:
                    seek_args = ["-noaccurate_seek", "-ss", f"{seek_target:.6f}"]
                else:
                    seek_args = []
                reference = boundaries[effective_index]
            else:
                # Probing unavailable (no ffmpeg? unreadable file?) --
                # trust the requested target the way this module did
                # before the probe existed. Exact for mp4-family sources;
                # the job spawn below would fail loudly anyway if ffmpeg
                # is genuinely gone.
                seek_args = ["-ss", f"{boundaries[start_index] + KEYFRAME_TIME_GUARD:.6f}"]
                reference = boundaries[start_index] + KEYFRAME_TIME_GUARD
        elif start_index:
            # Re-encode: the video is decoded, so accurate_seek drops
            # decoded frames up to the requested time -- output is
            # frame-exact regardless of where the demuxer landed, no
            # probing needed. The guard is subtracted here, not added:
            # accurate_seek keeps frames at-or-after the target, so
            # aiming just *past* the boundary (as the demux-seek paths
            # above must) would drop the boundary frame itself and start
            # the segment one frame late -- and since -segment_times are
            # measured from the first packet, that one-frame skew would
            # also push every split target past the keyframe forced
            # exactly at the next boundary, missing every cut. Verified
            # against a real encode: boundary-minus-guard starts the
            # output on the boundary frame exactly.
            seek_args = ["-ss", f"{boundaries[start_index] - KEYFRAME_TIME_GUARD:.6f}"]
            reference = boundaries[start_index]
        else:
            seek_args = []
            reference = 0.0

        split_times = [
            f"{b - reference - KEYFRAME_TIME_GUARD:.6f}"
            for b in boundaries[effective_index + 1:]
        ]
        if split_times:
            segment_muxer_args = ["-segment_times", ",".join(split_times)]
        else:
            # Job starts at the final segment: nothing left to split at,
            # but the muxer's default segment_time is 2s, so an explicit
            # never-reached value is needed to keep the remainder as one
            # segment.
            segment_muxer_args = ["-segment_time", "999999"]
    else:
        # Fixed-grid fallback for a file whose keyframes couldn't be probed
        # (see build_playlist) -- the original behavior, kept degraded
        # rather than removed: -ss snaps to whichever keyframe is nearest
        # the grid position, and real segment lengths won't match the
        # playlist's claimed 6s, but it still plays after a fashion (and
        # better than it used to: under -copyts hls.js at least sees the
        # real timeline instead of per-segment resets it can't stitch).
        seek_args = ["-ss", str(start_index * SEGMENT_SECONDS)] if start_index else []
        segment_muxer_args = ["-segment_time", str(SEGMENT_SECONDS)]

    # Explicit -map: without this, ffmpeg falls back to its own default
    # stream-selection heuristic (for audio, not simply "first" -- it
    # tends to prefer the highest channel count), which can silently
    # disagree with the stream app/scanner.py's _choose_audio_stream
    # already validated for compatibility/language -- e.g. serving a
    # foreign-language or commentary track instead of the English one that
    # was actually checked. audio_stream_index is None for a row scanned
    # before this existed or a file with no audio at all -- map_args stays
    # empty in that case (not just the audio -map) so ffmpeg's default
    # auto-selection still picks both video and audio itself, exactly as
    # before this existed. A -map for only one stream type is deliberately
    # never used: -map restricts the output to *only* what's explicitly
    # mapped, so "-map 0:v:0" alone would silently drop audio entirely
    # rather than falling back to auto-selecting it.
    map_args = []
    if audio_stream_index is not None:
        map_args = ["-map", "0:v:0", "-map", f"0:a:{audio_stream_index}"]

    # For a re-encode, the encoder places keyframes wherever its own GOP
    # logic likes -- forcing one at every boundary guarantees each output
    # segment still starts with an IDR frame (a decodable entry point, which
    # is what makes seeking to any segment work), exactly like the
    # stream-copy path gets for free by cutting at source keyframes. Same
    # relative-to-seek-origin times as the split points, minus the guard:
    # the forced keyframe must land at-or-after its split point so it's the
    # frame the muxer actually cuts on.
    force_keyframe_args = []
    if reencode_video and boundaries is not None and boundaries[start_index + 1:]:
        # Absolute times, not relative to the seek: -copyts disables
        # ffmpeg's -ss timestamp shifting for the whole pipeline, so the
        # encoder compares these against the source's own clock (verified
        # against a real encode -- a relative time under -copyts simply
        # never fires, leaving segments to split at whatever the encoder's
        # default GOP interval happens to produce).
        force_keyframe_args = [
            "-force_key_frames",
            ",".join(f"{b:.6f}" for b in boundaries[start_index + 1:]),
        ]

    pre_input_args = []
    if reencode_video:
        # Only reached when resolve_playable_path already confirmed
        # config.TRANSCODE_MODE allows re-encoding and a working (and, for
        # "auto", fast-enough) encoder -- get_encoder() is cached, this
        # doesn't re-probe. encode_video_args also wires up
        # whatever hwupload/device plumbing this specific encoder needs
        # (see app/encoder_detect.py) -- VAAPI/QSV can't just take -c:v on
        # its own the way stream-copy or NVENC/software can.
        encoder = encoder_detect.get_encoder()
        scale_args = _scale_args(video_width, video_height)
        scale_filter = scale_args[1] if scale_args else ""
        pre_input_args, video_args = encoder_detect.encode_video_args(encoder, video_width, video_height, scale_filter)
        if pre_input_args is None:
            # e.g. get_encoder() cached "h264_vaapi" earlier this process's
            # life, but the render node it needs has since disappeared
            # (device unplugged, permissions changed) -- get_encoder()
            # won't re-probe, so this would otherwise recur on every
            # re-encode request until restart. Fail this one job clearly
            # instead of splatting None into the ffmpeg command below.
            raise RemuxFailed(
                f"Encoder '{encoder}' is no longer usable on this machine (its hardware "
                "device may have disappeared after being detected earlier this run) -- "
                "restart the server to re-detect a working encoder."
            )
    else:
        video_args = ["-c:v", "copy"]  # today's exact stream-copy path, untouched

    cmd = [
        "ffmpeg", "-y", "-v", "error",
        *pre_input_args,
        *seek_args,
        "-i", str(src_path),
        *map_args,
        *video_args, *force_keyframe_args, *audio_args, *copyts_args,
        "-f", "segment",
        *segment_muxer_args,
        "-segment_start_number", str(effective_index),
        # Deliberately NO -reset_timestamps here -- see the -copyts
        # comment at the top of this function for why per-segment resets
        # audibly broke audio in real browsers.
        segment_pattern,
    ]
    process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    with _all_processes_guard:
        _all_processes.add(process)
    job = _Job(process, effective_index, reencode_video)
    threading.Thread(target=_watch_job, args=(job, hls_dir), daemon=True).start()
    threading.Thread(target=_poll_job_progress, args=(job, hls_dir), daemon=True).start()
    return job


def _poll_job_progress(job: "_Job", hls_dir: Path):
    """The one place that still polls the filesystem for this job -- ffmpeg
    itself can't notify Python when it rotates to a new segment file, so
    something has to keep checking. Doing it here, once per job, is the
    actual fix for thread-pool pressure: previously every ensure_segment
    caller waiting on this job ran its own independent sleep(0.1) poll loop,
    so N concurrent viewers meant N threads churning through os.stat calls.
    Now there's exactly one poller regardless of how many requests are
    waiting; they just block on job.progress_event instead. Note this does
    NOT eliminate one thread being parked per in-flight request -- that's
    inherent to ensure_segment being called synchronously from FastAPI's
    sync threadpool, and fixing that would need an async route with an
    async wait primitive, out of scope here."""
    last_seen = job.start_index - 1
    while not job.done.is_set():
        current = _highest_contiguous_segment(hls_dir, job.start_index)
        if current != last_seen:
            last_seen = current
            job.progress_event.set()
        job.done.wait(timeout=0.1)


def _watch_job(job: "_Job", hls_dir: Path):
    stderr = b""
    try:
        _, stderr = job.process.communicate()
    finally:
        with _all_processes_guard:
            _all_processes.discard(job.process)
        if job.reencode_video:
            _transcode_semaphore.release()
    if job.superseded:
        # Deliberately killed by _terminate_stale_jobs/_reap_idle_jobs_locked
        # to make way for a newer job or because nobody was still watching
        # it -- not a real failure, don't log an error or set job.error for
        # a kill this module itself initiated. Any request still waiting on
        # this specific job (see ensure_segment) gets a plain
        # FileNotFoundError once job.done is set below, same as any other
        # "job finished without producing this segment" case.
        job.progress_event.set()
        job.done.set()
        return
    if job.process.returncode != 0:
        message = stderr.decode(errors="replace").strip() or f"ffmpeg exited {job.process.returncode}"
        logger.error(
            "HLS segment generation failed for %s (starting at segment %s): %s",
            hls_dir, job.start_index, message,
        )
        job.error = RemuxFailed(message)
    else:
        cache.prune()
    job.progress_event.set()
    job.done.set()


def terminate_all_jobs():
    """Best-effort termination of any still-running ffmpeg jobs. Called from
    app/main.py's shutdown so restarting/stopping the server doesn't leave
    orphaned ffmpeg processes running in the background."""
    with _all_processes_guard:
        processes = list(_all_processes)
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
