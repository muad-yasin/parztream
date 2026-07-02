from pathlib import Path

from .config import CACHE_DIR, CACHE_MAX_BYTES

# Shared by anything that writes derived artifacts into CACHE_DIR --
# currently app/transcode.py's remuxed videos and app/artwork.py's video
# thumbnails -- so they're pruned as one combined budget rather than each
# tracking their own.


def prune(protect: Path):
    """Delete the oldest cached files until CACHE_DIR is back under
    CACHE_MAX_BYTES, if a limit is configured. Never deletes `protect` (the
    file that was just created and is about to be served for this request),
    even if it alone exceeds the limit -- an oversized cache in that edge
    case is preferable to breaking the request that just created it."""
    if CACHE_MAX_BYTES is None:
        return

    cached = [p for p in CACHE_DIR.glob("*") if p.is_file()]
    total = sum(f.stat().st_size for f in cached)
    evictable = sorted((f for f in cached if f != protect), key=lambda f: f.stat().st_mtime)

    for f in evictable:
        if total <= CACHE_MAX_BYTES:
            break
        total -= f.stat().st_size
        f.unlink()
