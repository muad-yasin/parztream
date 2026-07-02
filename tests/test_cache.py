import os
import time
from pathlib import Path

from app import cache


def _cache_file(cache_dir, name, size, age_seconds):
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / name
    path.write_bytes(b"x" * size)
    now = time.time()
    os.utime(path, (now - age_seconds, now - age_seconds))
    return path


def test_prune_does_nothing_when_no_limit_configured(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(cache, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(cache, "CACHE_MAX_BYTES", None)
    old = _cache_file(cache_dir, "1.mp4", 1000, age_seconds=1000)
    new = _cache_file(cache_dir, "2.mp4", 1000, age_seconds=0)

    cache.prune(protect=new)

    assert old.is_file()
    assert new.is_file()


def test_prune_evicts_oldest_first_until_under_budget(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(cache, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(cache, "CACHE_MAX_BYTES", 250)
    oldest = _cache_file(cache_dir, "1.mp4", 100, age_seconds=200)
    middle = _cache_file(cache_dir, "2.mp4", 100, age_seconds=100)
    newest = _cache_file(cache_dir, "3.mp4", 100, age_seconds=0)

    cache.prune(protect=newest)

    assert not oldest.is_file()
    assert middle.is_file()
    assert newest.is_file()


def test_prune_never_deletes_the_protected_file_even_if_alone_over_budget(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(cache, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(cache, "CACHE_MAX_BYTES", 10)
    protected = _cache_file(cache_dir, "1.mp4", 1000, age_seconds=0)

    cache.prune(protect=protected)

    assert protected.is_file()


def test_prune_treats_mixed_file_types_as_one_shared_budget(tmp_path, monkeypatch):
    # Remux (.mp4) and thumbnail (.jpg) cache entries share CACHE_DIR and
    # should be pruned together, oldest-first, regardless of type.
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(cache, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(cache, "CACHE_MAX_BYTES", 150)
    old_thumb = _cache_file(cache_dir, "1_thumb.jpg", 100, age_seconds=200)
    new_remux = _cache_file(cache_dir, "2.mp4", 100, age_seconds=0)

    cache.prune(protect=new_remux)

    assert not old_thumb.is_file()
    assert new_remux.is_file()


def test_prune_tolerates_a_file_vanishing_during_size_listing(tmp_path, monkeypatch):
    # Two different resources (e.g. one media item's remux, another's
    # thumbnail) can legitimately finish and call prune() around the same
    # time, each unaware of the other -- simulate one disappearing between
    # this prune() listing it and stat'ing it.
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(cache, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(cache, "CACHE_MAX_BYTES", 50)
    vanishing = _cache_file(cache_dir, "vanishing.mp4", 100, age_seconds=100)
    survivor = _cache_file(cache_dir, "survivor.mp4", 100, age_seconds=0)

    real_stat = Path.stat

    def flaky_stat(self, *args, **kwargs):
        if self == vanishing:
            vanishing.unlink()  # simulate a concurrent prune() removing it first
            raise FileNotFoundError()
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)

    cache.prune(protect=survivor)  # must not raise

    assert not vanishing.is_file()
    assert survivor.is_file()


def test_prune_tolerates_a_file_vanishing_just_before_unlink(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(cache, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(cache, "CACHE_MAX_BYTES", 10)
    oldest = _cache_file(cache_dir, "oldest.mp4", 100, age_seconds=200)
    older = _cache_file(cache_dir, "older.mp4", 100, age_seconds=100)
    newest = _cache_file(cache_dir, "newest.mp4", 100, age_seconds=0)

    real_unlink = Path.unlink

    def flaky_unlink(self, *args, **kwargs):
        if self == oldest:
            # Simulate a concurrent prune() call deleting `older` in between
            # this prune() selecting it as a candidate and reaching it.
            real_unlink(older, missing_ok=True)
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    cache.prune(protect=newest)  # must not raise despite `older` already gone

    assert not oldest.is_file()
    assert not older.is_file()
    assert newest.is_file()


def test_lock_for_returns_the_same_lock_for_the_same_key():
    a = cache.lock_for("same-key")
    b = cache.lock_for("same-key")
    assert a is b


def test_lock_for_returns_different_locks_for_different_keys():
    a = cache.lock_for("key-a")
    b = cache.lock_for("key-b")
    assert a is not b
