from __future__ import annotations

import socket

import pytest

from ai_systems_performance_lab.cli import _port, _port_is_in_use


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        (None, 8003, 8003),
        ("5174", 8003, 5174),
        ("1", 8003, 1),
        ("65535", 8003, 65535),
    ],
)
def test_port_accepts_valid_port_or_default(value, default, expected):
    assert _port(value, default, "PORT") == expected


@pytest.mark.parametrize("value", ["nope", "0", "65536", "-1"])
def test_port_rejects_invalid_value(value):
    with pytest.raises(ValueError, match="PORT"):
        _port(value, 8003, "PORT")


def test_port_check_detects_listener():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        host, port = listener.getsockname()
        assert _port_is_in_use(host, port)


def test_port_check_accepts_unused_ephemeral_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    assert not _port_is_in_use("127.0.0.1", port)
