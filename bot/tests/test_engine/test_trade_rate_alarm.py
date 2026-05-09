"""Tests for the trade-rate anomaly alarm in EngineRunner.

Verifies the auto-pause behavior introduced after the 2026-05-09
hash_momentum SOL-spam incident: a strategy that suddenly trades
much faster than its baseline gets disabled in Redis and an error
event is emitted to Telegram.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hypertrade.engine.runner import EngineRunner
from hypertrade.events.types import ErrorOccurred


def _runner_with(hourly: dict, weekly: dict) -> tuple[EngineRunner, MagicMock, MagicMock]:
    """Build an EngineRunner with stubbed repo + control returning fake counts."""
    repo = MagicMock()
    repo.get_trade_counts_per_strategy = AsyncMock(side_effect=[hourly, weekly])
    control = MagicMock()
    control.get_disabled_strategies = AsyncMock(return_value=set())
    control.disable_strategy = AsyncMock()
    event_bus = MagicMock()
    event_bus.publish = AsyncMock()

    runner = EngineRunner(
        exchange=MagicMock(),
        strategies=[],
        repo=repo,
        event_bus=event_bus,
        control=control,
    )
    return runner, control, event_bus


@pytest.mark.asyncio
async def test_no_alarm_when_rate_within_baseline():
    """Strategy at 3/h with 7d baseline 50 (=0.3/h) — well under
    the floor of 5 trades/hour; no alarm."""
    runner, control, bus = _runner_with(
        hourly={"calm_strategy": 3},
        weekly={"calm_strategy": 50},
    )
    await runner._check_trade_rate_anomalies()
    control.disable_strategy.assert_not_called()
    bus.publish.assert_not_called()
    assert runner._rate_alarm_paused == set()


@pytest.mark.asyncio
async def test_alarm_on_baseline_spike():
    """Strategy at 30/h with 7d baseline 84 (=0.5/h). 30 > 5×0.5=2.5
    AND 30 > floor 5 → spike trigger."""
    runner, control, bus = _runner_with(
        hourly={"spamming_strategy": 30},
        weekly={"spamming_strategy": 84},
    )
    await runner._check_trade_rate_anomalies()
    control.disable_strategy.assert_called_once_with("spamming_strategy")
    bus.publish.assert_called_once()
    event = bus.publish.call_args[0][0]
    assert isinstance(event, ErrorOccurred)
    assert event.strategy == "spamming_strategy"
    assert "AUTO-PAUSED" in event.message
    assert "spamming_strategy" in runner._rate_alarm_paused


@pytest.mark.asyncio
async def test_alarm_on_absolute_ceiling_for_first_run_strategy():
    """Strategy with NO 7d history (baseline=0) but >20/h → ceiling trigger.
    Catches first-time-active strategies the baseline-multiplier path misses."""
    runner, control, bus = _runner_with(
        hourly={"new_strategy": 25},
        weekly={},
    )
    await runner._check_trade_rate_anomalies()
    control.disable_strategy.assert_called_once_with("new_strategy")
    event = bus.publish.call_args[0][0]
    assert "absolute ceiling" in event.message


@pytest.mark.asyncio
async def test_no_alarm_just_below_floor():
    """4/h with 0 baseline — under both ceiling (20) AND floor (5).
    The floor protects quiet strategies whose 5×baseline is meaningless."""
    runner, control, bus = _runner_with(
        hourly={"quiet": 4},
        weekly={},
    )
    await runner._check_trade_rate_anomalies()
    control.disable_strategy.assert_not_called()


@pytest.mark.asyncio
async def test_dedupe_does_not_alarm_twice():
    """Once a strategy has been auto-paused this process lifetime,
    the alarm doesn't fire again on the next check (which would spam
    Telegram while the spam burst is still in the 1h rolling window)."""
    runner, control, bus = _runner_with(
        hourly={"spammer": 50},
        weekly={"spammer": 0},
    )
    runner._rate_alarm_paused = {"spammer"}
    await runner._check_trade_rate_anomalies()
    control.disable_strategy.assert_not_called()
    bus.publish.assert_not_called()


@pytest.mark.asyncio
async def test_dedupe_skips_already_disabled_strategy():
    """If operator has already disabled the strategy via /options or
    Redis, alarm doesn't redundantly disable + alert."""
    runner, control, bus = _runner_with(
        hourly={"spammer": 50},
        weekly={"spammer": 0},
    )
    control.get_disabled_strategies = AsyncMock(return_value={"spammer"})
    await runner._check_trade_rate_anomalies()
    control.disable_strategy.assert_not_called()
    bus.publish.assert_not_called()


@pytest.mark.asyncio
async def test_no_trades_is_clean_no_op():
    """Empty hourly counts → method returns immediately without touching
    Redis or the event bus. The settings-gate (trade_rate_alarm_enabled)
    lives in the runner main loop, not inside this method, so we can't
    exercise it here — that's a higher-level integration concern."""
    runner, control, bus = _runner_with(hourly={}, weekly={})
    await runner._check_trade_rate_anomalies()
    control.disable_strategy.assert_not_called()
    bus.publish.assert_not_called()
