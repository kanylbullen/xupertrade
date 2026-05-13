"""Tests for the HODL verdict-change emitter in EngineRunner.

Bug context (2026-05-13): the runner published verdict changes via
`ErrorOccurred`, which the Telegram formatter renders with a ⚠️ ERROR
prefix. The user got pinged "ERROR hodl/vault_picks" for what was
actually a normal recovery from a transient HL fetch failure.

These tests assert:
  1. normal → normal (different): publishes HodlVerdictChanged.
  2. normal → "Unknown — evaluation failed": publishes (a real failure
     the user wants to know about).
  3. "Unknown — evaluation failed" → normal: does NOT publish (recovery
     noise — the user was never pinged about the failure).
  4. unchanged verdict: does NOT publish.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from hypertrade.engine.runner import EngineRunner
from hypertrade.events.types import HodlVerdictChanged


@dataclass
class _FakeState:
    asset: str
    verdict: str
    score: float = 0.0


class _FakeSignal:
    def __init__(self, name: str, asset: str, verdict: str) -> None:
        self.name = name
        self.asset = asset
        self._verdict = verdict

    async def evaluate(self) -> _FakeState:
        return _FakeState(asset=self.asset, verdict=self._verdict)


class _RecordingBus:
    def __init__(self) -> None:
        self.published: list = []

    async def publish(self, event) -> None:
        self.published.append(event)


class _LoudExchange:
    """Stub exchange for the runner constructor. _evaluate_hodl_signals
    must NOT touch the exchange — any attribute access here raises so a
    future refactor that accidentally pulls the exchange into the HODL
    path fails loudly instead of returning None silently. Copilot
    review fix on PR #98 (was `exchange=None`).
    """

    def __getattr__(self, name: str):  # noqa: D401
        raise AssertionError(
            f"_evaluate_hodl_signals must not touch exchange; got .{name}"
        )


def _make_runner(bus: _RecordingBus) -> EngineRunner:
    # strategies + exchange aren't touched by _evaluate_hodl_signals;
    # _LoudExchange enforces that at runtime if a regression slips in.
    runner = EngineRunner(
        exchange=_LoudExchange(),  # type: ignore[arg-type]
        strategies=[],
        repo=None,
        event_bus=bus,
        control=None,
    )
    return runner


async def _run_with_signal(
    runner: EngineRunner, sig: _FakeSignal, monkeypatch
) -> None:
    """Patch the hodl registry lookup to return our fake signal."""
    import hypertrade.hodl.registry as registry

    monkeypatch.setattr(registry, "load_all", lambda: None)
    monkeypatch.setattr(registry, "all_signals", lambda: [sig])
    await runner._evaluate_hodl_signals()


@pytest.mark.asyncio
async def test_normal_to_normal_publishes_verdict_changed(monkeypatch):
    bus = _RecordingBus()
    runner = _make_runner(bus)
    runner._last_hodl_zones["vault_picks"] = "Wait — soft pool"

    sig = _FakeSignal("vault_picks", "USD", "5 solid pool — pick top 2-3 by Sharpe")
    await _run_with_signal(runner, sig, monkeypatch)

    assert len(bus.published) == 1
    evt = bus.published[0]
    assert isinstance(evt, HodlVerdictChanged)
    assert evt.strategy == "hodl/vault_picks"
    assert evt.asset == "USD"
    assert evt.prev_verdict == "Wait — soft pool"
    assert evt.new_verdict == "5 solid pool — pick top 2-3 by Sharpe"


@pytest.mark.asyncio
async def test_normal_to_unknown_failed_publishes(monkeypatch):
    """A fresh failure IS noteworthy — the user wants to know when
    something just broke. Only the recovery is noise."""
    bus = _RecordingBus()
    runner = _make_runner(bus)
    runner._last_hodl_zones["vault_picks"] = "Watch — 3 candidates"

    sig = _FakeSignal("vault_picks", "USD", "Unknown — evaluation failed")
    await _run_with_signal(runner, sig, monkeypatch)

    assert len(bus.published) == 1
    evt = bus.published[0]
    assert isinstance(evt, HodlVerdictChanged)
    assert evt.new_verdict == "Unknown — evaluation failed"


@pytest.mark.asyncio
async def test_unknown_failed_to_normal_does_not_publish(monkeypatch):
    """Recovery noise — original failure didn't ping, recovery shouldn't either."""
    bus = _RecordingBus()
    runner = _make_runner(bus)
    runner._last_hodl_zones["vault_picks"] = "Unknown — evaluation failed"

    sig = _FakeSignal("vault_picks", "USD", "5 solid pool — pick top 2-3 by Sharpe")
    await _run_with_signal(runner, sig, monkeypatch)

    assert bus.published == []
    # State still updated so subsequent normal→normal transitions publish
    assert runner._last_hodl_zones["vault_picks"] == "5 solid pool — pick top 2-3 by Sharpe"


@pytest.mark.asyncio
async def test_unknown_no_data_to_normal_does_not_publish(monkeypatch):
    """Other 'Unknown — …' sentinels (e.g. 'no data') are also recovery noise."""
    bus = _RecordingBus()
    runner = _make_runner(bus)
    runner._last_hodl_zones["vault_picks"] = "Unknown — no data"

    sig = _FakeSignal("vault_picks", "USD", "Watch — 2 candidates")
    await _run_with_signal(runner, sig, monkeypatch)

    assert bus.published == []


@pytest.mark.asyncio
async def test_transient_to_other_transient_does_not_publish(monkeypatch):
    """Transient → another transient is still broken — don't ping twice.

    Copilot review fix on PR #98: previously this transition WOULD have
    published because only the prev side was checked.
    """
    bus = _RecordingBus()
    runner = _make_runner(bus)
    runner._last_hodl_zones["vault_picks"] = "Unknown — evaluation failed"

    sig = _FakeSignal("vault_picks", "USD", "Unknown — no data")
    await _run_with_signal(runner, sig, monkeypatch)

    assert bus.published == []


@pytest.mark.asyncio
async def test_non_transient_unknown_recovery_publishes(monkeypatch):
    """Non-transient Unknown verdicts (e.g. operator-disabled) are real
    state, not error noise. Recovery from them DOES warrant a ping.

    Copilot review fix on PR #98: previously the predicate was
    `startswith("unknown")` which would have suppressed this; narrowed
    to specific transient sentinels so legit Unknowns aren't muted.
    """
    bus = _RecordingBus()
    runner = _make_runner(bus)
    runner._last_hodl_zones["vault_picks"] = "Unknown — manual disabled"

    sig = _FakeSignal("vault_picks", "USD", "Watch — 2 candidates")
    await _run_with_signal(runner, sig, monkeypatch)

    assert len(bus.published) == 1


@pytest.mark.asyncio
async def test_unchanged_verdict_does_not_publish(monkeypatch):
    bus = _RecordingBus()
    runner = _make_runner(bus)
    runner._last_hodl_zones["vault_picks"] = "Wait — soft pool"

    sig = _FakeSignal("vault_picks", "USD", "Wait — soft pool")
    await _run_with_signal(runner, sig, monkeypatch)

    assert bus.published == []


@pytest.mark.asyncio
async def test_first_observation_does_not_publish(monkeypatch):
    """No prior verdict known → seed state silently, don't publish."""
    bus = _RecordingBus()
    runner = _make_runner(bus)
    assert "vault_picks" not in runner._last_hodl_zones

    sig = _FakeSignal("vault_picks", "USD", "Watch — 2 candidates")
    await _run_with_signal(runner, sig, monkeypatch)

    assert bus.published == []
    assert runner._last_hodl_zones["vault_picks"] == "Watch — 2 candidates"
