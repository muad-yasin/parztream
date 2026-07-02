import re
import socket
from unittest.mock import patch

from app.network import get_local_ip

IPV4_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")


def test_get_local_ip_returns_a_real_looking_ipv4_address():
    ip = get_local_ip()
    assert IPV4_RE.match(ip)
    assert all(0 <= int(octet) <= 255 for octet in ip.split("."))


def test_get_local_ip_falls_back_to_loopback_on_error():
    with patch("socket.socket") as mock_socket_cls:
        mock_socket = mock_socket_cls.return_value.__enter__.return_value
        mock_socket.connect.side_effect = OSError("network unreachable")

        assert get_local_ip() == "127.0.0.1"
