import glob
import logging
import subprocess
import sys
import threading

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
