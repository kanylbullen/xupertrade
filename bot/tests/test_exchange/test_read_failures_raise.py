"""A failed exchange read must raise, never look like an empty book.

`HyperLiquidExchange.get_positions()` used to catch every exception and
return `[]`. Reconcile could not tell that apart from "you are flat" and
orphan-closed the whole book on a 502 — 31 % of testnet closes since May
(`bot/reports/analysis-2026-09-15.md` § 2). `get_balance()` had the same
shape and answered with `Balance(total=0)`, which wrote a $0 equity
snapshot.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from hypertrade.exchange.base import ExchangeReadError
from hypertrade.exchange.hyperliquid import HyperLiquidExchange
from hypertrade.exchange.paper import PaperExchange


@pytest.fixture
def fake_exchange():
    with patch.object(HyperLiquidExchange, "__init__", return_value=None):
        ex = HyperLiquidExchange()
        ex._account = MagicMock()
        ex._account_address = "0xabc"
        ex._info = MagicMock()
        ex._exchange = MagicMock()
        ex._executor = ThreadPoolExecutor(max_workers=2)
        ex._sz_decimals = {"BTC": 5, "ETH": 4}
        try:
            yield ex
        finally:
            ex._executor.shutdown(wait=False, cancel_futures=True)


def _boom(*_a, **_k):
    raise ConnectionError("502 Bad Gateway")


@pytest.mark.asyncio
async def test_get_positions_raises_instead_of_returning_empty(fake_exchange):
    fake_exchange._info.user_state = _boom
    with pytest.raises(ExchangeReadError):
        await fake_exchange.get_positions()


@pytest.mark.asyncio
async def test_get_balance_raises_instead_of_returning_zero(fake_exchange):
    fake_exchange._info.user_state = _boom
    with pytest.raises(ExchangeReadError):
        await fake_exchange.get_balance()


@pytest.mark.asyncio
async def test_get_position_propagates_the_read_error(fake_exchange):
    fake_exchange._info.user_state = _boom
    with pytest.raises(ExchangeReadError):
        await fake_exchange.get_position("BTC")


@pytest.mark.asyncio
async def test_the_original_error_is_kept_as_the_cause(fake_exchange):
    """The runner's transient-error filter reads `__cause__` to decide
    whether a failure is worth a Telegram ping."""
    fake_exchange._info.user_state = _boom
    with pytest.raises(ExchangeReadError) as excinfo:
        await fake_exchange.get_positions()
    assert isinstance(excinfo.value.__cause__, ConnectionError)


@pytest.mark.asyncio
async def test_a_genuinely_empty_book_still_returns_empty(fake_exchange):
    """The distinction only works if "flat" still reads as flat."""
    fake_exchange._info.user_state = lambda *_a, **_k: {
        "assetPositions": [], "marginSummary": {"accountValue": "900.0"},
        "withdrawable": "900.0",
    }
    assert await fake_exchange.get_positions() == []
    balance = await fake_exchange.get_balance()
    assert balance.total == pytest.approx(900.0)


@pytest.mark.asyncio
async def test_paper_exchange_behaviour_is_unchanged():
    """PaperExchange reads from memory — it cannot fail a network read,
    and nothing here should have given it new failure modes."""
    ex = PaperExchange(initial_balance=10_000)
    ex.set_price("BTC", 50_000)
    assert await ex.get_positions() == []
    assert await ex.get_position("BTC") is None
    balance = await ex.get_balance()
    assert balance.total == pytest.approx(10_000)

    await ex.place_order("BTC", "buy", 0.1)
    positions = await ex.get_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "BTC"
