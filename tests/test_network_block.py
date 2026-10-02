"""Pins for the suite-wide network block (``tests/conftest.py``,
audit open bug 3, 2026-09-09).

The suite is offline-only by policy; these tests are the proof that
the policy is now ENFORCED rather than assumed: a raw ``urlopen`` (or a
raw socket connect) to anything non-loopback is refused with an error
that names the test and the host, loopback stays open for the Flask
test client and the desktop tests' real 127.0.0.1 servers, and the
loopback proxy a sandbox may route egress through cannot smuggle a
request past the block.

The tests that deliberately provoke the block clear the fixture's
attempt record afterwards — otherwise the fixture's own teardown check
would (correctly) fail them for having reached out.
"""
from __future__ import annotations

import os
import socket
import urllib.request

import pytest

from tests.conftest import _PROXY_ENV_VARS, NetworkBlockedError


def test_raw_urlopen_is_refused_with_an_error_naming_test_and_host(
    request, network_block,
):
    """The headline pin: an unpatched lookup path reaching the network
    fails on the first attempt, loudly, instead of retrying for 30 s."""
    with pytest.raises(NetworkBlockedError) as excinfo:
        urllib.request.urlopen("https://example.invalid", timeout=5)
    msg = str(excinfo.value)
    assert "example.invalid" in msg
    assert request.node.nodeid in msg
    assert "offline-only" in msg
    assert network_block.attempts == ["example.invalid:443"]
    network_block.attempts.clear()  # deliberate — see the fixture docstring


def test_direct_socket_connect_to_a_public_address_is_refused(network_block):
    """Not just urllib: the guard sits on the socket itself, so an IP
    literal (no DNS step) is refused at ``connect``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        with pytest.raises(NetworkBlockedError, match=r"203\.0\.113\.1:80"):
            s.connect(("203.0.113.1", 80))
    assert network_block.attempts == ["203.0.113.1:80"]
    network_block.attempts.clear()


def test_loopback_stays_open(network_block):
    """The web tests bind real 127.0.0.1 servers; the block must not
    see those as network."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=2) as c:
            assert c.getpeername()[1] == port
    assert socket.getaddrinfo("localhost", port)  # resolver path too
    assert network_block.attempts == []


def test_loopback_proxy_env_is_stripped():
    """A sandbox that routes egress through ``HTTPS_PROXY=http://127.0.0.1:N``
    would otherwise tunnel a blocked request through the loopback
    allowance — urllib re-reads these variables on every ``urlopen``."""
    for var in _PROXY_ENV_VARS:
        assert var not in os.environ, var


def test_error_is_not_an_oserror():
    """urllib wraps OSError into URLError and the repo's backoff loops
    retry URLError — the block must escape both, or a refused connect
    would still cost the sleeps it was meant to eliminate."""
    assert issubclass(NetworkBlockedError, RuntimeError)
    assert not issubclass(NetworkBlockedError, OSError)
