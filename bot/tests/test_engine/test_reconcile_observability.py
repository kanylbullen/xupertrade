"""The runner half of the reconcile fix.

`bot/reports/analysis-2026-09-15.md` § 2 and § 4:

- Reconcile results only ever reached `logger.warning`, never the event
  bus, so Telegram stayed silent while the book was being flattened.
- Reconcile ran BEFORE the tick's `paused` check (runner.py:312 vs
  :433), so pausing the bot did not stop pass 2 market-closing the very
  positions the pause existed to preserve.
- Flat-all acknowledged the operator's request even when
  `get_positions()` had failed or some closes had not filled.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hypertrade.db.repo import ReconcileResult
from hypertrade.engine.runner import EngineRunner, _is_transient_network_error
from hypertrade.events.types import ErrorOccurred
from hypertrade.exchange.base import (
    ExchangeReadError,
    Order,
    OrderStatus,
    OrderType,
    Position,
)


def _runner(*, result=None, paused=False, pause_raises=False):
    repo = MagicMock()
    repo.reconcile_positions = AsyncMock(
        return_value=result if result is not None else ReconcileResult()
    )
    control = MagicMock()
    if pause_raises:
        control.is_paused = AsyncMock(side_effect=RuntimeError("redis down"))
    else:
        control.is_paused = AsyncMock(return_value=paused)
    event_bus = MagicMock()
    event_bus.publish = AsyncMock()
    runner = EngineRunner(
        exchange=MagicMock(), strategies=[], repo=repo,
        event_bus=event_bus, control=control,
    )
    runner.portfolio = MagicMock()
    runner.portfolio.record_pnl = AsyncMock()
    return runner, repo, event_bus


def _published(event_bus) -> list:
    return [c.args[0] for c in event_bus.publish.await_args_list]


# --- pause gating -----------------------------------------------------


@pytest.mark.asyncio
async def test_paused_bot_reconciles_as_a_dry_run():
    """A pause has to stop WRITES, not just orders. Pass 1 was still
    closing rows, writing Trades and booking PnL while paused."""
    runner, repo, _ = _runner(paused=True)
    await runner._run_reconcile("Periodic")
    kwargs = repo.reconcile_positions.await_args.kwargs
    assert kwargs["dry_run"] is True
    assert kwargs["close_exchange_orphans"] is False


@pytest.mark.asyncio
async def test_running_bot_still_closes_exchange_orphans():
    runner, repo, _ = _runner(paused=False)
    await runner._run_reconcile("Periodic")
    kwargs = repo.reconcile_positions.await_args.kwargs
    assert kwargs["dry_run"] is False
    assert kwargs["close_exchange_orphans"] is True


@pytest.mark.asyncio
async def test_unreadable_pause_flag_is_treated_as_paused():
    """Fail safe: a Redis blip must not turn into writes or orders."""
    runner, repo, _ = _runner(pause_raises=True)
    await runner._run_reconcile("Periodic")
    kwargs = repo.reconcile_positions.await_args.kwargs
    assert kwargs["dry_run"] is True
    assert kwargs["close_exchange_orphans"] is False


# --- the startup reconcile (review item 4) ----------------------------


@pytest.mark.asyncio
async def test_startup_reconciles_before_restoring_state():
    """The boot reconcile moved out of `main.py` into `startup()`, where
    the pause gate, the PnL callback and the event bus exist. It must
    still run BEFORE state restoration, or a strategy gets restored into
    a row reconcile is about to close."""
    order: list[str] = []
    runner, repo, _ = _runner(paused=False)

    async def reconcile(*_a, **_k):
        order.append("reconcile")
        return ReconcileResult()

    async def get_open_positions():
        order.append("restore")
        return []

    repo.reconcile_positions = AsyncMock(side_effect=reconcile)
    repo.get_open_positions = AsyncMock(side_effect=get_open_positions)

    await runner.startup()

    assert order == ["reconcile", "restore"]


@pytest.mark.asyncio
async def test_startup_reconcile_respects_the_pause_gate():
    runner, repo, _ = _runner(paused=True)
    repo.get_open_positions = AsyncMock(return_value=[])

    await runner.startup()

    kwargs = repo.reconcile_positions.await_args.kwargs
    assert kwargs["dry_run"] is True
    assert kwargs["close_exchange_orphans"] is False


@pytest.mark.asyncio
async def test_startup_reconcile_books_pnl_through_the_callback():
    """The `main.py` version had no callback at all, so a boot-time
    close never reached the daily-loss counter."""
    runner, repo, _ = _runner(paused=False)
    repo.get_open_positions = AsyncMock(return_value=[])

    await runner.startup()

    cb = repo.reconcile_positions.await_args.kwargs["on_strategy_close"]
    assert cb == runner._on_reconcile_close


@pytest.mark.asyncio
async def test_startup_survives_a_failing_reconcile():
    """A reconcile that blows up must not stop the bot booting."""
    runner, repo, _ = _runner()
    repo.reconcile_positions = AsyncMock(side_effect=RuntimeError("boom"))
    repo.get_open_positions = AsyncMock(return_value=[])

    await runner.startup()

    repo.get_open_positions.assert_awaited_once()


# --- event publishing -------------------------------------------------


@pytest.mark.asyncio
async def test_actions_are_published():
    result = ReconcileResult(
        actions=["closed orphan: hash_momentum long 1.0 BTC @ 110 (fill), pnl -3.2"],
        db_open=1,
    )
    runner, _, event_bus = _runner(result=result)

    await runner._run_reconcile("Periodic")

    events = _published(event_bus)
    assert len(events) == 1
    assert isinstance(events[0], ErrorOccurred)
    assert events[0].strategy == "reconcile"
    assert "hash_momentum" in events[0].message


@pytest.mark.asyncio
async def test_read_failure_skip_is_published():
    """A silent reconcile is exactly how this went unnoticed since May."""
    result = ReconcileResult(skipped="exchange read failed: 502 Bad Gateway")
    runner, _, event_bus = _runner(result=result)

    await runner._run_reconcile("Periodic")

    events = _published(event_bus)
    assert len(events) == 1
    assert "skipped" in events[0].message
    assert "502" in events[0].message


@pytest.mark.asyncio
async def test_failures_are_published_too():
    result = ReconcileResult(
        failures=["FAILED to close exchange-orphan long 2.0 ETH: order status rejected"],
    )
    runner, _, event_bus = _runner(result=result)

    await runner._run_reconcile("Periodic")

    events = _published(event_bus)
    assert len(events) == 1
    assert "rejected" in events[0].message


@pytest.mark.asyncio
async def test_identical_outcome_is_published_once():
    """An unpriceable row, or a multi-hour HL outage, repeats verbatim
    every 5 minutes. CLAUDE.md § 6: Telegram is for humans."""
    result = ReconcileResult(skipped="exchange read failed: 502 Bad Gateway")
    runner, _, event_bus = _runner(result=result)

    await runner._run_reconcile("Periodic")
    await runner._run_reconcile("Periodic")
    await runner._run_reconcile("Periodic")

    assert event_bus.publish.await_count == 1


@pytest.mark.asyncio
async def test_a_clean_pass_rearms_the_notice():
    """Recovery followed by a new failure must ping again."""
    runner, repo, event_bus = _runner(
        result=ReconcileResult(skipped="exchange read failed: 502")
    )
    await runner._run_reconcile("Periodic")
    repo.reconcile_positions = AsyncMock(return_value=ReconcileResult())
    await runner._run_reconcile("Periodic")
    repo.reconcile_positions = AsyncMock(
        return_value=ReconcileResult(skipped="exchange read failed: 502")
    )
    await runner._run_reconcile("Periodic")

    assert event_bus.publish.await_count == 2


@pytest.mark.asyncio
async def test_clean_pass_publishes_nothing():
    """Telegram is for humans (CLAUDE.md § 6) — a clean pass is a log
    line, not a ping."""
    runner, _, event_bus = _runner(result=ReconcileResult(db_open=2, exchange_open=2))
    await runner._run_reconcile("Periodic")
    assert event_bus.publish.await_count == 0


@pytest.mark.asyncio
async def test_the_error_message_is_html_escaped_by_the_formatter():
    """`ErrorOccurred.strategy` is NOT escaped by the Telegram formatter
    (analysis § 4, Low), so reconcile passes a fixed literal and puts
    everything variable in `message`, which IS escaped."""
    from hypertrade.notify.telegram import _format_event

    rendered = _format_event({
        "type": "error",
        "strategy": "reconcile",
        "message": "closed orphan: <b>strat</b> & co",
    })
    assert "&lt;b&gt;strat&lt;/b&gt; &amp; co" in rendered
    assert "<b>strat</b>" not in rendered


# --- the reconcile close reaches the portfolio ------------------------


@pytest.mark.asyncio
async def test_reconcile_close_books_pnl_into_the_portfolio():
    """Without this the MAX_DAILY_LOSS_USD counter never sees the
    losses these closes represent."""
    runner, _, _ = _runner()
    strategy = MagicMock()
    strategy.name = "hash_momentum"
    runner.strategies = [strategy]

    await runner._on_reconcile_close("hash_momentum", -12.5)

    strategy.reset_state.assert_called_once()
    runner.portfolio.record_pnl.assert_awaited_once_with(-12.5)


@pytest.mark.asyncio
async def test_reconcile_close_without_pnl_only_resets_state():
    runner, _, _ = _runner()
    strategy = MagicMock()
    strategy.name = "hash_momentum"
    runner.strategies = [strategy]

    await runner._on_reconcile_close("hash_momentum", None)

    strategy.reset_state.assert_called_once()
    runner.portfolio.record_pnl.assert_not_awaited()


# --- flat-all ---------------------------------------------------------


def _flat_runner(exchange):
    repo = MagicMock()
    repo.get_open_positions_for_symbol = AsyncMock(return_value=[])
    repo.record_trade = AsyncMock()
    repo.record_trade_and_close_position = AsyncMock()
    control = MagicMock()
    control.get_pending_flat_request = AsyncMock(return_value="tok-1")
    control.acknowledge_flat_request = AsyncMock()
    runner = EngineRunner(
        exchange=exchange, strategies=[], repo=repo,
        event_bus=None, control=control,
    )
    runner.portfolio = MagicMock()
    runner.portfolio.record_pnl = AsyncMock()
    return runner, control


@pytest.mark.asyncio
async def test_flat_all_read_failure_is_not_ok():
    exchange = MagicMock()
    exchange.get_positions = AsyncMock(
        side_effect=ExchangeReadError("get_positions failed: 502")
    )
    runner, _ = _flat_runner(exchange)

    status = await runner._flat_all_positions()

    assert status.ok is False
    assert "read failed" in status.reason
    exchange.place_order.assert_not_called()


def _tick_control(pending="tok-1"):
    """Just enough BotControl for one `tick()` with repo=None."""
    control = MagicMock()
    control.beat_heartbeat = AsyncMock()
    control.get_pending_flat_request = AsyncMock(return_value=pending)
    control.acknowledge_flat_request = AsyncMock()
    control.is_paused = AsyncMock(return_value=True)  # skip the strategy loop
    control.get_disabled_strategies = AsyncMock(return_value=set())
    control.get_all_leverage_overrides = AsyncMock(return_value={})
    return control


@pytest.mark.asyncio
async def test_tick_does_not_acknowledge_flat_all_on_read_failure():
    """The whole point, driven through the real tick: an unacknowledged
    request stays pending and retries, instead of telling the operator
    the book is flat when we never managed to look at it."""
    exchange = MagicMock()
    exchange.get_positions = AsyncMock(side_effect=ExchangeReadError("502"))
    exchange.get_balance = AsyncMock(side_effect=ExchangeReadError("502"))
    control = _tick_control()
    event_bus = MagicMock()
    event_bus.publish = AsyncMock()
    runner = EngineRunner(
        exchange=exchange, strategies=[], repo=None,
        event_bus=event_bus, control=control,
    )

    await runner.tick()

    control.acknowledge_flat_request.assert_not_awaited()
    exchange.place_order.assert_not_called()
    messages = [e.message for e in _published(event_bus)]
    assert any("Flat-all did not complete" in m for m in messages)


@pytest.mark.asyncio
async def test_tick_acknowledges_a_flat_all_that_actually_worked(monkeypatch):
    from hypertrade.config import settings

    monkeypatch.setattr(settings, "taker_fee_rate", 0.0)
    exchange = MagicMock()
    exchange.get_positions = AsyncMock(return_value=[])
    exchange.get_balance = AsyncMock(side_effect=ExchangeReadError("502"))
    control = _tick_control()
    runner = EngineRunner(
        exchange=exchange, strategies=[], repo=None,
        event_bus=None, control=control,
    )

    await runner.tick()

    control.acknowledge_flat_request.assert_awaited_once_with("tok-1")


@pytest.mark.asyncio
async def test_tick_writes_no_equity_snapshot_when_the_balance_read_fails():
    """`get_balance()` used to answer a 502 with Balance(total=0), which
    wrote a $0 equity snapshot and made the drawdown maths see a blown
    account."""
    exchange = MagicMock()
    exchange.get_positions = AsyncMock(return_value=[])
    exchange.get_balance = AsyncMock(side_effect=ExchangeReadError("502"))
    repo = MagicMock()
    repo.snapshot_equity = AsyncMock()
    repo.get_open_positions = AsyncMock(return_value=[])
    repo.reconcile_positions = AsyncMock(return_value=ReconcileResult())
    runner = EngineRunner(
        exchange=exchange, strategies=[], repo=repo,
        event_bus=None, control=_tick_control(pending=None),
    )

    await runner.tick()

    repo.snapshot_equity.assert_not_awaited()


@pytest.mark.asyncio
async def test_flat_all_not_ok_when_a_close_is_rejected(monkeypatch):
    from hypertrade.config import settings

    monkeypatch.setattr(settings, "taker_fee_rate", 0.0)
    exchange = MagicMock()
    exchange.get_positions = AsyncMock(return_value=[
        Position(symbol="BTC", side="long", size=1.0, entry_price=100.0),
    ])
    exchange.place_order = AsyncMock(return_value=Order(
        id="o1", symbol="BTC", side="sell", size=1.0,
        order_type=OrderType.MARKET, status=OrderStatus.REJECTED,
    ))
    runner, _ = _flat_runner(exchange)

    status = await runner._flat_all_positions()

    assert status.ok is False
    assert status.failed == 1


@pytest.mark.asyncio
async def test_flat_all_ok_on_a_clean_sweep(monkeypatch):
    from hypertrade.config import settings

    monkeypatch.setattr(settings, "taker_fee_rate", 0.0)
    exchange = MagicMock()
    exchange.get_positions = AsyncMock(return_value=[
        Position(symbol="BTC", side="long", size=1.0, entry_price=100.0),
    ])
    exchange.place_order = AsyncMock(return_value=Order(
        id="o1", symbol="BTC", side="sell", size=1.0,
        order_type=OrderType.MARKET, filled_price=110.0,
        status=OrderStatus.FILLED,
    ))
    runner, _ = _flat_runner(exchange)

    status = await runner._flat_all_positions()

    assert status.ok is True
    assert status.closed == 1


@pytest.mark.asyncio
async def test_flat_all_on_an_empty_book_is_ok():
    exchange = MagicMock()
    exchange.get_positions = AsyncMock(return_value=[])
    runner, _ = _flat_runner(exchange)

    status = await runner._flat_all_positions()

    assert status.ok is True
    assert status.closed == 0


# --- outage aggregation still works through the new wrapper -----------


def test_transient_errors_are_seen_through_exchange_read_error():
    """`ExchangeReadError` is a wrapper, not a diagnosis. Without
    unwrapping, every read that now raises would publish one Telegram
    error per strategy per tick for the whole outage."""
    cause = TimeoutError("read timed out")
    wrapped = ExchangeReadError("get_positions failed")
    wrapped.__cause__ = cause
    assert _is_transient_network_error(wrapped) is True


def test_non_transient_causes_still_publish():
    wrapped = ExchangeReadError("get_positions failed")
    wrapped.__cause__ = ValueError("bad payload")
    assert _is_transient_network_error(wrapped) is False


# --- _resolve_close_size with no DB (review item 11) ------------------


def _close_size_runner(exchange, repo=None):
    runner = EngineRunner(
        exchange=exchange, strategies=[], repo=repo,
        event_bus=None, control=MagicMock(),
    )
    runner.portfolio = MagicMock()
    runner.portfolio.record_pnl = AsyncMock()
    return runner


@pytest.mark.asyncio
async def test_close_size_without_db_raises_on_a_read_failure():
    """With no DB the exchange IS the source of truth, so an unreadable
    exchange must not return None — that reads as "no position" and
    silently drops the close signal. Raising routes it into the tick's
    handler and, for a transient cause, the outage aggregator."""
    exchange = MagicMock()
    exchange.get_position = AsyncMock(side_effect=ExchangeReadError("502"))
    runner = _close_size_runner(exchange)

    with pytest.raises(ExchangeReadError):
        await runner._resolve_close_size("strat", "BTC", "long")


@pytest.mark.asyncio
async def test_close_size_without_db_returns_the_exchange_size():
    exchange = MagicMock()
    exchange.get_position = AsyncMock(
        return_value=Position(symbol="BTC", side="long", size=1.5, entry_price=100.0)
    )
    runner = _close_size_runner(exchange)

    assert await runner._resolve_close_size("strat", "BTC", "long") == 1.5


@pytest.mark.asyncio
async def test_close_size_clamp_falls_back_to_db_size_on_a_read_failure():
    """With a DB row the clamp is only a safety net, so a read failure
    degrades to the DB size rather than raising."""
    db_pos = MagicMock()
    db_pos.side = "long"
    db_pos.size = 2.0
    repo = MagicMock()
    repo.get_open_position = AsyncMock(return_value=db_pos)
    exchange = MagicMock()
    exchange.get_position = AsyncMock(side_effect=ExchangeReadError("502"))
    runner = _close_size_runner(exchange, repo=repo)

    assert await runner._resolve_close_size("strat", "BTC", "long") == 2.0
