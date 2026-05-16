"""Tests for per-tenant mainnet strategy allowlist (UI-driven layer 2).

Operator's `MAINNET_ENABLED_STRATEGIES` env (layer 1) caps what *can*
trade; the per-tenant Redis set (layer 2) is what the tenant has
explicitly opted into. Effective enabled = intersection. Either layer
empty => no mainnet trading.

These tests cover the BotControl Redis helpers and the effective-set
intersection logic the runner applies each tick.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from hypertrade.engine.control import BotControl

TENANT = "00000000-0000-0000-0000-00000000aaaa"


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


# ---- Intersection logic (mirrors runner per-tick filtering) ----

def _effective(
    all_strategies: list[str],
    operator_cap: set[str],
    tenant_enabled: set[str],
) -> list[str]:
    """Mirror of the per-tick filter in runner._tick: a strategy trades
    iff it's in BOTH the operator cap AND the tenant's opt-in set."""
    return [
        n for n in all_strategies
        if n in operator_cap and n in tenant_enabled
    ]


def test_intersection_empty_operator_cap_blocks_everything():
    assert _effective(["a", "b"], set(), {"a", "b"}) == []


def test_intersection_empty_tenant_blocks_everything():
    assert _effective(["a", "b"], {"a", "b"}, set()) == []


def test_intersection_tenant_subset_of_cap():
    assert _effective(["a", "b", "c"], {"a", "b", "c"}, {"a"}) == ["a"]


def test_intersection_tenant_outside_cap_is_dropped():
    # Tenant tries to enable "x" but operator doesn't allow it.
    assert _effective(["a", "b", "x"], {"a", "b"}, {"a", "x"}) == ["a"]


def test_intersection_both_agree():
    assert _effective(["a", "b", "c"], {"a", "c"}, {"a", "c"}) == ["a", "c"]
