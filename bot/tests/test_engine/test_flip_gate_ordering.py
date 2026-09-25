"""Open gates run BEFORE a flip closes anything, opposite sides never
coexist on one coin, and a flip never leaves the strategy believing in a
position the DB does not hold (`bot/reports/analysis-2026-09-15.md` § 4,
plus the PR #172 review).

- The synthesized flip-close used to run before the coin/family gate:
  the strategy was closed first, the re-open could then be refused, and
  the coin was left free for another strategy mid-tick. Now a flip whose
  open a gate refuses (coin, family, opposite side, exposure, size
  ceiling, leverage push, or a gate that could not be read) closes
  nothing, and the strategy is re-synced to the position it keeps.
- A risk-limit refusal (kill switch, daily-loss cap) is different:
  closing reduces risk, and for hash_supertrend/kalman_breakout the flip
  is the only exit. The close goes through, the open is refused, and the
  strategy is reset to flat.
- Every refusal is logged and published once per (category, bar), so an
  edge-triggered strategy re-emitting on the same bar does not spam.
- The coin gate read one arbitrary row (`get_open_position_any`, a bare
  `LIMIT 1`); it now reads every open row.
- With `allow_multi_coin=True`, a long and a short from two strategies on
  one coin net to ~0 on HL and the signed-net parity check passes. An
  OPEN opposite to another strategy's open row is refused whatever the
  flag says; same-side stacking stays allowed.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pandas as pd
import pytest

from hypertrade.config import settings
from hypertrade.db import models
from hypertrade.db.repo import Repository
from hypertrade.engine.runner import EngineRunner
from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.events.types import ErrorOccurred
from hypertrade.exchange.base import Order, OrderStatus, OrderType

BAR_1 = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
BAR_2 = BAR_1 + timedelta(hours=1)


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
        self.resets = 0

    def restore_state(self, side, entry_price):
        self.restored.append((side, entry_price))

    def restore_from_json(self, side, entry_price, state):
        self.restored.append((side, entry_price))

    def reset_state(self):
        self.resets += 1

    def reset_position(self):
        # The runner resets through reset_position (keeps cooldown).
        self.resets += 1

    def export_state(self):
        return None

    def on_filled(self, side, price):
        pass


def _runner(own_row, others, *, allow_multi, fills=None, strategy=None):
    """`own_row`: the signalling strategy's open row (None = flat); it is
    cleared when a close is booked and replaced when an open is booked.
    `others`: other strategies' open rows. `fills`: order status per
    place_order call (default: every order fills)."""
    strat = strategy or _Strat("hash_supertrend")
    state = {"own": own_row}
    statuses = list(fills or [])

    repo = MagicMock()

    async def _get_open_position(name, symbol):
        return state["own"]

    async def _get_open_positions():
        return list(others) + ([state["own"]] if state["own"] else [])

    async def _book_close(**kw):
        state["own"] = None

    async def _book_open(**kw):
        state["own"] = _row(kw["strategy_name"], side=kw["position_side"])

    repo.get_open_position = AsyncMock(side_effect=_get_open_position)
    repo.get_open_positions = AsyncMock(side_effect=_get_open_positions)
    repo.record_trade_and_close_position = AsyncMock(side_effect=_book_close)
    repo.record_trade_and_open_position = AsyncMock(side_effect=_book_open)

    control = MagicMock()
    control.get_allow_multi_coin = AsyncMock(return_value=allow_multi)
    control.save_strategy_state = AsyncMock()

    exchange = MagicMock()
    exchange.update_leverage = AsyncMock(return_value=True)
    exchange.get_position = AsyncMock(return_value=None)

    async def _place(symbol, side, size, order_type=OrderType.MARKET):
        status = statuses.pop(0) if statuses else OrderStatus.FILLED
        return Order(
            id=f"o{exchange.place_order.await_count}", symbol=symbol,
            side=side, size=size, order_type=order_type,
            filled_price=50_000.0, status=status,
        )

    exchange.place_order = AsyncMock(side_effect=_place)
    bus = MagicMock()
    bus.publish = AsyncMock(return_value=True)

    runner = EngineRunner(
        exchange=exchange, strategies=[strat], repo=repo,
        event_bus=bus, control=control,
    )
    runner.portfolio = MagicMock()
    runner.portfolio.check_risk_limits = AsyncMock(return_value=True)
    runner.portfolio.record_pnl = AsyncMock()
    runner.portfolio.opens_held = None  # no reconcile hold (NU-2)
    runner._check_parity_after_trade = AsyncMock(return_value=True)
    return runner, strat, exchange, repo, bus


def _risk_blocks_opens(runner):
    async def _check(is_open=True):
        return not is_open
    runner.portfolio.check_risk_limits = AsyncMock(side_effect=_check)


def _errors(bus):
    return [
        c.args[0] for c in bus.publish.await_args_list
        if isinstance(c.args[0], ErrorOccurred)
    ]


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
# Gate refusals close nothing (allow_multi_coin=False)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flip_allowed_when_strategy_is_the_only_holder():
    own = _row("hash_supertrend", side="short")
    runner, strat, exchange, repo, bus = _runner(own, [], allow_multi=False)

    ok = await runner._execute_signal(_open_long(), current_price=50_000.0)

    assert ok is True
    sides = [c.args[1] for c in exchange.place_order.await_args_list]
    assert sides == ["buy", "buy"], "close the short, then open the long"
    repo.record_trade_and_close_position.assert_awaited_once()
    repo.record_trade_and_open_position.assert_awaited_once()


@pytest.mark.asyncio
async def test_flip_refused_closes_nothing_when_another_strategy_holds_the_coin(caplog):
    own = _row("hash_supertrend", side="short")
    other = _row("daily_long_0830", side="long")
    runner, strat, exchange, repo, bus = _runner(own, [other], allow_multi=False)

    with caplog.at_level(logging.INFO, logger="hypertrade.engine.runner"):
        ok = await runner._execute_signal(_open_long(), current_price=50_000.0)

    assert ok is False
    exchange.place_order.assert_not_awaited()
    repo.record_trade_and_close_position.assert_not_awaited()
    # The strategy asked for long; it is pointed back at the short it keeps.
    assert strat.restored == [("short", 50_000.0)]
    refusals = [r.message for r in caplog.records if "refused" in r.message]
    assert len(refusals) == 1
    assert "daily_long_0830" in refusals[0]


@pytest.mark.asyncio
async def test_repeated_refused_flip_is_logged_once(caplog):
    own = _row("hash_supertrend", side="short")
    other = _row("daily_long_0830", side="long")
    runner, strat, exchange, repo, bus = _runner(own, [other], allow_multi=False)

    with caplog.at_level(logging.INFO, logger="hypertrade.engine.runner"):
        for _ in range(3):
            await runner._execute_signal(
                _open_long(), current_price=50_000.0, bar_time=BAR_1,
            )

    refusals = [r for r in caplog.records if "refused" in r.message]
    assert len(refusals) == 1
    exchange.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_refusal_on_a_later_bar_is_logged_again(caplog):
    own = _row("hash_supertrend", side="short")
    other = _row("daily_long_0830", side="long")
    runner, strat, exchange, repo, bus = _runner(own, [other], allow_multi=False)

    with caplog.at_level(logging.INFO, logger="hypertrade.engine.runner"):
        for bar in (BAR_1, BAR_1, BAR_2):
            await runner._execute_signal(
                _open_long(), current_price=50_000.0, bar_time=bar,
            )

    refusals = [
        r for r in caplog.records
        if "refused" in r.message and r.levelno >= logging.WARNING
    ]
    assert len(refusals) == 2, "once for BAR_1, once more for BAR_2"


@pytest.mark.asyncio
async def test_without_bar_time_a_note_expires(caplog, monkeypatch):
    own = _row("hash_supertrend", side="short")
    other = _row("daily_long_0830", side="long")
    runner, strat, exchange, repo, bus = _runner(own, [other], allow_multi=False)
    clock = {"t": 1_000.0}
    monkeypatch.setattr(
        "hypertrade.engine.runner.time.monotonic", lambda: clock["t"],
    )

    with caplog.at_level(logging.INFO, logger="hypertrade.engine.runner"):
        await runner._execute_signal(_open_long(), current_price=50_000.0)
        clock["t"] += runner._REFUSAL_NOTE_TTL_SECONDS - 1
        await runner._execute_signal(_open_long(), current_price=50_000.0)
        clock["t"] += 2
        await runner._execute_signal(_open_long(), current_price=50_000.0)

    refusals = [
        r for r in caplog.records
        if "refused" in r.message and r.levelno >= logging.WARNING
    ]
    assert len(refusals) == 2


@pytest.mark.asyncio
async def test_exposure_refusal_dedups_on_category_not_numbers(caplog, monkeypatch):
    """The message carries live dollar amounts; the dedup key must not."""
    monkeypatch.setattr(settings, "max_total_exposure_usd", 100)
    own = _row("hash_supertrend", side="short")
    other = _row("eth_strat", symbol="ETH", side="long", size=1.0, entry=2_000.0)
    runner, strat, exchange, repo, bus = _runner(own, [other], allow_multi=True)

    with caplog.at_level(logging.INFO, logger="hypertrade.engine.runner"):
        for price in (50_000.0, 50_100.0, 49_900.0):
            await runner._execute_signal(
                _open_long(), current_price=price, bar_time=BAR_1,
            )

    refusals = [
        r for r in caplog.records
        if "exposure cap" in r.message and r.levelno >= logging.WARNING
    ]
    assert len(refusals) == 1
    exchange.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_coin_gate_sees_other_holder_even_behind_own_row():
    """`get_open_position_any` could return the strategy's OWN row and
    let the gate pass; every row is read now. Plain open, different
    strategy: another strategy's row on the coin must block."""
    mine = _row("hash_supertrend", symbol="BTC", side="long")
    other = _row("daily_long_0830", symbol="BTC", side="long")
    runner, strat, exchange, repo, bus = _runner(None, [mine, other], allow_multi=False)
    repo.get_open_position_any = AsyncMock(return_value=mine)

    ok = await runner._execute_signal(_open_long(), current_price=50_000.0)

    assert ok is False
    exchange.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_read_failure_during_flip_resyncs_and_refuses(caplog):
    """A DB/Redis read failing inside the gates must not strand the
    strategy on the side it asked for while the old position is kept."""
    own = _row("hash_supertrend", side="short")
    runner, strat, exchange, repo, bus = _runner(own, [], allow_multi=False)
    runner.control.get_allow_multi_coin = AsyncMock(
        side_effect=ConnectionError("redis down"),
    )

    with caplog.at_level(logging.INFO, logger="hypertrade.engine.runner"):
        ok = await runner._execute_signal(_open_long(), current_price=50_000.0)

    assert ok is False
    exchange.place_order.assert_not_awaited()
    assert strat.restored == [("short", 50_000.0)]
    assert any("gate read failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_gate_read_failure_on_plain_open_still_raises():
    runner, strat, exchange, repo, bus = _runner(None, [], allow_multi=False)
    repo.get_open_positions = AsyncMock(side_effect=ConnectionError("db down"))

    with pytest.raises(ConnectionError):
        await runner._execute_signal(_open_long(), current_price=50_000.0)
    exchange.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_leverage_push_failure_refuses_whole_flip():
    """The push runs before the flip's close now: a failure closes
    nothing and re-syncs, instead of leaving the book flat."""
    own = _row("hash_supertrend", side="short")
    runner, strat, exchange, repo, bus = _runner(own, [], allow_multi=False)
    exchange.update_leverage = AsyncMock(return_value=False)

    ok = await runner._execute_signal(_open_long(), current_price=50_000.0)

    assert ok is False
    exchange.place_order.assert_not_awaited()
    repo.record_trade_and_close_position.assert_not_awaited()
    assert strat.restored == [("short", 50_000.0)]


# ----------------------------------------------------------------------
# Risk-limit refusal: close half goes through, open refused, reset flat
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_risk_refused_flip_still_closes_and_resets_flat():
    own = _row("hash_supertrend", side="short")
    runner, strat, exchange, repo, bus = _runner(own, [], allow_multi=False)
    _risk_blocks_opens(runner)

    ok = await runner._execute_signal(
        _open_long(), current_price=50_000.0, bar_time=BAR_1,
    )

    assert ok is False
    sides = [c.args[1] for c in exchange.place_order.await_args_list]
    assert sides == ["buy"], "the short is closed, no long is opened"
    repo.record_trade_and_close_position.assert_awaited_once()
    repo.record_trade_and_open_position.assert_not_awaited()
    assert strat.resets == 1
    assert strat.restored == []
    errors = _errors(bus)
    assert len(errors) == 1 and "Risk limit" in errors[0].message


@pytest.mark.asyncio
async def test_risk_refusal_publishes_once_across_ticks_on_one_bar():
    """After the reset the strategy is flat and re-emits the open on every
    tick of the same bar; only the first refusal reaches Telegram."""
    own = _row("hash_supertrend", side="short")
    runner, strat, exchange, repo, bus = _runner(own, [], allow_multi=False)
    _risk_blocks_opens(runner)

    for _ in range(4):
        await runner._execute_signal(
            _open_long(), current_price=50_000.0, bar_time=BAR_1,
        )
    assert len(_errors(bus)) == 1
    assert exchange.place_order.await_count == 1, "one close, no opens"

    await runner._execute_signal(
        _open_long(), current_price=50_000.0, bar_time=BAR_2,
    )
    assert len(_errors(bus)) == 2, "a new bar is news"


@pytest.mark.asyncio
async def test_real_hash_supertrend_risk_refused_flip(monkeypatch):
    """The reviewer's reproduction with the real strategy: a bearish
    Supertrend flip while long, kill switch on. One close, one alert over
    three ticks on the same bar, and the strategy ends flat."""
    from hypertrade.strategies.registry import get_strategy, load_all

    load_all()
    s = get_strategy("hash_supertrend")
    n = 120
    close = np.concatenate([np.linspace(100, 160, n - 1), [120.0]])
    df = pd.DataFrame({
        "timestamp": [BAR_1 + timedelta(hours=i) for i in range(n)],
        "open": close, "high": close + 0.5, "low": close - 0.5,
        "close": close, "volume": np.ones(n),
    })
    s.restore_from_json(
        "long", 110.0, {"position_side": "long", "entry_price": 110.0},
    )
    own = _row(s.name, symbol=s.symbol, side="long", entry=110.0)
    own.state_json = json.dumps({"position_side": "long", "entry_price": 110.0})
    runner, _, exchange, repo, bus = _runner(
        own, [], allow_multi=False, strategy=s,
    )
    _risk_blocks_opens(runner)
    bar = df["timestamp"].iloc[-1]

    sig = await s.on_candle(df)
    assert sig is not None and sig.action == SignalAction.OPEN_SHORT
    await runner._execute_signal(sig, 120.0, bar_time=bar)
    assert s._position_side is None, "closed long, open refused → flat"

    for _ in range(2):
        sig = await s.on_candle(df)
        if sig is not None:
            await runner._execute_signal(sig, 120.0, bar_time=bar)

    assert len(_errors(bus)) == 1
    assert [c.args[1] for c in exchange.place_order.await_args_list] == ["sell"]


# ----------------------------------------------------------------------
# A flip that fails half-way leaves the strategy matching the DB
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flip_close_not_filled_resyncs_and_alerts_once():
    own = _row("hash_supertrend", side="short")
    runner, strat, exchange, repo, bus = _runner(
        own, [], allow_multi=False,
        fills=[OrderStatus.REJECTED] * 3,
    )

    for _ in range(3):
        ok = await runner._execute_signal(
            _open_long(), current_price=50_000.0, bar_time=BAR_1,
        )
        assert ok is False

    assert exchange.place_order.await_count == 3, "one close attempt per tick"
    repo.record_trade_and_open_position.assert_not_awaited()
    assert strat.restored == [("short", 50_000.0)] * 3
    errors = _errors(bus)
    assert len(errors) == 1 and "Flip-close failed" in errors[0].message


@pytest.mark.asyncio
async def test_open_not_filled_after_flip_close_resets_flat():
    own = _row("hash_supertrend", side="short")
    runner, strat, exchange, repo, bus = _runner(
        own, [], allow_multi=False,
        fills=[OrderStatus.FILLED, OrderStatus.REJECTED],
    )

    ok = await runner._execute_signal(_open_long(), current_price=50_000.0)

    assert ok is False
    repo.record_trade_and_close_position.assert_awaited_once()
    repo.record_trade_and_open_position.assert_not_awaited()
    assert strat.resets == 1
    assert len(_errors(bus)) == 1


@pytest.mark.asyncio
async def test_open_raising_after_flip_close_resets_flat_and_reraises():
    own = _row("hash_supertrend", side="short")
    runner, strat, exchange, repo, bus = _runner(own, [], allow_multi=False)
    real_place = exchange.place_order.side_effect

    async def _place(symbol, side, size, order_type=OrderType.MARKET):
        if exchange.place_order.await_count == 2:
            raise TimeoutError("open timed out")
        return await real_place(symbol, side, size, order_type)

    exchange.place_order.side_effect = _place

    with pytest.raises(TimeoutError):
        await runner._execute_signal(_open_long(), current_price=50_000.0)
    repo.record_trade_and_close_position.assert_awaited_once()
    assert strat.resets == 1


# ----------------------------------------------------------------------
# Opposite side on one coin (allow_multi_coin=True)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_opposite_side_refused_even_with_allow_multi(caplog):
    other = _row("daily_long_0830", side="short")
    runner, strat, exchange, repo, bus = _runner(None, [other], allow_multi=True)

    with caplog.at_level(logging.INFO, logger="hypertrade.engine.runner"):
        ok = await runner._execute_signal(_open_long(), current_price=50_000.0)

    assert ok is False
    exchange.place_order.assert_not_awaited()
    msg = next(r.message for r in caplog.records if "opposite side" in r.message)
    assert "hash_supertrend" in msg and "daily_long_0830" in msg


@pytest.mark.asyncio
async def test_same_side_stacking_still_allowed_with_allow_multi():
    other = _row("daily_long_0830", side="long")
    runner, strat, exchange, repo, bus = _runner(None, [other], allow_multi=True)

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
    runner, strat, exchange, repo, bus = _runner(own, [other], allow_multi=True)
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
    runner, strat, exchange, repo, bus = _runner(None, [other], allow_multi=True)
    runner.control = None

    ok = await runner._execute_signal(_open_long(), current_price=50_000.0)

    assert ok is False
    exchange.place_order.assert_not_awaited()


# ----------------------------------------------------------------------
# get_open_position_any is deterministic
# ----------------------------------------------------------------------


@pytest.fixture
async def repo_db():
    r = Repository("sqlite+aiosqlite:///:memory:")
    r._mode = "testnet"
    r._is_paper = False
    await r.init_db()
    yield r
    await r._engine.dispose()


@pytest.mark.asyncio
async def test_get_open_position_any_returns_oldest(repo_db):
    now = datetime.now(timezone.utc)
    newer = await repo_db.open_position(
        strategy_name="newer", symbol="BTC", side="long", size=1.0, entry_price=1.0,
    )
    older = await repo_db.open_position(
        strategy_name="older", symbol="BTC", side="long", size=1.0, entry_price=1.0,
    )
    async with repo_db._session_factory() as session:
        (await session.get(models.PositionRecord, newer.id)).opened_at = now
        (await session.get(models.PositionRecord, older.id)).opened_at = (
            now - timedelta(hours=2)
        )
        await session.commit()

    got = await repo_db.get_open_position_any("BTC")
    assert got.strategy_name == "older"
