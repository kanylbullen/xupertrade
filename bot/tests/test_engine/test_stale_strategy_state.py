"""The DB decides whether a strategy holds a position — not its Redis
snapshot, and not its own memory.

Production, 2026-09-23 (testnet restarted on 9b0b103): DB and exchange
agreed testnet held only daily_long_0830 BTC and sma_rsi ETH, yet the
restore logs showed btc_mean_reversion short @78834 (phantom-closed
2026-09-09), hash_supertrend short @78424 (2026-09-10), keltner_breakout,
pivot_supertrend, kalman_breakout, bb_rsi_scalper and hash_momentum
restored as in position — seven of fifteen enabled strategies silently
dead. Three layers, each tested here:

1. Startup: a strategy with no open DB row restores flat from its
   snapshot, keeping only cooldown fields, logs that it did, and rewrites
   the snapshot.
2. `_reset_strategy_state` (reconcile, flat-all, flips that end flat)
   rewrites the snapshot too; a None export deletes it.
3. A CLOSE ignored for want of a position resets the strategy to flat.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from hypertrade.engine.control import BotControl
from hypertrade.engine.runner import EngineRunner
from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.exchange.base import (
    ExchangeReadError, Order, OrderStatus, OrderType, Position,
)
from hypertrade.strategies.btc_mean_reversion import BTCMeanReversionStrategy
from hypertrade.strategies.hash_momentum import HashMomentumStrategy
from hypertrade.strategies.registry import get_strategy, list_strategies, load_all

BAR_TS = datetime(2026, 9, 22, 8, tzinfo=timezone.utc)

# What testnet's Redis held for btc_mean_reversion on 2026-09-23: the
# snapshot written after its OPEN, never overwritten after the close.
STALE_BMR = {
    "position_side": "short", "entry_price": 78834.0,
    "stop_loss": 81987.36, "take_profit": 74103.96,
}


def _row(strategy, symbol="BTC", side="short", entry=78834.0, state=None):
    r = MagicMock()
    r.strategy_name = strategy
    r.symbol = symbol
    r.side = side
    r.size = 0.01
    r.entry_price = entry
    r.state_json = json.dumps(state) if state is not None else None
    return r


def _runner(strategies, *, snapshots=None, repo=True, open_row=None):
    ctl = MagicMock()
    snapshots = snapshots or {}
    ctl.load_strategy_state = AsyncMock(
        side_effect=lambda name: snapshots.get(name),
    )
    ctl.save_strategy_state = AsyncMock()
    r = None
    if repo:
        r = MagicMock()
        r.get_open_position = AsyncMock(return_value=open_row)
    exchange = MagicMock()
    exchange.place_order = AsyncMock()
    exchange.get_position = AsyncMock(return_value=None)
    runner = EngineRunner(
        exchange=exchange, strategies=list(strategies), repo=r,
        event_bus=None, control=ctl,
    )
    runner.portfolio = MagicMock()
    runner.portfolio.check_risk_limits = AsyncMock(return_value=True)
    runner.portfolio.record_pnl = AsyncMock()
    return runner, ctl, exchange


def _saved(ctl):
    return {c.args[0]: c.args[1] for c in ctl.save_strategy_state.await_args_list}


# ----------------------------------------------------------------------
# 1. Startup
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_restores_a_stale_in_position_snapshot_flat(caplog):
    strat = BTCMeanReversionStrategy()
    runner, ctl, _ = _runner([strat], snapshots={strat.name: STALE_BMR})

    with caplog.at_level(logging.INFO, logger="hypertrade.engine.runner"):
        await runner._restore_strategies([])

    assert not strat.holds_position()
    assert strat._entry_price is None and strat._stop_loss is None
    assert (
        "btc_mean_reversion: snapshot said in position but no open DB row "
        "— starting flat" in caplog.text
    )
    # The stale snapshot is deleted so the next restart starts clean.
    assert _saved(ctl) == {"btc_mean_reversion": None}


@pytest.mark.asyncio
async def test_startup_keeps_cooldown_but_not_position_from_the_snapshot():
    strat = HashMomentumStrategy()
    stale = {
        "in_long": True, "in_short": False, "entry": 180.0, "sl": 176.0,
        "tp": 190.0, "bars_since_close": 2,
        "last_closed_bar_ts": BAR_TS.isoformat(),
    }
    runner, ctl, _ = _runner([strat], snapshots={strat.name: stale})

    await runner._restore_strategies([])

    assert not strat.holds_position()
    assert strat._sl is None and strat._entry is None
    assert strat._bars_since_close == 2
    assert strat._last_closed_bar_ts == BAR_TS
    saved = _saved(ctl)["hash_momentum"]
    assert saved["in_long"] is False and saved["bars_since_close"] == 2


@pytest.mark.asyncio
async def test_startup_leaves_a_cooldown_only_snapshot_alone(caplog):
    strat = HashMomentumStrategy()
    snap = {
        "in_long": False, "in_short": False, "entry": 180.0, "sl": 176.0,
        "tp": 190.0, "bars_since_close": 1,
        "last_closed_bar_ts": BAR_TS.isoformat(),
    }
    runner, ctl, _ = _runner([strat], snapshots={strat.name: snap})

    with caplog.at_level(logging.INFO, logger="hypertrade.engine.runner"):
        await runner._restore_strategies([])

    assert strat._bars_since_close == 1
    assert "snapshot said in position" not in caplog.text
    assert "Restored hash_momentum cooldown state from Redis" in caplog.text
    ctl.save_strategy_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_restores_a_strategy_with_a_db_row_from_the_row():
    strat = BTCMeanReversionStrategy()
    runner, ctl, _ = _runner([strat], snapshots={strat.name: STALE_BMR})

    await runner._restore_strategies(
        [_row(strat.name, side="short", entry=78000.0, state={
            "position_side": "short", "entry_price": 78000.0,
            "stop_loss": 81120.0, "take_profit": 73320.0,
        })],
    )

    assert strat._position_side == "short"
    assert strat._entry_price == 78000.0
    ctl.load_strategy_state.assert_not_awaited()
    ctl.save_strategy_state.assert_not_awaited()


# ----------------------------------------------------------------------
# 2. Engine-driven resets rewrite the snapshot
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_close_resets_memory_and_snapshot(caplog):
    strat = BTCMeanReversionStrategy()
    strat.restore_state("short", 78834.0)
    runner, ctl, _ = _runner([strat])

    with caplog.at_level(logging.INFO, logger="hypertrade.engine.runner"):
        await runner._on_reconcile_close(strat.name, -3.2)

    assert not strat.holds_position()
    assert _saved(ctl) == {"btc_mean_reversion": None}
    assert "Reset btc_mean_reversion to flat" in caplog.text


@pytest.mark.asyncio
async def test_flat_all_resets_the_strategy_whose_row_it_closed():
    strat = BTCMeanReversionStrategy()
    strat.restore_state("short", 78834.0)
    runner, ctl, exchange = _runner([strat])
    runner.exchange.get_positions = AsyncMock(return_value=[
        Position(symbol="BTC", side="short", size=0.01, entry_price=78834.0),
    ])
    exchange.place_order = AsyncMock(return_value=Order(
        id="o1", symbol="BTC", side="buy", size=0.01,
        order_type=OrderType.MARKET, filled_price=78000.0,
        status=OrderStatus.FILLED,
    ))
    runner.repo.get_open_positions_for_symbol = AsyncMock(
        return_value=[_row(strat.name)],
    )
    runner.repo.record_trade_and_close_position = AsyncMock()

    status = await runner._flat_all_positions()

    assert status.ok
    assert not strat.holds_position()
    assert _saved(ctl) == {"btc_mean_reversion": None}


@pytest.mark.asyncio
async def test_save_strategy_state_deletes_the_snapshot_on_none():
    """export_state() is None for a flat strategy with nothing to carry
    over. Skipping the write on None is how the OPEN-time snapshot
    survived every close."""
    ctl = BotControl(redis_url="redis://unused/0", mode="testnet")
    ctl._redis = MagicMock()
    ctl._redis.set = AsyncMock()
    ctl._redis.delete = AsyncMock()
    key = "hypertrade:testnet:control:strategy:btc_mean_reversion:state"

    await ctl.save_strategy_state("btc_mean_reversion", None)
    ctl._redis.delete.assert_awaited_once_with(key)

    await ctl.save_strategy_state("btc_mean_reversion", STALE_BMR)
    ctl._redis.set.assert_awaited_once_with(key, json.dumps(STALE_BMR))


# ----------------------------------------------------------------------
# 3. A CLOSE ignored for want of a position resets the strategy
# ----------------------------------------------------------------------


def _close_short(strat):
    return Signal(
        action=SignalAction.CLOSE_SHORT, symbol="BTC",
        strategy_name=strat.name, reason="SL hit",
    )


@pytest.mark.asyncio
async def test_close_without_a_db_position_resets_the_strategy(caplog):
    strat = BTCMeanReversionStrategy()
    strat.restore_from_json("flat", 0.0, STALE_BMR)  # the pre-fix restore
    assert strat.holds_position()
    runner, ctl, exchange = _runner([strat], open_row=None)

    with caplog.at_level(logging.INFO, logger="hypertrade.engine.runner"):
        ok = await runner._execute_signal(_close_short(strat), 78000.0)

    assert ok is False
    exchange.place_order.assert_not_awaited()
    assert not strat.holds_position()
    assert _saved(ctl) == {"btc_mean_reversion": None}
    resets = [
        r for r in caplog.records
        if r.levelno == logging.INFO and "Reset btc_mean_reversion to flat" in r.getMessage()
    ]
    assert len(resets) == 1
    assert "no open DB position" in resets[0].getMessage()


@pytest.mark.asyncio
async def test_close_without_db_or_exchange_position_resets_the_strategy():
    strat = BTCMeanReversionStrategy()
    strat.restore_state("short", 78834.0)
    runner, ctl, exchange = _runner([strat], repo=False)

    ok = await runner._execute_signal(_close_short(strat), 78000.0)

    assert ok is False
    exchange.place_order.assert_not_awaited()
    assert not strat.holds_position()
    assert _saved(ctl) == {"btc_mean_reversion": None}


@pytest.mark.asyncio
async def test_close_with_a_db_position_does_not_reset_before_closing():
    """Control: the reset is for the no-position branch only."""
    strat = BTCMeanReversionStrategy()
    strat.restore_state("short", 78834.0)
    runner, ctl, exchange = _runner(
        [strat], open_row=_row(strat.name, side="short"),
    )
    runner._reset_strategy_state = AsyncMock()
    exchange.place_order = AsyncMock(return_value=Order(
        id="o1", symbol="BTC", side="buy", size=0.01,
        order_type=OrderType.MARKET, filled_price=78000.0,
        status=OrderStatus.REJECTED,
    ))

    await runner._execute_signal(_close_short(strat), 78000.0)

    exchange.place_order.assert_awaited_once()
    runner._reset_strategy_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_ignored_close_keeps_the_cooldown_its_own_close_started():
    """Runtime resets go through reset_position: hash_momentum's own exit
    starts a 6-bar cooldown before the CLOSE reaches the runner, and
    reset_state() would have wiped it back to 999."""
    strat = HashMomentumStrategy()
    strat.restore_state("long", 180.0)
    strat._last_closed_bar_ts = pd.Timestamp(BAR_TS)
    strat._in_long = False          # what its SL/TP exit does ...
    strat._bars_since_close = 0     # ... before returning CLOSE_LONG
    runner, ctl, _ = _runner([strat], open_row=None)

    await runner._execute_signal(Signal(
        action=SignalAction.CLOSE_LONG, symbol="SOL",
        strategy_name=strat.name, reason="SL hit",
    ), 176.0)

    assert not strat.holds_position()
    assert strat._bars_since_close == 0
    assert strat._last_closed_bar_ts == BAR_TS
    assert _saved(ctl)["hash_momentum"]["bars_since_close"] == 0


# ----------------------------------------------------------------------
# Round trip: a normal close → snapshot → restart keeps the cooldown
# ----------------------------------------------------------------------


class _DictRedis:
    """The three Redis calls BotControl's snapshot methods make."""

    def __init__(self):
        self.data: dict[str, str] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value):
        self.data[key] = value

    async def delete(self, key):
        self.data.pop(key, None)


def _real_control(redis):
    ctl = BotControl(redis_url="redis://unused/0", mode="testnet")
    ctl._redis = redis
    return ctl


def _hm_setup(s):
    s.restore_state("long", 100.0)
    s._bars_since_close = 50
    s._last_closed_bar_ts = pd.Timestamp(BAR_TS)


def _hm_close(s):
    s._in_long = False           # hash_momentum's own SL/TP exit
    s._bars_since_close = 0


def _st_setup(s):
    s.restore_state("long", 100.0)
    s._last_entry_time = BAR_TS


def _vb_setup(s):
    s.restore_state("long", 100.0)
    s._last_trade_time = BAR_TS


def _qb_setup(s):
    s.restore_state("long", 100.0)


# name → (put in a position, the strategy's own exit transition,
#         "is the re-entry cooldown still running" one bar after the close)
ROUND_TRIP = {
    "hash_momentum": (
        _hm_setup, _hm_close,
        lambda s: s._bars_since_close < s.cooldown_bars,
    ),
    "supertrend": (
        _st_setup, lambda s: s._reset_position_state(),
        lambda s: s._last_entry_time is not None and (
            (BAR_TS + timedelta(days=1) - s._last_entry_time)
            / timedelta(days=1) <= s.cooldown_bars
        ),
    ),
    "volatility_breakout": (
        _vb_setup, lambda s: s._reset_position_state(),
        lambda s: s._last_trade_time is not None and (
            BAR_TS + timedelta(hours=1) - s._last_trade_time
            < timedelta(hours=s.cooldown_hours)
        ),
    ),
    "qullamagi_breakout": (
        _qb_setup, lambda s: s._reset(),
        lambda s: s._bars_since_flat <= s.cooldown_bars,
    ),
}


def test_every_cooldown_strategy_has_a_round_trip_case():
    load_all()
    declared = {
        n for n in list_strategies() if get_strategy(n).cooldown_attrs
    }
    assert declared == set(ROUND_TRIP)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(ROUND_TRIP))
async def test_cooldown_survives_close_snapshot_and_restart(name, caplog):
    load_all()
    setup, own_close, cooling = ROUND_TRIP[name]
    redis = _DictRedis()

    # A normal close through the runner, with a DB row to close.
    strat = get_strategy(name)
    setup(strat)
    own_close(strat)
    assert cooling(strat)
    runner, _, exchange = _runner(
        [strat], open_row=_row(name, symbol=strat.symbol, side="long",
                               entry=100.0),
    )
    runner.control = _real_control(redis)
    runner.repo.record_trade_and_close_position = AsyncMock()
    runner._check_parity_after_trade = AsyncMock(return_value=True)
    exchange.place_order = AsyncMock(return_value=Order(
        id="o1", symbol=strat.symbol, side="sell", size=0.01,
        order_type=OrderType.MARKET, filled_price=101.0,
        status=OrderStatus.FILLED,
    ))
    assert await runner._execute_signal(Signal(
        action=SignalAction.CLOSE_LONG, symbol=strat.symbol,
        strategy_name=name, reason="exit",
    ), 101.0)
    runner.repo.record_trade_and_close_position.assert_awaited_once()
    assert redis.data, f"{name}: the close left no snapshot"

    # Restart: a fresh instance, no open DB row, the same Redis.
    fresh = get_strategy(name)
    restarted, _, _ = _runner([fresh])
    restarted.control = _real_control(redis)
    with caplog.at_level(logging.INFO, logger="hypertrade.engine.runner"):
        await restarted._restore_strategies([])

    assert not fresh.holds_position()
    assert cooling(fresh), f"{name}: the cooldown did not survive a restart"
    assert "snapshot said in position" not in caplog.text


# ----------------------------------------------------------------------
# Flat-all pauses the bot
# ----------------------------------------------------------------------


def _tick_runner(*, flat_ok: bool, pause_fails: bool = False):
    state = {"paused": False}
    ctl = MagicMock()
    ctl.beat_heartbeat = AsyncMock()
    ctl.get_pending_flat_request = AsyncMock(return_value="tok-1")
    ctl.acknowledge_flat_request = AsyncMock()
    ctl.get_disabled_strategies = AsyncMock(return_value=set())
    ctl.get_all_leverage_overrides = AsyncMock(return_value={})
    ctl.save_strategy_state = AsyncMock()

    async def _set_paused(value):
        if pause_fails:
            raise ConnectionError("redis down")
        state["paused"] = value

    async def _is_paused():
        return state["paused"]

    ctl.set_paused = AsyncMock(side_effect=_set_paused)
    ctl.is_paused = AsyncMock(side_effect=_is_paused)

    exchange = MagicMock()
    if flat_ok:
        exchange.get_positions = AsyncMock(return_value=[])
    else:
        exchange.get_positions = AsyncMock(side_effect=ExchangeReadError("502"))
    exchange.get_balance = AsyncMock(side_effect=ExchangeReadError("skip"))
    bus = MagicMock()
    bus.publish = AsyncMock()
    strat = HashMomentumStrategy()
    runner = EngineRunner(
        exchange=exchange, strategies=[strat], repo=None,
        event_bus=bus, control=ctl,
    )
    runner._run_strategy = AsyncMock()
    return runner, ctl, bus, state


def _messages(bus):
    return [c.args[0].message for c in bus.publish.await_args_list
            if hasattr(c.args[0], "message")]


@pytest.mark.asyncio
async def test_completed_flat_all_pauses_the_bot_before_any_strategy_runs(caplog):
    runner, ctl, bus, state = _tick_runner(flat_ok=True)

    with caplog.at_level(logging.WARNING, logger="hypertrade.engine.runner"):
        await runner.tick()

    ctl.set_paused.assert_awaited_once_with(True)
    assert state["paused"] is True
    ctl.acknowledge_flat_request.assert_awaited_once_with("tok-1")
    runner._run_strategy.assert_not_awaited()  # no OPEN in the same tick
    assert any("bot is now PAUSED" in m for m in _messages(bus))
    assert "bot is now PAUSED" in caplog.text


@pytest.mark.asyncio
async def test_flat_all_holds_the_tick_even_if_the_pause_write_fails():
    runner, ctl, bus, _ = _tick_runner(flat_ok=True, pause_fails=True)

    await runner.tick()

    runner._run_strategy.assert_not_awaited()
    assert any("PAUSING THE BOT FAILED" in m for m in _messages(bus))


@pytest.mark.asyncio
async def test_failed_flat_all_does_not_pause():
    runner, ctl, _, _ = _tick_runner(flat_ok=False)

    await runner.tick()

    ctl.set_paused.assert_not_awaited()
    ctl.acknowledge_flat_request.assert_not_awaited()
    runner._run_strategy.assert_awaited()  # trading continues; retried next tick


# ----------------------------------------------------------------------
# A plain OPEN refused before anything is sent leaves the strategy flat
# ----------------------------------------------------------------------


def _open_short(strat):
    return Signal(
        action=SignalAction.OPEN_SHORT, symbol="BTC",
        strategy_name=strat.name, reason="z-score entry",
    )


def _refusing_runner(strat, *, risk_ok=True):
    """btc_mean_reversion flat in the DB; daily_long_0830 holds BTC."""
    runner, ctl, exchange = _runner([strat], open_row=None)
    runner.repo.get_open_positions = AsyncMock(return_value=[
        _row("daily_long_0830", side="long", entry=78000.0),
    ])
    ctl.get_allow_multi_coin = AsyncMock(return_value=False)
    runner.portfolio.check_risk_limits = AsyncMock(return_value=risk_ok)
    return runner, ctl, exchange


@pytest.mark.asyncio
async def test_open_refused_by_the_coin_gate_resets_the_strategy(caplog):
    strat = BTCMeanReversionStrategy()
    strat.restore_state("short", 78000.0)  # what it set when it signalled
    runner, ctl, exchange = _refusing_runner(strat)
    bar = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)

    with caplog.at_level(logging.INFO, logger="hypertrade.engine.runner"):
        ok = await runner._execute_signal(
            _open_short(strat), 78000.0, bar_time=bar,
        )
        # A standing condition re-emits on the next tick of the same bar.
        strat.restore_state("short", 78000.0)
        await runner._execute_signal(_open_short(strat), 78000.0, bar_time=bar)

    assert ok is False
    exchange.place_order.assert_not_awaited()
    assert not strat.holds_position()
    assert _saved(ctl) == {"btc_mean_reversion": None}
    notes = [
        r for r in caplog.records
        if r.levelno >= logging.INFO
        and "strategy reset to flat" in r.getMessage()
    ]
    assert len(notes) == 1  # logged once per (category, bar)
    assert "daily_long_0830 (long) already holds BTC" in notes[0].getMessage()


@pytest.mark.asyncio
async def test_open_refused_by_a_risk_limit_resets_the_strategy():
    strat = BTCMeanReversionStrategy()
    strat.restore_state("short", 78000.0)
    runner, ctl, exchange = _refusing_runner(strat, risk_ok=False)

    ok = await runner._execute_signal(_open_short(strat), 78000.0)

    assert ok is False
    exchange.place_order.assert_not_awaited()
    assert not strat.holds_position()
    assert _saved(ctl) == {"btc_mean_reversion": None}


@pytest.mark.asyncio
async def test_refused_open_keeps_the_cooldown():
    """The reset is reset_position(): a refused hash_momentum OPEN keeps
    its cooldown counter and bar baseline."""
    strat = HashMomentumStrategy()
    strat._bars_since_close = 7
    strat._last_closed_bar_ts = pd.Timestamp(BAR_TS)
    strat.restore_state("short", 180.0)
    runner, _, _ = _refusing_runner(strat)
    runner.repo.get_open_positions = AsyncMock(return_value=[
        _row("hash_supertrend", symbol="SOL", side="long", entry=180.0),
    ])

    await runner._execute_signal(Signal(
        action=SignalAction.OPEN_SHORT, symbol="SOL",
        strategy_name=strat.name, reason="momentum",
    ), 180.0)

    assert not strat.holds_position()
    assert strat._bars_since_close == 7
    assert strat._last_closed_bar_ts == BAR_TS
