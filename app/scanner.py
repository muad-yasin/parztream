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
    with get_connection() as conn:
        for media_dir in settings.get_media_dirs():
            if not media_dir.is_dir():
                continue
            # followlinks=False: don't descend into symlinked subdirectories,
            # and individual symlinked files are skipped below -- otherwise a
            # symlink placed inside a scanned folder (even one named
            # "song.mp3") could point anywhere on disk and get scanned,
            # indexed, and served as if it were a real media file.
            for root, dirnames, filenames in os.walk(media_dir, followlinks=False):
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
                # "is" the movie.
                is_movie_folder = not has_season_subfolder and len(video_paths) == 1

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

        _remove_missing(conn, found_paths)


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
    info = _extract_metadata(path, media_type, media_root, is_movie_folder)
    info["size_bytes"] = path.stat().st_size
    conn.execute(
        """
        INSERT INTO media
            (path, media_type, title, artist, album, duration, size_bytes,
             video_codec, audio_codec, show_name, season_number, episode_number)
        VALUES
            (:path, :media_type, :title, :artist, :album, :duration, :size_bytes,
             :video_codec, :audio_codec, :show_name, :season_number, :episode_number)
        ON CONFLICT(path) DO UPDATE SET
            title=excluded.title, artist=excluded.artist, album=excluded.album,
            duration=excluded.duration, size_bytes=excluded.size_bytes,
            video_codec=excluded.video_codec, audio_codec=excluded.audio_codec,
            show_name=excluded.show_name, season_number=excluded.season_number,
            episode_number=excluded.episode_number
        """,
        {"path": str(path), "media_type": media_type, **info},
    )
    return media_type == "video" and info["duration"] is None


def _extract_metadata(path: Path, media_type: str, media_root: Path = None, is_movie_folder: bool = False):
    info = {
        "title": path.stem,
        "artist": None,
        "album": None,
        "duration": None,
        "video_codec": None,
        "audio_codec": None,
        "show_name": None,
        "season_number": None,
        "episode_number": None,
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
        info["duration"], info["video_codec"], info["audio_codec"] = _probe_video_info(path)

        show_name, season_number, episode_number = (None, None, None)
        if media_root is not None:
            show_name, season_number, episode_number = _parse_folder_show_episode(path, media_root)
        if show_name is None:
            show_name, season_number, episode_number = _parse_show_episode(path.stem)
        info["show_name"], info["season_number"], info["episode_number"] = (
            show_name, season_number, episode_number,
        )

        # Only a fallback for files that aren't part of a recognized show --
        # e.g. a season folder with just one episode ripped so far must
        # never be retitled to its folder's name just because it happens to
        # be the only video there (show_name is already set above by then).
        if show_name is None and is_movie_folder:
            info["title"] = path.parent.name

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


def _parse_folder_show_episode(path: Path, media_root: Path):
    """Detect the Plex/Jellyfin-style "<Show>/<Season Folder>/<episode
    file>" convention. Returns (show_name, season_number, episode_number),
    all None if the structure doesn't unambiguously match -- callers should
    fall back to _parse_show_episode(path.stem) in that case, never mix
    partial results. Only ever looks at the immediate parent folder, so an
    Extras/Behind the Scenes folder nested inside a season folder is
    correctly left ungrouped rather than misread as an episode."""
    season_dir = path.parent
    match = _SEASON_FOLDER_RE.fullmatch(season_dir.name.strip())
    if not match:
        return None, None, None

    show_dir = season_dir.parent
    # A season folder sitting directly under a configured library root has
    # no distinct show folder above it -- using the library root's own name
    # ("TV", "Media", ...) as the show name would be worse than not
    # grouping at all.
    if show_dir == media_root:
        return None, None, None

    show_name = show_dir.name.strip()
    if not show_name:
        return None, None, None

    season_number = int(match.group("s1") or match.group("s2"))
    episode_number = _parse_episode_in_stem(path.stem)
    if episode_number is None:
        return None, None, None

    return show_name, season_number, episode_number


def _probe_video_info(path: Path):
    """Return (duration, video_codec, audio_codec) via a single ffprobe call.
    video_codec/audio_codec are the *first* video/audio stream's codec name
    (e.g. "h264", "ac3"), used by app/transcode.py to decide whether a file
    can be played directly in a browser."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration:stream=codec_type,codec_name",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
    except (FileNotFoundError, subprocess.SubprocessError, json.JSONDecodeError):
        return None, None, None

    duration = None
    try:
        duration = float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        pass

    video_codec = None
    audio_codec = None
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and video_codec is None:
            video_codec = stream.get("codec_name")
        elif stream.get("codec_type") == "audio" and audio_codec is None:
            audio_codec = stream.get("codec_name")

    return duration, video_codec, audio_codec


def _remove_missing(conn, found_paths: set):
    existing = conn.execute("SELECT id, path FROM media").fetchall()
    for row in existing:
        if row["path"] not in found_paths:
            conn.execute("DELETE FROM media WHERE id = ?", (row["id"],))
