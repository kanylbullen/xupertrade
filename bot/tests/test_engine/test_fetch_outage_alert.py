"""Tests for the fetch-outage window aggregator on the strategy-tick path.

Bug context: the strategy-tick path used to emit one `ErrorOccurred` per
strategy per failed tick — ~22 events/min during the 2026-05-09 HL outage
(≈6000 events over 4.5h). The PR-#24 error-type filter traded that spam
for the opposite failure mode: transient errors (and the empty-DataFrame
signature `fetch_candles` returns after its retries are exhausted) became
FULLY silent, so a multi-hour outage never reached Telegram at all.

The aggregator in `EngineRunner._settle_fetch_outage` implements the
rate-limit/dedup middle ground (CLAUDE.md § 6 "Telegram is for humans"):
  1. a failure window that clears before FETCH_OUTAGE_ALERT_SECONDS
     produces NO notification (transient blip, bot auto-recovers);
  2. a window persisting ≥ the threshold produces EXACTLY ONE
     ErrorOccurred summarizing every affected strategy;
  3. recovery closes the window log-only — never a second notification.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pandas as pd
import pytest

import hypertrade.engine.runner as runner_mod
from hypertrade.config import settings
from hypertrade.engine.runner import EngineRunner
from hypertrade.events.types import ErrorOccurred


class _RecordingBus:
    def __init__(self) -> None:
        self.published: list = []

    async def publish(self, event) -> None:
        self.published.append(event)

    def errors(self) -> list[ErrorOccurred]:
        return [e for e in self.published if isinstance(e, ErrorOccurred)]


class _FakeExchange:
    """Minimal exchange for tick()-level tests: the equity-snapshot
    tail of tick() needs get_balance/get_positions; nothing else is
    touched on a fetch-failure tick."""

    async def get_balance(self):
        return SimpleNamespace(total=0.0, available=0.0, unrealized_pnl=0.0)

    async def get_positions(self):
        return []


class _FakeStrategy:
    def __init__(self, name: str) -> None:
        self.name = name
        self.symbol = "BTC"
        self.timeframe = "4h"

    async def on_candle(self, candles):
        return None  # HOLD


class _FakeClock:
    """Controllable time.time replacement so outage-window tests are
    deterministic instead of sleeping."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _make_runner(strategies: list | None = None) -> tuple[EngineRunner, _RecordingBus]:
    bus = _RecordingBus()
    runner = EngineRunner(
        exchange=_FakeExchange(),
        strategies=strategies or [],
        repo=None,
        event_bus=bus,
        control=None,
    )
    return runner, bus


def _one_candle() -> pd.DataFrame:
    return pd.DataFrame(
        [{
            "timestamp": pd.Timestamp("2026-08-31", tz="UTC"),
            "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0,
        }]
    )


def _mk_fetch(empty: bool):
    async def _fetch(symbol, timeframe, limit=300):
        return pd.DataFrame() if empty else _one_candle()

    return _fetch


# ─── Unit: window semantics via _settle_fetch_outage ─────────────────────────


async def test_transient_window_under_threshold_no_notification():
    """REQUIRED: a transient fetch failure (window shorter than the alert
    threshold) produces NO notification — the bot auto-recovers."""
    runner, bus = _make_runner()

    runner._tick_fetch_failures = {"ema_crossover"}
    await runner._settle_fetch_outage(now=1000.0)  # window opens
    runner._tick_fetch_failures = {"ema_crossover"}
    await runner._settle_fetch_outage(now=1300.0)  # still under 600s
    runner._tick_fetch_failures = set()
    await runner._settle_fetch_outage(now=1400.0)  # recovered

    assert bus.errors() == []
    # Window is reset so a later blip starts fresh.
    assert runner._outage_start is None
    assert runner._outage_affected == set()
    assert runner._outage_alerted is False


async def test_persistent_window_exactly_one_notification():
    """REQUIRED: a fetch-failure window persisting ≥ the threshold
    produces EXACTLY ONE notification — not one per tick."""
    runner, bus = _make_runner()

    runner._tick_fetch_failures = {"ema_crossover"}
    await runner._settle_fetch_outage(now=1000.0)   # window opens
    assert bus.errors() == []
    runner._tick_fetch_failures = {"ema_crossover"}
    await runner._settle_fetch_outage(now=1400.0)   # 400s in — quiet
    assert bus.errors() == []
    runner._tick_fetch_failures = {"ema_crossover"}
    await runner._settle_fetch_outage(now=1601.0)   # 601s ≥ threshold → alert
    assert len(bus.errors()) == 1
    runner._tick_fetch_failures = {"ema_crossover"}
    await runner._settle_fetch_outage(now=2200.0)   # still down — deduped
    runner._tick_fetch_failures = {"ema_crossover"}
    await runner._settle_fetch_outage(now=2800.0)   # still down — deduped
    assert len(bus.errors()) == 1


async def test_alert_summary_lists_affected_strategies():
    """The one-per-window alert carries the affected-strategy summary —
    that summary is the whole point vs full suppression."""
    runner, bus = _make_runner()

    runner._tick_fetch_failures = {"bb_short", "ema_crossover"}
    await runner._settle_fetch_outage(now=1000.0)
    runner._tick_fetch_failures = {"ema_crossover"}
    await runner._settle_fetch_outage(now=1700.0)

    (evt,) = bus.errors()
    assert isinstance(evt, ErrorOccurred)
    assert evt.strategy == "candle-fetch"
    assert "ema_crossover" in evt.message
    assert "bb_short" in evt.message
    assert "700s" in evt.message  # duration since first failure
    assert "2 strategy(ies)" in evt.message

async def test_failures_after_alert_stay_silent():
    """Strategies joining the outage AFTER the alert don't re-alert —
    one notification per window even as the outage evolves."""
    runner, bus = _make_runner()

    runner._tick_fetch_failures = {"ema_crossover"}
    await runner._settle_fetch_outage(now=1000.0)
    runner._tick_fetch_failures = {"ema_crossover"}
    await runner._settle_fetch_outage(now=1700.0)  # alert #1
    assert len(bus.errors()) == 1

    runner._tick_fetch_failures = {"bb_short", "ema_crossover"}
    await runner._settle_fetch_outage(now=2000.0)
    runner._tick_fetch_failures = {"bb_short", "ema_crossover", "supertrend"}
    await runner._settle_fetch_outage(now=3000.0)
    assert len(bus.errors()) == 1


async def test_recovery_after_alert_no_second_notification():
    """Recovery closes an already-alerted window log-only — the alert is
    the single notification for that outage window."""
    runner, bus = _make_runner()

    runner._tick_fetch_failures = {"ema_crossover"}
    await runner._settle_fetch_outage(now=1000.0)
    runner._tick_fetch_failures = {"ema_crossover"}
    await runner._settle_fetch_outage(now=1700.0)  # alert fired
    runner._tick_fetch_failures = set()
    await runner._settle_fetch_outage(now=2500.0)  # recovered

    assert len(bus.errors()) == 1
    assert runner._outage_start is None  # window closed + reset

    # A NEW persistent window alerts again (fresh window, fresh latch).
    runner._tick_fetch_failures = {"ema_crossover"}
    await runner._settle_fetch_outage(now=3000.0)
    runner._tick_fetch_failures = {"ema_crossover"}
    await runner._settle_fetch_outage(now=3700.0)
    assert len(bus.errors()) == 2


async def test_threshold_zero_disables_alert():
    """FETCH_OUTAGE_ALERT_SECONDS=0 → full-suppression escape hatch:
    windows are still tracked/logged but never notify."""
    runner, bus = _make_runner()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "fetch_outage_alert_seconds", 0)
        runner._tick_fetch_failures = {"ema_crossover"}
        await runner._settle_fetch_outage(now=1000.0)
        runner._tick_fetch_failures = {"ema_crossover"}
        await runner._settle_fetch_outage(now=100_000.0)

    assert bus.errors() == []
    assert runner._outage_start is not None  # window still open/tracked


# ─── Integration: tick()-level wiring ────────────────────────────────────────


async def test_tick_empty_candles_transient_no_notification(monkeypatch):
    """REQUIRED, end-to-end: `fetch_candles` failing after its retries
    surfaces as an EMPTY DataFrame (network/5xx errors are swallowed).
    A blip of such ticks inside the threshold must NOT notify."""
    clock = _FakeClock()
    monkeypatch.setattr(runner_mod.time, "time", clock)
    monkeypatch.setattr(settings, "fetch_outage_alert_seconds", 600)
    monkeypatch.setattr(runner_mod, "fetch_candles", _mk_fetch(empty=True))

    runner, bus = _make_runner(strategies=[_FakeStrategy("ema_crossover")])
    await runner.tick()
    clock.advance(60.0)
    await runner.tick()
    assert bus.errors() == []

    # HL comes back → candles flow again → no notification ever fired.
    monkeypatch.setattr(runner_mod, "fetch_candles", _mk_fetch(empty=False))
    clock.advance(60.0)
    await runner.tick()
    assert bus.errors() == []
    assert runner._outage_start is None


async def test_tick_persistent_outage_exactly_one_notification(monkeypatch):
    """REQUIRED, end-to-end: consecutive ticks with empty candles spanning
    the threshold produce EXACTLY ONE ErrorOccurred on the bus."""
    clock = _FakeClock()
    monkeypatch.setattr(runner_mod.time, "time", clock)
    monkeypatch.setattr(settings, "fetch_outage_alert_seconds", 600)
    monkeypatch.setattr(runner_mod, "fetch_candles", _mk_fetch(empty=True))

    runner, bus = _make_runner(strategies=[_FakeStrategy("ema_crossover")])
    await runner.tick()                    # t=1000 window opens
    clock.advance(300.0)
    await runner.tick()                    # t=1300 quiet
    clock.advance(301.0)
    await runner.tick()                    # t=1601 ≥ 600s → ONE alert
    assert len(bus.errors()) == 1
    evt = bus.errors()[0]
    assert evt.strategy == "candle-fetch"
    assert "ema_crossover" in evt.message

    clock.advance(600.0)
    await runner.tick()                    # t=2201 still down — deduped
    assert len(bus.errors()) == 1


async def test_tick_transient_exception_no_immediate_publish(monkeypatch):
    """A strategy tick raising a transient network error (e.g. the bare
    TimeoutError that escapes fetch_candles' reraise) must NOT publish
    immediately — it feeds the outage window instead."""
    clock = _FakeClock()
    monkeypatch.setattr(runner_mod.time, "time", clock)
    monkeypatch.setattr(settings, "fetch_outage_alert_seconds", 600)

    runner, bus = _make_runner(strategies=[_FakeStrategy("ema_crossover")])

    async def timeout_tick(strategy):
        raise asyncio.TimeoutError()

    runner._run_strategy = timeout_tick
    await runner.tick()
    clock.advance(60.0)
    await runner.tick()

    assert bus.errors() == []
    assert runner._outage_start is not None  # window opened from the catch-all


async def test_tick_non_transient_exception_publishes_immediately(monkeypatch):
    """Regression guard for the PR-#24 error-type filter: a non-transient
    error (real bug) still publishes 'Strategy tick failed' immediately —
    the outage aggregator must not swallow it."""
    clock = _FakeClock()
    monkeypatch.setattr(runner_mod.time, "time", clock)

    runner, bus = _make_runner(strategies=[_FakeStrategy("ema_crossover")])

    async def buggy_tick(strategy):
        raise ValueError("invalid SL")

    runner._run_strategy = buggy_tick
    await runner.tick()

    (evt,) = bus.errors()
    assert evt.strategy == "ema_crossover"
    assert evt.message == "Strategy tick failed"
    # And a logic bug must NOT be misread as a fetch outage.
    assert runner._outage_start is None
