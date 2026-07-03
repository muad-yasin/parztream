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
    show_name TEXT,
    season_number INTEGER,
    episode_number INTEGER,
    is_movie INTEGER NOT NULL DEFAULT 0,
    is_extra INTEGER NOT NULL DEFAULT 0,
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
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_connection() as conn:
        conn.executescript(SCHEMA)
