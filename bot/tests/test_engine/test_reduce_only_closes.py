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
b. anything else, an unreadable exchange included → the close is the
   engine's from then on, sent every tick whatever the strategy's rule says,
   until it goes through or the row is gone (it survives a restart); the
   strategy is re-armed with its state from before the exit; one alert per
   (strategy, coin), a wider IOC band from K misses on (rejected, zero or
   short fills), and a critical alert when that fails too.

Nothing that guards opens blocks an exit: a flip whose open half a gate
refuses still closes, and an unreadable control flag holds opens, never
exits. Flat-all holds opens until it completes and books what the rows
held beyond the exchange.

Real `Repository` on SQLite, real `BotControl` on a dict-backed Redis, the
runner's own paths, and the paper exchange — HyperLiquid's reduce-only rule
as paper mode runs it — with injected faults.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
from dataclasses import replace
from unittest.mock import AsyncMock

import pandas as pd
import pytest
from sqlalchemy import select

from hypertrade.config import settings
from hypertrade.db import models
from hypertrade.db.repo import PENDING_CLOSE_KEY, Repository
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
from hypertrade.strategies.daily_long_0830 import DailyLong0830Strategy

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
    `refuse_symbols` rejects every order on those coins, `cap` fills at
    most that much of any order (a thin book);
    `read_errors` makes the next N `get_position` reads raise, `blind`
    makes them answer flat while the coin is held (a read that misses
    it); `stale` is what `get_positions` answers instead of the book;
    `fills` is the fill history, `fills_error` makes reading it fail (HL's
    wrapper answers `[]`, or raises when asked `strict`). `no_mid` makes
    the mid unreadable (HL's wrapper answers 0.0); `late = (source, oid)`
    reports the next fills as HL's wrapper reports a close that filled
    after its POST timed out (`Order.late`, and the oid it matched, if
    any); `lost` fills the next N orders and answers them REJECTED with
    the outcome unknown (a timeout whose fill the wrapper saw but could
    not price).
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
        self.refuse_symbols: set[str] = set()
        self.cap: float | None = None
        self.no_mid = False
        self.fills_error = False
        self.late: tuple[str, str | None] | None = None
        self.lost = 0

    async def get_current_price(self, symbol):
        return 0.0 if self.no_mid else await super().get_current_price(symbol)

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
        # Copies, as HL answers: a fill must not change what was read.
        return [replace(p) for p in await super().get_positions()]

    async def fetch_user_fills(self, address=None, since_ms=None, *, strict=False):
        if self.fills_error:  # HL's wrapper: [] on a failed read, unless strict
            if strict:
                raise ExchangeReadError("userFillsByTime: 429 Too Many Requests")
            return []
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
        if self.refuse or self.zero_fill or symbol in self.refuse_symbols:
            zero = not self.refuse and symbol not in self.refuse_symbols
            if symbol not in self.refuse_symbols:
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
        if self.cap is not None:
            size = min(size, self.cap)
        order = await super().place_order(
            symbol, side, size, order_type, price,
            reduce_only=reduce_only, slippage=slippage,
        )
        if self.lost and order.status == OrderStatus.FILLED:
            self.lost -= 1
            return Order(
                id=order.id, symbol=symbol, side=side, size=size,
                order_type=order_type, status=OrderStatus.REJECTED,
                error="outcome unknown: it filled after the timeout, and no "
                      "fill or mid prices it",
            )
        if self.late and order.status == OrderStatus.FILLED:
            order.late, oid = self.late
            order.id = oid or order.id
        return order


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

    def holds_position(self):
        return self.side is not None

    def on_filled(self, side, price):
        pass


class Flip(Exit):
    """Holds a side; `flip_to` makes it emit that side's OPEN, switching
    to it as it does — once (an edge-triggered reversal, the only exit
    hash_supertrend has), or on every tick while `sticky`."""

    def __init__(self, symbol="ETH"):
        super().__init__(symbol)
        self.flip_to: str | None = None
        self.sticky = False

    async def on_candle(self, candles):
        if self.flip_to is None or self.flip_to == self.side:
            return None
        self.side = self.flip_to
        if not self.sticky:
            self.flip_to = None
        action = SignalAction.OPEN_LONG if self.side == "long" else SignalAction.OPEN_SHORT
        return Signal(
            action=action, symbol=self.symbol, strategy_name=self.name,
            reason="flip",
        )


class Opener(Exit):
    """A standing entry (sma_rsi, penguin_volatility): flat, it asks to
    go long on every tick."""

    async def on_candle(self, candles):
        if self.side is not None:
            return None
        self.side = "long"
        return Signal(
            action=SignalAction.OPEN_LONG, symbol=self.symbol,
            strategy_name=self.name, reason="standing entry",
        )


class Redis(FakeRedis):
    """FakeRedis with sets, and set or hash reads that can fail."""

    def __init__(self):
        super().__init__()
        self.sets: dict[str, set] = {}
        self.fail_smembers: set[str] = set()
        self.fail_hgetall: set[str] = set()

    async def smembers(self, key):
        if key in self.fail_smembers:
            raise ConnectionError(f"SMEMBERS {key} failed")
        return set(self.sets.get(key, ()))

    async def sadd(self, key, *values):
        self.sets.setdefault(key, set()).update(values)

    async def hgetall(self, key):
        if key in self.fail_hgetall:
            raise ConnectionError(f"HGETALL {key} failed")
        return {}


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
    control._redis = Redis()
    bus = Bus()
    runner = EngineRunner(
        exchange=exchange, strategies=list(strategies), repo=repo,
        event_bus=bus, control=control,
    )
    runner._awaiting = None  # running a while: opens are not waiting
    runner.CLOSE_CONFIRM_DELAY_SECONDS = 0
    return runner, bus


async def _ready_for_ticks(runner, monkeypatch):
    """`tick()` with only flat-all and the strategy loop to do: the
    sentinel is there, reconcile and the funding poll are not due, and
    neither the side services nor the rate alarm run."""
    await runner.control.write_sentinel()
    runner._last_reconcile = runner._last_funding_poll = time.time()
    monkeypatch.setattr(settings, "services_owner", False)
    monkeypatch.setattr(settings, "trade_rate_alarm_enabled", False)


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
    assert runner._pending_closes == {}


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
    assert runner._pending_closes == {}


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


# ----------------------------------------------------------------------
# A close that did not go through is the engine's until it does
# ----------------------------------------------------------------------


def _bars_to(hhmm: str, close=MID):
    """15m bars whose last CLOSED one opens at `hhmm` UTC; the runner
    drops the forming bar after it."""
    end = pd.Timestamp(f"2026-09-25 {hhmm}", tz="UTC") + pd.Timedelta(minutes=15)
    ts = pd.date_range(end=end, periods=4, freq="15min")
    return pd.DataFrame({
        "timestamp": ts, "open": close, "high": close, "low": close,
        "close": close, "volume": 1.0,
    })


async def _daily_long(repo, monkeypatch, refuse):
    """daily_long_0830 holding BTC 0.01 on the 08:00 bar, its only exit."""
    clock = {"bar": "08:00"}

    async def _fetch(symbol, timeframe, *a, **kw):
        return _bars_to(clock["bar"])

    monkeypatch.setattr(runner_mod, "fetch_candles", _fetch)
    strat = DailyLong0830Strategy()
    strat.restore_state("long", ENTRY)
    ex = HLLike(Position(symbol="BTC", side="long", size=0.01, entry_price=ENTRY))
    ex.refuse = refuse
    runner, bus = _runner(repo, ex, strat)
    await repo.open_position(
        strategy_name=strat.name, symbol="BTC", side="long", size=0.01,
        entry_price=ENTRY, state_json='{"in_position": true}',
    )
    return strat, ex, runner, bus, clock


async def test_an_edge_triggered_exit_is_sent_until_it_fills(repo, monkeypatch):
    """exits-never-blocked-1. daily_long_0830 exits on the 08:00 bar only.
    HL refuses that close, and on the 08:15 bar the rule no longer fires:
    the position used to ride until 08:00 the next day. The close is the
    engine's now, sent every tick whatever the rule says, until it fills
    — as Pine's `strategy.close` fills on the next bar."""
    strat, ex, runner, bus, clock = await _daily_long(repo, monkeypatch, refuse=2)

    await runner._run_strategy(strat)  # 08:00: the exit, refused
    assert strat.holds_position(), "re-armed on its row"
    clock["bar"] = "08:15"  # the rule is quiet from here on
    await runner._run_strategy(strat)  # sent by the engine: refused again
    await runner._run_strategy(strat)  # and again: it fills

    assert [(o["side"], o["size"], o["reduce_only"]) for o in ex.orders] == [
        ("sell", 0.01, True),
    ] * 3
    assert ex.book == {} and await repo.get_open_positions() == []
    [trade] = await _trades(repo)
    assert trade.reason.startswith("Exit at 08:00 UTC")
    assert trade.reason.endswith("(pending close, attempt 3)")
    assert not strat.holds_position()
    assert len(bus.errors(strat.name)) == 1, "one alert for the episode"
    assert runner._pending_closes == {}


async def test_a_pending_close_survives_a_restart(repo, monkeypatch):
    """The close the engine owes is noted on the row: a restart restores
    the strategy from its own state beside the note, and sends the close
    on its first tick although the rule will not fire for a day."""
    strat, ex, runner, bus, clock = await _daily_long(repo, monkeypatch, refuse=1)
    await runner._run_strategy(strat)  # 08:00: refused
    [row] = await repo.get_open_positions()
    state = json.loads(row.state_json)
    assert state["in_position"] is True and PENDING_CLOSE_KEY in state

    fresh = DailyLong0830Strategy()  # the bot restarts
    runner2, _ = _runner(repo, ex, fresh)
    assert await runner2._restore_from_db(())
    assert fresh.holds_position(), "the marker is not the strategy's state"
    clock["bar"] = "08:15"
    await runner2._run_strategy(fresh)

    assert ex.book == {} and await repo.get_open_positions() == []
    assert len(ex.orders) == 2 and not fresh.holds_position()


async def test_the_marker_is_not_the_strategys_state(repo, caplog):
    """A row opened without state of its own gets the marker alone; on
    restore the strategy is recomputed from the row, as before the note,
    not handed the engine's marker as its state — also after the marker
    was updated with a later attempt's count."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    ex.refuse = 2
    runner, _ = _runner(repo, ex, strat)
    await _hold(repo, strat)  # no state_json
    for _ in range(2):
        await runner._run_strategy(strat)
    [row] = await repo.get_open_positions()
    marker = json.loads(row.state_json)[PENDING_CLOSE_KEY]
    assert marker["only"] is True and marker["misses"] == 2

    fresh = Exit()
    runner2, _ = _runner(repo, ex, fresh)
    with caplog.at_level(logging.INFO, logger="hypertrade.engine.runner"):
        assert await runner2._restore_from_db(())

    assert "Restored s_ETH state (recomputed)" in caplog.text
    assert fresh.side == "long" and ("s_ETH", "ETH") in runner2._pending_closes


@pytest.mark.parametrize("trouble", [
    "on_candle raises", "no candles", "fetch raises", "malformed candles",
])
async def test_the_close_the_engine_owes_needs_no_signal(repo, monkeypatch, trouble):
    """Neither a strategy that raises nor a candle fetch that comes back
    empty, raises (aiohttp's timeout is a plain TimeoutError, re-raised
    after the feed's retries) or brings a frame with no close column holds
    back the close the engine owes (r1-exits-never-blocked-2); the error
    still reaches the tick's handler."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    ex.refuse = 1
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)
    await runner._run_strategy(strat)  # refused: the close is the engine's

    if trouble == "no candles":
        async def _empty(*a, **kw):
            return pd.DataFrame()

        monkeypatch.setattr(runner_mod, "fetch_candles", _empty)
        await runner._run_strategy(strat)
    elif trouble == "fetch raises":
        monkeypatch.setattr(runner_mod, "fetch_candles", AsyncMock(
            side_effect=asyncio.TimeoutError(),
        ))
        with pytest.raises(asyncio.TimeoutError):
            await runner._run_strategy(strat)
    elif trouble == "malformed candles":
        monkeypatch.setattr(runner_mod, "fetch_candles", AsyncMock(
            return_value=pd.DataFrame({"open": [1.0]}),
        ))
        with pytest.raises(KeyError):
            await runner._run_strategy(strat)
    else:
        strat.on_candle = AsyncMock(side_effect=ValueError("indicator bug"))
        with pytest.raises(ValueError):
            await runner._run_strategy(strat)

    assert ex.book == {} and await repo.get_open_positions() == []


async def test_a_db_error_while_closing_leaves_the_close_to_the_engine(repo):
    """exits-never-blocked-2. The stop fires and the first read of its row
    raises (a DB blip). The strategy had dropped the position, so the exit
    was gone until a restart. Now it is re-armed on its row, and the close
    goes next tick even though the stop is no longer hit."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)
    real, calls = repo.get_open_position, []

    async def _get(*a, **kw):
        calls.append(a)
        if len(calls) == 1:
            raise ConnectionError("db blip")
        return await real(*a, **kw)

    repo.get_open_position = _get

    with pytest.raises(ConnectionError):
        await runner._run_strategy(strat)
    assert ex.orders == [] and strat.side == "long", "re-armed on its row"
    strat.quiet = True
    await runner._run_strategy(strat)

    assert [o["reduce_only"] for o in ex.orders] == [True]
    assert ex.book == {} and await repo.get_open_positions() == []


async def test_an_order_that_raises_leaves_the_close_to_the_engine(repo):
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)
    real = ex.place_order

    async def _place(*a, **kw):
        if not ex.orders:
            ex.orders.append({})
            raise RuntimeError("SDK blew up")
        return await real(*a, **kw)

    ex.place_order = _place

    with pytest.raises(RuntimeError):
        await runner._run_strategy(strat)
    strat.quiet = True
    await runner._run_strategy(strat)

    assert ex.book == {} and await repo.get_open_positions() == []
    assert strat.side is None


async def test_a_flip_whose_position_read_raises_still_closes_the_old_side(repo):
    """The flip's own-position read raises: nothing is sent, the strategy
    — which switched to short when it emitted the flip — is reset to
    flat, and the long it held is closed by the engine next tick."""
    strat = Flip()
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)
    strat.flip_to = "short"
    real, calls = repo.get_open_position, []

    async def _get(*a, **kw):
        calls.append(a)
        if len(calls) == 1:
            raise ConnectionError("db blip")
        return await real(*a, **kw)

    repo.get_open_position = _get

    with pytest.raises(ConnectionError):
        await runner._run_strategy(strat)
    assert ex.orders == [] and strat.side is None
    await runner._run_strategy(strat)  # the flip does not fire again

    assert [(o["side"], o["reduce_only"]) for o in ex.orders] == [("sell", True)]
    assert ex.book == {} and await repo.get_open_positions() == []


async def test_short_fills_widen_the_band_and_alert_critical(repo):
    """exits-never-blocked-5. A thin book fills 0.1 of each close. Each
    fill used to end the streak, so the band never widened and nothing
    alerted. Short fills count as misses now; the trade rows of each go to
    Telegram as trades, so only the critical one alerts."""
    k = CLOSE_REJECTS_BEFORE_WIDE_BAND
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    ex.cap = 0.1
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)

    for _ in range(k + 2):
        await runner._run_strategy(strat)

    assert [o["slippage"] for o in ex.orders] == [None] * k + [WIDE_CLOSE_BAND] * 2
    [alert] = bus.errors(strat.name)
    assert alert.startswith("CRITICAL") and "filled short" in alert
    [row] = await repo.get_open_positions()
    assert row.size == pytest.approx(1.0 - 0.1 * (k + 2))
    assert len(await _trades(repo)) == k + 2


async def test_a_disabled_strategy_still_gets_the_close_the_engine_owes(
    repo, monkeypatch,
):
    """The rate alarm (or the operator) disables a strategy mid-exit. It
    no longer runs, but the close the engine owes its row still goes."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    ex.refuse = 1
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)
    await _ready_for_ticks(runner, monkeypatch)

    await runner.tick()  # its stop: refused
    await runner.control.disable_strategy(strat.name)
    strat.on_candle = AsyncMock(return_value=None)
    await runner.tick()

    strat.on_candle.assert_not_awaited()
    assert [o["reduce_only"] for o in ex.orders] == [True, True]
    assert ex.book == {} and await repo.get_open_positions() == []


async def test_a_readable_pause_freezes_the_close_the_engine_owes(repo, monkeypatch):
    """Decision 5.4: pause freezes everything, exits included."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    ex.refuse = 1
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)
    await _ready_for_ticks(runner, monkeypatch)
    await runner.tick()  # refused
    strat.quiet = True

    await runner.control.set_paused(True)
    await runner.tick()
    assert len(ex.orders) == 1, "frozen"
    await runner.control.set_paused(False)
    await runner.tick()

    assert len(ex.orders) == 2 and ex.book == {}


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
    assert runner._pending_closes == {}


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
    """The read says 1.0 but the exchange holds 0.4: reduce-only fills
    0.4, and 0.4 is what is booked. A confirmed read finds nothing left,
    so the 0.6 the row still held is not left behind an acknowledged
    flat-all (flip-and-flat-1). No fill closed it, so the exchange never
    held it: size drift, booked at the flat-all's fill with pnl 0, logged
    and not published — not at the mid with a PnL (double-close-2)."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=0.4, entry_price=ENTRY))
    ex.stale = [Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY)]
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)

    status = await runner._flat_all_positions()

    assert ex.book == {} and await repo.get_open_positions() == []
    assert sorted(
        (t.size, t.reason, t.price, t.pnl, t.fee) for t in await _trades(repo)
    ) == [
        (pytest.approx(0.4), "Flat-all from dashboard", MID,
         pytest.approx((MID - ENTRY) * 0.4 - MID * 0.4 * settings.taker_fee_rate),
         pytest.approx(MID * 0.4 * settings.taker_fee_rate)),
        (pytest.approx(0.6), "size drift: the row held more than the exchange; pnl 0",
         MID, 0.0, 0.0),
    ]
    assert status.ok and strat.side is None
    assert bus.errors(strat.name) == []


async def test_flat_all_books_a_row_larger_than_the_exchange(repo, monkeypatch):
    """flip-and-flat-1, through the tick: the row is long 1.0, a fresh
    read shows 0.6 (0.4 liquidated). The 0.6 is closed and booked, the
    0.4 from its liquidation fill, the strategy reset; only then is the
    request acknowledged and the bot paused. It used to acknowledge with
    0.4 left open on the row for the whole pause, reconcile in dry run."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=0.6, entry_price=ENTRY))
    ex.fills = [_liquidation(size="0.4")]
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)
    await _ready_for_ticks(runner, monkeypatch)
    await runner.control.request_flat_all("tok-1")

    await runner.tick()

    assert ex.book == {} and await repo.get_open_positions() == []
    assert sorted((t.size, t.reason) for t in await _trades(repo)) == [
        (pytest.approx(0.4), "reconcile: external close (fill)"),
        (pytest.approx(0.6), "Flat-all from dashboard"),
    ]
    assert strat.side is None
    assert await runner.control.is_paused()
    assert await runner.control.get_pending_flat_request() is None


async def test_a_flat_all_that_cannot_finish_holds_opens_and_says_so_once(
    repo, monkeypatch, caplog,
):
    """flip-and-flat-2. BTC's close keeps failing, so flat-all is not
    acknowledged and the bot not paused. ETH's standing entry used to
    re-open the coin flat-all had just closed, in the same tick, and the
    next tick closed it again — two fees a tick — while "did not complete"
    went to Telegram every tick. Opens are held while the request is
    pending; exits run; the notice goes once."""
    eth, btc = Opener("ETH"), Exit("BTC")
    btc.quiet = True
    ex = HLLike(
        Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY),
        Position(symbol="BTC", side="long", size=1.0, entry_price=ENTRY),
    )
    ex.refuse_symbols = {"BTC"}
    runner, bus = _runner(repo, ex, eth, btc)
    await _hold(repo, eth)
    await _hold(repo, btc)
    await _ready_for_ticks(runner, monkeypatch)
    await runner.control.request_flat_all("tok-1")

    with caplog.at_level(logging.INFO, logger="hypertrade.engine.runner"):
        for _ in range(3):
            await runner.tick()

    assert [(o["symbol"], o["side"]) for o in ex.orders if o["symbol"] == "ETH"] == [
        ("ETH", "sell"),
    ], "closed once, never re-opened"
    assert "ETH" not in ex.book and eth.side is None
    [notice] = bus.errors("flat-all")
    assert notice.startswith(
        "Flat-all did not complete: BTC: rejected (Order could not "
        "immediately match), still open."
    )
    assert "Flat-all: long 1.0 BTC rejected (Order could not immediately match)" in caplog.text
    assert not await runner.control.is_paused()

    ex.refuse_symbols.clear()
    await runner.tick()
    assert ex.book == {} and await repo.get_open_positions() == []
    assert await runner.control.is_paused()


async def test_flat_all_books_a_wrong_side_row_from_fills(repo):
    """The long row was closed by hand and a short opened that no row
    owns. Flat-all buys the short back as a row-less close; the long row
    is booked on its own, not with that buy's price as if it had closed
    the long."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="short", size=1.0, entry_price=MID))
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)

    status = await runner._flat_all_positions()

    assert status.ok and ex.book == {}
    assert await repo.get_open_positions() == []
    assert sorted((t.strategy_name, t.reason) for t in await _trades(repo)) == [
        ("manual_flat", "Flat-all from dashboard (no open DB rec)"),
        (strat.name, "ESTIMATED reconcile: external close @ mid"),
    ]


async def test_flat_all_without_a_row_says_what_is_left(repo, caplog):
    """noise-and-logs-4: a partial fill on a coin with no row said
    "Closed long ETH" with no size, and nothing said what was left. The
    coin's line says it on every attempt (WARNING); the tick logs the
    reason at ERROR once per request and reason."""
    ex = HLLike(Position(symbol="ETH", side="long", size=0.5, entry_price=ENTRY))
    ex.cap = 0.25
    runner, _ = _runner(repo, ex)

    with caplog.at_level(logging.WARNING, logger="hypertrade.engine.runner"):
        status = await runner._flat_all_positions()

    assert not status.ok and status.reason == "ETH: filled short, still open"
    assert any(
        r.levelno == logging.WARNING
        and "long 0.5 ETH filled short — filled 0.25 of 0.5, 0.25 still open"
        in r.getMessage()
        for r in caplog.records
    )
    [trade] = await _trades(repo)
    assert trade.size == pytest.approx(0.25)


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
    # Short of what was read: the pass cannot vouch for the coin (booking-3).
    assert result.unresolved_orphans == ["ETH"]


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


# ----------------------------------------------------------------------
# Nothing that guards opens blocks an exit
# ----------------------------------------------------------------------


@pytest.mark.parametrize("gate", ["exposure cap", "leverage push", "gate read"])
async def test_a_flip_refused_by_any_gate_still_closes(repo, monkeypatch, gate):
    """exits-never-blocked-3. The flip is the only exit hash_supertrend
    and kalman_breakout have, and every gate but a risk limit used to
    refuse it whole: the long stayed, and after the bar the exit was gone.
    The gate refuses the open half only; the close half goes out and the
    strategy ends flat."""
    strat = Flip()
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)
    if gate == "exposure cap":
        monkeypatch.setattr(settings, "max_total_exposure_usd", 1)
    elif gate == "leverage push":
        ex.update_leverage = AsyncMock(return_value=False)
    else:
        runner.control.get_allow_multi_coin = AsyncMock(
            side_effect=ConnectionError("redis blip"),
        )
    strat.flip_to = "short"

    await runner._run_strategy(strat)

    assert [(o["side"], o["reduce_only"]) for o in ex.orders] == [("sell", True)]
    assert ex.book == {} and await repo.get_open_positions() == []
    assert strat.side is None
    [trade] = await _trades(repo)
    assert trade.reason.startswith("Auto-close before flip")


@pytest.mark.parametrize("flag", [
    "flat-all request", "paused flag", "disabled set", "leverage overrides",
    "disabled set and rows",
])
async def test_an_unreadable_control_flag_holds_opens_never_exits(
    repo, monkeypatch, flag,
):
    """exits-never-blocked-4. A failed Redis read of a control flag raised
    out of `tick()` before any strategy ran, exits included, every tick
    Redis was down. It holds opens for the tick instead (a flat-all is
    skipped), and the stop still closes."""
    held, opener = Exit("ETH"), Opener("BTC")
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    runner, bus = _runner(repo, ex, held, opener)
    await _hold(repo, held)
    await _ready_for_ticks(runner, monkeypatch)
    ctl, redis = runner.control, runner.control._redis
    if flag == "flat-all request":
        redis.fail_get.add(ctl._key_flat_req)
    elif flag == "paused flag":
        redis.fail_get.add(ctl._key_paused)
    elif flag.startswith("disabled set"):
        redis.fail_smembers.add(ctl._key_disabled)
        if flag.endswith("rows"):  # who holds a position: from memory
            repo.get_open_positions = AsyncMock(side_effect=ConnectionError("db"))
    else:
        redis.fail_hgetall.add(ctl._key_leverage)

    await runner.tick()

    assert [(o["symbol"], o["side"], o["reduce_only"]) for o in ex.orders] == [
        ("ETH", "sell", True),
    ], "the exit, and no open"
    assert ex.book == {} and not held.holds_position()
    assert opener.side is None


async def test_an_unreadable_mainnet_allowlist_still_runs_exits(repo, monkeypatch):
    """It failed closed to an empty set: no strategy ran, exits included.
    Strategies holding a position still run, for their exits; nothing
    opens."""
    monkeypatch.setattr(settings, "exchange_mode", "mainnet")
    held, opener = Exit("ETH"), Opener("BTC")
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    runner, bus = _runner(repo, ex, held, opener)
    await _hold(repo, held)
    await _ready_for_ticks(runner, monkeypatch)
    runner.control._redis.fail_smembers.add(
        runner.control._mainnet_enabled_strategies_key(TENANT),
    )
    opener.on_candle = AsyncMock(return_value=None)

    await runner.tick()

    assert [(o["symbol"], o["side"]) for o in ex.orders] == [("ETH", "sell")]
    assert ex.book == {}
    opener.on_candle.assert_not_awaited()


# ----------------------------------------------------------------------
# A stuck close is one alert per episode and one log line per attempt
# ----------------------------------------------------------------------


async def test_a_flip_close_refused_across_bars_alerts_twice(repo):
    """noise-and-logs-1. A level-triggered flip whose close HL keeps
    refusing: 5 ticks a bar over 6 bars sent 7 Telegrams, one more "flip
    close failed" per bar, ~96 a day. Now the episode's alert and its
    critical one, and no open is ever sent."""
    strat = Flip()
    ex = HLLike(Position(symbol="ETH", side="short", size=1.0, entry_price=ENTRY))
    ex.refuse = 100
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat, side="short")
    start = pd.Timestamp("2026-09-25", tz="UTC")

    for bar in range(6):
        for _ in range(5):
            strat.side = "long"  # it switches as it emits the flip
            await runner._execute_signal(Signal(
                action=SignalAction.OPEN_LONG, symbol="ETH",
                strategy_name=strat.name, reason="flip",
            ), MID, bar_time=start + pd.Timedelta(minutes=15 * bar))

    alerts = bus.errors(strat.name)
    assert len(alerts) == 2 and alerts[1].startswith("CRITICAL")
    assert all(o["side"] == "buy" and o["reduce_only"] for o in ex.orders)


@pytest.mark.parametrize("case", ["delivered", "undelivered", "unreadable", "clamped"])
async def test_a_stuck_close_logs_one_line_per_attempt(repo, caplog, case):
    """noise-and-logs-2. Each refused attempt logged 3-4 WARNING/ERROR
    lines, every tick (the ERROR alert text every tick while undelivered).
    Now one line per attempt carries count, band, HL's reason and the
    exchange; only the alert and the critical one log above INFO. An
    exchange that cannot be read, or holds less than the row, says so at
    WARNING on the first attempt and at INFO after: the close goes out
    every tick (r1-tests-and-noise-5). Every `hypertrade` logger counts."""
    strat = Exit()
    held = 0.5 if case == "clamped" else 1.0
    ex = HLLike(Position(symbol="ETH", side="long", size=held, entry_price=ENTRY))
    ex.refuse = 100
    ex.read_errors = 1000 if case == "unreadable" else 0
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)
    bus.deliver = case != "undelivered"
    loud, per_attempt = [], []

    for _ in range(7):
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="hypertrade"):
            await runner._run_strategy(strat)
        records = [r for r in caplog.records if r.name.startswith("hypertrade")]
        loud.append([r.levelno for r in records if r.levelno >= logging.WARNING])
        per_attempt.append(sum(
            "Order could not immediately match" in r.getMessage() for r in records
        ))

    first = [logging.ERROR]
    if case in ("unreadable", "clamped"):
        first = [logging.WARNING, logging.ERROR]
    assert loud == [first, [], [], [logging.ERROR], [], [], []]
    assert per_attempt == [1] * 7


async def test_a_refused_orphan_close_says_why(repo):
    """noise-and-logs-3. The orphan went between the reads and the
    reduce-only close; the notice said "position still open"."""
    ex = HLLike()
    ex.stale = [Position(symbol="ETH", side="long", size=0.5, entry_price=ENTRY)]

    result = await repo.reconcile_positions(
        ex, confirm_delay_seconds=0, hold_orphans=None, traded_symbols={"ETH"},
    )

    [failure] = result.failures
    assert "Reduce only order would increase position" in failure
    assert "still open" not in failure


async def test_a_failed_flip_open_says_why(repo):
    strat = Flip()
    ex = HLLike(Position(symbol="ETH", side="short", size=1.0, entry_price=ENTRY))
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat, side="short")
    real = ex.place_order

    async def _place(symbol, side, size, *a, reduce_only=False, **kw):
        if not reduce_only:
            return Order(
                id="x", symbol=symbol, side=side, size=size,
                order_type=OrderType.MARKET, status=OrderStatus.REJECTED,
                error="Insufficient margin to place order.",
            )
        return await real(symbol, side, size, *a, reduce_only=True, **kw)

    ex.place_order = _place
    strat.flip_to = "long"

    await runner._run_strategy(strat)

    [message] = bus.errors(strat.name)
    assert "Insufficient margin to place order." in message
    assert strat.side is None and await repo.get_open_positions() == []


# ----------------------------------------------------------------------
# A flip never opens onto a position nobody owns
# ----------------------------------------------------------------------


async def test_a_flip_into_an_unowned_opposite_position_does_not_stack(repo):
    """flip-and-flat-4, r1-tests-and-noise-3. The long row is gone from the
    exchange, which is short 1.0 that no row owns. The reduce-only sell is
    refused and the long is booked; the flip's open used to go out too,
    stacking 0.476 onto the unowned short under a DB that saw only 0.476.
    It is not sent, and not on the next tick either, when a level-triggered
    flip (flat by then) fires again as a plain open: opens on the coin wait
    until the short is gone. Then they go."""
    strat = Flip()
    strat.sticky = True
    short = Position(symbol="ETH", side="short", size=1.0, entry_price=MID)
    ex = HLLike(short)
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)
    strat.flip_to = "short"

    for _ in range(3):
        await runner._run_strategy(strat)

    assert [(o["side"], o["reduce_only"]) for o in ex.orders] == [("sell", True)]
    assert ex.book == {"ETH": short} and short.size == 1.0
    assert await repo.get_open_positions() == []
    assert strat.side is None
    [notice] = bus.errors(strat.name)
    assert "no row owns" in notice and "open is not sent" in notice

    ex.book.clear()  # reconcile (or a human) closed the short
    await runner._run_strategy(strat)

    assert [(o["side"], o["reduce_only"]) for o in ex.orders][1:] == [("sell", False)]
    [row] = await repo.get_open_positions()
    assert row.side == "short" and ex.book["ETH"].size == pytest.approx(row.size)


# ----------------------------------------------------------------------
# Booking: what a close leaves, a late fill, one order on many rows
# ----------------------------------------------------------------------


async def test_the_rest_of_a_clamped_close_no_fill_covers_is_size_drift(
    repo, caplog,
):
    """double-close-2. The row is long 1.0, the exchange 0.8, and no fill
    ever closed the 0.2: bookkeeping drift. The close is clamped to 0.8
    and fills. The 0.2 used to be booked as an external close at the mid —
    a PnL on size the exchange never held, into the daily-loss counter,
    and a notice. It is size drift: booked at the close's fill, pnl 0, no
    fee, logged at WARNING, not published."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=0.8, entry_price=ENTRY))
    ex.set_price("ETH", 1900.0)  # fill and mid
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat, size=1.0)

    with caplog.at_level(logging.WARNING, logger="hypertrade.engine.runner"):
        await runner._execute_signal(_close(strat), MID)

    assert [(o["size"], o["reduce_only"]) for o in ex.orders] == [(0.8, True)]
    assert ex.book == {} and await repo.get_open_positions() == []
    fill, drift = sorted(await _trades(repo), key=lambda t: -t.size)
    assert (fill.size, fill.reason) == (pytest.approx(0.8), "stop hit")
    assert (drift.size, drift.price, drift.pnl, drift.fee) == (
        pytest.approx(0.2), 1900.0, 0.0, 0.0,
    )
    assert drift.reason.startswith("size drift")
    assert runner.portfolio._daily_pnl == pytest.approx(fill.pnl)
    assert bus.errors(strat.name) == []
    assert any(
        r.levelno == logging.WARNING and "Size drift" in r.getMessage()
        for r in caplog.records
    )
    assert strat.side is None and runner._pending_closes == {}


@pytest.mark.parametrize("late, oid, reason", [
    ("fill", "4242", "stop hit"),
    ("mid", None, "ESTIMATED stop hit (filled after a timeout; no fill of its "
                  "own found, @ mid)"),
], ids=["own fill", "mid"])
async def test_a_late_close_is_booked_as_what_priced_it(repo, late, oid, reason):
    """booking-5. A close that filled after its POST timed out and was
    priced at the mid was booked as an ordinary fill under the strategy's
    reason; /eval and the daily-loss counter could not tell. Priced from
    its own fill, it is booked under that fill's oid."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    ex.late = (late, oid)
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)

    await runner._execute_signal(_close(strat), MID)

    [trade] = await _trades(repo)
    assert trade.reason == reason
    if oid:
        assert trade.order_id == oid
    assert await repo.get_open_positions() == []


async def test_a_late_close_matched_to_a_booked_fill_is_no_divergence(repo):
    """booking-1, double-close-1. Two strategies hold ETH longs and exit on
    one tick. A's close fills and is booked; B's times out and the late-fill
    match hands it A's oid. The UNIQUE `order_id` used to fail B's insert:
    auto-pause, a false "DB-write divergence" alert, and every exit stopped
    until a human unpaused. It is booked ESTIMATED under a fresh id."""
    a, b = Exit(), Exit()
    b.name = "s_ETH_b"
    ex = HLLike(Position(symbol="ETH", side="long", size=2.0, entry_price=ENTRY))
    runner, bus = _runner(repo, ex, a, b)
    await _hold(repo, a)
    await _hold(repo, b)

    await runner._execute_signal(_close(a), MID)
    [a_trade] = await _trades(repo)
    ex.late = ("fill", a_trade.order_id)
    await runner._execute_signal(_close(b), MID)

    assert await repo.get_open_positions() == [] and ex.book == {}
    b_trade = next(t for t in await _trades(repo) if t.strategy_name == b.name)
    assert b_trade.order_id != a_trade.order_id
    assert b_trade.reason.startswith(
        "ESTIMATED stop hit (filled after a timeout; the fill matched was "
        f"order {a_trade.order_id}'s, already booked)"
    )
    assert not await runner.control.is_paused()
    assert bus.errors(b.name) == []


async def test_flat_all_books_every_row_on_one_coin(repo):
    """booking-2. One flat-all order closes two strategies' ETH longs and
    every row was booked under its oid: `trades.order_id` is UNIQUE, so the
    second row's write failed, flat-all reported failure on a flat book,
    and that row stayed open with its strategy still long."""
    a, b = Exit(), Exit()
    b.name = "s_ETH_b"
    ex = HLLike(Position(symbol="ETH", side="long", size=2.0, entry_price=ENTRY))
    runner, bus = _runner(repo, ex, a, b)
    await _hold(repo, a)
    await repo.open_position(
        strategy_name=b.name, symbol="ETH", side="long", size=1.0,
        entry_price=1900.0,
    )
    b.side = "long"

    status = await runner._flat_all_positions()

    assert status.ok and status.closed == 1 and status.failed == 0
    assert ex.book == {} and await repo.get_open_positions() == []
    trades = {t.strategy_name: t for t in await _trades(repo)}
    oid = trades[a.name].order_id
    assert trades[b.name].order_id == f"{oid}-2"
    fee = MID * 1.0 * settings.taker_fee_rate
    assert trades[a.name].pnl == pytest.approx((MID - ENTRY) - fee)
    assert trades[b.name].pnl == pytest.approx((MID - 1900.0) - fee)
    assert runner.portfolio._daily_pnl == pytest.approx(
        trades[a.name].pnl + trades[b.name].pnl,
    )
    assert a.side is None and b.side is None


async def test_flat_all_marks_a_late_close_at_the_mid_estimated(repo):
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    ex.late = ("mid", None)
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)

    await runner._flat_all_positions()

    [trade] = await _trades(repo)
    assert trade.reason == (
        "ESTIMATED Flat-all from dashboard (filled after a timeout; no fill "
        "of its own found, @ mid)"
    )


async def test_reconcile_marks_a_late_orphan_close_at_the_mid_estimated(repo):
    ex = HLLike(Position(symbol="ETH", side="long", size=0.5, entry_price=ENTRY))
    ex.late = ("mid", None)

    await repo.reconcile_positions(
        ex, confirm_delay_seconds=0, hold_orphans=None, traded_symbols={"ETH"},
    )

    [trade] = await _trades(repo)
    assert trade.reason == (
        "ESTIMATED reconcile: exchange-orphan close (filled after a timeout; "
        "no fill of its own found, @ mid)"
    )


async def test_an_orphan_close_that_filled_short_does_not_count(repo, monkeypatch):
    """booking-3. Pass 2's reduce-only close of a lone orphan long of 2.0
    fills 0.5 (a thin book). The pass used to count anyway: opens were
    released with 1.5 untracked on the coin for the next OPEN to net into.
    It does not count until a pass finds the coin clean."""
    ex = HLLike(Position(symbol="ETH", side="long", size=2.0, entry_price=ENTRY))
    ex.cap = 0.5
    runner, bus = _runner(repo, ex, Exit())
    monkeypatch.setattr(repo, "reconcile_positions", functools.partial(
        repo.reconcile_positions, confirm_delay_seconds=0,
    ))
    await runner.control.write_sentinel()
    runner._awaiting = runner_mod._AWAIT_PASS_AFTER_CLEAR

    result = await runner._run_reconcile("Periodic")

    assert ex.book["ETH"].size == pytest.approx(1.5)
    assert result.unresolved_orphans == ["ETH"]
    assert "closed 0.5 of 2.0; the rest may still be open" in result.summary()
    assert runner._awaiting == runner_mod._AWAIT_PASS_AFTER_CLEAR
    assert not await runner.portfolio.check_risk_limits(is_open=True)

    ex.cap = None
    result = await runner._run_reconcile("Periodic")

    assert ex.book == {} and result.unresolved_orphans == []
    assert runner._awaiting is None
    assert await runner.portfolio.check_risk_limits(is_open=True)


# ----------------------------------------------------------------------
# A row nothing can price stays open for reconcile
# ----------------------------------------------------------------------


async def test_an_unpriceable_external_close_stays_open_for_reconcile(repo, caplog):
    """tests-real-1 (a). The long was liquidated; HL is degraded, so no fill
    is readable and no mid. The row is left open — never closed at a
    fabricated price — and said so once; the strategy stays flat (the
    exchange holds nothing for it to manage: re-armed, it would send a
    refused order every tick), no pending close, no notice. Reconcile
    prices the row once it can."""
    strat = Exit()
    ex = HLLike()
    ex.no_mid = True
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)

    with caplog.at_level(logging.WARNING, logger="hypertrade.engine.runner"):
        await runner._run_strategy(strat)
        await runner._run_strategy(strat)

    assert [o["reduce_only"] for o in ex.orders] == [True]
    [row] = await repo.get_open_positions()
    assert (row.side, row.size) == ("long", pytest.approx(1.0))
    assert await _trades(repo) == []
    assert strat.side is None and runner._pending_closes == {}
    assert bus.errors(strat.name) == []
    [line] = [r for r in caplog.records if r.name == "hypertrade.engine.runner"]
    assert line.levelno == logging.WARNING
    assert "could not be priced" in line.getMessage()


def _hourly(clock: dict):
    """A candle feed whose last closed bar is `clock["bar"]` (an int)."""
    async def _fetch(symbol, timeframe, *a, **kw):
        end = pd.Timestamp("2026-09-25", tz="UTC") + pd.Timedelta(hours=clock["bar"] + 1)
        return _candles().assign(
            timestamp=pd.date_range(end=end, periods=3, freq="1h"),
        )

    return _fetch


async def test_a_flip_whose_old_side_cannot_be_priced_does_not_open(
    repo, monkeypatch, caplog,
):
    """tests-real-1 (b), r1-tests-and-noise-4. The flip's long was
    liquidated and nothing prices it (no fill readable, no mid). The long
    row stays open, so the short must not be opened under it (audit H1).
    The strategy used to be re-armed on that row: a level-triggered flip
    then sent a refused reduce-only sell every tick, with a "Flip-close
    failed" Telegram every bar. The exchange holds nothing for it to
    manage, so it is flat, and the flip is not tried on that row again for
    a reconcile interval: one refused sell, one line a bar, no Telegram."""
    clock = {"bar": 0}
    monkeypatch.setattr(runner_mod, "fetch_candles", _hourly(clock))
    strat = Flip()
    strat.sticky = True
    ex = HLLike()
    ex.no_mid = True
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)
    strat.flip_to = "short"

    with caplog.at_level(logging.WARNING, logger="hypertrade.engine.runner"):
        for bar in range(3):
            clock["bar"] = bar
            for _ in range(3):
                await runner._run_strategy(strat)

    assert [(o["side"], o["reduce_only"]) for o in ex.orders] == [("sell", True)]
    assert ex.book == {}
    [row] = await repo.get_open_positions()
    assert row.side == "long"
    assert await _trades(repo) == []
    assert strat.side is None and runner._pending_closes == {}
    assert bus.errors(strat.name) == []
    assert sum("open not sent" in r.getMessage() for r in caplog.records) == 3

    runner.UNBOOKED_FLIP_RETRY_SECONDS = 0  # a reconcile interval later
    await runner._run_strategy(strat)
    assert len(ex.orders) == 2, "tried again, once"


# ----------------------------------------------------------------------
# The defensive lines: a pending close ends with its row, a closed row is
# not booked twice
# ----------------------------------------------------------------------


async def test_an_episode_after_a_reconcile_close_alerts_again(repo):
    """tests-real-3. A rejected close alerts once per episode. Reconcile
    then closes the row (liquidated) and resets the strategy, which must
    end the episode too: a stale one swallowed the next position's first
    exit, or counted its rejection on the old episode with no alert."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    ex.refuse = 1
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)
    await runner._run_strategy(strat)
    assert len(bus.errors(strat.name)) == 1

    ex.book.clear()
    ex.fills = [_liquidation()]
    await repo.reconcile_positions(
        ex, confirm_delay_seconds=0, on_strategy_close=runner._on_reconcile_close,
    )
    assert await repo.get_open_positions() == [] and strat.side is None

    ex.book["ETH"] = Position(symbol="ETH", side="long", size=1.0, entry_price=MID)
    await _hold(repo, strat)
    ex.refuse = 1
    await runner._run_strategy(strat)

    alerts = bus.errors(strat.name)
    assert len(alerts) == 2 and "still open" in alerts[1]
    assert [o["reduce_only"] for o in ex.orders] == [True, True]


async def test_a_flip_that_books_the_old_side_ends_its_pending_close(repo):
    """tests-real-3. The long's close was refused, so the engine owes it.
    The long is then liquidated and the strategy flips: the close half is
    booked from the fill and the short opens. The close owed to the long
    must end there; left behind, it swallowed the strategy's next signal —
    here its flip back, leaving it long in memory over a short row."""
    strat = Flip()
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    ex.refuse = 1
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)
    await runner._execute_signal(_close(strat), MID)
    assert (strat.name, "ETH") in runner._pending_closes

    ex.book.clear()
    ex.fills = [_liquidation()]
    strat.flip_to = "short"
    await runner._run_strategy(strat)
    [row] = await repo.get_open_positions()
    assert row.side == "short" and runner._pending_closes == {}

    strat.flip_to = "long"
    await runner._run_strategy(strat)

    [row] = await repo.get_open_positions()
    assert row.side == "long" and ex.book["ETH"].side == "long"


async def test_an_external_close_of_a_row_closed_meanwhile_writes_nothing(repo):
    """tests-real-3. The row was read open, then the signal path closed it.
    Booking it as closed outside the bot must not write a second trade row
    or a second PnL."""
    ex = HLLike()
    ex.fills = [_liquidation()]
    stale = await repo.open_position(
        strategy_name="s_ETH", symbol="ETH", side="long", size=1.0,
        entry_price=ENTRY,
    )
    await repo.record_trade_and_close_position(
        order_id="o1", strategy_name="s_ETH", symbol="ETH", trade_side="sell",
        size=1.0, price=MID, pnl=100.0, reason="stop hit",
    )

    assert await repo.close_row_externally(ex, stale) is None

    [trade] = await _trades(repo)
    assert trade.order_id == "o1"
    async with repo._session_factory() as session:
        row = await session.get(models.PositionRecord, stale.id)
    assert row.pnl == pytest.approx(100.0) and row.exit_price == MID


# ----------------------------------------------------------------------
# A real strategy's live state survives a flip-close miss and a short fill
# ----------------------------------------------------------------------


async def _ratcheted_ath(repo, monkeypatch, **faults):
    """ath_breakout long 0.01 BTC, its peak ratcheted to 200 (trail stop
    130) while its row's open-time state says 101 (stop 65.65); bars at
    125, so its trail stop is hit."""
    async def _fetch(symbol, timeframe, *a, **kw):
        return _candles(close=125.0)

    monkeypatch.setattr(runner_mod, "fetch_candles", _fetch)
    strat = AthBreakoutStrategy(lookback=1)
    strat.restore_from_json(
        "long", 100.0, {"entry_price": 100.0, "peak_since_entry": 200.0},
    )
    ex = HLLike(Position(symbol="BTC", side="long", size=0.01, entry_price=100.0))
    for name, value in faults.items():
        setattr(ex, name, value)
    runner, bus = _runner(repo, ex, strat)
    await repo.open_position(
        strategy_name=strat.name, symbol="BTC", side="long", size=0.01,
        entry_price=100.0,
        state_json='{"entry_price": 100.0, "peak_since_entry": 101.0}',
    )
    return strat, ex, runner


async def test_a_refused_flip_close_re_arms_a_real_strategy_live(repo, monkeypatch):
    """tests-real-4. The fakes the flip tests use export no state, so the
    pre-signal re-arm was never exercised for a flip. ath_breakout flips
    (its OPEN_SHORT stands in for a reversal), dropping its long as it
    does; HL refuses the close half. Re-armed from its row, the peak fell
    back to 101 and the trail stop to 65.65; re-armed with its live state,
    the ratchet holds, and its own trail stop closes it next tick."""
    strat, ex, runner = await _ratcheted_ath(repo, monkeypatch, refuse=1)
    real = strat.on_candle

    async def _flips(candles):
        strat.reset_position()
        return Signal(
            action=SignalAction.OPEN_SHORT, symbol="BTC",
            strategy_name=strat.name, reason="reversal",
        )

    strat.on_candle = _flips
    await runner._run_strategy(strat)

    assert [(o["side"], o["reduce_only"]) for o in ex.orders] == [("sell", True)]
    assert strat.holds_position() and strat._peak_since_entry == 200.0

    strat.on_candle = real
    await runner._run_strategy(strat)

    assert ex.book == {} and await repo.get_open_positions() == []
    [trade] = await _trades(repo)
    assert trade.reason.startswith("Trail stop hit")


async def test_a_short_fill_re_arms_a_real_strategy_live(repo, monkeypatch):
    """tests-real-4. The trail-stop exit fills 0.004 of 0.01 (a thin book);
    the rest stays on the row and the strategy is re-armed with its
    ratchet, so the same stop closes the rest next tick."""
    strat, ex, runner = await _ratcheted_ath(repo, monkeypatch, cap=0.004)

    await runner._run_strategy(strat)

    [row] = await repo.get_open_positions()
    assert row.size == pytest.approx(0.006)
    assert strat.holds_position() and strat._peak_since_entry == 200.0

    ex.cap = None
    await runner._run_strategy(strat)

    assert ex.book == {} and await repo.get_open_positions() == []
    assert [t.reason[:14] for t in await _trades(repo)] == ["Trail stop hit"] * 2


# ----------------------------------------------------------------------
# Review round 1 (r1-*): a close that is lost, late, or one of several
# ----------------------------------------------------------------------


def _own_fill(size, px=MID, oid=7777):
    """HL's fill of this bot's own ETH sell, stamped after it was sent."""
    return {
        "coin": "ETH", "side": "A", "sz": str(size), "px": str(px),
        "fee": "0.5", "time": int(time.time() * 1000) + 60_000, "oid": oid,
    }


async def test_a_lost_close_beside_a_sibling_row_is_not_sent_twice(repo):
    """r1-double-close-1. A and B each hold a 1.0 ETH long
    (allow_multi_coin), the exchange 2.0. A's reduce-only sell fills but
    comes back REJECTED with its outcome unknown (a timeout whose fill no
    price was found for). The read after it found B's 1.0 and took it for
    A's: A's close became the engine's, went again next tick sized from
    A's row, and closed B's position. What the other rows on the side hold
    counts against the read: A's row is booked from its own fill, and B's
    position and row are untouched."""
    a, b = Exit(), Exit()
    b.name = "s_ETH_b"
    ex = HLLike(Position(symbol="ETH", side="long", size=2.0, entry_price=ENTRY))
    ex.lost = 1
    ex.fills = [_own_fill(1.0, oid=5151)]
    runner, bus = _runner(repo, ex, a, b)
    await _hold(repo, a)
    await _hold(repo, b)

    await runner._run_strategy(a)
    await runner._run_strategy(a)  # the next tick: nothing is owed

    assert [(o["side"], o["size"]) for o in ex.orders] == [("sell", 1.0)]
    assert ex.book["ETH"].size == pytest.approx(1.0)
    [row] = await repo.get_open_positions()
    assert (row.strategy_name, row.size) == (b.name, pytest.approx(1.0))
    [trade] = await _trades(repo)
    assert (trade.strategy_name, trade.order_id) == (a.name, "5151")
    assert trade.reason == "reconcile: external close (fill)"
    assert a.side is None and b.side == "long"
    assert runner._pending_closes == {}


@pytest.mark.parametrize("path", ["close", "flat-all"])
async def test_the_rest_of_a_late_close_is_not_priced_from_its_own_fill(repo, path):
    """r1-double-close-2, r1-booking-2. The row is long 1.0, the exchange
    0.8 (0.2 of drift). The 0.8 close times out and fills, and no fill of
    its own is found yet: it is booked ESTIMATED @ mid under a fresh id,
    and its real fill (oid 7777) stays unbooked. By the time the 0.2 rest
    is priced, that fill is readable, and it priced the rest too — one 0.8
    fill booked as 1.0, a PnL on the 0.2 and an "External close" notice.
    Fills from the close's send on are its own: the rest is size drift,
    pnl 0, no notice."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=0.8, entry_price=ENTRY))
    ex.set_price("ETH", 1900.0)
    ex.late = ("mid", None)
    ex.fills = [_own_fill(0.8, px=1900.0)]
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat, size=1.0)

    if path == "close":
        await runner._execute_signal(_close(strat), MID)
    else:
        assert (await runner._flat_all_positions()).ok

    assert ex.book == {} and await repo.get_open_positions() == []
    fill, rest = sorted(await _trades(repo), key=lambda t: -t.size)
    assert fill.size == pytest.approx(0.8) and fill.reason.startswith("ESTIMATED")
    assert (rest.size, rest.pnl, rest.fee) == (pytest.approx(0.2), 0.0, 0.0)
    assert rest.reason.startswith("size drift") and rest.order_id != "7777"
    assert runner.portfolio._daily_pnl == pytest.approx(fill.pnl)
    assert bus.errors(strat.name) == []


async def test_a_failed_marker_write_is_retried_with_the_next_attempt(
    repo, monkeypatch,
):
    """r1-exits-never-blocked-1, r1-restart-and-state-2. daily_long_0830
    exits on the 08:00 bar, HL refuses the close, and writing the marker
    fails (a DB blip). It was written once per episode, so the close lived
    in memory only: the next restart forgot it, and the long rode to 08:00
    the next day. The next attempt writes it, and a restart sends it."""
    strat, ex, runner, bus, clock = await _daily_long(repo, monkeypatch, refuse=2)
    real, calls = repo.mark_pending_close, []

    async def _mark(*a, **kw):
        calls.append(a)
        if len(calls) == 1:
            raise ConnectionError("db blip")
        return await real(*a, **kw)

    monkeypatch.setattr(repo, "mark_pending_close", _mark)
    await runner._run_strategy(strat)  # 08:00: refused, the write fails
    [row] = await repo.get_open_positions()
    assert PENDING_CLOSE_KEY not in json.loads(row.state_json)
    clock["bar"] = "08:15"
    await runner._run_strategy(strat)  # refused again: the marker lands
    [row] = await repo.get_open_positions()
    assert json.loads(row.state_json)[PENDING_CLOSE_KEY]["misses"] == 2

    fresh = DailyLong0830Strategy()  # the bot restarts
    runner2, _ = _runner(repo, ex, fresh)
    assert await runner2._restore_from_db(())
    await runner2._run_strategy(fresh)

    assert ex.book == {} and await repo.get_open_positions() == []
    assert len(ex.orders) == 3


async def test_a_failed_marker_write_is_retried_when_the_close_raises_again(
    repo, monkeypatch,
):
    """The same through a close that raises (`_close_raised`): the marker
    write fails with the first, and the next one that raises writes it."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)
    ex.place_order = AsyncMock(side_effect=ConnectionError("HL down"))
    real, calls = repo.mark_pending_close, []

    async def _mark(*a, **kw):
        calls.append(a)
        if len(calls) == 1:
            raise ConnectionError("db blip")
        return await real(*a, **kw)

    monkeypatch.setattr(repo, "mark_pending_close", _mark)
    for _ in range(2):
        with pytest.raises(ConnectionError):
            await runner._run_strategy(strat)

    [row] = await repo.get_open_positions()
    assert PENDING_CLOSE_KEY in json.loads(row.state_json)


async def test_a_row_read_that_fails_after_a_short_fill_books_and_re_arms(repo):
    """r1-exits-never-blocked-3. The close filled 0.4 of 1.0, and the read
    of its row that prices the fill raised (a DB blip). That read sat
    outside every guard: the fill was not booked, the strategy, which had
    dropped the position, was not re-armed, and nothing owed the 0.6 — it
    stayed open with no exit. The fill is booked against the row read
    before the order, the strategy re-armed, and its exit closes the rest."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    ex.cap = 0.4
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)
    real_get, calls = repo.get_open_position, []

    async def _get(*a, **kw):
        calls.append(a)
        if len(calls) == 2:  # the read that prices the fill
            raise ConnectionError("db blip")
        return await real_get(*a, **kw)

    repo.get_open_position = _get

    await runner._run_strategy(strat)

    [row] = await repo.get_open_positions()
    assert row.size == pytest.approx(0.6)
    [trade] = await _trades(repo)
    assert trade.size == pytest.approx(0.4)
    assert strat.side == "long"

    ex.cap = None
    await runner._run_strategy(strat)
    assert ex.book == {} and await repo.get_open_positions() == []


async def test_a_db_write_failure_stops_the_tick(repo, monkeypatch):
    """r1-exits-never-blocked-4. The ETH close fills and its DB write
    fails: the bot pauses itself. The tick went on, and the BTC close went
    out and failed its write the same way — every further fill widened the
    divergence the pause is there to stop. Nothing more goes this tick."""
    eth, btc = Exit("ETH"), Exit("BTC")
    ex = HLLike(
        Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY),
        Position(symbol="BTC", side="long", size=1.0, entry_price=ENTRY),
    )
    runner, bus = _runner(repo, ex, eth, btc)
    await _hold(repo, eth)
    await _hold(repo, btc)
    await _ready_for_ticks(runner, monkeypatch)
    repo.record_trade_and_close_position = AsyncMock(
        side_effect=ConnectionError("db down"),
    )

    await runner.tick()

    assert [o["symbol"] for o in ex.orders] == ["ETH"]
    assert await runner.control.is_paused()
    assert ex.book["BTC"].size == pytest.approx(1.0)


async def test_a_rest_is_not_drift_when_the_fills_cannot_be_read(repo):
    """r1-booking-1. Row long 1.0 @ 2000; 0.6 was liquidated @ 1800. The
    exit is clamped to the 0.4 left and fills. HL's fill history answers
    429, and an unreadable history looked like an empty one: the 0.6 was
    booked as size drift — pnl 0, no notice — the liquidation's loss never
    reached the daily counter, and with the row gone nothing looked again.
    It stays open for reconcile, which prices it from the fill once HL
    answers."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=0.4, entry_price=ENTRY))
    ex.fills = [_liquidation(size="0.6")]
    ex.fills_error = True
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat, size=1.0)

    await runner._run_strategy(strat)

    assert ex.book == {}
    [row] = await repo.get_open_positions()
    assert row.size == pytest.approx(0.6)
    [trade] = await _trades(repo)
    assert (trade.size, trade.reason) == (pytest.approx(0.4), "stop hit")

    ex.fills_error = False
    await repo.reconcile_positions(
        ex, confirm_delay_seconds=0, on_strategy_close=runner._on_reconcile_close,
    )

    assert await repo.get_open_positions() == []
    liquidation = next(t for t in await _trades(repo) if t.order_id == "9001")
    assert liquidation.size == pytest.approx(0.6)
    assert liquidation.pnl == pytest.approx((1800.0 - ENTRY) * 0.6 - 0.9)
    assert runner.portfolio._daily_pnl == pytest.approx(
        sum(t.pnl for t in await _trades(repo)),
    )


async def test_a_partial_liquidation_and_drift_are_booked_apart(repo):
    """r1-booking-1. Row long 1.0 @ 2000. 0.4 was liquidated @ 1500 (its
    fill is readable), and 0.2 of the row the exchange never held; 0.4 is
    left. The exit is clamped to 0.4 and fills. Fills had to cover all of
    the 0.6 rest, so it was all drift and the liquidation's ~200 loss was
    dropped. The 0.4 is booked from its fill, only the 0.2 as drift."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=0.4, entry_price=ENTRY))
    ex.fills = [_liquidation(size="0.4", px="1500.0")]
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat, size=1.0)

    await runner._run_strategy(strat)

    assert ex.book == {} and await repo.get_open_positions() == []
    trades = {t.reason: t for t in await _trades(repo)}
    liquidation = trades["reconcile: external close (fill)"]
    assert (liquidation.size, liquidation.order_id) == (pytest.approx(0.4), "9001")
    assert liquidation.pnl == pytest.approx((1500.0 - ENTRY) * 0.4 - 0.9)
    drift = trades["size drift: the row held more than the exchange; pnl 0"]
    assert (drift.size, drift.pnl, drift.fee) == (pytest.approx(0.2), 0.0, 0.0)
    assert trades["stop hit"].size == pytest.approx(0.4)
    assert runner.portfolio._daily_pnl == pytest.approx(
        sum(t.pnl for t in trades.values()),
    )
    [notice] = bus.errors(strat.name)
    assert notice.startswith("External close") and "size drift" in notice


async def test_flat_all_books_what_filled_beyond_the_rows(repo):
    """r1-booking-3, r1-flip-and-flat-3. The exchange holds long 2.0, the
    only row long 1.0. Flat-all sells 2.0 and it fills; only the row's 1.0
    was booked — the other 1.0, its fee and its PnL appeared nowhere, and
    the oid on the row kept any ledger from pricing it later. It is booked
    as a fill with no row is, under `<oid>-surplus`."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=2.0, entry_price=1900.0))
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)

    status = await runner._flat_all_positions()

    assert status.ok and ex.book == {} and await repo.get_open_positions() == []
    trades = {t.strategy_name: t for t in await _trades(repo)}
    row, surplus = trades[strat.name], trades["manual_flat"]
    assert row.size == pytest.approx(1.0) and surplus.size == pytest.approx(1.0)
    assert surplus.order_id == f"{row.order_id}-surplus"
    fee = MID * 1.0 * settings.taker_fee_rate
    assert surplus.fee == pytest.approx(fee)
    assert surplus.pnl == pytest.approx((MID - 1900.0) - fee)
    assert runner.portfolio._daily_pnl == pytest.approx(row.pnl + surplus.pnl)


@pytest.mark.parametrize("path", ["flat-all", "reconcile"])
async def test_a_late_fill_matched_to_a_booked_oid_gets_a_fresh_id(repo, path):
    """r1-booking-4. A close timed out and filled, and the late-fill match
    handed it oid 777, already a trade row (a sibling's close seconds
    before). Flat-all and reconcile's pass 2 wrote it straight into the
    UNIQUE `trades.order_id`: flat-all failed the coin with its row open
    on a flat exchange, pass 2 reported the close UNBOOKKEPT. It is booked
    ESTIMATED under a fresh id, as the strategy path books it."""
    await repo.record_trade(
        order_id="777", strategy_name="sibling", symbol="ETH", side="sell",
        size=0.1, price=MID, reason="stop hit",
    )
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    ex.late = ("fill", "777")
    if path == "flat-all":
        strat = Exit()
        runner, bus = _runner(repo, ex, strat)
        await _hold(repo, strat)
        assert (await runner._flat_all_positions()).ok
    else:
        result = await repo.reconcile_positions(
            ex, confirm_delay_seconds=0, hold_orphans=None,
            traded_symbols={"ETH"},
        )
        assert not any("UNBOOKKEPT" in f for f in result.failures)

    assert ex.book == {} and await repo.get_open_positions() == []
    [late] = [t for t in await _trades(repo) if t.order_id != "777"]
    assert late.reason.startswith("ESTIMATED")
    assert "the fill matched was order 777's, already booked" in late.reason


async def test_flat_all_prices_two_rows_from_one_fill(repo):
    """r1-booking-5. Two strategies hold 0.5 ETH long each; one liquidation
    fill of 0.4 @ 1500 left 0.6. Flat-all closes the 0.6, and each row
    keeps 0.2. Each rest got a ledger of its own, which left out a fill
    already booked under its oid — so once the first row had taken part of
    it, the second row's 0.2 was booked as drift, pnl 0, ~100 of loss
    dropped. One ledger for the pass prices both from the fill."""
    a, b = Exit(), Exit()
    b.name = "s_ETH_b"
    ex = HLLike(Position(symbol="ETH", side="long", size=0.6, entry_price=ENTRY))
    ex.fills = [_liquidation(size="0.4", px="1500.0")]
    runner, bus = _runner(repo, ex, a, b)
    await _hold(repo, a, size=0.5)
    await _hold(repo, b, size=0.5)

    status = await runner._flat_all_positions()

    assert status.ok and ex.book == {} and await repo.get_open_positions() == []
    trades = await _trades(repo)
    liquidation = [t for t in trades if t.reason == "reconcile: external close (fill)"]
    assert sorted(t.strategy_name for t in liquidation) == [a.name, b.name]
    assert all(
        (t.size, t.price) == (pytest.approx(0.2), pytest.approx(1500.0))
        for t in liquidation
    )
    assert not any(t.reason.startswith("size drift") for t in trades)


@pytest.mark.parametrize("case", ["hand flip", "hand flip, no row", "missed coin"])
async def test_flat_all_is_not_done_while_the_other_side_is_open(
    repo, monkeypatch, case,
):
    """r1-flip-and-flat-1. ETH long was flipped to a short by hand. The
    flat-all read still shows the long (its reduce-only sell is refused:
    "would increase position"), or it misses the coin. Both called the
    coin done: the request was acknowledged and the bot paused with the
    short open and reconcile in dry run. The request stays pending; the
    next attempt reads the short and closes it, and only then pauses."""
    strat = Exit()
    strat.quiet = True
    short = Position(symbol="ETH", side="short", size=1.0, entry_price=MID)
    ex = HLLike(short)
    ex.stale = [] if case == "missed coin" else [
        Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY),
    ]
    runner, bus = _runner(repo, ex, strat)
    if case != "hand flip, no row":
        await _hold(repo, strat)
    await _ready_for_ticks(runner, monkeypatch)
    await runner.control.request_flat_all("tok-1")

    await runner.tick()

    assert ex.book == {"ETH": short}
    assert not await runner.control.is_paused()
    assert await runner.control.get_pending_flat_request() == "tok-1"
    [notice] = bus.errors("flat-all")
    assert "ETH: the other side is open on the exchange now" in notice

    ex.stale = None
    await runner.tick()

    assert ex.book == {} and await repo.get_open_positions() == []
    assert await runner.control.is_paused()
    assert await runner.control.get_pending_flat_request() is None


async def test_a_refused_flip_whose_old_side_is_gone_says_its_open_is_refused(
    repo, monkeypatch,
):
    """r1-flip-and-flat-2. The long row was liquidated, and the strategy
    flips to short while a gate (the exposure cap) refuses the open. The
    close half still goes, is refused, and the row is booked from the
    liquidation's fill — with "The flip's open goes ahead." in the notice,
    while no open was ever sent. It says the open is refused, and why."""
    strat = Flip()
    ex = HLLike()
    ex.fills = [_liquidation()]
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)
    monkeypatch.setattr(settings, "max_total_exposure_usd", 1)
    strat.flip_to = "short"

    await runner._run_strategy(strat)

    assert [(o["side"], o["reduce_only"]) for o in ex.orders] == [("sell", True)]
    assert ex.book == {} and await repo.get_open_positions() == []
    assert strat.side is None
    [notice] = bus.errors(strat.name)
    assert notice.startswith("External close")
    assert "goes ahead" not in notice
    assert "The flip's open is refused (exposure cap): flat." in notice


async def test_two_failed_reads_keep_the_close_the_engine_owes(repo):
    """r1-restart-and-state-1. A strategy holding ETH long flips to short
    while its row cannot be read (DB down): nothing is sent, it is reset,
    and the engine owes the long a close. Next tick, still down, the flat
    strategy fires its flip again as a plain open; that read raises too,
    and its reset dropped the owed close for good — with the DB back and
    the flip quiet, nothing would ever close the long. The close is kept,
    and goes."""
    strat = Flip()
    strat.sticky = True
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)
    strat.flip_to = "short"
    real = repo.get_open_position
    repo.get_open_position = AsyncMock(side_effect=ConnectionError("db down"))

    for _ in range(2):
        with pytest.raises(ConnectionError):
            await runner._run_strategy(strat)
    assert ex.orders == [] and strat.side is None

    repo.get_open_position = real
    strat.flip_to = None  # its condition has lapsed
    await runner._run_strategy(strat)

    assert [(o["side"], o["reduce_only"]) for o in ex.orders] == [("sell", True)]
    assert ex.book == {} and await repo.get_open_positions() == []


async def test_a_restart_keeps_the_band_and_the_alerts_delivered(repo):
    """r1-restart-and-state-3. Five refused closes: the band is wide and the
    critical alert went out. A restart rebuilt the pending close at zero —
    three more attempts at the default band, and both alerts again, on
    every boot of a crash loop. The marker carries the count and the
    alerts delivered."""
    k = CLOSE_REJECTS_BEFORE_WIDE_BAND
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    ex.refuse = 100
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)
    for _ in range(k + 2):
        await runner._run_strategy(strat)
    assert len(bus.errors(strat.name)) == 2

    fresh = Exit()  # the bot restarts
    runner2, bus2 = _runner(repo, ex, fresh)
    assert await runner2._restore_from_db(())
    fresh.quiet = True  # its own rule is quiet; the engine's close goes
    for _ in range(2):
        await runner2._run_strategy(fresh)

    assert [o["slippage"] for o in ex.orders][k + 2:] == [WIDE_CLOSE_BAND] * 2
    assert bus2.errors(fresh.name) == []
    assert runner2._pending_closes[(fresh.name, "ETH")].misses == k + 4


async def test_a_pending_close_still_goes_after_its_strategy_is_dropped(
    repo, monkeypatch,
):
    """r1-restart-and-state-4. A refused close is owed and noted on its row;
    the operator then takes the strategy off the tenant's allowlist, and
    the bot restarts without it. The close was abandoned with one alert,
    the position left with no exit. It needs only the row: it still goes,
    every tick, until it fills."""
    strat = Exit()
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    ex.refuse = 2
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)
    await runner._run_strategy(strat)  # refused: owed, and noted

    runner2, bus2 = _runner(repo, ex, Exit("BTC"))  # restarted without it
    assert await runner2._restore_from_db(())
    await _ready_for_ticks(runner2, monkeypatch)
    for _ in range(2):  # refused once more, then it fills
        await runner2.tick()

    assert [(o["symbol"], o["reduce_only"]) for o in ex.orders] == [("ETH", True)] * 3
    assert ex.book == {} and await repo.get_open_positions() == []
    [trade] = await _trades(repo)
    assert trade.reason == "stop hit (pending close, attempt 3; strategy not running)"
    [unmanaged] = bus2.errors("state-restore")
    assert "(its pending close is still sent)" in unmanaged
    assert bus2.errors(strat.name) == [], "its alert went out before the restart"


async def test_flat_all_is_not_done_while_a_row_the_read_missed_is_held(
    repo, monkeypatch,
):
    """r1-tests-and-noise-1 (1). The flat-all read misses ETH while the
    long and its row are there: the row is "still held on the exchange",
    so the request stays pending and the bot is not paused."""
    strat = Exit()
    strat.quiet = True
    ex = HLLike(Position(symbol="ETH", side="long", size=1.0, entry_price=ENTRY))
    ex.stale = []
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)
    await _ready_for_ticks(runner, monkeypatch)
    await runner.control.request_flat_all("tok-1")

    status = await runner._flat_all_positions()
    assert not status.ok and status.reason == "ETH: still held on the exchange"
    await runner.tick()

    assert await runner.control.get_pending_flat_request() == "tok-1"
    assert not await runner.control.is_paused()
    assert ex.orders == [] and len(await repo.get_open_positions()) == 1


async def test_flat_all_is_not_done_while_a_gone_row_cannot_be_booked(repo):
    """r1-tests-and-noise-1 (2). The exchange is flat and nothing prices
    the row (no fill, no mid): it "could not be booked" and stays open."""
    strat = Exit()
    ex = HLLike()
    ex.no_mid = True
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)

    status = await runner._flat_all_positions()

    assert not status.ok
    assert status.reason == (
        "ETH: a row the exchange no longer holds could not be booked"
    )
    assert len(await repo.get_open_positions()) == 1


async def test_a_flat_all_whose_reason_changes_says_so_once(repo, monkeypatch):
    """r1-tests-and-noise-2. A thin book: BTC's close is refused on one
    tick and fills 0.1 on the next. "Did not complete" was keyed on the
    reason, each coin's outcome word in it, so it went to Telegram every
    tick. It is said once per request and set of coins left open."""
    btc = Exit("BTC")
    btc.quiet = True
    ex = HLLike(Position(symbol="BTC", side="long", size=1.0, entry_price=ENTRY))
    runner, bus = _runner(repo, ex, btc)
    await _hold(repo, btc)
    await _ready_for_ticks(runner, monkeypatch)
    await runner.control.request_flat_all("tok-1")
    real = ex.place_order

    async def _thin(*a, **kw):
        if len(ex.orders) % 2 == 0:
            ex.refuse = 1
        else:
            ex.cap = 0.1
        try:
            return await real(*a, **kw)
        finally:
            ex.cap = None

    ex.place_order = _thin
    for _ in range(6):
        await runner.tick()

    assert len(ex.orders) == 6
    assert ex.book["BTC"].size == pytest.approx(0.7)
    [notice] = bus.errors("flat-all")
    assert notice.startswith("Flat-all did not complete: BTC: rejected")
