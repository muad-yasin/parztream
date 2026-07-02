import socket


def get_local_ip() -> str:
    """Best-effort guess at this machine's LAN-facing IP address.

    The classic trick: ask the OS which local interface it would use to
    reach an external address. UDP is connectionless, so connect() here
    doesn't actually send a packet or require real internet access -- it
    just makes the kernel pick a route, which is exactly the local address
    we want. Falls back to loopback if there's no route at all (e.g. no
    network configured), which just means mDNS advertises a useless address
    rather than crashing anything.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"
