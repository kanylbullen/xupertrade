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
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from hypertrade.engine.control import BotControl
from hypertrade.engine.runner import EngineRunner
from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.exchange.base import Order, OrderStatus, OrderType, Position
from hypertrade.strategies.btc_mean_reversion import BTCMeanReversionStrategy
from hypertrade.strategies.hash_momentum import HashMomentumStrategy

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
