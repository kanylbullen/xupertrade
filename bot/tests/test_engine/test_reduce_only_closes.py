"""NU-5a: exits that cannot open anything, and a rejected exit that is not
forgotten (roadmap `docs/plans/next-level-roadmap.md`, § NU-5).

No order was reduce-only, and `_resolve_close_size` sent the full DB size
when the exchange was flat or on the other side. After a liquidation or a
close by hand, a strategy's exit therefore OPENED the opposite side, which
reconcile market-closed ~5 minutes later: two fees and five minutes of
unintended exposure. And a rejected CLOSE was only logged, after the
strategy had already dropped the position, so nothing managed it.

Now every close is reduce-only, a flat exchange is never sent a close, and
a close that does not fill is classified by a successful read:

a. nothing left on the row's side → the row is booked as an external close
   priced from fills (#167's ledger); no order, no retry, one notice;
b. anything else, an unreadable exchange included → the strategy is
   re-synced to its row and retries next tick; one alert per (strategy,
   coin), a wider IOC band once after K rejections, then a critical alert.

Real `Repository` on SQLite, real `BotControl` on a dict-backed Redis, the
runner's own paths, and an exchange with HyperLiquid's reduce-only rule
and injected faults.
"""

from __future__ import annotations

import time

import pandas as pd
import pytest
from sqlalchemy import select

from hypertrade.config import settings
from hypertrade.db import models
from hypertrade.db.repo import Repository
from hypertrade.engine import runner as runner_mod
from hypertrade.engine.control import BotControl
from hypertrade.engine.runner import (
    CLOSE_REJECTS_BEFORE_WIDE_BAND,
    WIDE_CLOSE_BAND,
    EngineRunner,
)
from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.events.types import ErrorOccurred
from hypertrade.exchange.base import (
    ExchangeReadError,
    Order,
    OrderStatus,
    OrderType,
    Position,
)

TENANT = "00000000-0000-4000-8000-000000000001"
ENTRY = 2000.0
MID = 2100.0


class FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value

    async def delete(self, key):
        self.data.pop(key, None)

    async def smembers(self, key):
        return set()

    async def hgetall(self, key):
        return {}


class HLLike:
    """HyperLiquid as a close sees it: one netted position per coin, and
    its reduce-only rule — an order fills at most the position it reduces
    and is REJECTED against a flat position or one on its own side.

    Faults: `refuse` rejects the next N orders with the book untouched (an
    IOC that found no liquidity), `zero_fill` answers them FILLED with
    size 0; `read_errors` makes the next N `get_position` reads raise;
    `fills` is the fill history.
    """

    def __init__(self, *positions: Position):
        self.book = {p.symbol: p for p in positions}
        self.orders: list[dict] = []
        self.refuse = 0
        self.zero_fill = 0
        self.read_errors = 0
        self.fills: list[dict] = []

    def _signed(self, symbol: str) -> float:
        p = self.book.get(symbol)
        return 0.0 if p is None else (p.size if p.side == "long" else -p.size)

    async def get_position(self, symbol):
        if self.read_errors:
            self.read_errors -= 1
            raise ExchangeReadError("clearinghouseState: 502 Bad Gateway")
        return self.book.get(symbol)

    async def get_positions(self):
        return list(self.book.values())

    def get_size_precision(self, symbol):
        return 4

    async def fetch_user_fills(self, address=None, since_ms=None):
        return list(self.fills)

    async def get_current_price(self, symbol):
        return MID

    async def update_leverage(self, symbol, leverage, is_cross=True):
        return True

    async def place_order(
        self, symbol, side, size, order_type=OrderType.MARKET, *,
        reduce_only=False, slippage=None,
    ):
        self.orders.append({
            "symbol": symbol, "side": side, "size": size,
            "reduce_only": reduce_only, "slippage": slippage,
        })
        refused = Order(
            id=f"o{len(self.orders)}", symbol=symbol, side=side, size=size,
            order_type=order_type, status=OrderStatus.REJECTED,
        )
        if self.refuse:
            self.refuse -= 1
            return refused
        if self.zero_fill:
            self.zero_fill -= 1
            return Order(
                id=f"o{len(self.orders)}", symbol=symbol, side=side, size=0.0,
                order_type=order_type, filled_price=MID,
                status=OrderStatus.FILLED,
            )
        before = self._signed(symbol)
        delta = size if side == "buy" else -size
        if reduce_only:
            if before * delta >= 0:
                return refused  # "Reduce only order would increase position."
            delta = max(delta, -before) if delta < 0 else min(delta, -before)
        after = before + delta
        if abs(after) < 1e-12:
            self.book.pop(symbol, None)
        else:
            self.book[symbol] = Position(
                symbol=symbol, side="long" if after > 0 else "short",
                size=abs(after), entry_price=MID,
            )
        return Order(
            id=f"o{len(self.orders)}", symbol=symbol, side=side,
            size=abs(delta), order_type=order_type, filled_price=MID,
            status=OrderStatus.FILLED,
        )


class Exit:
    """Believes in a position and emits its exit on every bar while it
    does — a stop that stays hit. Like the real strategies, it drops the
    position the moment it emits the exit."""

    timeframe = "1h"
    leverage = 1

    def __init__(self, symbol="ETH"):
        self.name, self.symbol = f"s_{symbol}", symbol
        self.side: str | None = None

    async def on_candle(self, candles):
        if self.side is None:
            return None
        held, self.side = self.side, None
        action = (
            SignalAction.CLOSE_LONG if held == "long" else SignalAction.CLOSE_SHORT
        )
        return Signal(
            action=action, symbol=self.symbol, strategy_name=self.name,
            reason="stop hit",
        )

    def export_state(self):
        return {"side": self.side} if self.side else None

    def restore_state(self, side, entry):
        self.side = side

    def restore_from_json(self, side, entry, state):
        self.side = side

    def reset_position(self):
        self.side = None

    def on_filled(self, side, price):
        pass


class Bus:
    def __init__(self):
        self.events: list = []

    async def publish(self, event):
        self.events.append(event)
        return True

    def errors(self, strategy: str) -> list[str]:
        return [
            e.message for e in self.events
            if isinstance(e, ErrorOccurred) and e.strategy == strategy
        ]


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "tenant_id", TENANT)
    monkeypatch.setattr(settings, "kill_switch", False)
    monkeypatch.setattr(settings, "max_total_exposure_usd", 0)

    async def _fetch(symbol, timeframe, *a, **kw):
        ts = pd.date_range("2026-09-25", periods=3, freq="1h", tz="UTC")
        return pd.DataFrame({
            "timestamp": ts, "open": MID, "high": MID, "low": MID,
            "close": MID, "volume": 1.0,
        })

    monkeypatch.setattr(runner_mod, "fetch_candles", _fetch)


@pytest.fixture
async def repo():
    r = Repository("sqlite+aiosqlite:///:memory:")
    r._mode = "testnet"
    r._is_paper = False
    await r.init_db()
    yield r
    await r._engine.dispose()


def _runner(repo, exchange, *strategies):
    control = BotControl(redis_url="redis://unused/0", mode="testnet")
    control._redis = FakeRedis()
    bus = Bus()
    runner = EngineRunner(
        exchange=exchange, strategies=list(strategies), repo=repo,
        event_bus=bus, control=control,
    )
    runner._awaiting = None  # running a while: opens are not waiting
    return runner, bus


async def _hold(repo, strat, side="long", size=1.0):
    await repo.open_position(
        strategy_name=strat.name, symbol=strat.symbol, side=side,
        size=size, entry_price=ENTRY,
    )
    strat.side = side


def _close(strat, side="long"):
    action = SignalAction.CLOSE_LONG if side == "long" else SignalAction.CLOSE_SHORT
    return Signal(
        action=action, symbol=strat.symbol, strategy_name=strat.name,
        reason="stop hit",
    )


def _liquidation(symbol="ETH", size="1.0", px="1800.0", oid=9001):
    """HL's fill for a long closed outside the bot, after the row opened."""
    return {
        "coin": symbol, "side": "A", "sz": size, "px": px, "fee": "0.9",
        "time": int(time.time() * 1000) + 60_000, "oid": oid,
    }


async def _trades(repo):
    async with repo._session_factory() as session:
        return list((await session.execute(select(models.Trade))).scalars())


# ----------------------------------------------------------------------
# A close can never open or flip a position
# ----------------------------------------------------------------------


async def test_an_oversized_close_cannot_flip_the_position(repo):
    """The exchange holds 0.4 of a 1.0 row (part was closed by hand) and
    the read that would clamp the close fails, so the full 1.0 goes out.
    Reduce-only, it takes the long to flat and no further — it used to
    leave a 0.6 short for reconcile to close five minutes later."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=0.4, entry_price=ENTRY))
    ex.read_errors = 1
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat, size=1.0)

    await runner._execute_signal(_close(strat), MID)

    assert ex.orders == [{
        "symbol": "ETH", "side": "sell", "size": 1.0,
        "reduce_only": True, "slippage": None,
    }]
    assert ex.book == {}, "flat — never short"
    [row] = await repo.get_open_positions()
    assert row.size == pytest.approx(0.6), "the unfilled rest stays on its row"

    # The next exit finds the exchange flat: the rest is booked as closed
    # outside the bot. Nothing is sent.
    strat.side = "long"
    await runner._execute_signal(_close(strat), MID)
    assert len(ex.orders) == 1
    assert await repo.get_open_positions() == []
    assert ex.book == {}


async def test_a_flat_exchange_is_never_sent_a_close(repo):
    """Liquidated: the exchange is flat, the row still open. No order; the
    row is booked as an external close priced from HL's own fill, the PnL
    reaches the daily counter, one notice, nothing to retry."""
    strat = Exit()
    ex = HLLike()
    ex.fills = [_liquidation()]
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)

    await runner._run_strategy(strat)
    await runner._run_strategy(strat)  # the next tick: nothing to retry

    assert ex.orders == []
    assert await repo.get_open_positions() == []
    [trade] = await _trades(repo)
    assert trade.reason == "reconcile: external close (fill)"
    assert trade.order_id == "9001"
    assert trade.price == pytest.approx(1800.0)
    assert trade.pnl == pytest.approx((1800.0 - ENTRY) * 1.0 - 0.9)
    assert runner.portfolio._daily_pnl == pytest.approx(trade.pnl)
    assert strat.side is None
    [notice] = bus.errors(strat.name)
    assert notice.startswith("External close") and "CRITICAL" not in notice
    assert runner._close_rejects == {}


async def test_the_other_side_is_never_sent_a_close(repo):
    """The long was closed and a short opened by hand: selling the DB size
    would have added to that short."""
    strat = Exit()
    short = Position(symbol="ETH", side="short", size=3.0, entry_price=MID)
    ex = HLLike(short)
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)

    await runner._run_strategy(strat)

    assert ex.orders == []
    assert ex.book == {"ETH": short}, "the human's short is untouched"
    assert await repo.get_open_positions() == []
    [trade] = await _trades(repo)
    assert trade.reason == "ESTIMATED reconcile: external close @ mid"


async def test_a_close_the_exchange_refuses_as_flat_is_booked_from_fills(repo):
    """With allow_multi_coin on the close is sent whatever the read says;
    HL refuses the reduce-only order against the flat book. The refusal is
    classified by a read, not retried: the row is booked from fills, one
    notice, no critical alert, no wider band."""
    strat = Exit()
    ex = HLLike()
    ex.fills = [_liquidation()]
    runner, bus = _runner(repo, ex, strat)
    await runner.control.set_allow_multi_coin(True)
    await _hold(repo, strat)

    for _ in range(3):
        await runner._run_strategy(strat)

    assert [o["reduce_only"] for o in ex.orders] == [True], "sent once"
    assert await repo.get_open_positions() == []
    [trade] = await _trades(repo)
    assert trade.reason == "reconcile: external close (fill)"
    [notice] = bus.errors(strat.name)
    assert notice.startswith("External close") and "CRITICAL" not in notice
    assert runner._close_rejects == {}


# ----------------------------------------------------------------------
# An unreadable exchange is never "flat"
# ----------------------------------------------------------------------


async def test_a_read_error_is_never_treated_as_flat(repo):
    """Both reads fail — before the close and after its rejection. The
    close still goes out at the DB size, reduce-only; its rejection keeps
    the row open and the strategy re-synced, never an external close."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    ex.read_errors = 2
    ex.refuse = 1
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)

    await runner._run_strategy(strat)

    assert [(o["size"], o["reduce_only"]) for o in ex.orders] == [(1.0, True)]
    [row] = await repo.get_open_positions()
    assert row.size == pytest.approx(1.0)
    assert await _trades(repo) == []
    assert strat.side == "long", "re-synced to its row"
    [alert] = bus.errors(strat.name)
    assert "unreadable" in alert and "still open" in alert


# ----------------------------------------------------------------------
# Any other rejection is retried
# ----------------------------------------------------------------------


@pytest.mark.parametrize("fault", ["refuse", "zero_fill"])
async def test_another_rejected_close_is_retried_next_tick(repo, fault):
    """The IOC found no liquidity (rejected, or "filled" 0); the position
    is still there. The strategy had dropped it on emitting the exit —
    re-synced, it emits the exit again next tick, and that one fills."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    setattr(ex, fault, 1)
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)

    await runner._run_strategy(strat)
    assert strat.side == "long"
    assert len(await repo.get_open_positions()) == 1
    assert await _trades(repo) == [], "a zero fill books nothing"

    await runner._run_strategy(strat)

    assert [o["reduce_only"] for o in ex.orders] == [True, True]
    assert ex.book == {}
    assert await repo.get_open_positions() == []
    [trade] = await _trades(repo)
    assert trade.reason == "stop hit"
    assert len(bus.errors(strat.name)) == 1, "one alert for the episode"
    assert runner._close_rejects == {}


async def test_k_rejections_widen_the_band_once_then_alert_critical(repo):
    k = CLOSE_REJECTS_BEFORE_WIDE_BAND
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    ex.refuse = k + 3
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)

    for _ in range(k + 3):
        await runner._run_strategy(strat)

    bands = [o["slippage"] for o in ex.orders]
    assert bands == [None] * k + [WIDE_CLOSE_BAND] + [None] * 2
    alerts = bus.errors(strat.name)
    assert len(alerts) == 2
    assert "CRITICAL" not in alerts[0] and alerts[1].startswith("CRITICAL")
    assert strat.side == "long"
    assert len(await repo.get_open_positions()) == 1

    await runner._run_strategy(strat)  # the book clears: it still resolves
    assert ex.book == {} and await repo.get_open_positions() == []
    assert runner._close_rejects == {}


# ----------------------------------------------------------------------
# Every close path is reduce-only; opens are not
# ----------------------------------------------------------------------


async def test_a_flip_closes_reduce_only_and_opens_without(repo):
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="short", size=1.0, entry_price=ENTRY))
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat, side="short")

    ok = await runner._execute_signal(Signal(
        action=SignalAction.OPEN_LONG, symbol="ETH",
        strategy_name=strat.name, reason="flip",
    ), MID)

    assert ok is True
    assert [(o["side"], o["reduce_only"]) for o in ex.orders] == [
        ("buy", True), ("buy", False),
    ]
    assert ex.book["ETH"].side == "long"
    [row] = await repo.get_open_positions()
    assert row.side == "long"


async def test_flat_all_closes_reduce_only(repo):
    eth, btc = Exit("ETH"), Exit("BTC")
    ex = HLLike(
        Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY),
        Position(symbol="BTC", side="short", size=0.5, entry_price=ENTRY),
    )
    runner, bus = _runner(repo, ex, eth, btc)
    await _hold(repo, eth)
    await _hold(repo, btc, side="short", size=0.5)

    status = await runner._flat_all_positions()

    assert status.ok and status.closed == 2
    assert [o["reduce_only"] for o in ex.orders] == [True, True]
    assert ex.book == {} and await repo.get_open_positions() == []


# ----------------------------------------------------------------------
# The NU-2 reconcile hold (#186) stops opens, never exits
# ----------------------------------------------------------------------


async def test_the_reconcile_hold_never_blocks_any_of_this(repo):
    eth, btc = Exit("ETH"), Exit("BTC")
    ex = HLLike(Position(symbol="BTC", side="long", size=0.5, entry_price=ENTRY))
    ex.fills = [_liquidation()]
    ex.refuse = 1
    runner, bus = _runner(repo, ex, eth, btc)
    await runner.control.write_sentinel()
    await runner.control.set_reconcile_hold(True)
    await runner._check_control_state()
    assert not await runner.portfolio.check_risk_limits(is_open=True), "held"
    await _hold(repo, eth)  # liquidated: the exchange is flat on ETH
    await _hold(repo, btc, size=0.5)

    for _ in range(2):
        for strat in (eth, btc):
            await runner._run_strategy(strat)

    assert await repo.get_open_positions() == []
    reasons = sorted(t.reason for t in await _trades(repo))
    assert reasons == ["reconcile: external close (fill)", "stop hit"]
    assert [o["symbol"] for o in ex.orders] == ["BTC", "BTC"], "refused, retried"
    assert ex.book == {}
    assert not await runner.portfolio.check_risk_limits(is_open=True), "still held"
