import sqlite3
import threading

from app import db


def _media_columns():
    with db.get_connection() as conn:
        return {row["name"] for row in conn.execute("PRAGMA table_info(media)")}


def test_init_db_adds_missing_columns_to_an_existing_database(tmp_path):
    # A real install's DB predates the segment_boundaries column and also
    # holds user configuration (the settings table), so "delete and rescan"
    # stopped being an acceptable upgrade path -- init_db must ALTER the
    # existing table into shape without touching its rows.
    old_db = db.DB_PATH
    conn = sqlite3.connect(old_db)
    conn.executescript(
        """
        DROP TABLE IF EXISTS media;
        -- The media table exactly as it looked right before
        -- segment_boundaries existed (SCHEMA's indexes still need the
        -- other columns present when init_db re-runs over this DB).
        CREATE TABLE media (
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
            added_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO media (path, media_type, title) VALUES ('/m/song.mp3', 'audio', 'Song');
        """
    )
    conn.commit()
    conn.close()

    db.init_db()

    assert "segment_boundaries" in _media_columns()
    with db.get_connection() as conn:
        row = conn.execute("SELECT title, segment_boundaries FROM media").fetchone()
    assert row["title"] == "Song"
    assert row["segment_boundaries"] is None


def test_init_db_is_idempotent_once_migrated():
    db.init_db()
    db.init_db()  # a second run must not try to re-ALTER an existing column

    assert "segment_boundaries" in _media_columns()


def test_get_connection_enables_wal_mode():
    with db.get_connection() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_concurrent_read_does_not_block_on_an_open_write_connection():
    # The actual regression test for M1: without WAL, a reader hitting a
    # connection with an open write transaction gets "database is locked"
    # (sqlite3's default busy_timeout is only 5s). A large scan holding one
    # connection open for its whole run must not cause a concurrent
    # POST /api/setup-style read/write to fail this way.
    db.init_db()
    write_conn = sqlite3.connect(db.DB_PATH)
    write_conn.execute("PRAGMA journal_mode=WAL")
    write_conn.execute("BEGIN IMMEDIATE")
    write_conn.execute("INSERT INTO settings (key, value) VALUES ('probe', 'value')")

    result = {}

    def read_in_another_connection():
        try:
            with db.get_connection() as conn:
                conn.execute("SELECT * FROM media").fetchall()
            result["ok"] = True
        except sqlite3.OperationalError as exc:
            result["ok"] = False
            result["error"] = str(exc)

    t = threading.Thread(target=read_in_another_connection)
    t.start()
    t.join(timeout=5)

    write_conn.rollback()
    write_conn.close()

    assert result.get("ok") is True, result.get("error")
