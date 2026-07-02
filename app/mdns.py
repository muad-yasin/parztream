import logging
import socket
import threading

from zeroconf import ServiceInfo, Zeroconf

from . import config
from .network import get_local_ip

logger = logging.getLogger("parztream")

_zeroconf = None
_service_info = None


def start_mdns():
    """Advertise http://<MDNS_HOSTNAME>.local:<PORT>/ over mDNS, so devices
    that support it (macOS/iOS reliably, Linux via Avahi, Windows/Android
    inconsistently) can reach the server without knowing its IP.

    Runs the actual registration in a background thread rather than inline:
    zeroconf's sync API schedules work on its own internal event loop via
    run_coroutine_threadsafe and waits for it, which reliably times out
    (zeroconf._exceptions.EventLoopBlocked) when called directly from
    inside the FastAPI lifespan, since that's already running on uvicorn's
    event loop on this thread -- confirmed live, not theoretical. A plain
    background thread sidesteps that contention entirely, and there's no
    reason startup should block on this anyway.

    Failure here is never fatal -- the server works fine by IP either
    way -- so every failure path just logs, it never raises into the
    caller."""
    if not config.MDNS_ENABLED:
        return
    threading.Thread(target=_register, daemon=True).start()


def _register():
    global _zeroconf, _service_info

    try:
        local_ip = get_local_ip()
        info = ServiceInfo(
            type_="_http._tcp.local.",
            name=f"{config.MDNS_HOSTNAME}._http._tcp.local.",
            addresses=[socket.inet_aton(local_ip)],
            port=config.PORT,
            server=f"{config.MDNS_HOSTNAME}.local.",
        )
        zc = Zeroconf()
        zc.register_service(info)
    except Exception:
        logger.warning(
            "mDNS registration failed -- %s.local won't resolve, but the "
            "server still works fine by IP.",
            config.MDNS_HOSTNAME,
            exc_info=True,
        )
        return

    _zeroconf = zc
    _service_info = info
    logger.info(
        "mDNS: advertising http://%s.local:%d/ (currently resolves to %s)",
        config.MDNS_HOSTNAME, config.PORT, local_ip,
    )


def stop_mdns():
    global _zeroconf, _service_info

    if _zeroconf is None:
        return

    try:
        if _service_info is not None:
            _zeroconf.unregister_service(_service_info)
    except Exception:
        pass
    finally:
        _zeroconf.close()
        _zeroconf = None
        _service_info = None
