"""Tests for per-tenant operator-set strategy allowlist (alembic 0016).

NULL allowlist = no filter (legacy behavior); list = intersection;
empty list = no strategies allowed. Bot reads this at startup as
defense-in-depth against bypassed dashboard enforcement.
"""

from __future__ import annotations

from hypertrade.engine.strategy_allowlist import (
    apply_tenant_allowlist,
    parse_tenant_allowlist,
)


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


# ----- parse_tenant_allowlist (env-injected source) -----
#
# Regression cover for the fail-open bug: the bot used to read
# `tenants.allowed_strategies` from Postgres, but its per-tenant PG role
# has no grant on that dashboard-owned table. Every boot raised
# InsufficientPrivilegeError, which a fail-open `except` swallowed, so
# the allowlist never actually filtered anything. It now arrives as a
# JSON array in TENANT_ALLOWED_STRATEGIES.


def test_parse_unset_means_no_allowlist():
    assert parse_tenant_allowlist(None) is None
    assert parse_tenant_allowlist("") is None
    assert parse_tenant_allowlist("   ") is None


def test_parse_valid_array():
    assert parse_tenant_allowlist('["bb_short", "hash_momentum"]') == [
        "bb_short",
        "hash_momentum",
    ]


def test_parse_empty_array_is_not_none():
    """`[]` means zero strategies may trade — collapsing it to None
    would silently grant the tenant every strategy."""
    result = parse_tenant_allowlist("[]")
    assert result == []
    assert result is not None


def test_parse_malformed_json_fails_closed():
    assert parse_tenant_allowlist("{not json") == []


def test_parse_wrong_type_fails_closed():
    # A JSON object / string / number is not an allowlist. Must not be
    # read as "no allowlist".
    for bad in ('{"a": 1}', '"bb_short"', "42", "null"):
        assert parse_tenant_allowlist(bad) == [], bad


def test_parse_non_string_members_fail_closed():
    assert parse_tenant_allowlist('["bb_short", 7]') == []


def test_malformed_end_to_end_blocks_every_strategy():
    """The whole point: a corrupt value must not widen access."""
    names = ["bb_short", "hash_momentum", "supertrend"]
    parsed = parse_tenant_allowlist("garbage")
    assert apply_tenant_allowlist(names, parsed) == []
