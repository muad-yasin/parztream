import json
import logging
import os
import re
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from mutagen import File as MutagenFile

from . import settings
from .config import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS
from .db import get_connection

logger = logging.getLogger("parztream")

_scan_lock = threading.Lock()
_scan_state = {
    "status": "idle",
    "error": None,
    "last_scan_at": None,
    "scanned_count": 0,
    "failed_count": 0,
    "failed_examples": [],
    "incomplete_count": 0,
    "incomplete_examples": [],
}

# Cap on how many per-file diagnostic entries are kept in detail -- the
# counts (failed_count/incomplete_count) keep counting past this, only the
# example lists stop growing, so the JSON payload stays small even for a
# scan with hundreds of problem files.
_MAX_DIAGNOSTIC_EXAMPLES = 20


def get_scan_status():
    return dict(_scan_state)


def start_scan():
    """Try to claim the scan lock. Returns False if a scan is already running."""
    if not _scan_lock.acquire(blocking=False):
        return False
    _scan_state["status"] = "scanning"
    _scan_state["error"] = None
    _scan_state["scanned_count"] = 0
    _scan_state["failed_count"] = 0
    _scan_state["failed_examples"] = []
    _scan_state["incomplete_count"] = 0
    _scan_state["incomplete_examples"] = []
    return True


def run_claimed_scan():
    """Run a scan previously claimed with start_scan(). Releases the lock when done."""
    try:
        scan_media_dirs()
    except Exception as exc:
        _scan_state["status"] = "error"
        _scan_state["error"] = str(exc)
    else:
        _scan_state["status"] = "idle"
        _scan_state["last_scan_at"] = datetime.now(timezone.utc).isoformat()
    finally:
        _scan_lock.release()


def scan_media_dirs():
    found_paths = set()
    # Only roots that were actually available this scan -- a configured dir
    # that's temporarily missing (unmounted NAS, unplugged USB) must never
    # wipe its rows just because os.walk saw nothing under it this time.
    # _remove_missing only deletes rows living under one of these, leaving
    # rows under a currently-unavailable root completely untouched.
    scanned_roots = []
    with get_connection() as conn:
        for media_dir in settings.get_media_dirs():
            if not media_dir.is_dir():
                continue
            scanned_roots.append(media_dir)
            # followlinks=False: don't descend into symlinked subdirectories,
            # and individual symlinked files are skipped below -- otherwise a
            # symlink placed inside a scanned folder (even one named
            # "song.mp3") could point anywhere on disk and get scanned,
            # indexed, and served as if it were a real media file.
            # onerror: os.walk silently skips a directory it can't list by
            # default (e.g. a permission error partway through the tree) --
            # that's still a partial-scan risk (files under it look "missing"
            # and get removed below, same class of issue as the whole-root
            # case this function's scanned_roots tracking fixes), but at
            # least logs it instead of vanishing without a trace.
            for root, dirnames, filenames in os.walk(
                media_dir, followlinks=False,
                onerror=lambda exc: logger.warning("Scan: couldn't list a directory: %s", exc),
            ):
                root_path = Path(root)
                # This directory is a TV show folder if any of its immediate
                # children looks like a season folder -- used below to keep
                # a season folder that (so far) only has one ripped episode
                # from being mistaken for a movie folder.
                has_season_subfolder = any(
                    _SEASON_FOLDER_RE.fullmatch(d.strip()) for d in dirnames
                )

                video_paths = []
                audio_paths = []
                for filename in filenames:
                    path = root_path / filename
                    if path.is_symlink() or not path.is_file():
                        continue
                    ext = path.suffix.lower()
                    if ext in AUDIO_EXTENSIONS:
                        audio_paths.append(path)
                    elif ext in VIDEO_EXTENSIONS:
                        if _TRAILER_SAMPLE_RE.search(path.stem):
                            # Never added to the library at all -- if one of
                            # these was scanned before (e.g. it used to have
                            # a different name), _remove_missing below drops
                            # it since it's absent from found_paths.
                            continue
                        video_paths.append(path)
                    else:
                        continue

                # A folder with exactly one real video and no season
                # subfolders reads as a single movie -- its folder name is
                # usually clean even when the release filename inside it
                # isn't (e.g. "Inception (2010)/Inception.2010.GROUP.mkv").
                # Ambiguous cases (2+ real videos, no season structure) are
                # deliberately left alone rather than guessing which file
                # "is" the movie. Excludes an extras-bucket folder itself
                # (e.g. "Movie (2010)/Special Features/bonus.mkv") -- that
                # single video is bonus content, not a second movie titled
                # after its bucket folder's name.
                is_movie_folder = (
                    not has_season_subfolder and len(video_paths) == 1
                    and not _EXTRAS_FOLDER_RE.fullmatch(root_path.name.strip())
                )

                for path in video_paths:
                    found_paths.add(str(path))
                    try:
                        incomplete = _upsert_media(conn, path, "video", media_dir, is_movie_folder)
                    except Exception as exc:
                        _record_scan_failure(path, exc)
                        continue
                    _scan_state["scanned_count"] += 1
                    if incomplete:
                        _record_incomplete_metadata(path)
                for path in audio_paths:
                    found_paths.add(str(path))
                    try:
                        _upsert_media(conn, path, "audio", media_dir)
                    except Exception as exc:
                        _record_scan_failure(path, exc)
                        continue
                    _scan_state["scanned_count"] += 1

        _remove_missing(conn, found_paths, scanned_roots)


def _record_scan_failure(path: Path, exc: Exception):
    """A single file's processing failed (e.g. it vanished mid-scan, or hit
    a DB error) -- log it and record it as a diagnostic, but never let it
    abort the rest of the scan. Confirmed real bug before this existed: one
    bad file raised all the way up through this loop into run_claimed_scan's
    whole-scan except, silently skipping every file that would have come
    after it in the walk."""
    logger.warning("Scan: skipping %s after error: %s: %s", path, type(exc).__name__, exc)
    _scan_state["failed_count"] += 1
    if len(_scan_state["failed_examples"]) < _MAX_DIAGNOSTIC_EXAMPLES:
        _scan_state["failed_examples"].append(
            {"path": str(path), "error": f"{type(exc).__name__}: {exc}"}
        )


def _record_incomplete_metadata(path: Path):
    _scan_state["incomplete_count"] += 1
    if len(_scan_state["incomplete_examples"]) < _MAX_DIAGNOSTIC_EXAMPLES:
        _scan_state["incomplete_examples"].append({"path": str(path)})


def _upsert_media(conn, path: Path, media_type: str, media_root: Path = None, is_movie_folder: bool = False) -> bool:
    """Returns True if this was a video whose duration couldn't be
    determined (ffprobe failed) -- checked specifically because a None
    duration later 500s app/routers/stream.py's HLS playlist endpoint,
    which can't build a playlist without a known duration. video_codec/
    audio_codec aren't checked here: a legitimately silent video has no
    audio_codec without ffprobe having failed at all, so that would
    false-positive."""
    size_bytes = path.stat().st_size

    # If a previous scan already paid the cost of the packet-scan duration
    # fallback (see _probe_duration_via_packets) for this exact path and
    # the file is still the same size, reuse that duration instead of
    # re-walking every packet again -- that fallback is proportional to a
    # file's duration/bitrate, so re-running it on every single rescan is
    # genuinely expensive for a large (e.g. ~2GB TV episode) file, unlike
    # every other field extracted here. A same-size-but-different-content
    # replacement (rare) would serve a stale duration -- an accepted
    # trade-off, the same class of assumption any size/mtime-based change
    # detection makes.
    cached_duration = None
    if media_type == "video":
        existing = conn.execute(
            "SELECT duration, size_bytes FROM media WHERE path = ?", (str(path),)
        ).fetchone()
        if existing is not None and existing["size_bytes"] == size_bytes and existing["duration"] is not None:
            cached_duration = existing["duration"]

    info = _extract_metadata(path, media_type, media_root, is_movie_folder, cached_duration)
    info["size_bytes"] = size_bytes
    conn.execute(
        """
        INSERT INTO media
            (path, media_type, title, artist, album, duration, size_bytes,
             video_codec, audio_codec, video_width, video_height,
             show_name, season_number, episode_number, is_movie, is_extra)
        VALUES
            (:path, :media_type, :title, :artist, :album, :duration, :size_bytes,
             :video_codec, :audio_codec, :video_width, :video_height,
             :show_name, :season_number, :episode_number, :is_movie, :is_extra)
        ON CONFLICT(path) DO UPDATE SET
            title=excluded.title, artist=excluded.artist, album=excluded.album,
            duration=excluded.duration, size_bytes=excluded.size_bytes,
            video_codec=excluded.video_codec, audio_codec=excluded.audio_codec,
            video_width=excluded.video_width, video_height=excluded.video_height,
            show_name=excluded.show_name, season_number=excluded.season_number,
            episode_number=excluded.episode_number, is_movie=excluded.is_movie,
            is_extra=excluded.is_extra
        """,
        {"path": str(path), "media_type": media_type, **info},
    )
    return media_type == "video" and info["duration"] is None


def _extract_metadata(
    path: Path, media_type: str, media_root: Path = None, is_movie_folder: bool = False,
    cached_duration: float = None,
):
    info = {
        "title": path.stem,
        "artist": None,
        "album": None,
        "duration": None,
        "video_codec": None,
        "audio_codec": None,
        "video_width": None,
        "video_height": None,
        "show_name": None,
        "season_number": None,
        "episode_number": None,
        "is_movie": False,
        "is_extra": False,
    }

    if media_type == "audio":
        try:
            audio = MutagenFile(path, easy=True)
        except Exception:
            audio = None
        if audio is not None:
            if audio.tags:
                info["title"] = _first_tag(audio.tags, "title", info["title"])
                info["artist"] = _first_tag(audio.tags, "artist", None)
                info["album"] = _first_tag(audio.tags, "album", None)
            try:
                if audio.info:
                    info["duration"] = audio.info.length
            except Exception:
                pass
    else:
        (
            info["duration"], info["video_codec"], info["audio_codec"],
            info["video_width"], info["video_height"],
        ) = _probe_video_info(path, cached_duration)

        show_name, season_number, episode_number, is_extra = (None, None, None, False)
        if media_root is not None:
            show_name, season_number, episode_number, is_extra = _parse_folder_show_episode(path, media_root)
        if show_name is None and not is_extra:
            # The filename-only fallback never recognizes extras -- it only
            # matches the "Show Name S01E02" convention, which a bonus-
            # content file never happens to look like. Only reached when
            # the folder heuristic found nothing recognizable at all --
            # not even "this is bonus content with no show to attach it
            # to" (show_name is None but is_extra is already True) is
            # overwritten here.
            show_name, season_number, episode_number = _parse_show_episode(path.stem)
        info["show_name"], info["season_number"], info["episode_number"], info["is_extra"] = (
            show_name, season_number, episode_number, is_extra,
        )

        # Only a fallback for files that aren't part of a recognized show --
        # e.g. a season folder with just one episode ripped so far must
        # never be retitled to its folder's name just because it happens to
        # be the only video there (show_name is already set above by then).
        if show_name is None and is_movie_folder:
            info["title"] = path.parent.name

        # A video belongs in the Movies grid when it isn't part of any show
        # (grouped or not) and isn't bonus content -- this deliberately keeps
        # a loose/ungrouped standalone video (no season structure at all)
        # counted as a movie, same as it's always been treated, just
        # persisted now instead of inferred at query time.
        info["is_movie"] = show_name is None and not is_extra

    return info


def _first_tag(tags, key: str, default):
    try:
        values = tags.get(key)
        return values[0] if values else default
    except Exception:
        return default


# Matches the common "Show Name S01E02[...]" convention, with '.', '_', or
# spaces as separators (e.g. "The.Chosen.S01E02", "the_chosen_s01e02").
# Anything else (1x02, absolute numbering, no episode markers at all) isn't
# recognized -- those files just stay ungrouped, same as before this
# feature existed, rather than guessing wrong.
_SHOW_EPISODE_RE = re.compile(r"^(?P<show>.+?)[\s._-]+[Ss](?P<season>\d{1,2})[Ee](?P<episode>\d{1,3})")


def _parse_show_episode(stem: str):
    match = _SHOW_EPISODE_RE.match(stem)
    if not match:
        return None, None, None
    show_name = re.sub(r"[\s._-]+", " ", match.group("show")).strip()
    if not show_name:
        return None, None, None
    return show_name, int(match.group("season")), int(match.group("episode"))


# Matches a season folder name in isolation (full match, not a substring):
# "Season 1", "Season 01", "Season  12", "S01", "S1", "Season 00" (specials).
# Trailing junk ("Season 1 (2013)", "Season 1 Extras") is deliberately
# rejected -- same "don't guess wrong" policy as _SHOW_EPISODE_RE.
_SEASON_FOLDER_RE = re.compile(r"^(?:season\s*(?P<s1>\d{1,2})|s(?P<s2>\d{1,2}))$", re.IGNORECASE)

# Full-match against a folder name -- these are physical "bucket" folders
# for bonus/extra content (Featurettes, Deleted Scenes, etc.), so a full
# match is much safer here than a filename substring check. Deliberately
# excludes bare "specials"/"special" -- that's the existing Plex/Jellyfin
# convention for a season-0 folder of real episodes, a different concept
# from bonus "special features".
_EXTRAS_FOLDER_RE = re.compile(
    r"^(?:extras?|featurettes?|deleted\s+scenes?|behind\s+the\s+scenes?|"
    r"gag\s+reels?|interviews?|(?:the\s+)?making\s+of|"
    r"bonus(?:\s+features?)?|special\s+features?|bloopers?|outtakes?)$",
    re.IGNORECASE,
)

# Trailing-token match against a filename stem, same style as
# _TRAILER_SAMPLE_RE below -- a fallback for a loose extras file that has no
# bucket folder of its own (e.g. sitting directly in a season folder next
# to real episodes). Deliberately narrower than _EXTRAS_FOLDER_RE's word
# list: bare "interview"/"bonus"/"extra" are excluded here since they're too
# likely to collide with a real title in trailing position (e.g. "The
# Interview.mkv" is a real 2014 movie) -- only multi-word, unambiguous
# phrases are matched by filename alone.
_EXTRAS_FILENAME_RE = re.compile(
    r"(?:^|[\W_])(?:featurettes?|deleted[\W_]+scenes?|behind[\W_]+the[\W_]+scenes?|"
    r"gag[\W_]+reels?|(?:the[\W_]+)?making[\W_]+of|bonus[\W_]+features?|"
    r"special[\W_]+features?|bloopers?|outtakes?)[\W_\d]*$",
    re.IGNORECASE,
)

# A video whose name ends in "trailer"/"sample" (optionally pluralized or
# followed by digits/punctuation, e.g. "trailer1", "Inception-trailer",
# "samples") is excluded from the library entirely. End-anchored rather
# than a bare substring search so a legitimately-titled file like
# "Trailer Park Boys.mkv" is left alone -- "trailer"/"sample" only counts
# when it's the trailing token, matching how these files are actually named
# in practice.
_TRAILER_SAMPLE_RE = re.compile(r"(?:^|[\W_])(?:trailer|sample)s?[\W_\d]*$", re.IGNORECASE)

# Episode number extracted from a filename when the season number is
# already known from a season folder (see _parse_folder_show_episode) --
# tried in order: an explicit S##E## tag anywhere in the name (season part
# ignored, the folder's season wins), then a leading "Episode N" word form,
# then a bare leading number ("01 - Uno.mkv"). The \d{1,3} cap on all three
# means a 4-digit filename like "1984.mkv" can never match as an episode
# number -- greedy \d{1,3} plus the required trailing separator/end-of-string
# can't consume all 4 digits and still satisfy the boundary.
_EPISODE_TAG_RE = re.compile(r"[Ss]\d{1,2}[Ee](?P<episode>\d{1,3})")
_EPISODE_WORD_RE = re.compile(r"^episode[\s._-]*(?P<episode>\d{1,3})\b", re.IGNORECASE)
_LEADING_EPISODE_RE = re.compile(r"^(?P<episode>\d{1,3})(?=[\s._-]|$)")


def _parse_episode_in_stem(stem: str):
    match = _EPISODE_TAG_RE.search(stem)
    if match:
        return int(match.group("episode"))
    match = _EPISODE_WORD_RE.match(stem)
    if match:
        return int(match.group("episode"))
    match = _LEADING_EPISODE_RE.match(stem)
    if match:
        return int(match.group("episode"))
    return None


def _find_show_dir_above_extras(extras_dir: Path, media_root: Path):
    """Starting from a folder recognized as an extras bucket (Featurettes,
    Deleted Scenes, etc.), walk up past any season-folder or further
    extras-bucket ancestors to find the real show folder above them.
    Returns None if the walk reaches media_root without finding one."""
    candidate = extras_dir.parent
    while candidate != media_root:
        name = candidate.name.strip()
        if not name:
            return None
        if _SEASON_FOLDER_RE.fullmatch(name) or _EXTRAS_FOLDER_RE.fullmatch(name):
            candidate = candidate.parent
            continue
        return candidate
    return None


def _has_season_subfolder(folder: Path):
    try:
        return any(
            _SEASON_FOLDER_RE.fullmatch(child.name.strip())
            for child in folder.iterdir() if child.is_dir()
        )
    except OSError:
        return False


def _parse_folder_show_episode(path: Path, media_root: Path):
    """Detect the Plex/Jellyfin-style "<Show>/<Season Folder>/<episode
    file>" convention, plus TV-show bonus/extra content living in a bucket
    folder (Featurettes, Deleted Scenes, Behind the Scenes, ...) anywhere
    under the show. Returns (show_name, season_number, episode_number,
    is_extra) -- show_name is None if the structure doesn't unambiguously
    match anything here, in which case callers should fall back to
    _parse_show_episode(path.stem), never mixing partial results."""
    season_dir = path.parent

    # Case 1: the immediate parent IS an extras bucket, e.g.
    # "<Show>/Featurettes/file.mkv" or "<Show>/Season 03/Deleted Scenes/file.mkv".
    if _EXTRAS_FOLDER_RE.fullmatch(season_dir.name.strip()):
        show_dir = _find_show_dir_above_extras(season_dir, media_root)
        if show_dir is None:
            # A recognized extras-bucket folder, but nothing plausible
            # above it to attribute it to -- still bonus content (the
            # folder name says so), just with no show to attach it to.
            return None, None, None, True
        # Guard against a movie's own bonus-features folder being mistaken
        # for a TV show -- only trust this walk-up when the resolved folder
        # actually has a real season subfolder somewhere. A real
        # season-organized show will have that; a movie's own folder (e.g.
        # "Movie (2010)/Special Features/") never will. Without this check,
        # "Movie (2010)/Special Features/bonus.mkv" would fabricate a
        # phantom one-episode "TV show" called "Movie (2010)". It's still
        # correctly recognized as bonus content (is_extra=True) either way
        # -- see is_movie_folder in scan_media_dirs for the corresponding
        # suppression that keeps it from being retitled/counted as its own
        # movie.
        if not _has_season_subfolder(show_dir):
            return None, None, None, True
        show_name = show_dir.name.strip()
        if not show_name:
            return None, None, None, True
        return show_name, None, None, True

    match = _SEASON_FOLDER_RE.fullmatch(season_dir.name.strip())
    if not match:
        return None, None, None, False

    show_dir = season_dir.parent

    # Case 2: what looks like the show folder is itself an extras bucket --
    # the "Featurettes/Season 10/file.mkv" shape. This "season" folder
    # isn't a season of the show at all, it's a season-mirrored subfolder
    # inside an extras bucket -- walk further up to the real show, and
    # discard the season/episode number entirely since this is bonus
    # content, not a real episode.
    if _EXTRAS_FOLDER_RE.fullmatch(show_dir.name.strip()):
        real_show_dir = _find_show_dir_above_extras(show_dir, media_root)
        if real_show_dir is None:
            return None, None, None, False
        show_name = real_show_dir.name.strip()
        if not show_name:
            return None, None, None, False
        return show_name, None, None, True

    # A season folder sitting directly under a configured library root has
    # no distinct show folder above it -- using the library root's own name
    # ("TV", "Media", ...) as the show name would be worse than not
    # grouping at all.
    if show_dir == media_root:
        return None, None, None, False

    show_name = show_dir.name.strip()
    if not show_name:
        return None, None, None, False

    season_number = int(match.group("s1") or match.group("s2"))
    episode_number = _parse_episode_in_stem(path.stem)
    if episode_number is None:
        # A loose extras file sitting directly in a season folder with no
        # bucket subfolder of its own (e.g. "Season 01/Gag Reel.mkv").
        if _EXTRAS_FILENAME_RE.search(path.stem):
            return show_name, None, None, True
        return None, None, None, False

    return show_name, season_number, episode_number, False


def _probe_video_info(path: Path, cached_duration: float = None):
    """Return (duration, video_codec, audio_codec, width, height) via a
    single ffprobe call. video_codec/audio_codec are the *first* video/audio
    stream's codec name (e.g. "h264", "ac3"), used by app/transcode.py to
    decide whether a file can be played directly in a browser. width/height
    are the first video stream's dimensions, used by app/transcode.py to
    decide whether a re-encode needs to scale down to fit the resolution
    cap. cached_duration, when given (see _upsert_media), skips the
    expensive packet-scan fallback below entirely by reusing a previous
    scan's result for this same file."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration:stream=codec_type,codec_name,width,height",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
    except (FileNotFoundError, subprocess.SubprocessError, json.JSONDecodeError):
        return None, None, None, None, None

    duration = None
    try:
        duration = float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        pass

    video_codec = None
    audio_codec = None
    width = None
    height = None
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and video_codec is None:
            video_codec = stream.get("codec_name")
            width = stream.get("width")
            height = stream.get("height")
        elif stream.get("codec_type") == "audio" and audio_codec is None:
            audio_codec = stream.get("codec_name")

    if duration is None:
        duration = cached_duration if cached_duration is not None else _probe_duration_via_packets(path)

    return duration, video_codec, audio_codec, width, height


def _probe_duration_via_packets(path: Path):
    """Fallback for containers with no Duration in their header -- seen in
    the wild on .mkv "featurette"/bonus-content files muxed through a
    non-seekable pipe (mkvmerge/ffmpeg piped straight to stdout), which
    can't seek back afterward to write Matroska's Segment Duration
    element. format.duration then comes back empty even though the file
    plays fine and its codec/width/height are readable. This walks the
    video stream's packet headers (demuxing only, no real decode) to find
    the last packet's pts_time -- a real duration estimate, but unlike the
    primary probe its cost scales with the file's duration/bitrate (a
    ~2GB, hour-long 1080p file can have 60,000+ packets to read
    sequentially), so it is genuinely not cheap for a large file on slow
    storage. _upsert_media caches the result (keyed on path + size_bytes)
    so this only ever runs once per file rather than on every rescan,
    which is what makes a generous timeout below safe rather than a
    per-scan tax."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "packet=pts_time",
                "-of", "csv=print_section=0",
                str(path),
            ],
            capture_output=True, text=True, timeout=240,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None

    last_pts = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line == "N/A":
            continue
        try:
            last_pts = float(line)
        except ValueError:
            continue

    return last_pts


def _remove_missing(conn, found_paths: set, scanned_roots: list):
    """Delete rows for files no longer found on disk -- but only rows that
    live under a root this scan actually walked. A configured dir that was
    unavailable this run (unmounted NAS, unplugged USB drive) contributes no
    scanned_roots entry at all, so its rows are left completely alone rather
    than being wiped just because nothing was seen under it this time."""
    existing = conn.execute("SELECT id, path FROM media").fetchall()
    for row in existing:
        if row["path"] in found_paths:
            continue
        path = Path(row["path"])
        if any(path.is_relative_to(root) for root in scanned_roots):
            conn.execute("DELETE FROM media WHERE id = ?", (row["id"],))
