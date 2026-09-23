"""Tests for the boot-time per-tenant strategy cap (alembic 0016).

`tenants.max_active_strategies` was only checked by the dashboard when a
tenant switched a strategy ON. A freshly spawned bot started with every
allowlisted strategy enabled, so a tenant capped at 3 ran all 22 until
they touched a switch. The orchestrator now injects the cap as
TENANT_MAX_ACTIVE_STRATEGIES and `enforce_strategy_cap` trims the
surplus into the Redis `disabled` set at boot.
"""

from __future__ import annotations

import logging

import pytest

from hypertrade.config import Settings
from hypertrade.events.types import ErrorOccurred
from hypertrade.strategy_cap import (
    MalformedCapError,
    enforce_strategy_cap,
    parse_strategy_cap,
)

NAMES = ["a", "b", "c", "d", "e"]


class FakeControl:
    """The two BotControl methods the cap uses, over an in-memory set."""

    def __init__(
        self,
        disabled: set[str] | None = None,
        *,
        read_fails: bool = False,
        fail_on: set[str] | None = None,
    ) -> None:
        self.disabled = set(disabled or ())
        self.read_fails = read_fails
        self.fail_on = set(fail_on or ())
        self.disable_calls: list[str] = []

    async def get_disabled_strategies(self) -> set[str]:
        if self.read_fails:
            raise ConnectionError("redis gone")
        return set(self.disabled)

    async def disable_strategy(self, name: str) -> None:
        self.disable_calls.append(name)
        if name in self.fail_on:
            raise ConnectionError("redis gone")
        self.disabled.add(name)


class FakeBus:
    def __init__(self) -> None:
        self.events: list[ErrorOccurred] = []

    async def publish(self, event: ErrorOccurred) -> None:
        self.events.append(event)


# ----- parse_strategy_cap -----


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_parse_unset_means_no_cap(raw):
    assert parse_strategy_cap(raw) is None


def test_parse_valid():
    assert parse_strategy_cap("3") == 3
    assert parse_strategy_cap(" 12 ") == 12


def test_parse_zero_is_a_real_cap():
    assert parse_strategy_cap("0") == 0


@pytest.mark.parametrize("raw", ["-1", "+3", "3.0", "abc", "1_000", "1e3", "３"])
def test_parse_malformed_raises(raw):
    with pytest.raises(MalformedCapError):
        parse_strategy_cap(raw)


def test_settings_reads_the_env_var(monkeypatch):
    monkeypatch.setenv("TENANT_MAX_ACTIVE_STRATEGIES", "4")
    assert Settings().tenant_max_active_strategies == "4"


# ----- enforce_strategy_cap -----


async def test_no_cap_changes_nothing():
    control, bus = FakeControl(), FakeBus()
    out = await enforce_strategy_cap(NAMES, "", control, bus, "testnet")
    assert out == NAMES
    assert control.disable_calls == []
    assert bus.events == []


async def test_within_cap_changes_nothing():
    control, bus = FakeControl(disabled={"d", "e"}), FakeBus()
    out = await enforce_strategy_cap(NAMES, "3", control, bus, "testnet")
    assert out == NAMES
    assert control.disable_calls == []
    assert bus.events == []


async def test_surplus_is_disabled_keeping_first_n_in_registry_order(caplog):
    control, bus = FakeControl(), FakeBus()
    with caplog.at_level(logging.WARNING, logger="hypertrade"):
        out = await enforce_strategy_cap(NAMES, "2", control, bus, "testnet")

    # Still instantiated — the surplus is switched off, not removed,
    # exactly as if the tenant had toggled it.
    assert out == NAMES
    assert control.disabled == {"c", "d", "e"}
    # One WARNING per disabled strategy.
    for name in ("c", "d", "e"):
        assert any(
            f"disabled {name} at boot" in r.getMessage()
            and r.levelno == logging.WARNING
            for r in caplog.records
        )
    # Exactly one event, naming all of them.
    assert len(bus.events) == 1
    event = bus.events[0]
    assert isinstance(event, ErrorOccurred)
    assert event.strategy == "strategy-cap"
    assert "c, d, e" in event.message
    assert "testnet" in event.message


async def test_already_disabled_strategies_do_not_count():
    # b is already off, so the enabled set is a, c, d, e: keep a and c.
    control, bus = FakeControl(disabled={"b"}), FakeBus()
    await enforce_strategy_cap(NAMES, "2", control, bus, "paper")
    assert control.disabled == {"b", "d", "e"}
    assert control.disable_calls == ["d", "e"]


async def test_cap_zero_disables_everything_enabled():
    control, bus = FakeControl(), FakeBus()
    out = await enforce_strategy_cap(NAMES, "0", control, bus, "testnet")
    assert out == NAMES
    assert control.disabled == set(NAMES)
    assert len(bus.events) == 1


async def test_second_boot_is_a_no_op():
    control, bus = FakeControl(), FakeBus()
    await enforce_strategy_cap(NAMES, "2", control, bus, "testnet")
    await enforce_strategy_cap(NAMES, "2", control, bus, "testnet")
    assert control.disable_calls == ["c", "d", "e"]
    assert len(bus.events) == 1


async def test_malformed_fails_closed_without_touching_redis(caplog):
    control, bus = FakeControl(), FakeBus()
    with caplog.at_level(logging.ERROR, logger="hypertrade"):
        out = await enforce_strategy_cap(NAMES, "lots", control, bus, "testnet")
    assert out == []
    assert control.disable_calls == []
    assert any("malformed" in r.getMessage() for r in caplog.records)
    assert len(bus.events) == 1


async def test_no_control_instantiates_only_first_n():
    # Without Redis the runner has no disabled set to consult and would
    # run everything it was given.
    out = await enforce_strategy_cap(NAMES, "2", None, FakeBus(), "testnet")
    assert out == ["a", "b"]


async def test_unreadable_disabled_set_instantiates_only_first_n():
    control = FakeControl(read_fails=True)
    out = await enforce_strategy_cap(NAMES, "2", control, FakeBus(), "testnet")
    assert out == ["a", "b"]
    assert control.disable_calls == []


async def test_failed_disable_instantiates_only_the_kept():
    # d stays enabled in Redis, and the runner reads that set every
    # tick — so it must not be instantiated.
    control = FakeControl(fail_on={"d"})
    out = await enforce_strategy_cap(NAMES, "2", control, FakeBus(), "testnet")
    assert out == ["a", "b"]
    assert control.disabled == {"c", "e"}


async def test_no_event_bus_is_tolerated():
    control = FakeControl()
    out = await enforce_strategy_cap(NAMES, "1", control, None, "testnet")
    assert out == NAMES
    assert control.disabled == {"b", "c", "d", "e"}
