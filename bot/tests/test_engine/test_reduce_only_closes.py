"""NU-5a: exits that cannot open anything, and a rejected exit that is not
forgotten (roadmap `docs/plans/next-level-roadmap.md`, § NU-5).

No order was reduce-only, and `_resolve_close_size` sent the full DB size
when the exchange was flat or on the other side. After a liquidation or a
close by hand, a strategy's exit therefore OPENED the opposite side, which
reconcile market-closed ~5 minutes later: two fees and five minutes of
unintended exposure. And a rejected CLOSE was only logged, after the
strategy had already dropped the position, so nothing managed it.

Now every close is reduce-only, and a close that leaves its row open is
classified by a read that a second read confirms:

a. nothing left on the row's side → the row is booked as an external close
   priced from fills (#167's ledger); no retry, one notice;
b. anything else, an unreadable exchange included → the strategy is
   re-armed with its state from before the exit and retries next tick; one
   alert per (strategy, coin), a wider IOC band from K rejections on, and a
   critical alert when that fails too.

Real `Repository` on SQLite, real `BotControl` on a dict-backed Redis, the
runner's own paths, and the paper exchange — HyperLiquid's reduce-only rule
as paper mode runs it — with injected faults.
"""

from __future__ import annotations

import logging
import time
from unittest.mock import AsyncMock

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
from hypertrade.exchange.paper import PaperExchange
from hypertrade.strategies.ath_breakout import AthBreakoutStrategy

from .test_restore_guards import FakeRedis

TENANT = "00000000-0000-4000-8000-000000000001"
ENTRY = 2000.0
MID = 2100.0


class HLLike(PaperExchange):
    """The paper exchange, whose reduce-only rule is HyperLiquid's (fills
    at most the position it reduces, REJECTED against a flat position or
    one on its own side), with faults.

    `refuse` rejects the next N orders with the book untouched (an IOC
    that found no liquidity), `zero_fill` answers them FILLED with size 0;
    `read_errors` makes the next N `get_position` reads raise, `blind`
    makes them answer flat while the coin is held (a read that misses
    it); `stale` is what `get_positions` answers instead of the book;
    `fills` is the fill history.
    """

    def __init__(self, *positions: Position):
        super().__init__(initial_balance=1_000_000.0)
        for symbol in ("ETH", "BTC"):
            self.set_price(symbol, MID)
        self._positions.update({p.symbol: p for p in positions})
        self.orders: list[dict] = []
        self.refuse = self.zero_fill = self.read_errors = self.blind = 0
        self.fills: list[dict] = []
        self.stale: list[Position] | None = None

    @property
    def book(self) -> dict[str, Position]:
        return self._positions

    async def get_position(self, symbol):
        if self.read_errors:
            self.read_errors -= 1
            raise ExchangeReadError("clearinghouseState: 502 Bad Gateway")
        if self.blind:
            self.blind -= 1
            return None
        return await super().get_position(symbol)

    async def get_positions(self):
        if self.stale is not None:
            return list(self.stale)
        return await super().get_positions()

    async def fetch_user_fills(self, address=None, since_ms=None):
        return list(self.fills)

    async def update_leverage(self, symbol, leverage, is_cross=True):
        return True

    async def place_order(
        self, symbol, side, size, order_type=OrderType.MARKET, price=None, *,
        reduce_only=False, slippage=None,
    ):
        self.orders.append({
            "symbol": symbol, "side": side, "size": size,
            "reduce_only": reduce_only, "slippage": slippage,
        })
        if self.refuse or self.zero_fill:
            zero = not self.refuse
            self.refuse, self.zero_fill = (
                (self.refuse - 1, self.zero_fill) if self.refuse
                else (0, self.zero_fill - 1)
            )
            return Order(
                id=f"o{len(self.orders)}", symbol=symbol, side=side,
                size=0.0 if zero else size, order_type=order_type,
                filled_price=MID if zero else None,
                status=OrderStatus.FILLED if zero else OrderStatus.REJECTED,
                error=None if zero else "Order could not immediately match",
            )
        return await super().place_order(
            symbol, side, size, order_type, price,
            reduce_only=reduce_only, slippage=slippage,
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
        self.quiet = False  # the stop is no longer hit

    async def on_candle(self, candles):
        if self.side is None or self.quiet:
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
        self.deliver = True  # False: nobody received it (a notifier gap)

    async def publish(self, event):
        if self.deliver:
            self.events.append(event)
        return self.deliver

    def errors(self, strategy: str) -> list[str]:
        return [
            e.message for e in self.events
            if isinstance(e, ErrorOccurred) and e.strategy == strategy
        ]


def _candles(close=MID, high=None, n=3):
    ts = pd.date_range("2026-09-25", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({
        "timestamp": ts, "open": close, "high": high or close, "low": close,
        "close": close, "volume": 1.0,
    })


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "tenant_id", TENANT)
    monkeypatch.setattr(settings, "kill_switch", False)
    monkeypatch.setattr(settings, "max_total_exposure_usd", 0)

    async def _fetch(symbol, timeframe, *a, **kw):
        return _candles()

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
    runner.CLOSE_CONFIRM_DELAY_SECONDS = 0
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
    """The exchange holds 0.4 of a 1.0 row (0.6 was liquidated) and the
    read that would clamp the close fails, so the full 1.0 goes out.
    Reduce-only, it takes the long to flat and no further — it used to
    leave a 0.6 short for reconcile to close five minutes later. The 0.6
    that was gone before is booked from its own fill."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=0.4, entry_price=ENTRY))
    ex.fills = [_liquidation(size="0.6")]
    ex.read_errors = 1
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat, size=1.0)

    await runner._execute_signal(_close(strat), MID)

    assert ex.orders == [{
        "symbol": "ETH", "side": "sell", "size": 1.0,
        "reduce_only": True, "slippage": None,
    }]
    assert ex.book == {}, "flat — never short"
    assert await repo.get_open_positions() == []
    trades = sorted(await _trades(repo), key=lambda t: t.size)
    assert [(t.size, t.reason) for t in trades] == [
        (pytest.approx(0.4), "stop hit"),
        (pytest.approx(0.6), "reconcile: external close (fill)"),
    ]
    assert strat.side is None


async def test_a_partial_liquidation_is_booked_from_its_fill(repo):
    """A successful read shows 0.4 of the 1.0 row: the close is clamped to
    0.4, and the 0.6 liquidated before it is booked from its fill in the
    same tick — it used to reach neither a trade row nor the daily PnL."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=0.4, entry_price=ENTRY))
    ex.fills = [_liquidation(size="0.6")]
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat, size=1.0)

    await runner._run_strategy(strat)

    assert [(o["size"], o["reduce_only"]) for o in ex.orders] == [(0.4, True)]
    assert ex.book == {} and await repo.get_open_positions() == []
    trades = await _trades(repo)
    assert sorted(t.size for t in trades) == [pytest.approx(0.4), pytest.approx(0.6)]
    assert runner.portfolio._daily_pnl == pytest.approx(sum(t.pnl for t in trades))
    [notice] = bus.errors(strat.name)
    assert notice.startswith("External close: long 0.6")


async def test_a_close_against_a_flat_exchange_is_booked_from_fills(repo, caplog):
    """Liquidated: the exchange is flat, the row still open. The close goes
    out reduce-only and is refused; a confirmed read books the row from
    HL's own fill, the PnL reaches the daily counter, one notice (logged
    at WARNING), nothing is retried."""
    strat = Exit()
    ex = HLLike()
    ex.fills = [_liquidation()]
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)

    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            await runner._run_strategy(strat)

    assert [o["reduce_only"] for o in ex.orders] == [True], "sent once"
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
    assert "Reduce only order would increase position" in notice
    assert any(
        r.levelno == logging.WARNING and "External close" in r.getMessage()
        for r in caplog.records
    )
    assert runner._close_rejects == {}


async def test_the_other_side_is_refused_and_left_alone(repo):
    """The long was closed and a short opened by hand. The reduce-only sell
    cannot add to that short; the row is booked, the short untouched."""
    strat = Exit()
    short = Position(symbol="ETH", side="short", size=3.0, entry_price=MID)
    ex = HLLike(short)
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)

    await runner._run_strategy(strat)

    assert [(o["side"], o["reduce_only"]) for o in ex.orders] == [("sell", True)]
    assert ex.book == {"ETH": short} and short.size == 3.0
    assert await repo.get_open_positions() == []
    [trade] = await _trades(repo)
    assert trade.reason == "ESTIMATED reconcile: external close @ mid"


# ----------------------------------------------------------------------
# A read is never enough to call a position gone
# ----------------------------------------------------------------------


async def test_a_read_that_misses_the_coin_still_sends_the_close(repo):
    """A successful read leaves ETH out while the long is open. The close
    goes out anyway, fills, and books the real exit — nothing is booked
    from the read, and the position is never orphaned."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    ex.blind = 1
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)

    await runner._run_strategy(strat)

    assert ex.book == {}
    [trade] = await _trades(repo)
    assert trade.reason == "stop hit" and trade.size == pytest.approx(1.0)
    assert bus.errors(strat.name) == []


async def test_a_flat_read_after_a_refusal_needs_a_second_read(repo):
    """The close is refused (no liquidity) and the next read misses ETH;
    the confirming read finds the long. Not booked: the strategy is
    re-armed and the exit fills next tick."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    ex.refuse = 1
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)
    real = ex.get_position
    reads = []

    async def _get_position(symbol):
        reads.append(symbol)
        if len(reads) == 2:  # the first read after the refusal
            return None
        return await real(symbol)

    ex.get_position = _get_position

    await runner._run_strategy(strat)
    assert await _trades(repo) == [] and strat.side == "long"
    await runner._run_strategy(strat)

    assert ex.book == {}
    [trade] = await _trades(repo)
    assert trade.reason == "stop hit"


async def test_a_read_error_is_never_treated_as_flat(repo):
    """Both reads fail — before the close and after its rejection. The
    close still goes out at the DB size, reduce-only; its rejection keeps
    the row open and the strategy re-armed, never an external close."""
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
    assert strat.side == "long", "re-armed on its row"
    [alert] = bus.errors(strat.name)
    assert "unreadable" in alert and "still open" in alert
    assert "Order could not immediately match" in alert, "HL's reason"


# ----------------------------------------------------------------------
# Any other rejection is retried
# ----------------------------------------------------------------------


@pytest.mark.parametrize("fault", ["refuse", "zero_fill"])
async def test_another_rejected_close_is_retried_next_tick(repo, fault):
    """The IOC found no liquidity (rejected, or "filled" 0); the position
    is still there. The strategy had dropped it on emitting the exit —
    re-armed, it emits the exit again next tick, and that one fills."""
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


async def test_a_ratcheted_trailing_stop_is_re_armed_not_rolled_back(repo, monkeypatch):
    """ath_breakout opened at 100 on a bar whose high was 101 — what its
    row's state_json says. Its peak then ratcheted to 200, so the trail
    stop is 130, and a close at 125 fires CLOSE_LONG. HL refuses it.
    Re-synced to the row the peak fell back to 101 and the stop to 65.65:
    the exit never fired again. Re-armed with its pre-exit state, it is
    sent again next tick and fills."""
    async def _fetch(symbol, timeframe, *a, **kw):
        return _candles(close=125.0)

    monkeypatch.setattr(runner_mod, "fetch_candles", _fetch)
    strat = AthBreakoutStrategy(lookback=1)
    strat.restore_from_json(
        "long", 100.0, {"entry_price": 100.0, "peak_since_entry": 200.0},
    )
    ex = HLLike(Position(symbol="BTC", side="long", size=0.01, entry_price=100.0))
    ex.refuse = 1
    runner, bus = _runner(repo, ex, strat)
    await repo.open_position(
        strategy_name=strat.name, symbol="BTC", side="long", size=0.01,
        entry_price=100.0,
        state_json='{"entry_price": 100.0, "peak_since_entry": 101.0}',
    )

    await runner._run_strategy(strat)
    assert strat._peak_since_entry == 200.0, "the ratchet is kept"
    [alert] = bus.errors(strat.name)
    assert "re-armed" in alert

    await runner._run_strategy(strat)

    assert [o["reduce_only"] for o in ex.orders] == [True, True]
    assert ex.book == {} and await repo.get_open_positions() == []
    [trade] = await _trades(repo)
    assert trade.reason.startswith("Trail stop hit")


async def test_a_streak_ends_when_the_exit_stops_firing(repo):
    """A close is refused once, then its stop is no longer hit. The streak
    ends, so a later rejection — here the close half of a flip — alerts
    again, and says the flip's open was not sent."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="short", size=1.0, entry_price=ENTRY))
    ex.refuse = 2
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat, side="short")

    await runner._run_strategy(strat)  # refused: alert, streak 1
    strat.quiet = True
    await runner._run_strategy(strat)  # no exit: the streak ends
    assert runner._close_rejects == {}

    ok = await runner._execute_signal(Signal(
        action=SignalAction.OPEN_LONG, symbol="ETH",
        strategy_name=strat.name, reason="flip",
    ), MID)

    assert ok is False
    first, second = bus.errors(strat.name)
    assert "the close half of a flip, so its open is not sent" in second
    assert [o["side"] for o in ex.orders] == ["buy", "buy"], "no open sent"


async def test_k_rejections_widen_the_band_then_alert_critical(repo):
    k = CLOSE_REJECTS_BEFORE_WIDE_BAND
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    ex.refuse = k + 3
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)

    for _ in range(k + 3):
        await runner._run_strategy(strat)

    bands = [o["slippage"] for o in ex.orders]
    assert bands == [None] * k + [WIDE_CLOSE_BAND] * 3
    alerts = bus.errors(strat.name)
    assert len(alerts) == 2
    assert "CRITICAL" not in alerts[0] and alerts[1].startswith("CRITICAL")
    assert strat.side == "long"
    assert len(await repo.get_open_positions()) == 1

    await runner._run_strategy(strat)  # the book clears: it still resolves
    assert ex.book == {} and await repo.get_open_positions() == []
    assert runner._close_rejects == {}


async def test_an_undelivered_critical_alert_is_sent_again(repo):
    """The bus took nobody's delivery when the critical alert went out
    (a notifier re-subscribing): it goes again with the next rejection."""
    k = CLOSE_REJECTS_BEFORE_WIDE_BAND
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    ex.refuse = k + 3
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)

    for _ in range(k):
        await runner._run_strategy(strat)
    bus.deliver = False
    await runner._run_strategy(strat)  # the critical one: lost
    bus.deliver = True
    for _ in range(2):
        await runner._run_strategy(strat)

    alerts = bus.errors(strat.name)
    assert [a.startswith("CRITICAL") for a in alerts] == [False, True]


async def test_a_failed_classification_still_re_arms_the_strategy(repo):
    """Whatever raises while a refused close is classified, the strategy
    is pointed back at its row — it had dropped the position, and left
    flat nothing would ever close it."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    ex.refuse = 1
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)
    runner._held_after_close = AsyncMock(side_effect=ConnectionError("db blip"))

    await runner._run_strategy(strat)

    assert strat.side == "long"
    assert len(await repo.get_open_positions()) == 1


async def test_a_db_failure_after_a_short_fill_still_re_arms(repo):
    """The close filled 0.4 of 1.0 and the re-read of its row fails: the
    strategy is re-armed on the row it had, so its exit closes the rest."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)
    real_place = ex.place_order

    async def _short_fill(symbol, side, size, *a, **kw):
        return await real_place(symbol, side, 0.4, *a, **kw)

    ex.place_order = _short_fill
    real_get = repo.get_open_position
    calls = []

    async def _get(*a, **kw):
        calls.append(a)
        if len(calls) > 2:  # the classifier's re-read
            raise ConnectionError("db blip")
        return await real_get(*a, **kw)

    repo.get_open_position = _get

    await runner._run_strategy(strat)

    assert strat.side == "long"
    [row] = await repo.get_open_positions()
    assert row.size == pytest.approx(0.6)


# ----------------------------------------------------------------------
# A flip into a position that is already gone
# ----------------------------------------------------------------------


async def test_a_flip_whose_old_side_was_liquidated_still_opens(repo):
    """The long row was liquidated; the strategy flips to short. The close
    half is refused and booked from the liquidation fill, and the open
    goes ahead — DB and exchange agree nothing was held. One notice."""
    strat = Exit()
    ex = HLLike()
    ex.fills = [_liquidation()]
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)
    strat.side = "short"  # it switched when it emitted the flip

    ok = await runner._execute_signal(Signal(
        action=SignalAction.OPEN_SHORT, symbol="ETH",
        strategy_name=strat.name, reason="flip",
    ), MID)

    assert ok is True
    assert [(o["side"], o["reduce_only"]) for o in ex.orders] == [
        ("sell", True), ("sell", False),
    ]
    assert ex.book["ETH"].side == "short"
    [row] = await repo.get_open_positions()
    assert row.side == "short"
    assert strat.side == "short", "not reset: its open went ahead"
    [notice] = bus.errors(strat.name)
    assert notice.startswith("External close") and "open goes ahead" in notice


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


async def test_flat_all_books_what_filled_not_what_was_read(repo):
    """The read says 1.0 but 0.6 was cut before the order: reduce-only
    fills 0.4, and 0.4 is what is booked — the rest stays on the row for
    reconcile to price from fills, and the book is flat."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=0.4, entry_price=ENTRY))
    ex.stale = [Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY)]
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)

    status = await runner._flat_all_positions()

    assert ex.book == {}
    [trade] = await _trades(repo)
    assert trade.size == pytest.approx(0.4)
    [row] = await repo.get_open_positions()
    assert row.size == pytest.approx(0.6)
    assert not status.ok, "0.6 of what it read did not fill"


async def test_flat_all_without_a_row_books_what_filled(repo):
    ex = HLLike(Position(symbol="ETH", side="long", size=0.4, entry_price=ENTRY))
    ex.stale = [Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY)]
    runner, _ = _runner(repo, ex)

    await runner._flat_all_positions()

    [trade] = await _trades(repo)
    assert (trade.strategy_name, trade.size) == ("manual_flat", pytest.approx(0.4))
    assert trade.fee == pytest.approx(MID * 0.4 * settings.taker_fee_rate)


async def test_reconcile_books_the_orphan_size_that_filled(repo):
    """Pass 2 confirms an orphan long of 2.0; it is cut to 0.5 before the
    reduce-only close lands. The trade row and its fee are 0.5, not 2.0."""
    ex = HLLike(Position(symbol="ETH", side="long", size=0.5, entry_price=ENTRY))
    ex.stale = [Position(symbol="ETH", side="long", size=2.0, entry_price=ENTRY)]

    result = await repo.reconcile_positions(
        ex, confirm_delay_seconds=0, hold_orphans=None,
        traded_symbols={"ETH"},
    )

    assert ex.orders[0]["reduce_only"] is True and ex.book == {}
    [trade] = await _trades(repo)
    assert trade.size == pytest.approx(0.5)
    assert trade.fee == pytest.approx(MID * 0.5 * settings.taker_fee_rate)
    assert result.actions == [
        f"closed exchange-orphan: long 0.5 ETH @ {MID:.6f} (filled of 2.0)",
    ]


# ----------------------------------------------------------------------
# The NU-2 reconcile hold (#186) stops opens, never exits
# ----------------------------------------------------------------------


async def test_the_reconcile_hold_never_blocks_any_of_this(repo):
    eth, btc = Exit("ETH"), Exit("BTC")
    ex = HLLike(Position(symbol="BTC", side="long", size=0.5, entry_price=ENTRY))
    ex.fills = [_liquidation()]
    ex.refuse = 1
    runner, bus = _runner(repo, ex, btc, eth)
    await runner.control.write_sentinel()
    await runner.control.set_reconcile_hold(True)
    await runner._check_control_state()
    assert not await runner.portfolio.check_risk_limits(is_open=True), "held"
    await _hold(repo, eth)  # liquidated: the exchange is flat on ETH
    await _hold(repo, btc, size=0.5)

    for _ in range(2):
        for strat in (btc, eth):
            await runner._run_strategy(strat)

    assert await repo.get_open_positions() == []
    reasons = sorted(t.reason for t in await _trades(repo))
    assert reasons == ["reconcile: external close (fill)", "stop hit"]
    assert [o["symbol"] for o in ex.orders] == ["BTC", "ETH", "BTC"]
    assert ex.book == {}
    assert not await runner.portfolio.check_risk_limits(is_open=True), "still held"
