"""Tests for MAINNET_ENABLED_STRATEGIES allowlist (audit C3).

Pre-fix: every registered strategy auto-instantiated on mainnet startup.
A fresh mainnet deploy with `disabled` set empty would immediately trade
all 21 strategies, including ones we know are net-negative (e.g.
penguin_volatility -10.3% in 180d backtest).

Post-fix: mainnet honors a fail-closed allowlist. EMPTY allowlist = zero
strategies. Paper/testnet are unaffected.

These tests exercise the production function directly (PR #29 review
fix — the previous version replicated the logic in a test helper, which
risked drifting from prod). The function is in
`hypertrade/engine/strategy_allowlist.py`.
"""

from __future__ import annotations

import logging

from hypertrade.engine.strategy_allowlist import apply_mainnet_allowlist


def test_paper_mode_unchanged():
    """Paper/testnet ignores the allowlist — full set runs as before."""
    assert apply_mainnet_allowlist(
        ["a", "b", "c"], is_mainnet=False, raw_csv="",
    ) == ["a", "b", "c"]


def test_mainnet_empty_allowlist_zero_strategies(caplog):
    """Empty allowlist on mainnet = no trading — fail-closed default."""
    with caplog.at_level(logging.CRITICAL, logger="hypertrade"):
        names = apply_mainnet_allowlist(
            ["a", "b", "c"], is_mainnet=True, raw_csv="",
        )
    assert names == []
    assert any("EMPTY MAINNET_ENABLED_STRATEGIES" in r.message for r in caplog.records)


def test_mainnet_single_strategy_allowed():
    """The most common first-mainnet-trade case: one strategy."""
    assert apply_mainnet_allowlist(
        ["a", "bb_short", "c"], is_mainnet=True, raw_csv="bb_short",
    ) == ["bb_short"]


def test_mainnet_multiple_with_whitespace():
    """Operator-friendly: tolerate whitespace + ordering."""
    # Output preserves registration order (not csv order) — deterministic.
    assert apply_mainnet_allowlist(
        ["a", "b", "c"], is_mainnet=True, raw_csv=" b , a ",
    ) == ["a", "b"]


def test_mainnet_unknown_names_logged_and_dropped(caplog):
    """Typo in .env shouldn't crash — log warning and skip."""
    with caplog.at_level(logging.WARNING, logger="hypertrade"):
        names = apply_mainnet_allowlist(
            ["a", "b"], is_mainnet=True, raw_csv="a,xtypo",
        )
    assert names == ["a"]
    assert any("xtypo" in r.message for r in caplog.records)


def test_mainnet_all_unknown_resolves_to_empty():
    """All-typo allowlist resolves to no trading (fail-closed)."""
    assert apply_mainnet_allowlist(
        ["a", "b"], is_mainnet=True, raw_csv="zzz,yyy",
    ) == []


def test_setting_default_is_empty_string():
    """Verify the .env default actually fails closed — no surprise default."""
    from hypertrade.config import Settings
    s = Settings()
    assert s.mainnet_enabled_strategies == ""
