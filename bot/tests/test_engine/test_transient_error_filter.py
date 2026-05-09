"""Tests for the transient-network-error filter on strategy-tick alerts.

Without this filter, a HL outage would publish one ErrorOccurred event
per strategy per tick → ~22 events/min → Telegram spam. The 2026-05-09
outage lasted 4.5h, would have produced ~6000 spam events.
"""

from __future__ import annotations

import asyncio
import socket

import aiohttp
import pytest

from hypertrade.engine.runner import _is_transient_network_error


class _RequestsConnectionError(ConnectionError):
    """Mimic the requests.exceptions.ConnectionError name without
    importing requests (it inherits from a different base than our
    standard ConnectionError on Python 3.x)."""
    pass


@pytest.mark.parametrize("exc", [
    asyncio.TimeoutError(),
    ConnectionError("connection refused"),
    socket.gaierror(8, "nodename nor servname provided"),
    aiohttp.ClientError("client died"),
    aiohttp.ClientConnectionError(),
])
def test_classifies_standard_network_errors_as_transient(exc):
    assert _is_transient_network_error(exc) is True


def test_classifies_5xx_server_error_as_transient():
    """HL SDK's ServerError is one class for all HTTP errors —
    discriminate by code in the message."""
    try:
        from hyperliquid.utils.error import ServerError
    except ImportError:
        pytest.skip("hyperliquid SDK not available")
    for code in ("502", "503", "504", "408", "429"):
        exc = ServerError(int(code), f"<html><h1>{code} Bad Gateway</h1></html>")
        assert _is_transient_network_error(exc) is True, (
            f"HTTP {code} should be transient"
        )


def test_classifies_4xx_validation_error_as_NON_transient():
    """4xx (other than 408/429) means our request is bad — retrying
    won't help and we DO want a Telegram alert."""
    try:
        from hyperliquid.utils.error import ServerError
    except ImportError:
        pytest.skip("hyperliquid SDK not available")
    exc = ServerError(400, "<html>400 Bad Request — invalid order size</html>")
    assert _is_transient_network_error(exc) is False


def test_classifies_logic_bugs_as_NON_transient():
    """ValueError / KeyError / AttributeError from a buggy strategy or
    a bad DB row aren't network — we want the alert."""
    for exc in (
        ValueError("invalid SL"),
        KeyError("missing column"),
        AttributeError("'Strategy' object has no attribute 'foo'"),
        RuntimeError("some logic bug"),
        ZeroDivisionError("nope"),
    ):
        assert _is_transient_network_error(exc) is False, (
            f"{type(exc).__name__} should NOT be classified as transient"
        )


def test_classifies_requests_connection_error_via_name_match():
    """The script's name-based fallback covers requests.ConnectionError
    even though it doesn't subclass our standard ConnectionError on
    Python 3.x. (Synthesized without importing requests.)"""
    fake = _RequestsConnectionError("HTTPSConnectionPool failed")
    # Our class IS a subclass of ConnectionError, so it'll hit the first
    # branch; for a TRUE non-subclass case we use a class with the right
    # __name__ but a different base.
    class FakeConnErr(Exception):
        pass
    FakeConnErr.__name__ = "ConnectionError"
    assert _is_transient_network_error(FakeConnErr()) is True


def test_classifies_read_timeout_via_name_match():
    class FakeReadTimeout(Exception):
        pass
    FakeReadTimeout.__name__ = "ReadTimeout"
    assert _is_transient_network_error(FakeReadTimeout()) is True
