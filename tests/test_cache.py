import os
import time

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
