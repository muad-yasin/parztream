import logging
import math
import subprocess
import threading
import time
from pathlib import Path

from . import cache
from .config import CACHE_DIR

logger = logging.getLogger("parztream")

# Codecs essentially every modern browser can decode natively.
COMPATIBLE_VIDEO_CODECS = {"h264", "vp8", "vp9", "av1"}
COMPATIBLE_AUDIO_CODECS = {"aac", "mp3", "opus", "vorbis"}

# Containers browsers can open directly, independent of what's inside.
DIRECT_PLAY_CONTAINERS = {".mp4", ".webm"}

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


class UnsupportedVideoCodec(Exception):
    """Raised when a video's codec itself (not just its container or audio
    track) can't be played in a browser without a full re-encode, which
    isn't implemented -- only cheap container/audio fixes are."""


class NeedsHlsRemux(Exception):
    """Raised by resolve_playable_path when the file's container or audio
    track needs fixing before it can play in a browser. Callers (see
    app/routers/stream.py) should route to the HLS playlist/segment
    endpoints -- build_playlist()/ensure_segment() -- instead of treating
    this row as a plain streamable file."""

    def __init__(self, remux_audio: bool):
        self.remux_audio = remux_audio
        super().__init__(f"remux_audio={remux_audio}")


class RemuxFailed(Exception):
    """Raised/stored when an HLS segment-generation ffmpeg process exits
    non-zero. Carries ffmpeg's stderr output for diagnostics."""


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
        raise UnsupportedVideoCodec(video_codec)

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


class _Job:
    def __init__(self, process: subprocess.Popen, start_index: int):
        self.process = process
        self.start_index = start_index
        self.done = threading.Event()
        self.error: RemuxFailed | None = None


_jobs_guard = threading.Lock()
_jobs: dict[Path, list] = {}  # hls_dir -> [_Job, ...] currently producing segments in it

_all_processes_guard = threading.Lock()
_all_processes: set = set()


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


def ensure_segment(media_id: int, src_path: Path, remux_audio: bool, index: int) -> Path:
    """Block until segment `index` is fully written to disk for this media
    (starting or reusing an ffmpeg job that produces it), then return its
    path. This is what makes seeking work during an in-progress conversion:
    a request for any segment index -- sequential or a forward/backward
    jump -- either finds it already cached, joins a job already headed
    there, or kicks off a new one seeked directly to that point."""
    hls_dir = hls_dir_for(media_id)
    hls_dir.mkdir(parents=True, exist_ok=True)
    target = _segment_path(hls_dir, index)

    job = _find_or_start_job(hls_dir, src_path, remux_audio, index)
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


def _find_or_start_job(hls_dir: Path, src_path: Path, remux_audio: bool, index: int):
    """Returns the _Job that will (eventually) produce `index`, or None if
    the segment is already complete on disk with no active job that could
    still be writing to it (safe to serve immediately)."""
    with _jobs_guard:
        alive = [job for job in _jobs.get(hls_dir, []) if job.process.poll() is None]
        _jobs[hls_dir] = alive
        for job in alive:
            progress = _highest_contiguous_segment(hls_dir, job.start_index)
            if job.start_index <= index <= progress + 1 + LOOKAHEAD_SEGMENTS:
                return job
        if _segment_path(hls_dir, index).is_file():
            return None
        job = _start_job(hls_dir, src_path, remux_audio, index)
        alive.append(job)
        return job


def _start_job(hls_dir: Path, src_path: Path, remux_audio: bool, start_index: int) -> "_Job":
    audio_args = ["-c:a", "aac"] if remux_audio else ["-c:a", "copy"]
    seek_args = ["-ss", str(start_index * SEGMENT_SECONDS)] if start_index else []
    segment_pattern = str(hls_dir / "segment_%05d.ts")
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        *seek_args,
        "-i", str(src_path),
        "-c:v", "copy", *audio_args,
        "-f", "segment",
        "-segment_time", str(SEGMENT_SECONDS),
        "-segment_start_number", str(start_index),
        "-reset_timestamps", "1",
        segment_pattern,
    ]
    process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    with _all_processes_guard:
        _all_processes.add(process)
    job = _Job(process, start_index)
    threading.Thread(target=_watch_job, args=(job, hls_dir), daemon=True).start()
    return job


def _watch_job(job: "_Job", hls_dir: Path):
    stderr = b""
    try:
        _, stderr = job.process.communicate()
    finally:
        with _all_processes_guard:
            _all_processes.discard(job.process)
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
