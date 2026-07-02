import json
from pathlib import Path

from . import config
from .db import get_connection


def get_media_dirs():
    """Return the currently configured media directories: whatever's been
    saved via the setup UI, falling back to PARZTREAM_MEDIA_DIRS if nothing
    has been configured that way yet. This is a live lookup (not a
    module-level constant like config.MEDIA_DIRS) so a folder change made
    through /setup takes effect on the next scan without restarting."""
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'media_dirs'").fetchone()
    if row is None:
        return config.MEDIA_DIRS
    return [Path(p) for p in json.loads(row["value"])]


def set_media_dirs(paths):
    value = json.dumps([str(p) for p in paths])
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value) VALUES ('media_dirs', :value)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            {"value": value},
        )


def is_configured():
    """True once at least one media directory has been set, either via the
    setup UI or PARZTREAM_MEDIA_DIRS."""
    return len(get_media_dirs()) > 0
