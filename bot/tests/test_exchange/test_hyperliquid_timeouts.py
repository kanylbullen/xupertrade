"""Tests for the HyperLiquid SDK timeout fix (audit M2).

Without timeouts, a hung HL API call would block the executor thread
indefinitely → block the runner tick → freeze heartbeat + risk-cap
checks. Verify `_run` enforces deadlines and that order placement
gracefully degrades to REJECTED on timeout.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch, MagicMock

import pytest

from hypertrade.config import settings
from hypertrade.exchange.base import OrderStatus
from hypertrade.exchange.hyperliquid import HyperLiquidExchange


@pytest.fixture
def fake_exchange():
    """Build a HyperLiquidExchange with all SDK init bypassed.

    We don't need a real HL connection — just the wrapper methods.
    Yields the instance and cancels the executor on teardown so worker
    threads stuck inside `time.sleep` (from the hang-tests) don't leak
    across the suite (would cause flaky hangs at process exit).
    """
    from concurrent.futures import ThreadPoolExecutor
    with patch.object(
        HyperLiquidExchange, "__init__", return_value=None,
    ) as _:
        ex = HyperLiquidExchange()
        ex._account = MagicMock()
        ex._account_address = "0xabc"
        ex._info = MagicMock()
        ex._exchange = MagicMock()
        ex._executor = ThreadPoolExecutor(max_workers=2)
        ex._sz_decimals = {"BTC": 5, "SOL": 2, "ETH": 4}
        try:
            yield ex
        finally:
            ex._executor.shutdown(wait=False, cancel_futures=True)


@pytest.mark.asyncio
async def test_run_returns_quickly_when_sdk_returns_quickly(fake_exchange):
    """Happy path: SDK call returns inside the deadline → result returned."""
    def quick():
        return {"ok": True}

    result = await fake_exchange._run(quick, timeout=2.0)
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_run_raises_timeout_when_sdk_hangs(fake_exchange):
    """If SDK call exceeds the deadline, `_run` raises TimeoutError.
    The runner tick must NOT block on hung HL calls — that's the
    audit M2 root cause."""
    def slow():
        time.sleep(2.0)
        return "should never get here"

    started = asyncio.get_running_loop().time()
    with pytest.raises(asyncio.TimeoutError):
        await fake_exchange._run(slow, timeout=0.3)
    elapsed = asyncio.get_running_loop().time() - started
    # We should have unblocked at ~the deadline, not at 2s.
    assert elapsed < 1.5, f"_run took {elapsed:.2f}s — timeout didn't fire"


@pytest.mark.asyncio
async def test_run_uses_settings_default_when_no_timeout_passed(fake_exchange):
    """Default timeout = settings.hl_read_timeout_seconds (5s)."""
    def quick():
        return 42
    # Just verify it works with default — actual seconds not asserted
    # since we'd need to override settings to test the 5s default.
    result = await fake_exchange._run(quick)
    assert result == 42


@pytest.mark.asyncio
async def test_place_order_returns_rejected_on_timeout(fake_exchange):
    """If HL `_exchange.order` hangs past the order-timeout, place_order
    returns an Order with status=REJECTED — not propagating the
    exception or blocking forever."""
    def hang(*args, **kwargs):
        time.sleep(2.0)
        return {"status": "ok", "response": {}}
    fake_exchange._exchange.order = hang

    # Stub get_current_price to skip the HTTP call
    async def _mid(_sym):
        return 50_000.0
    fake_exchange.get_current_price = _mid

    # Override the order timeout to a tiny value for the test.
    with patch.object(settings, "hl_order_timeout_seconds", 0.3):
        order = await fake_exchange.place_order(
            symbol="BTC", side="buy", size=0.001,
        )
    assert order.status == OrderStatus.REJECTED


@pytest.mark.asyncio
async def test_cancel_order_returns_false_on_timeout(fake_exchange):
    """Hung cancel doesn't propagate — returns False so the caller
    can decide on follow-up action."""
    def hang(_coin, _oid):
        time.sleep(2.0)
        return None
    fake_exchange._exchange.cancel = hang

    with patch.object(settings, "hl_order_timeout_seconds", 0.3):
        ok = await fake_exchange.cancel_order("123", "BTC")
    assert ok is False


@pytest.mark.asyncio
async def test_update_leverage_returns_false_on_timeout(fake_exchange):
    """Same pattern for leverage update — caller gets False, no hang."""
    def hang(_lev, _sym, _is_cross):
        time.sleep(2.0)
        return {"status": "ok"}
    fake_exchange._exchange.update_leverage = hang

    with patch.object(settings, "hl_order_timeout_seconds", 0.3):
        ok = await fake_exchange.update_leverage("BTC", 2)
    assert ok is False


# --- HL init retry (2026-05-09 outage hardening) ----------------------
#
# These drive the real SDK `Info` / `Exchange` constructors; only
# `requests.Session.post` is faked (`hl_http`, tests/test_exchange/
# conftest.py). Patching `Info` itself hid that the SDK constructor
# indexes its own metadata fetch unchecked.


def test_init_raises_clean_runtime_error_when_hl_unreachable(
    hl_http, build_exchange,
):
    """A HL outage at bot startup used to bubble a noisy SDK error +
    restart-loop. Now it retries `hl_init_retry_attempts` times then
    raises a clean RuntimeError naming HL as the cause."""
    hl_http.route("meta", (503, "Service Unavailable"))

    with pytest.raises(RuntimeError, match="HyperLiquid API unreachable"):
        build_exchange(attempts=3)
    assert hl_http.count("meta") == 3


def test_init_succeeds_on_retry_after_transient_failure(
    hl_http, build_exchange,
):
    """First N-1 attempts fail, last one succeeds → init completes and
    the szDecimals cache is populated from the answer."""
    from .conftest import META

    hl_http.route("meta", (503, "transient"), (502, "transient"), (200, META))

    ex = build_exchange(attempts=5)

    assert hl_http.count("meta") == 3
    assert ex._sz_decimals == {"BTC": 5, "ETH": 4, "SOL": 2}


def test_init_does_not_retry_on_non_transient_error(hl_http):
    """Programming/config errors (TypeError, AuthError, etc.) re-raise
    immediately instead of being misleadingly wrapped as 'HL API
    unreachable'. (PR #24 review fix.)"""
    from hypertrade.exchange import hyperliquid as hl_module

    call_count = {"n": 0}

    def _bug(*args, **kwargs):
        call_count["n"] += 1
        raise TypeError("bug in our config plumbing")

    with patch.object(hl_module, "HLExchange", side_effect=_bug), \
         patch.object(settings, "hl_init_retry_attempts", 5), \
         patch.object(settings, "hl_init_retry_backoff_seconds", 0.0), \
         patch.object(settings, "hyperliquid_private_key",
                      "0x" + "1" * 64):
        with pytest.raises(TypeError, match="bug in our config plumbing"):
            hl_module.HyperLiquidExchange()
    assert call_count["n"] == 1, (
        f"non-transient error should fire ONCE then propagate, "
        f"got {call_count['n']} attempts"
    )
    assert hl_http.count("meta") == 1
