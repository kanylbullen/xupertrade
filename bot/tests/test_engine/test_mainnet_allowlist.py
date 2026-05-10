"""Tests for MAINNET_ENABLED_STRATEGIES allowlist (audit C3).

Pre-fix: every registered strategy auto-instantiated on mainnet startup.
A fresh mainnet deploy with `disabled` set empty would immediately trade
all 21 strategies, including ones we know are net-negative (e.g.
penguin_volatility -10.3% in 180d backtest).

Post-fix: mainnet honors a fail-closed allowlist. EMPTY allowlist = zero
strategies. Paper/testnet are unaffected.

We test the filtering logic by exercising the same shape main.py uses,
without spinning up a full bot — the unit under test is the allowlist
decision, not the boot sequence.
"""

from __future__ import annotations

import logging

import pytest

from hypertrade.config import settings


def _filter(all_names: list[str], is_mainnet: bool, raw: str, caplog) -> list[str]:
    """Replica of main.py's allowlist filter — kept here for the test to
    exercise without spinning up the full bot. If the filter logic in
    main.py changes, update this helper to match (or refactor the prod
    code into a shared function and call that)."""
    logger = logging.getLogger("hypertrade")
    if is_mainnet:
        raw = raw.strip()
        if not raw:
            logger.critical(
                "MAINNET starting with EMPTY MAINNET_ENABLED_STRATEGIES — "
                "no strategies will trade. Set MAINNET_ENABLED_STRATEGIES="
                "name1,name2 in .env and restart."
            )
            return []
        requested = {n.strip() for n in raw.split(",") if n.strip()}
        unknown = requested - set(all_names)
        if unknown:
            logger.warning(
                "MAINNET_ENABLED_STRATEGIES references unknown names "
                "(ignored): %s", sorted(unknown),
            )
        return [n for n in all_names if n in requested]
    return all_names


def test_paper_mode_unchanged(caplog):
    """Paper/testnet ignores the allowlist — full set runs as before."""
    names = _filter(["a", "b", "c"], is_mainnet=False, raw="", caplog=caplog)
    assert names == ["a", "b", "c"]


def test_mainnet_empty_allowlist_zero_strategies(caplog):
    """Empty allowlist on mainnet = no trading — fail-closed default."""
    with caplog.at_level(logging.CRITICAL, logger="hypertrade"):
        names = _filter(["a", "b", "c"], is_mainnet=True, raw="", caplog=caplog)
    assert names == []
    assert any("EMPTY MAINNET_ENABLED_STRATEGIES" in r.message for r in caplog.records)


def test_mainnet_single_strategy_allowed(caplog):
    """The most common first-mainnet-trade case: one strategy."""
    names = _filter(["a", "bb_short", "c"], is_mainnet=True, raw="bb_short", caplog=caplog)
    assert names == ["bb_short"]


def test_mainnet_multiple_with_whitespace(caplog):
    """Operator-friendly: tolerate whitespace + ordering."""
    names = _filter(["a", "b", "c"], is_mainnet=True, raw=" b , a ", caplog=caplog)
    # Preserves registration order (not csv order) — deterministic across boots.
    assert names == ["a", "b"]


def test_mainnet_unknown_names_logged_and_dropped(caplog):
    """Typo in .env shouldn't crash — log warning and skip."""
    with caplog.at_level(logging.WARNING, logger="hypertrade"):
        names = _filter(["a", "b"], is_mainnet=True, raw="a,xtypo", caplog=caplog)
    assert names == ["a"]
    assert any("xtypo" in r.message for r in caplog.records)


def test_mainnet_all_unknown_resolves_to_empty(caplog):
    """All-typo allowlist resolves to no trading (fail-closed)."""
    names = _filter(["a", "b"], is_mainnet=True, raw="zzz,yyy", caplog=caplog)
    assert names == []


def test_setting_default_is_empty_string(monkeypatch):
    """Verify the .env default actually fails closed — no surprise default."""
    # Cold settings instance to avoid leaking a previously-set value.
    from hypertrade.config import Settings
    s = Settings()
    assert s.mainnet_enabled_strategies == ""
