import threading
from pathlib import Path

from .config import CACHE_DIR, CACHE_MAX_BYTES

# Shared by anything that writes derived artifacts into CACHE_DIR --
# currently app/transcode.py's remuxed videos and app/artwork.py's video
# thumbnails -- so they're pruned as one combined budget rather than each
# tracking their own.

_locks_guard = threading.Lock()
_locks = {}


def lock_for(key: str) -> threading.Lock:
    """Return a threading.Lock for the given key, creating one on first use.
    Callers use this to serialize creation of a specific cached file (keyed
    by its path) so concurrent requests for the same not-yet-cached
    resource don't each spawn their own ffmpeg process racing to write the
    same output path -- confirmed live to otherwise produce different byte
    content to different clients for what should be one canonical file.
    Locks are never removed once created; for a home media library's scale
    that's at most a few thousand small Lock objects over the process's
    lifetime, not worth the complexity of eviction."""
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


def prune(protect: Path):
    """Delete the oldest cached files until CACHE_DIR is back under
    CACHE_MAX_BYTES, if a limit is configured. Never deletes `protect` (the
    file that was just created and is about to be served for this request),
    even if it alone exceeds the limit -- an oversized cache in that edge
    case is preferable to breaking the request that just created it.

    Tolerant of files disappearing mid-computation: two different resources
    (e.g. one media item's remux and another's thumbnail) can legitimately
    finish and prune around the same time, each unaware of the other, and
    race on evicting the same old file."""
    if CACHE_MAX_BYTES is None:
        return

    sized = []
    for p in CACHE_DIR.glob("*"):
        try:
            if p.is_file():
                sized.append((p, p.stat().st_size, p.stat().st_mtime))
        except FileNotFoundError:
            continue  # deleted by a concurrent prune() -- just skip it

    total = sum(size for _, size, _ in sized)
    evictable = sorted((entry for entry in sized if entry[0] != protect), key=lambda entry: entry[2])

    for f, size, _mtime in evictable:
        if total <= CACHE_MAX_BYTES:
            break
        f.unlink(missing_ok=True)
        total -= size
