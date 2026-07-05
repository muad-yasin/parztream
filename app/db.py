import sqlite3
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    media_type TEXT NOT NULL CHECK(media_type IN ('audio', 'video')),
    title TEXT,
    artist TEXT,
    album TEXT,
    duration REAL,
    size_bytes INTEGER,
    video_codec TEXT,
    audio_codec TEXT,
    video_width INTEGER,
    video_height INTEGER,
    audio_channels INTEGER,
    audio_stream_index INTEGER,
    show_name TEXT,
    season_number INTEGER,
    episode_number INTEGER,
    is_movie INTEGER NOT NULL DEFAULT 0,
    is_extra INTEGER NOT NULL DEFAULT 0,
    segment_boundaries TEXT,
    added_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_media_type ON media(media_type);
CREATE INDEX IF NOT EXISTS idx_show_name ON media(show_name);
CREATE INDEX IF NOT EXISTS idx_is_movie ON media(is_movie);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL lets readers and writers avoid blocking each other -- without it,
    # a long-running scan (app/scanner.py holds one connection open for its
    # entire walk, since ffprobe/keyframe-probe timeouts per file can add up
    # to minutes) could make a concurrent POST /api/setup write fail with
    # "database is locked". A persistent, once-set property of the DB file
    # itself, so this is a cheap no-op on every connection after the first.
    # busy_timeout is raised from sqlite3's 5s connect-time default to 10s as
    # a second line of defense for genuine writer-vs-writer contention WAL
    # doesn't eliminate on its own (e.g. a scan's writes to `media` and
    # /api/setup's write to `settings` landing at the same moment).
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Columns added to the media table after it first shipped, applied via
# ALTER TABLE when missing -- the first (tiny) migration mechanism this
# project has. Earlier schema changes just said "delete the dev DB and
# rescan", but segment_boundaries landed after real installs existed whose
# DBs also hold user configuration (the settings table), so wiping is no
# longer an acceptable upgrade path. Rows keep a NULL value until the next
# scan (or a lazy backfill on first HLS request -- see
# app/routers/stream.py) fills it in.
_MEDIA_COLUMN_MIGRATIONS = {
    "segment_boundaries": "ALTER TABLE media ADD COLUMN segment_boundaries TEXT",
}


def init_db():
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        existing_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(media)")
        }
        for column, statement in _MEDIA_COLUMN_MIGRATIONS.items():
            if column not in existing_columns:
                conn.execute(statement)
