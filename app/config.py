import mimetypes
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _parse_int_env(var_name: str, default):
    """int(os.environ[var_name]) with a readable failure instead of a bare
    traceback pointing into this module's internals -- this runs at import
    time (before the app/its logging is even set up), so an invalid value
    (e.g. a stray typo in a systemd env file) used to crash with nothing
    but "ValueError: invalid literal for int() with base 10: '...'" and no
    indication of which setting or how to fix it."""
    raw = os.environ.get(var_name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(
            f"{var_name}={raw!r} isn't a whole number -- fix it, or unset it to use "
            f"the default ({default!r})."
        )

MEDIA_DIRS = [
    Path(p) for p in os.environ.get("PARZTREAM_MEDIA_DIRS", "").split(os.pathsep) if p
]

DB_PATH = Path(os.environ.get("PARZTREAM_DB_PATH", BASE_DIR / "parztream.db"))

# Where remuxed/audio-transcoded copies of videos get cached (see
# app/transcode.py). Roughly the size of the originals that need it, since
# video is copied, not re-encoded.
CACHE_DIR = Path(os.environ.get("PARZTREAM_CACHE_DIR", BASE_DIR / "cache"))

# Optional cap on CACHE_DIR's total size, in bytes -- oldest cached files are
# deleted (after a new one is created) once it's exceeded. Unset/0 means no
# limit, matching prior behavior, since deleting things nobody asked to be
# capped by default would be a surprising default.
CACHE_MAX_BYTES = _parse_int_env("PARZTREAM_CACHE_MAX_BYTES", None)

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".m4b", ".ogg", ".wav", ".aac"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm"}

# mimetypes.guess_type() consults platform tables (the Windows registry,
# /etc/mime.types on Linux), so what it knows varies machine to machine:
# .m4b is unknown everywhere, and .mkv resolves fine on Linux but came
# back unknown on a clean Windows install (caught by the first Windows CI
# run: streaming served application/octet-stream there, which browsers
# won't play). Register every extension we serve explicitly so the answer
# is deterministic on all platforms.
for _ext, _mime in {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".m4b": "audio/mp4",  # audiobook chapters, same MPEG-4 container as .m4a
    ".ogg": "audio/ogg",
    ".wav": "audio/x-wav",
    ".aac": "audio/aac",
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}.items():
    mimetypes.add_type(_mime, _ext)

# A 4-digit PIN rather than an arbitrary password -- faster to type on a
# phone/TV remote for a home-LAN tool where the realistic threat model is
# "someone on my network I don't trust", not a sophisticated remote
# attacker. Not validated to actually be 4 digits here (see auth.py's
# startup warning) so an existing longer value keeps working rather than
# hard-breaking on upgrade.
AUTH_PIN = os.environ.get("PARZTREAM_PIN")

# Signs session cookies (see app/auth.py). If unset, a random key is
# generated at every process start -- simplest zero-config default, at the
# cost of everyone's session getting invalidated (and needing to log in
# again) on every restart. Set PARZTREAM_SECRET_KEY to a fixed random value
# to keep sessions alive across restarts.
SECRET_KEY = os.environ.get("PARZTREAM_SECRET_KEY") or secrets.token_hex(32)

# How long a login lasts before needing to sign in again, in seconds.
# Deliberately long (90 days): this gates a home media library behind a
# single shared password, not sensitive per-user data, and the people most
# affected by frequent forced re-logins are exactly the least technical
# users this is meant to be easy for.
SESSION_MAX_AGE = 60 * 60 * 24 * 90

# The name advertised over mDNS -- http://<this>.local:<PORT>/ (see
# app/mdns.py). Also handy as the machine's actual OS hostname (a separate,
# manual deployment step -- see README) so plain http://<this>:PORT/ also
# resolves on networks whose router auto-registers DHCP hostnames.
MDNS_HOSTNAME = os.environ.get("PARZTREAM_MDNS_HOSTNAME", "parztream")

# Purely informational for the mDNS advertisement -- the app has no way to
# introspect what port uvicorn was actually started with, so this must be
# kept in sync with whatever --port is passed on the command line (see
# deploy/ templates). Wrong value just means the advertised address points
# at a port nothing's listening on; doesn't affect the app itself.
PORT = _parse_int_env("PARZTREAM_PORT", 8000)

MDNS_ENABLED = os.environ.get("PARZTREAM_MDNS_ENABLED", "true").lower() not in ("0", "false", "no")

# Extra Host header values SessionAuthMiddleware should trust beyond its
# built-in allowlist (localhost/loopback, *.local, and any private-use IP
# literal -- see app/auth.py's _is_trusted_host). Needed for setups where the
# request's Host header won't be one of those, e.g. a reverse proxy fronting
# this app under a real hostname, or a Docker bridge network. Comma-separated,
# compared case-insensitively, e.g. "media.example.internal,10.0.0.5".
TRUSTED_HOSTS = {
    h.strip().lower() for h in os.environ.get("PARZTREAM_TRUSTED_HOSTS", "").split(",") if h.strip()
}

# Enables real video re-encoding (not just container/audio remuxing) for
# videos whose video codec itself can't be played in a browser (e.g. HEVC)
# -- see app/transcode.py and app/encoder_detect.py. Off by default: even
# with a detected hardware encoder, this is meaningfully more CPU/GPU-
# intensive than the existing stream-copy remux path, and the only software
# encoder guaranteed present in the vendored ffmpeg (libopenh264, chosen for
# LGPL licensing -- see ADVANCED.md) is noticeably lower quality-per-bit
# than libx264. When unset, UnsupportedVideoCodec -> download-only behavior
# is completely unchanged, and app/encoder_detect.py is never even called.
TRANSCODE_ENABLED = os.environ.get("PARZTREAM_ENABLE_TRANSCODE", "").lower() in ("1", "true", "yes")

# Caps how many *re-encoding* (not stream-copy remux) HLS jobs run at once,
# system-wide -- separate from app/transcode.py's existing per-media job
# dedup, which already prevents redundant jobs for the *same* video. This
# instead prevents N different videos each spawning their own encode job
# and overwhelming a weak CPU (the libopenh264 software fallback) or a
# modest GPU. Small default: this is a home-LAN tool, not a transcoding farm.
MAX_CONCURRENT_TRANSCODES = _parse_int_env("PARZTREAM_MAX_CONCURRENT_TRANSCODES", 1)

# Caps how many video-thumbnail ffmpeg processes (app/artwork.py) run at
# once, system-wide. Without this, a first-ever poster-grid load of, say,
# 50 uncached tiles spawned 50 concurrent ffmpeg frame-grabs -- each one
# individually cheap, but 50 at once competing for disk/CPU noticeably
# stutters anyone already watching something. A single-frame grab is much
# cheaper than a transcode, so this default is higher than
# MAX_CONCURRENT_TRANSCODES's.
MAX_CONCURRENT_THUMBNAILS = _parse_int_env("PARZTREAM_MAX_CONCURRENT_THUMBNAILS", 3)
