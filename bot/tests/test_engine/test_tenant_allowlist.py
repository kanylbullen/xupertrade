"""Tests for per-tenant operator-set strategy allowlist (alembic 0016).

NULL allowlist = no filter (legacy behavior); list = intersection;
empty list = no strategies allowed. Bot reads this at startup as
defense-in-depth against bypassed dashboard enforcement.
"""

from __future__ import annotations

from hypertrade.engine.strategy_allowlist import apply_tenant_allowlist


def test_null_allowlist_returns_all():
    assert apply_tenant_allowlist(["a", "b", "c"], None) == ["a", "b", "c"]


def test_empty_allowlist_returns_empty():
    assert apply_tenant_allowlist(["a", "b", "c"], []) == []


def test_intersection_preserves_input_order():
    assert apply_tenant_allowlist(["a", "b", "c", "d"], ["c", "a"]) == ["a", "c"]


def test_unknown_names_in_allowlist_are_ignored():
    assert apply_tenant_allowlist(["a", "b"], ["a", "z"]) == ["a"]


def test_does_not_mutate_input():
    src = ["a", "b"]
    apply_tenant_allowlist(src, None)
    assert src == ["a", "b"]
