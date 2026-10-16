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
   critical alert when that fails too — unless HL could not say whether
   it filled ("outcome unknown"): then it is not sent again, one alert.

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
from hypertrade.events.types import ErrorOccurred, PositionClosed
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
        self.refusal = "Order could not immediately match"

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
            if zero:
                self.zero_fill -= 1
            else:
                self.refuse -= 1
            return Order(
                id=f"o{len(self.orders)}", symbol=symbol, side=side,
                size=0.0 if zero else size, order_type=order_type,
                filled_price=MID if zero else None,
                status=OrderStatus.FILLED if zero else OrderStatus.REJECTED,
                error=None if zero else self.refusal,
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

    def holds_position(self):
        return self.side is not None

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

    def closed(self) -> list[PositionClosed]:
        return [e for e in self.events if isinstance(e, PositionClosed)]


def _candles(close=MID):
    ts = pd.date_range("2026-09-25", periods=3, freq="1h", tz="UTC")
    return pd.DataFrame({
        "timestamp": ts, "open": close, "high": close, "low": close,
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


async def _exit_holding(repo, side="long", *, held=1.0, refuse=0):
    """An `Exit` holding a 1.0 ETH row on `side`, of which the exchange
    holds `held` (0: flat); HL refuses the next `refuse` orders."""
    strat = Exit()
    ex = HLLike(*(
        [Position(symbol="ETH", side=side, size=held, entry_price=ENTRY)]
        if held else []
    ))
    ex.refuse = refuse
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat, side=side)
    return strat, ex, runner, bus


async def _ratcheted_ath(repo, monkeypatch):
    """ath_breakout long from 100 on a bar whose high was 101, as its row's
    state_json says; its peak has since ratcheted to 200, so its trail
    stop is 130 and BTC at 125 is below it. HL refuses the next close."""
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
    return strat, ex, runner, bus


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
    leave a 0.6 short for reconcile to close five minutes later. The rest
    stays on the row (as for any short fill), and the re-armed exit's next
    attempt is refused and books it from its liquidation fill."""
    strat, ex, runner, bus = await _exit_holding(repo, held=0.4)
    ex.fills = [_liquidation(size="0.6")]
    ex.read_errors = 1

    await runner._run_strategy(strat)
    assert ex.orders[0] == {
        "symbol": "ETH", "side": "sell", "size": 1.0,
        "reduce_only": True, "slippage": None,
    }
    assert ex.book == {}, "flat — never short"
    [row] = await repo.get_open_positions()
    assert row.size == pytest.approx(0.6) and strat.side == "long"

    await runner._run_strategy(strat)

    assert [o["reduce_only"] for o in ex.orders] == [True, True]
    assert ex.book == {} and await repo.get_open_positions() == []
    trades = sorted(await _trades(repo), key=lambda t: t.size)
    assert [(t.size, t.reason) for t in trades] == [
        (pytest.approx(0.4), "stop hit"),
        (pytest.approx(0.6), "reconcile: external close (fill)"),
    ]
    assert strat.side is None


async def test_a_close_against_a_flat_exchange_is_booked_from_fills(repo, caplog):
    """Liquidated: the exchange is flat, the row still open. The close goes
    out reduce-only and is refused; a confirmed read books the row from
    HL's own fill, the PnL reaches the daily counter, one notice (logged
    at WARNING), nothing is retried."""
    strat, ex, runner, bus = await _exit_holding(repo, held=0)
    ex.fills = [_liquidation()]

    with caplog.at_level(logging.INFO):
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
    assert bus.errors(strat.name) == [], "information, not an alarm"
    [notice] = bus.closed()
    assert notice.strategy == f"{strat.name} (external close)"
    assert notice.pnl == pytest.approx(trade.pnl)
    [line] = [r.getMessage() for r in caplog.records
              if r.levelno == logging.WARNING and "External close" in r.getMessage()]
    assert "Reduce only order would increase position" in line and "at ~2100" in line
    assert runner._close_rejects == {}
    assert any("closed outside this bot" in r.getMessage() for r in caplog.records)
    assert not any("reconcile closed" in r.getMessage() for r in caplog.records)


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
    strat, ex, runner, bus = await _exit_holding(repo)
    ex.blind = 1

    await runner._run_strategy(strat)

    assert ex.book == {}
    [trade] = await _trades(repo)
    assert trade.reason == "stop hit" and trade.size == pytest.approx(1.0)
    assert bus.errors(strat.name) == []


async def test_a_flat_read_after_a_refusal_needs_a_second_read(repo):
    """The close is refused (no liquidity) and the next read misses ETH;
    the confirming read finds the long. Not booked: the strategy is
    re-armed and the exit fills next tick."""
    strat, ex, runner, bus = await _exit_holding(repo, refuse=1)
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


@pytest.mark.parametrize("reads", [
    ("raise", "raise"), ("real", None, "raise"), ("real", "raise", None),
], ids=["first-read", "confirm-read", "error-then-flat"])
async def test_a_read_error_is_never_treated_as_flat(repo, reads):
    """A read fails — the clamp's and the first after the rejection, or
    the one confirming a flat read, or one before a flat read. The close
    still goes out at the DB size, reduce-only; its rejection keeps the
    row open and the strategy re-armed, never an external close."""
    strat, ex, runner, bus = await _exit_holding(repo, refuse=1)
    real, script = ex.get_position, list(reads)

    async def _get_position(symbol):
        step = script.pop(0) if script else "real"
        if step == "raise":
            raise ExchangeReadError("clearinghouseState: 502 Bad Gateway")
        return None if step is None else await real(symbol)

    ex.get_position = _get_position

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
    strat, ex, runner, bus = await _exit_holding(repo)
    setattr(ex, fault, 1)

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
    """ath_breakout's trail stop fires CLOSE_LONG and HL refuses it.
    Re-synced to the row the peak fell back to 101 and the stop to 65.65:
    the exit never fired again. Re-armed with its pre-exit state, it is
    sent again next tick and fills."""
    strat, ex, runner, bus = await _ratcheted_ath(repo, monkeypatch)

    await runner._run_strategy(strat)
    assert strat._peak_since_entry == 200.0, "the ratchet is kept"
    [alert] = bus.errors(strat.name)
    assert "re-armed" in alert

    await runner._run_strategy(strat)

    assert [o["reduce_only"] for o in ex.orders] == [True, True]
    assert ex.book == {} and await repo.get_open_positions() == []
    [trade] = await _trades(repo)
    assert trade.reason.startswith("Trail stop hit")


async def test_an_exit_that_comes_and_goes_is_one_episode(
    repo, caplog, monkeypatch,
):
    """A refused stop that is hit, not hit, hit again… (HL refusing all
    along) alerts once: each time it stops firing a WARNING says the
    strategy still holds the position, and a re-fire is no news. Once its
    exit has been quiet for CLOSE_EPISODE_QUIET_SECONDS — its own window,
    not NU-2's HOLD_REALERT_SECONDS — a rejection, here the close half of
    a flip, alerts again and says its open was not sent."""
    strat, ex, runner, bus = await _exit_holding(repo, "short", refuse=5)

    with caplog.at_level(logging.WARNING):
        for quiet in (False, True) * 3:
            strat.quiet = quiet
            await runner._run_strategy(strat)
    assert len(ex.orders) == 3 and len(bus.errors(strat.name)) == 1
    ends = [r.getMessage() for r in caplog.records if "no longer fires" in r.getMessage()]
    assert len(ends) == 3 and "(short 1.0)" in ends[0]
    assert all("held by the strategy" in m for m in ends)

    flip = Signal(
        action=SignalAction.OPEN_LONG, symbol="ETH",
        strategy_name=strat.name, reason="flip",
    )
    monkeypatch.setattr(runner_mod, "HOLD_REALERT_SECONDS", 0.0)
    assert await runner._execute_signal(flip, MID) is False
    assert len(bus.errors(strat.name)) == 1, "NU-2's window is not this one"

    strat.quiet = True
    await runner._run_strategy(strat)
    monkeypatch.setattr(runner_mod, "CLOSE_EPISODE_QUIET_SECONDS", 0.0)
    assert await runner._execute_signal(flip, MID) is False
    first, second = bus.errors(strat.name)
    assert "the close half of a flip, so its open is not sent" in second
    assert [o["side"] for o in ex.orders] == ["buy"] * 5, "no open sent"


@pytest.mark.parametrize("fault", ["db read raises", "unpriceable"])
async def test_a_streak_that_lost_its_strategy_never_says_it_holds(repo, caplog, fault):
    """Refused once (re-armed, streak 1), then an attempt that leaves the
    strategy flat: its DB read raises before any order, or the position
    is gone and the row cannot be priced. The strategy dropped the
    position on its exit; no line may say it still holds it."""
    strat, ex, runner, bus = await _exit_holding(repo, refuse=1)
    await runner._run_strategy(strat)
    assert strat.side == "long"

    if fault == "db read raises":
        real = repo.get_open_position
        repo.get_open_position = AsyncMock(side_effect=ConnectionError("db blip"))
        with pytest.raises(ConnectionError):
            await runner._run_strategy(strat)
        repo.get_open_position = real
    else:
        ex.book.clear()
        ex.set_price("ETH", 0.0)
        strat.side = None  # it dropped the position on emitting the exit
        await runner._execute_signal(Signal(
            action=SignalAction.CLOSE_LONG, symbol="ETH",
            strategy_name=strat.name, reason="stop hit",
        ), MID)
        assert runner._close_rejects == {}
    with caplog.at_level(logging.WARNING):
        await runner._run_strategy(strat)

    assert strat.side is None and len(await repo.get_open_positions()) == 1
    ends = [r.getMessage() for r in caplog.records if "no longer fires" in r.getMessage()]
    assert len(ends) == (0 if fault == "unpriceable" else 1)
    assert not any("held by the strategy" in m for m in ends)


async def test_k_rejections_widen_the_band_then_alert_critical(repo):
    k = CLOSE_REJECTS_BEFORE_WIDE_BAND
    strat, ex, runner, bus = await _exit_holding(repo, refuse=k + 3)

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
    strat, ex, runner, bus = await _exit_holding(repo, refuse=k + 3)

    for _ in range(k):
        await runner._run_strategy(strat)
    bus.deliver = False
    await runner._run_strategy(strat)  # the critical one: lost
    bus.deliver = True
    for _ in range(2):
        await runner._run_strategy(strat)

    alerts = bus.errors(strat.name)
    assert [a.startswith("CRITICAL") for a in alerts] == [False, True]


@pytest.mark.parametrize("flip", [False, True])
async def test_a_failed_classification_still_re_arms_the_strategy(repo, caplog, flip):
    """Whatever raises while a refused close is classified, the strategy
    is pointed back at its row — it had dropped the position, and left
    flat nothing would ever close it. Unclassified, a flip aligns and
    alerts as before NU-5a, and sends no open."""
    strat, ex, runner, bus = await _exit_holding(repo, refuse=1)
    runner._held_after_close = AsyncMock(side_effect=ConnectionError("db blip"))

    if flip:
        strat.side = "short"  # it switched when it emitted the flip
        assert not await runner._execute_signal(Signal(
            action=SignalAction.OPEN_SHORT, symbol="ETH",
            strategy_name=strat.name, reason="flip",
        ), MID)
    else:
        await runner._run_strategy(strat)

    assert strat.side == "long"
    assert len(await repo.get_open_positions()) == 1
    assert len(ex.orders) == 1
    [alert] = bus.errors(strat.name)
    assert alert.startswith("Flip-close failed") == flip
    if flip:
        return
    assert "classifying it failed" in alert
    strat.quiet = True
    with caplog.at_level(logging.WARNING):
        await runner._run_strategy(strat)  # its exit no longer fires
    assert any("no longer fires" in r.getMessage() for r in caplog.records)


# ----------------------------------------------------------------------
# A flip into a position that is already gone
# ----------------------------------------------------------------------


@pytest.mark.parametrize("other", [None, "short"])
async def test_a_flip_whose_old_side_is_gone_books_it_and_opens_nothing(
    repo, caplog, other,
):
    """The long row was liquidated (and maybe a short opened by hand); the
    strategy flips to short. The close half is refused and booked from the
    liquidation fill; the open is not sent (audit H1) — nothing is stacked
    onto a position no row owns. One notice, the strategy flat."""
    strat = Exit()
    ex = HLLike(*(
        [Position(symbol="ETH", side=other, size=1.0, entry_price=ENTRY)]
        if other else []
    ))
    ex.fills = [_liquidation()]
    runner, bus = _runner(repo, ex, strat)
    await _hold(repo, strat)
    strat.side = "short"  # it switched when it emitted the flip

    with caplog.at_level(logging.WARNING):
        ok = await runner._execute_signal(Signal(
            action=SignalAction.OPEN_SHORT, symbol="ETH",
            strategy_name=strat.name, reason="flip",
        ), MID)

    assert ok is False
    assert [(o["side"], o["reduce_only"]) for o in ex.orders] == [("sell", True)]
    assert await repo.get_open_positions() == []
    assert (ex.book.get("ETH") and ex.book["ETH"].size) == (1.0 if other else None)
    assert strat.side is None
    assert bus.errors(strat.name) == []
    [notice] = bus.closed()
    assert notice.strategy == f"{strat.name} (external close)"
    [line] = [r.getMessage() for r in caplog.records if "Flip-close" in r.getMessage()]
    assert "the long was already gone and is booked as an external close" in line


# ----------------------------------------------------------------------
# Every close path is reduce-only; opens are not
# ----------------------------------------------------------------------


async def test_a_flip_closes_reduce_only_and_opens_without(repo):
    strat, ex, runner, bus = await _exit_holding(repo, "short")

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


async def test_flat_all_with_rows_beyond_the_exchange_completes_as_before(repo):
    """The row holds 1.0, the exchange 0.6 (partly liquidated). Flat-all's
    reduce-only close takes the 0.6; every row is closed and its strategy
    reset, the request acknowledged and the bot paused — as on master."""
    strat, ex, runner, bus = await _exit_holding(repo, held=0.6)
    await runner.control.request_flat_all("t1")

    await runner.tick()

    assert [(o["size"], o["reduce_only"]) for o in ex.orders] == [(0.6, True)]
    assert ex.book == {} and await repo.get_open_positions() == []
    assert strat.side is None
    assert await runner.control.is_paused()
    assert not await runner.control.get_pending_flat_request()


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


# ----------------------------------------------------------------------
# One alert per (strategy, coin), one line per attempt
# ----------------------------------------------------------------------


async def test_a_flip_close_refused_across_bars_alerts_twice(repo, caplog):
    """A standing flip whose close is refused on every tick of six bars:
    the streak's alert and its critical alert, nothing per bar, no open."""
    strat, ex, runner, bus = await _exit_holding(repo, "short", refuse=100)
    bars = pd.date_range("2026-09-25", periods=6, freq="15min", tz="UTC")

    with caplog.at_level(logging.WARNING):
        await runner._execute_signal(Signal(
            action=SignalAction.OPEN_LONG, symbol="ETH",
            strategy_name=strat.name, reason="flip",
        ), MID, bar_time=bars[0])
    [line] = [r.getMessage() for r in caplog.records if "Flip-close" in r.getMessage()]
    assert line.endswith("the short 1.0 is still open; strategy re-armed, its "
                         "close sent again while it fires.")
    for bar in bars:
        for _ in range(3):
            await runner._execute_signal(Signal(
                action=SignalAction.OPEN_LONG, symbol="ETH",
                strategy_name=strat.name, reason="flip",
            ), MID, bar_time=bar)

    alerts = bus.errors(strat.name)
    assert len(alerts) == 2 and alerts[1].startswith("CRITICAL")
    assert {o["reduce_only"] for o in ex.orders} == {True}, "no open sent"


async def test_an_unknown_outcome_is_not_sent_again(repo):
    """HL timed out on the close (after 30 s of polling): it may still
    fill. Sent again each tick, every attempt would cost the order deadline
    and the poll inside the tick, and once it filled late the re-send,
    clamped to what is left, would close the 0.5 opened by hand that no
    row owns. So as before NU-5a it is not sent again: one alert."""
    strat, ex, runner, bus = await _exit_holding(repo, held=1.5, refuse=1)
    ex.refusal = "outcome unknown: timed out, no fill seen in 30s"

    await runner._run_strategy(strat)
    ex.book["ETH"].size = 0.5  # the timed-out close fills late
    await runner._run_strategy(strat)

    assert len(ex.orders) == 1 and ex.book["ETH"].size == 0.5
    assert strat.side is None and runner._close_rejects == {}
    assert len(await repo.get_open_positions()) == 1 and await _trades(repo) == []
    [alert] = bus.errors(strat.name)
    assert "outcome unknown" in alert and "not sent again" in alert


async def test_a_stuck_close_logs_one_line_per_attempt(repo, caplog):
    strat, ex, runner, bus = await _exit_holding(repo, refuse=7)

    with caplog.at_level(logging.INFO, logger="hypertrade.engine.runner"):
        for _ in range(7):
            ex.read_errors = 1  # the clamp's read: its line is INFO after the first
            await runner._run_strategy(strat)

    lines = [r for r in caplog.records if "left" in r.getMessage()
             or "sent again" in r.getMessage() or "CRITICAL" in r.getMessage()]
    assert len(lines) == 7, "one line per attempt"
    assert [r.levelno for r in caplog.records if r.levelno >= logging.WARNING] == [
        logging.WARNING, logging.ERROR, logging.ERROR,
    ], "the first clamp line, the alert and the critical alert; nothing else"


async def test_an_episode_after_a_reset_alerts_again(repo):
    """A streak ends with the strategy's reset (reconcile closed its row):
    the next episode's first refusal is news again."""
    strat, ex, runner, bus = await _exit_holding(repo, refuse=2)
    await runner._run_strategy(strat)
    await runner._reset_strategy_state(strat.name, why="test")
    strat.side = "long"

    await runner._run_strategy(strat)

    assert len(bus.errors(strat.name)) == 2


# ----------------------------------------------------------------------
# A close that may have filled beside another row on its side
# ----------------------------------------------------------------------


@pytest.mark.parametrize("held", [False, True, "unreadable"])
async def test_a_sibling_row_on_the_side_keeps_the_old_behaviour(repo, caplog, held):
    """Two rows share the ETH long (allow_multi_coin). Booking one from
    the fill both share would leave the other priced at the mid; a re-send
    after an unknown outcome could close the other's position. So neither:
    the close is dropped as before NU-5a. Flat: reconcile then prices both
    rows from the one liquidation fill. Held: nothing is re-sent — nor
    when the rows on the coin cannot be read to tell, which alerts."""
    strat, other = Exit(), Exit()
    other.name = "other_ETH"
    ex = HLLike(*(
        [Position(symbol="ETH", side="long", size=2.0, entry_price=ENTRY)]
        if held else []
    ))
    ex.fills = [_liquidation(size="2.0")]
    if held:
        ex.refuse = 1
    if held is True:
        ex.refusal = "outcome unknown: timed out, no fill seen in 30s"
    runner, bus = _runner(repo, ex, strat, other)
    await _hold(repo, strat)
    await _hold(repo, other)
    if held == "unreadable":
        repo.get_open_positions_for_symbol = AsyncMock(
            side_effect=[ConnectionError("db blip")],
        )

    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            await runner._run_strategy(strat)

    assert len(ex.orders) == 1 and strat.side is None
    [line] = [r.getMessage() for r in caplog.records if "HL nets the coin" in r.getMessage()]
    assert "(long 1.0)" in line and "it is dropped and the row left open" in line
    assert await _trades(repo) == [] and runner._close_rejects == {}
    assert len(bus.errors(strat.name)) == (held == "unreadable")
    assert bus.closed() == []
    if held:
        return
    await repo.reconcile_positions(ex)
    trades = await _trades(repo)
    assert sorted(t.strategy_name for t in trades) == ["other_ETH", "s_ETH"]
    assert all(t.price == pytest.approx(1800.0) for t in trades), "no mid"


async def test_a_sibling_row_flip_is_re_aligned_as_before(repo, caplog, monkeypatch):
    """Kill switch on: the flip only closes. HL cannot say whether the
    close filled and another row shares the long, so the strategy is
    re-synced to its row as before NU-5a, never left on the side it asked
    for, and no open is sent."""
    monkeypatch.setattr(settings, "kill_switch", True)
    strat, other = Exit(), Exit()
    other.name = "other_ETH"
    ex = HLLike(Position(symbol="ETH", side="long", size=2.0, entry_price=ENTRY))
    ex.refuse, ex.refusal = 1, "outcome unknown: timed out, no fill seen in 30s"
    runner, bus = _runner(repo, ex, strat, other)
    await _hold(repo, strat)
    await _hold(repo, other)
    strat.side = "short"  # it switched when it emitted the flip

    strat.on_candle = AsyncMock(return_value=Signal(
        action=SignalAction.OPEN_SHORT, symbol="ETH",
        strategy_name=strat.name, reason="flip",
    ))
    with caplog.at_level(logging.WARNING):
        await runner._run_strategy(strat)
    assert any("the flip re-aligns to its row" in r.getMessage() for r in caplog.records)

    assert [(o["side"], o["reduce_only"]) for o in ex.orders] == [("sell", True)]
    assert strat.side == "long", "re-synced to its row"
    assert len(await repo.get_open_positions()) == 2


async def test_a_close_dropped_for_unreadable_rows_alerts_once(
    repo, caplog, monkeypatch,
):
    """The coin's rows cannot be read, so a refused close is handled as
    before NU-5a, though most likely no row shares the coin: it is dropped
    with its row left open and managed by nothing, and the streak a
    readable attempt began ends. That alerts (it used to reach nobody),
    once per bar (`_note_refusal`) even when the exit comes again on it,
    and nothing re-arms the strategy."""
    strat, ex, runner, bus = await _exit_holding(repo, refuse=4)
    rows = await repo.get_open_positions_for_symbol("ETH")
    repo.get_open_positions_for_symbol = AsyncMock(
        side_effect=[rows] + [ConnectionError("db blip")] * 3,
    )

    with caplog.at_level(logging.WARNING):
        await runner._run_strategy(strat)  # readable: re-armed
        assert strat.side == "long" and len(bus.errors(strat.name)) == 1
        await runner._run_strategy(strat)
        assert strat.side is None and runner._close_rejects == {}
        strat.side = "long"  # the exit comes again on the bar
        await runner._run_strategy(strat)

    assert [(o["side"], o["reduce_only"]) for o in ex.orders] == [("sell", True)] * 3
    assert strat.side is None, "never re-armed"
    assert len(await repo.get_open_positions()) == 1 and await _trades(repo) == []
    _, alert = bus.errors(strat.name)
    assert "(long 1.0)" in alert and "unreadable (ConnectionError: db blip)" in alert
    assert "dropped and the row left open, managed by nothing" in alert
    assert alert.endswith("Check the position.")
    lines = [r.getMessage() for r in caplog.records if "HL nets the coin" in r.getMessage()]
    assert len(lines) == 1, "the repeat logs at DEBUG"

    async def _next_bar(symbol, timeframe, *a, **kw):
        candles = _candles()
        candles["timestamp"] += pd.Timedelta(hours=1)
        return candles

    monkeypatch.setattr(runner_mod, "fetch_candles", _next_bar)
    strat.side = "long"
    await runner._run_strategy(strat)
    assert len(bus.errors(strat.name)) == 3 and strat.side is None


async def test_a_flip_close_dropped_for_unreadable_rows_alerts_once(repo, caplog):
    """The same for a flip: it re-aligns to its row as before NU-5a, sends
    no open, and its own note is its one alert on the bar."""
    strat, ex, runner, bus = await _exit_holding(repo, refuse=2)
    repo.get_open_positions_for_symbol = AsyncMock(
        side_effect=ConnectionError("db blip"),
    )

    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            assert await runner._execute_signal(Signal(
                action=SignalAction.OPEN_SHORT, symbol="ETH",
                strategy_name=strat.name, reason="flip",
            ), MID, bar_time=_candles()["timestamp"].iloc[-2]) is False

    assert [(o["side"], o["reduce_only"]) for o in ex.orders] == [("sell", True)] * 2
    assert strat.side == "long", "re-aligned to its row"
    assert runner._close_rejects == {}
    [alert] = bus.errors(strat.name)
    assert alert.startswith("Flip-close failed")
    assert any("the flip re-aligns to its row" in r.getMessage() for r in caplog.records)


# ----------------------------------------------------------------------
# A row nothing can price
# ----------------------------------------------------------------------


@pytest.mark.parametrize("flip", [False, True])
async def test_an_unpriceable_external_close_stays_open_for_reconcile(repo, flip):
    """Liquidated during an HL outage: no fills, no mid. The row stays open
    for reconcile, nothing is booked, and a flip sends no open."""
    strat, ex, runner, bus = await _exit_holding(repo, held=0)
    ex.set_price("ETH", 0.0)

    if flip:
        strat.side = "short"
        ok = await runner._execute_signal(Signal(
            action=SignalAction.OPEN_SHORT, symbol="ETH",
            strategy_name=strat.name, reason="flip",
        ), MID)
        assert ok is False
        assert strat.side == "long", "re-synced to the row it still has"
    else:
        strat.side = None  # it dropped the position when it emitted the exit
        await runner._execute_signal(Signal(
            action=SignalAction.CLOSE_LONG, symbol="ETH",
            strategy_name=strat.name, reason="stop hit",
        ), MID)
        assert strat.side is None

    assert all(o["reduce_only"] for o in ex.orders)
    assert len(await repo.get_open_positions()) == 1
    assert await _trades(repo) == []


async def test_an_external_close_of_a_row_closed_meanwhile_writes_nothing(repo):
    strat, ex, runner, bus = await _exit_holding(repo, held=0)
    ex.fills = [_liquidation()]
    [row] = await repo.get_open_positions()
    await repo.close_position(strat.name, "ETH", exit_price=MID, pnl=0.0)

    assert await repo.close_row_externally(ex, row) is None
    assert await _trades(repo) == []


async def test_a_refused_flip_close_re_arms_a_real_strategy_live(repo, monkeypatch):
    """ath_breakout flips (OPEN_SHORT stands in for a reversal) and HL
    refuses the close half.
    Re-armed with its live state the ratchet holds, and its own trail stop
    closes the long next tick."""
    strat, ex, runner, bus = await _ratcheted_ath(repo, monkeypatch)
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
