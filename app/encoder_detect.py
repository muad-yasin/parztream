import glob
import logging
import subprocess
import sys
import threading
import time

logger = logging.getLogger("parztream")

# Tried in order per platform -- hardware first, since it's the only way
# real-time transcoding is viable on the underpowered hardware (NAS boxes,
# old laptops, Raspberry Pi) this project actually targets. All of these are
# license-clean regardless of the vendored ffmpeg's GPL/LGPL build variant,
# since they call OS/vendor APIs at runtime rather than bundling GPL code.
CANDIDATES_BY_PLATFORM = {
    "darwin": ["h264_videotoolbox"],
    "win32": ["h264_qsv", "h264_nvenc", "h264_amf", "h264_vaapi"],
    "linux": ["h264_qsv", "h264_vaapi", "h264_nvenc", "h264_amf"],
}

# The only software H.264 encoder guaranteed present in the vendored LGPL
# ffmpeg build (BSD-licensed) -- see ADVANCED.md. Noticeably lower
# quality-per-bit than libx264, which isn't present on Windows/Linux at all
# due to that LGPL choice (confirmed for macOS's Homebrew ffmpeg, which is
# already GPL, but not relied upon here to keep the detection path uniform
# across platforms).
SOFTWARE_FALLBACK = "libopenh264"

_UNSET = object()
_detected_encoder = _UNSET
_detect_lock = threading.Lock()

# Verifying a candidate is a real (if tiny) encode, not just a check that
# it's listed in `ffmpeg -encoders` -- a hardware encoder can be compiled in
# but fail at runtime (no GPU present, missing driver, no permissions).
# Bounded short so a hung driver can't block the first transcode request.
_PROBE_TIMEOUT = 5


def get_encoder():
    """Returns the ffmpeg -c:v value to use for a real re-encode, or None if
    nothing usable was found on this machine. Cached for the life of the
    process after the first call -- probing spawns a handful of subprocesses,
    worth paying once, not per request. Thread-safe: concurrent first-callers
    (e.g. two devices opening two HEVC files at once) only trigger one round
    of probing."""
    global _detected_encoder
    if _detected_encoder is not _UNSET:
        return _detected_encoder
    with _detect_lock:
        if _detected_encoder is _UNSET:
            _detected_encoder = _detect()
            logger.info("Transcode encoder detection: using %s", _detected_encoder or "none available")
    return _detected_encoder


def _detect():
    try:
        listed = _list_encoders()
    except (FileNotFoundError, subprocess.SubprocessError):
        return None

    for candidate in CANDIDATES_BY_PLATFORM.get(sys.platform, []):
        if candidate in listed and _try_encode(candidate):
            return candidate

    if SOFTWARE_FALLBACK in listed and _try_encode(SOFTWARE_FALLBACK):
        return SOFTWARE_FALLBACK

    return None


def _list_encoders():
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        capture_output=True, text=True, timeout=10,
    )
    # Encoder lines look like " V....D libx264   libx264 H.264 / AVC ..." --
    # the name is always the second whitespace-separated token.
    names = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0][:1] in "VAS":
            names.add(parts[1])
    return names


# h264_vaapi/h264_qsv need frames already sitting on a hardware surface
# before the encoder will even open -- pointing either at a plain
# software-decoded frame fails immediately (confirmed real against an
# actual VAAPI render node: "Could not open encoder before EOF" without
# this wiring). NVENC/AMF/VideoToolbox accept normal software frames
# directly and upload internally, so they need none of this and are never
# passed to these two helpers.
_HWACCEL_ENCODERS = {"h264_vaapi", "h264_qsv"}


def _vaapi_device_path():
    """The render node ffmpeg's -vaapi_device should target. Picks the
    first of possibly several GPUs present -- good enough for this
    project's realistic single-GPU home-server/NAS/laptop targets; a
    multi-GPU box wanting a specific one can't be auto-detected correctly
    anyway and would need a real device-selection option, out of scope
    here. None if the machine has no render node at all (no GPU, or one
    without a kernel driver bound), in which case h264_vaapi can never
    work regardless of what ffmpeg itself reports supporting."""
    candidates = sorted(glob.glob("/dev/dri/renderD*"))
    return candidates[0] if candidates else None


def _hwaccel_pre_input_args(name: str):
    """ffmpeg args that must appear before -i to initialize the hardware
    device VAAPI/QSV encoding needs. None (not []) specifically means this
    candidate can't even be attempted here (e.g. vaapi with no render node
    present) -- distinct from "attempted and failed", so callers don't
    spawn a doomed ffmpeg process just to get the same answer more slowly."""
    if name == "h264_vaapi":
        device = _vaapi_device_path()
        return None if device is None else ["-vaapi_device", device]
    if name == "h264_qsv":
        return ["-init_hw_device", "qsv=hw", "-filter_hw_device", "hw"]
    return []


def _hwaccel_upload_filter(name: str) -> str:
    """The tail of a -vf filter chain that uploads a software frame onto
    the hardware surface VAAPI/QSV require -- appended after any scaling,
    since scaling itself still happens in software pixel-format space.
    Empty string for every other encoder (nothing to append)."""
    if name == "h264_vaapi":
        return "format=nv12,hwupload"
    if name == "h264_qsv":
        return "format=nv12,hwupload=extra_hw_frames=64"
    return ""


def encode_video_args(name: str, width, height, scale_filter: str = ""):
    """Builds the (pre_input_args, video_args) ffmpeg needs for a real
    re-encode with the given -c:v value -- shared by this module's own
    probing and app/transcode.py's real per-segment job, so the two can
    never drift apart (e.g. the probe passing but the real job missing the
    hwupload wiring, or vice versa). scale_filter is the caller's own
    already-built -vf chain (e.g. app/transcode.py's _scale_args), passed
    in rather than computed here since resolution-cap logic doesn't belong
    in this module."""
    pre_input_args = _hwaccel_pre_input_args(name)
    if pre_input_args is None:
        return None, None
    upload_filter = _hwaccel_upload_filter(name)
    if not upload_filter:
        video_args = ["-c:v", name] + (["-vf", scale_filter] if scale_filter else [])
        return pre_input_args, video_args
    combined = f"{scale_filter},{upload_filter}" if scale_filter else upload_filter
    return pre_input_args, ["-c:v", name, "-vf", combined]


def _try_encode(name: str) -> bool:
    pre_input_args, video_args = encode_video_args(name, None, None)
    if pre_input_args is None:
        return False  # e.g. h264_vaapi candidate but no render node present
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                *pre_input_args,
                "-f", "lavfi", "-i", "color=c=black:size=64x64:rate=1:duration=1",
                "-frames:v", "1",
                *video_args,
                "-f", "null", "-",
            ],
            capture_output=True, timeout=_PROBE_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


# Real-time factor an encoder must clear to auto-enable (config.TRANSCODE_MODE
# == "auto") -- deliberately well above 1.0x. A live HLS segment job has real
# overhead beyond pure encode throughput that a bare-1.0x benchmark leaves no
# room for at all: segment muxing, a viewer seeking mid-stream (a second job
# spun up alongside the first), another device's thumbnail/remux work
# competing for the same CPU/GPU. This is a starting judgment call, not a
# measured constant -- kept as a plain module constant rather than a new env
# var until there's a concrete reason to tune it (see CLAUDE.md's "don't add
# config just in case" convention).
MIN_REALTIME_FACTOR = 2.0

# 1080p, since app/transcode.py's _scale_args already caps real re-encodes at
# 1080p and never upscales -- benchmarking near the real ceiling a re-encode
# job would actually hit, not the trivial 64x64 existence-check size
# _try_encode uses (that one only proves the encoder *works*, not that it's
# *fast enough*).
_BENCHMARK_WIDTH = 1920
_BENCHMARK_HEIGHT = 1080
_BENCHMARK_CLIP_SECONDS = 3
# Generous but bounded: a hung driver during the benchmark must not hang the
# first real playback request indefinitely. Real hardware encoding 3 seconds
# of 1080p should finish in a small fraction of this on anything that could
# plausibly pass the MIN_REALTIME_FACTOR bar at all.
_BENCHMARK_TIMEOUT = 30

_UNSET_CAPABLE = object()
_auto_capable = _UNSET_CAPABLE
_capable_lock = threading.Lock()


def _measure_encode_seconds(pre_input_args, video_args):
    """Runs the benchmark encode and returns the wall-clock seconds it took,
    or None if it failed/timed out. Factored out as its own function
    specifically so tests can monkeypatch it to return an instant fake
    duration -- same seam pattern this module already uses for _try_encode/
    _list_encoders -- rather than actually spending _BENCHMARK_CLIP_SECONDS
    of real wall-clock time (or more) in the unit suite."""
    start = time.monotonic()
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                *pre_input_args,
                "-f", "lavfi", "-i",
                f"color=c=black:size={_BENCHMARK_WIDTH}x{_BENCHMARK_HEIGHT}:rate=30:duration={_BENCHMARK_CLIP_SECONDS}",
                *video_args,
                "-f", "null", "-",
            ],
            capture_output=True, timeout=_BENCHMARK_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return time.monotonic() - start


def is_hardware_transcode_capable() -> bool:
    """True if a real hardware encoder (never the libopenh264 software
    fallback -- see MIN_REALTIME_FACTOR's docstring for why) was detected on
    this machine AND benchmarks fast enough for real-time HLS re-encoding.
    Cached for the life of the process, same double-checked-locking pattern
    as get_encoder() (whose own cache this reuses -- no re-probing for
    existence, only the new speed benchmark is added on top)."""
    global _auto_capable
    if _auto_capable is not _UNSET_CAPABLE:
        return _auto_capable
    with _capable_lock:
        if _auto_capable is _UNSET_CAPABLE:
            _auto_capable = _check_capable()
        return _auto_capable


def _check_capable() -> bool:
    encoder = get_encoder()
    if encoder is None or encoder == SOFTWARE_FALLBACK:
        return False

    pre_input_args, video_args = encode_video_args(encoder, None, None)
    if pre_input_args is None:
        return False  # shouldn't happen -- get_encoder() already confirmed this candidate works

    elapsed = _measure_encode_seconds(pre_input_args, video_args)
    if elapsed is None or elapsed <= 0:
        logger.info("Transcode auto-detection: %s benchmark failed or timed out -- leaving transcoding disabled", encoder)
        return False

    factor = _BENCHMARK_CLIP_SECONDS / elapsed
    capable = factor >= MIN_REALTIME_FACTOR
    if capable:
        logger.info(
            "Transcode auto-detection: %s encodes at %.1fx real-time -- enabling automatic transcoding",
            encoder, factor,
        )
    else:
        logger.info(
            "Transcode auto-detection: %s only encodes at %.1fx real-time (need >= %.1fx) -- "
            "leaving transcoding disabled; set PARZTREAM_ENABLE_TRANSCODE=1 to force it anyway",
            encoder, factor, MIN_REALTIME_FACTOR,
        )
    return capable
