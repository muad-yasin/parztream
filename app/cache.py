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


def prune(protect: Path = None):
    """Delete the oldest cached files until CACHE_DIR is back under
    CACHE_MAX_BYTES, if a limit is configured. Never deletes `protect` (the
    file that was just created and is about to be served for this request),
    even if it alone exceeds the limit -- an oversized cache in that edge
    case is preferable to breaking the request that just created it.
    `protect` is optional since app/transcode.py's HLS segment jobs don't
    have one single file to protect -- their own recency (a segment written
    moments ago has a fresh mtime) already makes eviction naturally favor
    older, unwatched sessions first.

    Recurses one level into HLS segment directories (app/transcode.py,
    `{media_id}_hls/`) so individual segment files age out like any other
    cached file, rather than treating a whole directory as one unbreakable
    unit -- a missing segment is cheap to regenerate on the next request,
    same as any other cache miss here.

    Tolerant of files disappearing mid-computation: two different resources
    (e.g. one media item's remux and another's thumbnail) can legitimately
    finish and prune around the same time, each unaware of the other, and
    race on evicting the same old file. Also tolerant of a segment file
    disappearing/appearing while an HLS job is actively writing into its
    directory -- a rare race, acceptable here since eviction is opt-in
    (CACHE_MAX_BYTES unset by default) and a wrongly-evicted in-progress
    segment just gets regenerated on its next request like any cache miss."""
    if CACHE_MAX_BYTES is None:
        return

    sized = []
    for p in CACHE_DIR.glob("*"):
        try:
            if p.is_dir():
                for seg in p.glob("*"):
                    # Dotfiles are metadata, not cached content -- e.g.
                    # app/transcode.py's segment-format marker, whose
                    # eviction would wipe the whole directory's otherwise
                    # valid segments on the next request.
                    if seg.name.startswith("."):
                        continue
                    try:
                        if seg.is_file():
                            sized.append((seg, seg.stat().st_size, seg.stat().st_mtime))
                    except FileNotFoundError:
                        continue
            elif p.is_file():
                sized.append((p, p.stat().st_size, p.stat().st_mtime))
        except FileNotFoundError:
            continue  # deleted by a concurrent prune() -- just skip it

    total = sum(size for _, size, _ in sized)
    evictable = sorted(
        (entry for entry in sized if protect is None or entry[0] != protect),
        key=lambda entry: entry[2],
    )

    for f, size, _mtime in evictable:
        if total <= CACHE_MAX_BYTES:
            break
        f.unlink(missing_ok=True)
        total -= size
