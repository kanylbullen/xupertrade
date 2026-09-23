"""Tests for the boot-time per-tenant strategy cap (alembic 0016).

`tenants.max_active_strategies` was only checked by the dashboard when a
tenant switched a strategy ON. A freshly spawned bot started with every
allowlisted strategy enabled, so a tenant capped at 3 ran all 22 until
they touched a switch. The orchestrator now injects the cap as
TENANT_MAX_ACTIVE_STRATEGIES and `enforce_strategy_cap` trims the flat
surplus into the Redis `disabled` set at boot.

Review of #171: the tick loop skips disabled strategies entirely and
there are no exchange-side stops, so trimming a strategy that is in a
trade orphans the position. Strategies holding an open DB position are
never trimmed, and nothing is trimmed when positions can't be read.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from hypertrade.config import Settings
from hypertrade.events.types import ErrorOccurred
from hypertrade.strategy_cap import (
    MalformedCapError,
    enforce_strategy_cap,
    parse_strategy_cap,
)

NAMES = ["a", "b", "c", "d", "e"]
TENANT = "tenant-1"


class FakeControl:
    """The BotControl methods the cap uses, over in-memory sets."""

    def __init__(
        self,
        disabled: set[str] | None = None,
        *,
        opted_in: set[str] | None = None,
        read_fails: bool = False,
        opt_in_read_fails: bool = False,
        fail_on: set[str] | None = None,
    ) -> None:
        self.disabled = set(disabled or ())
        self.opted_in = set(opted_in or ())
        self.read_fails = read_fails
        self.opt_in_read_fails = opt_in_read_fails
        self.fail_on = set(fail_on or ())
        self.disable_calls: list[str] = []

    async def get_disabled_strategies(self) -> set[str]:
        if self.read_fails:
            raise ConnectionError("redis gone")
        return set(self.disabled)

    async def get_mainnet_enabled_strategies_for_tenant(
        self, tenant_id: str,
    ) -> set[str]:
        assert tenant_id == TENANT
        if self.opt_in_read_fails:
            raise ConnectionError("redis gone")
        return set(self.opted_in)

    async def disable_strategy(self, name: str) -> None:
        self.disable_calls.append(name)
        if name in self.fail_on:
            raise ConnectionError("redis gone")
        self.disabled.add(name)


class FakeRepo:
    """`get_open_positions()` for this mode: rows with a strategy_name."""

    def __init__(self, holding: set[str] | None = None, *, fails: bool = False):
        self.holding = set(holding or ())
        self.fails = fails
        self.calls = 0

    async def get_open_positions(self, strategy_name: str | None = None):
        self.calls += 1
        if self.fails:
            raise ConnectionError("db gone")
        return [
            SimpleNamespace(strategy_name=n, symbol="BTC") for n in sorted(self.holding)
        ]


class FakeBus:
    def __init__(self) -> None:
        self.events: list[ErrorOccurred] = []

    async def publish(self, event: ErrorOccurred) -> None:
        self.events.append(event)


async def run(
    raw: str,
    control=None,
    repo=None,
    bus=None,
    mode: str = "testnet",
    mainnet_tenant_id: str | None = None,
):
    return await enforce_strategy_cap(
        NAMES,
        raw,
        control,
        bus if bus is not None else FakeBus(),
        repo if repo is not None else FakeRepo(),
        mode,
        mainnet_tenant_id=mainnet_tenant_id,
    )


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


# ----- no cap / within cap -----


async def test_no_cap_changes_nothing_and_reads_nothing():
    control, repo, bus = FakeControl(), FakeRepo(), FakeBus()
    out = await run("", control, repo, bus)
    assert out == NAMES
    assert control.disable_calls == []
    assert repo.calls == 0
    assert bus.events == []


async def test_within_cap_changes_nothing():
    control, bus = FakeControl(disabled={"d", "e"}), FakeBus()
    out = await run("3", control, bus=bus)
    assert out == NAMES
    assert control.disable_calls == []
    assert bus.events == []


# ----- trimming flat strategies -----


async def test_surplus_is_disabled_keeping_first_n_in_registry_order(caplog):
    control, bus = FakeControl(), FakeBus()
    with caplog.at_level(logging.WARNING, logger="hypertrade"):
        out = await run("2", control, bus=bus)

    # Still instantiated — the surplus is switched off, not removed,
    # exactly as if the tenant had toggled it.
    assert out == NAMES
    assert control.disabled == {"c", "d", "e"}
    for name in ("c", "d", "e"):
        assert any(
            f"disabled {name} at boot" in r.getMessage()
            and r.levelno == logging.WARNING
            for r in caplog.records
        )
    assert len(bus.events) == 1
    event = bus.events[0]
    assert isinstance(event, ErrorOccurred)
    assert event.strategy == "strategy-cap"
    assert "c, d, e" in event.message
    assert "testnet" in event.message


async def test_already_disabled_strategies_do_not_count():
    # b is already off, so the enabled set is a, c, d, e: keep a and c.
    control = FakeControl(disabled={"b"})
    await run("2", control)
    assert control.disabled == {"b", "d", "e"}
    assert control.disable_calls == ["d", "e"]


async def test_cap_zero_disables_every_flat_enabled_strategy():
    control, bus = FakeControl(), FakeBus()
    out = await run("0", control, bus=bus)
    assert out == NAMES
    assert control.disabled == set(NAMES)
    assert len(bus.events) == 1


async def test_second_boot_is_a_no_op():
    control, bus = FakeControl(), FakeBus()
    await run("2", control, bus=bus)
    await run("2", control, bus=bus)
    assert control.disable_calls == ["c", "d", "e"]
    assert len(bus.events) == 1


# ----- open positions (review of #171, item 1) -----


async def test_open_position_on_a_surplus_strategy_stays_enabled():
    # d would be surplus at cap 2 by registry order, but it is in a
    # trade: disabling it would leave the position with no SL/TP/exit.
    # It is kept and counts first, so only one flat slot remains.
    control, bus = FakeControl(), FakeBus()
    out = await run("2", control, FakeRepo({"d"}), bus)

    assert out == NAMES
    assert "d" not in control.disabled
    assert control.disabled == {"b", "c", "e"}
    assert "holding positions: d" in bus.events[0].message


async def test_holding_strategies_are_kept_even_beyond_the_cap(caplog):
    control = FakeControl()
    with caplog.at_level(logging.WARNING, logger="hypertrade"):
        out = await run("1", control, FakeRepo({"b", "d", "e"}))
    assert out == NAMES
    assert control.disabled == {"a", "c"}
    assert any("hold open positions" in r.getMessage() for r in caplog.records)


async def test_positions_unreadable_means_no_trimming(caplog):
    control, bus = FakeControl(), FakeBus()
    with caplog.at_level(logging.ERROR, logger="hypertrade"):
        out = await run("1", control, FakeRepo(fails=True), bus)
    assert out == NAMES
    assert control.disable_calls == []
    assert len(bus.events) == 1
    assert "NOT applied" in bus.events[0].message
    assert any("NOT trimming" in r.getMessage() for r in caplog.records)


async def test_no_repo_means_no_trimming():
    control, bus = FakeControl(), FakeBus()
    out = await enforce_strategy_cap(NAMES, "1", control, bus, None, "paper")
    assert out == NAMES
    assert control.disable_calls == []
    assert len(bus.events) == 1


async def test_malformed_value_keeps_strategies_holding_positions(caplog):
    control, bus = FakeControl(), FakeBus()
    with caplog.at_level(logging.ERROR, logger="hypertrade"):
        out = await run("lots", control, FakeRepo({"c"}), bus)
    # Only the one in a trade runs; nothing is written to Redis.
    assert out == ["c"]
    assert control.disable_calls == []
    assert any("malformed" in r.getMessage() for r in caplog.records)
    assert len(bus.events) == 1


async def test_malformed_value_with_no_positions_runs_nothing():
    control = FakeControl()
    out = await run("lots", control, FakeRepo())
    assert out == []
    assert control.disable_calls == []


async def test_malformed_value_with_positions_unreadable_trims_nothing():
    # Position safety outranks the cap: blind trimming could orphan.
    control = FakeControl()
    out = await run("lots", control, FakeRepo(fails=True))
    assert out == NAMES


# ----- no Redis -----


async def test_no_control_instantiates_holding_plus_first_flat():
    # Without Redis the runner has no disabled set to consult and would
    # run everything it was given.
    out = await run("2", None, FakeRepo({"d"}))
    assert out == ["a", "d"]


async def test_unreadable_disabled_set_instantiates_holding_plus_first_flat():
    control = FakeControl(read_fails=True)
    out = await run("2", control, FakeRepo())
    assert out == ["a", "b"]
    assert control.disable_calls == []


async def test_no_redis_on_mainnet_instantiates_only_holding():
    # On mainnet the opt-in set decides which flat strategies may
    # trade; without it, picking by registry order could pick wrong.
    out = await run("2", None, FakeRepo({"e"}), mode="mainnet", mainnet_tenant_id=TENANT)
    assert out == ["e"]


async def test_failed_disable_does_not_instantiate_that_strategy():
    # d stays enabled in Redis, and the runner reads that set every
    # tick — so it must not be instantiated. It is flat by construction.
    control = FakeControl(fail_on={"d"})
    out = await run("2", control)
    assert out == ["a", "b", "c", "e"]
    assert control.disabled == {"c", "e"}


async def test_no_event_bus_is_tolerated():
    control = FakeControl()
    out = await enforce_strategy_cap(NAMES, "1", control, None, FakeRepo(), "testnet")
    assert out == NAMES
    assert control.disabled == {"b", "c", "d", "e"}


# ----- mainnet opt-in (review of #171, item 2) -----


async def test_mainnet_keeps_opted_in_strategies_over_registry_order():
    # Registry order would keep a and b; the tenant opted in d and e,
    # which are the only ones the runner trades on mainnet.
    control = FakeControl(opted_in={"d", "e"})
    out = await run("2", control, mode="mainnet", mainnet_tenant_id=TENANT)
    assert out == NAMES
    assert control.disabled == {"a", "b", "c"}


async def test_mainnet_holding_first_then_opted_in():
    # b holds a position (kept regardless), leaving one slot: the
    # opted-in e wins over the earlier-registered a and c.
    control = FakeControl(opted_in={"e"})
    await run("2", control, FakeRepo({"b"}), mode="mainnet", mainnet_tenant_id=TENANT)
    assert control.disabled == {"a", "c", "d"}


async def test_opt_in_is_ignored_off_mainnet():
    control = FakeControl(opted_in={"d", "e"})
    await run("2", control, mode="testnet", mainnet_tenant_id=None)
    assert control.disabled == {"c", "d", "e"}


async def test_unreadable_opt_in_set_on_mainnet_instantiates_only_holding():
    control = FakeControl(opt_in_read_fails=True)
    out = await run("2", control, FakeRepo({"c"}), mode="mainnet", mainnet_tenant_id=TENANT)
    assert out == ["c"]
    assert control.disable_calls == []
