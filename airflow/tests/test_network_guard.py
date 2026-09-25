"""The guard every suite installs refuses the internet and allows the machine and the stack."""

from __future__ import annotations

import socket

import pytest
from network_guard import ExternalNetworkError


def test_a_connection_to_a_public_address_is_refused() -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(ExternalNetworkError),
    ):
        sock.connect(("1.1.1.1", 443))


def test_a_request_to_a_feed_api_is_refused_before_it_leaves() -> None:
    import requests

    with pytest.raises((ExternalNetworkError, requests.ConnectionError)):
        requests.get("https://93.184.216.34/v1/2026-07-24", timeout=2)


def test_loopback_is_allowed() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.connect(server.getsockname())


@pytest.mark.integration
def test_the_integration_suite_refuses_a_public_address_inside_the_stack() -> None:
    """R16: the suite that is allowed to reach the stack's services still cannot leave.

    Runs inside the scheduler container, which does have a route out, so only the guard can
    produce this refusal: the assertion is on the guard's own exception, not on any connection
    error, which an offline machine would raise without a guard at all.
    """
    import requests

    with pytest.raises(ExternalNetworkError, match="1.1.1.1"):
        requests.get("https://1.1.1.1/", timeout=5)
