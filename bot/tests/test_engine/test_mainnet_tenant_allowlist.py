"""Tests for per-tenant mainnet strategy allowlist (UI-driven layer 2).

Operator's `MAINNET_ENABLED_STRATEGIES` env (layer 1) caps what *can*
trade; the per-tenant Redis set (layer 2) is what the tenant has
explicitly opted into. Effective enabled = intersection. Either layer
empty => no mainnet trading.

These tests cover the BotControl Redis helpers and the effective-set
intersection logic the runner applies each tick.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from hypertrade.engine.control import BotControl
from hypertrade.engine.strategy_allowlist import filter_strategies_for_tick

TENANT = "00000000-0000-0000-0000-00000000aaaa"


@dataclass
class _FakeStrategy:
    """Minimal stand-in for `Strategy` — the tick filter only reads
    `.name`, so a dataclass keeps the tests independent of the strategy
    base class and its dependencies."""

    name: str


def _strats(*names: str) -> list[_FakeStrategy]:
    return [_FakeStrategy(n) for n in names]


def _names(strategies: list[_FakeStrategy]) -> list[str]:
    return [s.name for s in strategies]


def _make_control() -> tuple[BotControl, AsyncMock]:
    """BotControl with its Redis swapped for an AsyncMock fake."""
    c = BotControl(redis_url="redis://unused/0", mode="mainnet")
    fake = AsyncMock()
    c._redis = fake  # noqa: SLF001 — test-only injection
    return c, fake


@pytest.mark.asyncio
async def test_get_returns_empty_when_set_missing():
    c, fake = _make_control()
    fake.smembers.return_value = set()
    assert await c.get_mainnet_enabled_strategies_for_tenant(TENANT) == set()
    fake.smembers.assert_awaited_with(
        f"hypertrade:mainnet:control:enabled_strategies:{TENANT}",
    )


@pytest.mark.asyncio
async def test_get_returns_members():
    c, fake = _make_control()
    fake.smembers.return_value = {"bb_short", "moon_phases"}
    assert await c.get_mainnet_enabled_strategies_for_tenant(TENANT) == {
        "bb_short", "moon_phases",
    }


@pytest.mark.asyncio
async def test_get_empty_tenant_id_returns_empty():
    c, _ = _make_control()
    assert await c.get_mainnet_enabled_strategies_for_tenant("") == set()


@pytest.mark.asyncio
async def test_set_enabled_calls_sadd():
    c, fake = _make_control()
    await c.set_mainnet_strategy_enabled(TENANT, "bb_short", True)
    fake.sadd.assert_awaited_with(
        f"hypertrade:mainnet:control:enabled_strategies:{TENANT}", "bb_short",
    )


@pytest.mark.asyncio
async def test_set_disabled_calls_srem():
    c, fake = _make_control()
    await c.set_mainnet_strategy_enabled(TENANT, "bb_short", False)
    fake.srem.assert_awaited_with(
        f"hypertrade:mainnet:control:enabled_strategies:{TENANT}", "bb_short",
    )


@pytest.mark.asyncio
async def test_set_no_op_when_no_redis():
    c = BotControl(redis_url="redis://unused/0", mode="mainnet")
    # Should not raise even though _redis is None
    await c.set_mainnet_strategy_enabled(TENANT, "bb_short", True)


# ---- Intersection logic (exercises runner per-tick filter directly) ----
#
# These tests drive `filter_strategies_for_tick`, the same function the
# runner calls each tick (see runner._tick). The "operator cap" in this
# context is layer 1 (`MAINNET_ENABLED_STRATEGIES`, enforced at boot
# via `apply_mainnet_allowlist`) — by the time `_tick` runs, the cap
# has already removed disallowed strategies from `self.strategies`, so
# `disabled` is the only runtime knob besides `mainnet_enabled`. We
# model the cap below by pre-filtering `all_strategies` accordingly.


def test_intersection_empty_operator_cap_blocks_everything():
    # Operator cap is empty -> no strategies even reach the tick.
    assert _names(
        filter_strategies_for_tick(_strats(), set(), {"a", "b"})
    ) == []


def test_intersection_empty_tenant_blocks_everything():
    assert _names(
        filter_strategies_for_tick(_strats("a", "b"), set(), set())
    ) == []


def test_intersection_tenant_subset_of_cap():
    assert _names(
        filter_strategies_for_tick(_strats("a", "b", "c"), set(), {"a"})
    ) == ["a"]


def test_intersection_tenant_outside_cap_is_dropped():
    # Tenant tries to enable "x" but operator cap (applied upstream)
    # already removed it from the strategy list.
    assert _names(
        filter_strategies_for_tick(_strats("a", "b"), set(), {"a", "x"})
    ) == ["a"]


def test_intersection_both_agree():
    assert _names(
        filter_strategies_for_tick(_strats("a", "b", "c"), set(), {"a", "c"})
    ) == ["a", "c"]


def test_filter_skips_disabled_strategies():
    # `disabled` always applies, regardless of mainnet allowlist state.
    assert _names(
        filter_strategies_for_tick(
            _strats("a", "b", "c"), {"b"}, {"a", "b", "c"},
        )
    ) == ["a", "c"]


def test_filter_none_mainnet_allowlist_skips_mainnet_layer():
    # Paper/testnet path: mainnet_enabled=None -> only `disabled`
    # filters; otherwise every strategy runs.
    assert _names(
        filter_strategies_for_tick(_strats("a", "b", "c"), set(), None)
    ) == ["a", "b", "c"]
    assert _names(
        filter_strategies_for_tick(_strats("a", "b", "c"), {"b"}, None)
    ) == ["a", "c"]


def test_filter_preserves_input_order():
    assert _names(
        filter_strategies_for_tick(
            _strats("c", "a", "b"), set(), {"a", "b", "c"},
        )
    ) == ["c", "a", "b"]
