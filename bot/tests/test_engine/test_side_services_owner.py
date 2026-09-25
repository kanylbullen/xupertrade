"""The side services run on the services-owner bot only (roadmap NU-7).

HODL evaluation and the vault scanner used to be gated on
`exchange_mode == "mainnet"`, so stopping the real-money bot silenced
them. They now follow `SERVICES_OWNER`, which the dashboard orchestrator
sets on exactly one bot per tenant (the paper bot by default). The
mode no longer matters: an owner in any mode runs them, a non-owner
mainnet bot does not.
"""

from __future__ import annotations

import pytest

from hypertrade.config import settings
from hypertrade.engine.runner import EngineRunner
from hypertrade.notify.telegram import TelegramNotifier


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def hook(self, name: str):
        async def _call() -> None:
            self.calls.append(name)

        return _call


def _runner(monkeypatch, rec: _Recorder) -> EngineRunner:
    runner = EngineRunner(
        exchange=object(),  # type: ignore[arg-type]
        strategies=[],
        repo=object(),  # truthy: the vault scan needs a repo
        event_bus=None,
        control=None,
    )
    monkeypatch.setattr(runner, "_evaluate_hodl_signals", rec.hook("hodl"))
    monkeypatch.setattr(runner, "_poll_vaults", rec.hook("vaults"))
    return runner


@pytest.mark.parametrize("mode", ["paper", "testnet", "mainnet"])
async def test_owner_runs_hodl_and_vaults_in_any_mode(monkeypatch, mode):
    monkeypatch.setattr(settings, "exchange_mode", mode)
    monkeypatch.setattr(settings, "services_owner", True)
    rec = _Recorder()
    await _runner(monkeypatch, rec)._run_side_services()
    assert rec.calls == ["hodl", "vaults"]


@pytest.mark.parametrize("mode", ["paper", "testnet", "mainnet"])
async def test_non_owner_runs_neither_even_on_mainnet(monkeypatch, mode):
    monkeypatch.setattr(settings, "exchange_mode", mode)
    monkeypatch.setattr(settings, "services_owner", False)
    rec = _Recorder()
    await _runner(monkeypatch, rec)._run_side_services()
    assert rec.calls == []


async def test_owner_runs_them_once_per_interval(monkeypatch):
    """HODL every 6h, vaults daily: a second tick right after the first
    runs neither again."""
    monkeypatch.setattr(settings, "services_owner", True)
    rec = _Recorder()
    runner = _runner(monkeypatch, rec)
    await runner._run_side_services()
    await runner._run_side_services()
    assert rec.calls == ["hodl", "vaults"]


async def test_failed_vault_scan_retries_next_tick(monkeypatch):
    """A failed scan does not advance the daily clock, so a transient HL
    outage retries on the next tick instead of a day later."""
    monkeypatch.setattr(settings, "services_owner", True)
    rec = _Recorder()
    runner = _runner(monkeypatch, rec)

    async def _boom() -> None:
        rec.calls.append("vaults")
        raise RuntimeError("HL 502")

    monkeypatch.setattr(runner, "_poll_vaults", _boom)
    await runner._run_side_services()
    await runner._run_side_services()
    assert rec.calls == ["hodl", "vaults", "vaults"]


def test_kelly_command_is_gone():
    """Operator decision 5.7: /kelly was advisory, fed by too few trades,
    and misleading. It is no longer registered or advertised."""
    notifier = TelegramNotifier(token="", chat_id="")
    assert "/kelly" not in notifier._commands
    assert not hasattr(notifier, "_cmd_kelly")
