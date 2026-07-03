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


def _try_encode(name: str) -> bool:
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-f", "lavfi", "-i", "color=c=black:size=64x64:rate=1:duration=1",
                "-frames:v", "1",
                "-c:v", name,
                "-f", "null", "-",
            ],
            capture_output=True, timeout=_PROBE_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return False
    return result.returncode == 0
