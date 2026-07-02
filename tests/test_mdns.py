import time
from unittest.mock import MagicMock, patch

import pytest

from app import config, mdns


@pytest.fixture(autouse=True)
def reset_mdns_module_state():
    """app/mdns.py keeps module-level _zeroconf/_service_info across calls
    (mirroring how a real running server would) -- reset between tests so
    one test's registration doesn't leak into another's."""
    mdns._zeroconf = None
    mdns._service_info = None
    yield
    mdns._zeroconf = None
    mdns._service_info = None


def test_start_mdns_does_nothing_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "MDNS_ENABLED", False)

    with patch("threading.Thread") as mock_thread_cls:
        mdns.start_mdns()

    mock_thread_cls.assert_not_called()


def test_start_mdns_spawns_a_background_thread_when_enabled(monkeypatch):
    # Not called inline: zeroconf's sync API schedules work on its own
    # internal event loop and waits for it, which reliably times out
    # (confirmed live) when called directly from inside the FastAPI
    # lifespan, since that's already running an event loop on this thread.
    monkeypatch.setattr(config, "MDNS_ENABLED", True)

    with patch("threading.Thread") as mock_thread_cls:
        mock_thread_cls.return_value = MagicMock()
        mdns.start_mdns()

    mock_thread_cls.assert_called_once()
    _, kwargs = mock_thread_cls.call_args
    assert kwargs["target"] is mdns._register
    assert kwargs["daemon"] is True
    mock_thread_cls.return_value.start.assert_called_once()


def test_register_creates_a_service_with_the_configured_name_and_port(monkeypatch):
    monkeypatch.setattr(config, "MDNS_HOSTNAME", "testhost")
    monkeypatch.setattr(config, "PORT", 1234)
    monkeypatch.setattr(mdns, "get_local_ip", lambda: "192.168.1.99")

    mock_instance = MagicMock()
    with patch.object(mdns, "Zeroconf", return_value=mock_instance) as mock_zeroconf_cls, \
         patch.object(mdns, "ServiceInfo") as mock_service_info_cls:
        mdns._register()

    mock_zeroconf_cls.assert_called_once()
    _, kwargs = mock_service_info_cls.call_args
    assert kwargs["server"] == "testhost.local."
    assert kwargs["port"] == 1234
    mock_instance.register_service.assert_called_once()
    assert mdns._zeroconf is mock_instance


def test_register_failure_is_non_fatal(monkeypatch):
    with patch.object(mdns, "Zeroconf", side_effect=OSError("multicast not permitted")):
        mdns._register()  # must not raise

    assert mdns._zeroconf is None


def test_stop_mdns_unregisters_and_closes():
    mock_instance = MagicMock()
    mdns._zeroconf = mock_instance
    mdns._service_info = MagicMock()

    mdns.stop_mdns()

    mock_instance.unregister_service.assert_called_once()
    mock_instance.close.assert_called_once()
    assert mdns._zeroconf is None
    assert mdns._service_info is None


def test_stop_mdns_is_a_noop_when_never_started():
    mdns.stop_mdns()  # must not raise
    assert mdns._zeroconf is None


def test_full_round_trip_with_real_zeroconf_and_a_real_background_thread(monkeypatch):
    # Exercises the actual public API end to end -- real thread, real
    # zeroconf registration, not mocked -- to confirm the fix for the
    # EventLoopBlocked issue actually works, not just that our code calls
    # the mocked API correctly.
    monkeypatch.setattr(config, "MDNS_ENABLED", True)
    monkeypatch.setattr(config, "MDNS_HOSTNAME", "parztream-test")
    monkeypatch.setattr(config, "PORT", 8123)

    mdns.start_mdns()

    deadline = time.time() + 5
    while mdns._zeroconf is None and time.time() < deadline:
        time.sleep(0.05)

    assert mdns._zeroconf is not None, "mDNS registration did not complete in time"
    mdns.stop_mdns()
    assert mdns._zeroconf is None
