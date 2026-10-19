import socket

import httpx
import pytest

from tests.conftest import NetworkAccessError


def test_real_http_call_fails():
    with pytest.raises((NetworkAccessError, httpx.ConnectError)) as e:
        httpx.get("https://example.com", timeout=2)
    assert "network" in str(e.value) or "resolve" in str(e.value)


def test_raw_socket_to_public_ip_fails():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(NetworkAccessError):
            s.connect(("93.184.216.34", 443))
    finally:
        s.close()


def test_loopback_is_allowed():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect(server.getsockname())  # no error
    finally:
        client.close()
        server.close()
