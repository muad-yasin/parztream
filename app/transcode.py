import logging
import math
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

# Length of each on-demand HLS segment, in seconds. Short enough that a
# forward seek into not-yet-generated territory only waits a few seconds
# for one segment (stream-copy is fast), long enough not to spawn an
# unreasonable number of tiny ffmpeg-adjacent files for a long video.
SEGMENT_SECONDS = 6

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
    the video codec itself is incompatible AND config.TRANSCODE_ENABLED AND
    a working encoder was detected (see resolve_playable_path) -- otherwise
    that case still raises UnsupportedVideoCodec exactly as before this
    existed."""

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

    # No codec info yet (ffprobe unavailable at scan time, or this row was
    # scanned before this feature existed) -- don't guess, just direct play
    # as before.
    if video_codec is None:
        return path

    video_ok = video_codec.lower() in COMPATIBLE_VIDEO_CODECS
    audio_ok = audio_codec is None or audio_codec.lower() in COMPATIBLE_AUDIO_CODECS
    container_ok = path.suffix.lower() in DIRECT_PLAY_CONTAINERS

    if video_ok and audio_ok and container_ok:
        return path

    if not video_ok:
        # Real re-encoding is opt-in (config.TRANSCODE_ENABLED, off by
        # default) and only attempted if a working encoder was actually
        # detected on this machine -- when either isn't true, this stays
        # exactly today's dead end (download-only), never a slow/broken
        # attempt at a codec this box genuinely can't encode. Short-circuit
        # evaluation means get_encoder() is never even called (no probing
        # subprocesses spawned) when transcoding isn't enabled at all.
        if config.TRANSCODE_ENABLED:
            if encoder_detect.get_encoder() is not None:
                raise NeedsHlsRemux(remux_audio=not audio_ok, reencode_video=True)
            raise UnsupportedVideoCodec(video_codec, transcode_enabled=True)
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


def build_playlist(duration: float) -> str:
    """A complete, static VOD playlist computed once from the file's known
    duration (from ffprobe at scan time) -- not ffmpeg's own growing "event"
    playlist. Since the total duration is already known upfront, there's no
    need for live-playlist semantics: every segment index is listed
    immediately, and each segment's actual bytes are generated on demand
    (see ensure_segment) whenever a player first requests it, whether that's
    sequential playback or a seek."""
    total_segments = max(1, math.ceil(duration / SEGMENT_SECONDS))
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{SEGMENT_SECONDS}",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]
    remaining = duration
    for i in range(total_segments):
        seg_len = min(SEGMENT_SECONDS, remaining) if remaining > 0 else SEGMENT_SECONDS
        lines.append(f"#EXTINF:{seg_len:.3f},")
        lines.append(f"segment_{i:05d}.ts")
        remaining -= SEGMENT_SECONDS
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


def ensure_segment(
    media_id: int, src_path: Path, remux_audio: bool, index: int,
    reencode_video: bool = False, video_width=None, video_height=None,
) -> Path:
    """Block until segment `index` is fully written to disk for this media
    (starting or reusing an ffmpeg job that produces it), then return its
    path. This is what makes seeking work during an in-progress conversion:
    a request for any segment index -- sequential or a forward/backward
    jump -- either finds it already cached, joins a job already headed
    there, or kicks off a new one seeked directly to that point."""
    hls_dir = hls_dir_for(media_id)
    hls_dir.mkdir(parents=True, exist_ok=True)
    target = _segment_path(hls_dir, index)

    job = _find_or_start_job(
        hls_dir, src_path, remux_audio, index, reencode_video, video_width, video_height,
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
        time.sleep(0.1)


def _find_or_start_job(
    hls_dir: Path, src_path: Path, remux_audio: bool, index: int,
    reencode_video: bool = False, video_width=None, video_height=None,
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
                job = _start_job(hls_dir, src_path, remux_audio, index, False, video_width, video_height)
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


def _start_job(
    hls_dir: Path, src_path: Path, remux_audio: bool, start_index: int,
    reencode_video: bool = False, video_width=None, video_height=None,
) -> "_Job":
    """Caller is responsible for holding a transcode-semaphore slot already
    (see _find_or_start_job) when reencode_video is True -- this function
    only spawns the process and never blocks."""
    audio_args = ["-c:a", "aac"] if remux_audio else ["-c:a", "copy"]
    seek_args = ["-ss", str(start_index * SEGMENT_SECONDS)] if start_index else []
    segment_pattern = str(hls_dir / "segment_%05d.ts")

    pre_input_args = []
    if reencode_video:
        # Only reached when resolve_playable_path already confirmed
        # config.TRANSCODE_ENABLED and a working encoder -- get_encoder()
        # is cached, this doesn't re-probe. encode_video_args also wires up
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
        *video_args, *audio_args,
        "-f", "segment",
        "-segment_time", str(SEGMENT_SECONDS),
        "-segment_start_number", str(start_index),
        "-reset_timestamps", "1",
        segment_pattern,
    ]
    process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    with _all_processes_guard:
        _all_processes.add(process)
    job = _Job(process, start_index, reencode_video)
    threading.Thread(target=_watch_job, args=(job, hls_dir), daemon=True).start()
    return job


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
