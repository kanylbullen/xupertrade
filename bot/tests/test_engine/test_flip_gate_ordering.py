"""Open gates run BEFORE a flip closes anything, and opposite sides never
coexist on one coin (`bot/reports/analysis-2026-09-15.md` § 4).

- The synthesized flip-close used to run before the coin/family gate:
  the strategy was closed first, the re-open could then be refused, and
  the coin was left free for another strategy mid-tick. Now a flip whose
  open would be refused closes nothing, and the strategy is re-synced to
  the position it keeps.
- The coin gate read one arbitrary row (`get_open_position_any`, a bare
  `LIMIT 1`); it now reads every open row.
- With `allow_multi_coin=True`, a long and a short from two strategies on
  one coin net to ~0 on HL and the signed-net parity check passes. An
  OPEN opposite to another strategy's open row is refused whatever the
  flag says; same-side stacking stays allowed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from hypertrade.config import settings
from hypertrade.db import models
from hypertrade.db.repo import Repository
from hypertrade.engine.runner import EngineRunner
from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.exchange.base import Order, OrderStatus, OrderType


def _row(strategy, symbol="BTC", side="short", size=0.01, entry=50_000.0):
    r = MagicMock()
    r.strategy_name = strategy
    r.symbol = symbol
    r.side = side
    r.size = size
    r.entry_price = entry
    r.state_json = None
    return r


class _Strat:
    def __init__(self, name):
        self.name = name
        self.symbol = "BTC"
        self.timeframe = "1h"
        self.leverage = 1
        self.restored: list[tuple] = []

    def restore_state(self, side, entry_price):
        self.restored.append((side, entry_price))

    def restore_from_json(self, side, entry_price, state):
        self.restored.append((side, entry_price))

    def export_state(self):
        return None

    def on_filled(self, side, price):
        pass


def _runner(own_row, all_rows, *, allow_multi):
    """`own_row`: what repo.get_open_position returns for the signalling
    strategy (None = flat). `all_rows`: every open row."""
    strat = _Strat("hash_supertrend")
    repo = MagicMock()
    repo.get_open_position = AsyncMock(return_value=own_row)
    repo.get_open_positions = AsyncMock(return_value=list(all_rows))
    repo.record_trade_and_open_position = AsyncMock()
    repo.record_trade_and_close_position = AsyncMock()

    control = MagicMock()
    control.get_allow_multi_coin = AsyncMock(return_value=allow_multi)
    control.save_strategy_state = AsyncMock()

    exchange = MagicMock()
    exchange.update_leverage = AsyncMock(return_value=True)
    exchange.get_position = AsyncMock(return_value=None)

    async def _place(symbol, side, size, order_type=OrderType.MARKET):
        return Order(
            id=f"o{exchange.place_order.await_count}", symbol=symbol,
            side=side, size=size, order_type=order_type,
            filled_price=50_000.0, status=OrderStatus.FILLED,
        )

    exchange.place_order = AsyncMock(side_effect=_place)

    runner = EngineRunner(
        exchange=exchange, strategies=[strat], repo=repo,
        event_bus=None, control=control,
    )
    runner.portfolio = MagicMock()
    runner.portfolio.check_risk_limits = AsyncMock(return_value=True)
    runner.portfolio.record_pnl = AsyncMock()
    runner._check_parity_after_trade = AsyncMock(return_value=True)
    return runner, strat, exchange


def _open_long():
    return Signal(
        action=SignalAction.OPEN_LONG, symbol="BTC",
        strategy_name="hash_supertrend", reason="Supertrend flip BULLISH",
    )


@pytest.fixture(autouse=True)
def _no_exposure_cap(monkeypatch):
    monkeypatch.setattr(settings, "max_total_exposure_usd", 0)
    monkeypatch.setattr(settings, "taker_fee_rate", 0.0)


# ----------------------------------------------------------------------
# Flip ordering (allow_multi_coin=False)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flip_allowed_when_strategy_is_the_only_holder():
    own = _row("hash_supertrend", side="short")
    runner, strat, exchange = _runner(own, [own], allow_multi=False)

    ok = await runner._execute_signal(_open_long(), current_price=50_000.0)

    assert ok is True
    sides = [c.args[1] for c in exchange.place_order.await_args_list]
    assert sides == ["buy", "buy"], "close the short, then open the long"
    runner.repo.record_trade_and_close_position.assert_awaited_once()
    runner.repo.record_trade_and_open_position.assert_awaited_once()


@pytest.mark.asyncio
async def test_flip_refused_closes_nothing_when_another_strategy_holds_the_coin(caplog):
    own = _row("hash_supertrend", side="short")
    other = _row("daily_long_0830", side="long")
    runner, strat, exchange = _runner(own, [own, other], allow_multi=False)

    with caplog.at_level(logging.INFO, logger="hypertrade.engine.runner"):
        ok = await runner._execute_signal(_open_long(), current_price=50_000.0)

    assert ok is False
    exchange.place_order.assert_not_awaited()
    runner.repo.record_trade_and_close_position.assert_not_awaited()
    # The strategy asked for long; it is pointed back at the short it keeps.
    assert strat.restored == [("short", 50_000.0)]
    refusals = [r.message for r in caplog.records if "refused" in r.message]
    assert len(refusals) == 1
    assert "daily_long_0830" in refusals[0]


@pytest.mark.asyncio
async def test_repeated_refused_flip_is_logged_once(caplog):
    own = _row("hash_supertrend", side="short")
    other = _row("daily_long_0830", side="long")
    runner, strat, exchange = _runner(own, [own, other], allow_multi=False)

    with caplog.at_level(logging.INFO, logger="hypertrade.engine.runner"):
        for _ in range(3):
            await runner._execute_signal(_open_long(), current_price=50_000.0)

    refusals = [r for r in caplog.records if "refused" in r.message]
    assert len(refusals) == 1
    exchange.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_coin_gate_sees_other_holder_even_behind_own_row():
    """`get_open_position_any` could return the strategy's OWN row and
    let the gate pass; every row is read now. Plain open, different
    strategy: another strategy's row on the coin must block."""
    mine = _row("hash_supertrend", symbol="BTC", side="long")
    other = _row("daily_long_0830", symbol="BTC", side="long")
    runner, strat, exchange = _runner(None, [mine, other], allow_multi=False)
    runner.repo.get_open_position_any = AsyncMock(return_value=mine)

    ok = await runner._execute_signal(_open_long(), current_price=50_000.0)

    assert ok is False
    exchange.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_kill_switch_refused_flip_keeps_and_resyncs():
    """A flip refused by the risk check keeps the position too, and the
    strategy is pointed back at it."""
    own = _row("hash_supertrend", side="short")
    runner, strat, exchange = _runner(own, [own], allow_multi=False)
    runner.portfolio.check_risk_limits = AsyncMock(return_value=False)

    ok = await runner._execute_signal(_open_long(), current_price=50_000.0)

    assert ok is False
    exchange.place_order.assert_not_awaited()
    assert strat.restored == [("short", 50_000.0)]


# ----------------------------------------------------------------------
# Opposite side on one coin (allow_multi_coin=True)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_opposite_side_refused_even_with_allow_multi(caplog):
    other = _row("daily_long_0830", side="short")
    runner, strat, exchange = _runner(None, [other], allow_multi=True)

    with caplog.at_level(logging.INFO, logger="hypertrade.engine.runner"):
        ok = await runner._execute_signal(_open_long(), current_price=50_000.0)

    assert ok is False
    exchange.place_order.assert_not_awaited()
    msg = next(r.message for r in caplog.records if "opposite side" in r.message)
    assert "hash_supertrend" in msg and "daily_long_0830" in msg


@pytest.mark.asyncio
async def test_same_side_stacking_still_allowed_with_allow_multi():
    other = _row("daily_long_0830", side="long")
    runner, strat, exchange = _runner(None, [other], allow_multi=True)

    ok = await runner._execute_signal(_open_long(), current_price=50_000.0)

    assert ok is True
    exchange.place_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_flip_into_another_strategys_opposite_side_is_refused():
    """allow_multi_coin=True: A holds long and flips to short while B also
    holds long. The new short would net against B's long, so the flip is
    refused before A's long is closed, and A is re-synced to its long."""
    own = _row("hash_supertrend", side="long")
    other = _row("daily_long_0830", side="long")
    runner, strat, exchange = _runner(own, [own, other], allow_multi=True)
    sig = Signal(
        action=SignalAction.OPEN_SHORT, symbol="BTC",
        strategy_name="hash_supertrend", reason="Supertrend flip BEARISH",
    )

    ok = await runner._execute_signal(sig, current_price=50_000.0)

    assert ok is False
    exchange.place_order.assert_not_awaited()
    assert strat.restored == [("long", 50_000.0)]


@pytest.mark.asyncio
async def test_no_control_means_allow_multi_coin_false():
    """Without BotControl the flag reads as its default, False — the gate
    no longer silently switches off."""
    other = _row("daily_long_0830", side="long")
    runner, strat, exchange = _runner(None, [other], allow_multi=True)
    runner.control = None

    ok = await runner._execute_signal(_open_long(), current_price=50_000.0)

    assert ok is False
    exchange.place_order.assert_not_awaited()


# ----------------------------------------------------------------------
# get_open_position_any is deterministic
# ----------------------------------------------------------------------


@pytest.fixture
async def repo():
    r = Repository("sqlite+aiosqlite:///:memory:")
    r._mode = "testnet"
    r._is_paper = False
    await r.init_db()
    yield r
    await r._engine.dispose()


@pytest.mark.asyncio
async def test_get_open_position_any_returns_oldest(repo):
    now = datetime.now(timezone.utc)
    newer = await repo.open_position(
        strategy_name="newer", symbol="BTC", side="long", size=1.0, entry_price=1.0,
    )
    older = await repo.open_position(
        strategy_name="older", symbol="BTC", side="long", size=1.0, entry_price=1.0,
    )
    async with repo._session_factory() as session:
        (await session.get(models.PositionRecord, newer.id)).opened_at = now
        (await session.get(models.PositionRecord, older.id)).opened_at = (
            now - timedelta(hours=2)
        )
        await session.commit()

    got = await repo.get_open_position_any("BTC")
    assert got.strategy_name == "older"
