"""Refuse any test connection to an address outside the machine and its private networks.

Specification 006 section 6: no test may require network access. The recorded fixtures are
what make that possible, and this is what makes it true: installed by every suite's conftest,
it replaces the socket connect calls so that a connection to a public address raises instead of
leaving the machine. Loopback and private ranges stay open, because the integration suite talks
to the stack's own services, which Docker addresses privately; that suite needs the stack, not
the internet, and the guard proves the second half of that sentence.

`make test-offline` proves the rest by running the unit and DAG suites in a container with no
network at all.
"""

from __future__ import annotations

import ipaddress
import socket


class ExternalNetworkError(RuntimeError):
    """A test tried to reach an address outside the machine and its private networks."""


def _allowed(address) -> bool:
    if not isinstance(address, tuple) or not address:
        return True  # a Unix socket path
    host = address[0]
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            ip = ipaddress.ip_address(socket.gethostbyname(host))
        except OSError:
            return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def install(monkeypatch) -> None:
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def connect(self, address):
        if not _allowed(address):
            raise ExternalNetworkError(f"a test tried to connect to {address!r}")
        return original_connect(self, address)

    def connect_ex(self, address):
        if not _allowed(address):
            raise ExternalNetworkError(f"a test tried to connect to {address!r}")
        return original_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
