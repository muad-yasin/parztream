from pathlib import Path

from app import settings


def test_get_media_dirs_falls_back_to_config_when_nothing_saved(monkeypatch):
    monkeypatch.setattr(settings.config, "MEDIA_DIRS", [Path("/from/env")])

    assert settings.get_media_dirs() == [Path("/from/env")]


def test_set_and_get_media_dirs_round_trips():
    settings.set_media_dirs([Path("/media/movies"), Path("/media/music")])

    assert settings.get_media_dirs() == [Path("/media/movies"), Path("/media/music")]


def test_set_media_dirs_overwrites_previous_value():
    settings.set_media_dirs([Path("/old")])
    settings.set_media_dirs([Path("/new")])

    assert settings.get_media_dirs() == [Path("/new")]


def test_set_media_dirs_takes_precedence_over_env_fallback(monkeypatch):
    monkeypatch.setattr(settings.config, "MEDIA_DIRS", [Path("/from/env")])
    settings.set_media_dirs([Path("/from/setup")])

    assert settings.get_media_dirs() == [Path("/from/setup")]


def test_is_configured_false_when_nothing_set(monkeypatch):
    monkeypatch.setattr(settings.config, "MEDIA_DIRS", [])

    assert settings.is_configured() is False


def test_is_configured_true_once_media_dirs_are_set():
    settings.set_media_dirs([Path("/media")])

    assert settings.is_configured() is True


def test_is_configured_true_from_env_fallback_alone(monkeypatch):
    monkeypatch.setattr(settings.config, "MEDIA_DIRS", [Path("/from/env")])

    assert settings.is_configured() is True
