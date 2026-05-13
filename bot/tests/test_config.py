"""Tests for the Settings model — focused on derived properties.

Background: PR 4c retired the compose-bot model; per-tenant bots are
spawned by the dashboard orchestrator. The credentials UI doesn't
expose a `VAULT_TRACKING_ADDRESS` slot for most tenants, which broke
the /vaults page's "your holdings" panel for mainnet bots whose own
wallet IS the vault-holding wallet. `effective_vault_tracking_address`
falls back to `HYPERLIQUID_ACCOUNT_ADDRESS` on mainnet only — testnet
wallets aren't on mainnet so the lookup would return nothing useful.
"""

from __future__ import annotations

from hypertrade.config import Settings


def _settings(**overrides) -> Settings:
    """Build a Settings instance with the given fields overridden.

    `Settings()` reads from process env / .env by default; pass explicit
    constructor kwargs to make these tests hermetic.
    """
    return Settings(**overrides)


def test_explicit_vault_tracking_address_wins_on_mainnet():
    s = _settings(
        exchange_mode="mainnet",
        vault_tracking_address="0xAAAA000000000000000000000000000000000001",
        hyperliquid_account_address="0xBBBB000000000000000000000000000000000002",
    )
    assert (
        s.effective_vault_tracking_address
        == "0xaaaa000000000000000000000000000000000001"
    )


def test_explicit_vault_tracking_address_wins_on_testnet():
    """Explicit override is honored on every mode — only the FALLBACK
    is mainnet-gated."""
    s = _settings(
        exchange_mode="testnet",
        vault_tracking_address="0xAAAA000000000000000000000000000000000001",
        hyperliquid_account_address="0xBBBB000000000000000000000000000000000002",
    )
    assert (
        s.effective_vault_tracking_address
        == "0xaaaa000000000000000000000000000000000001"
    )


def test_mainnet_falls_back_to_hyperliquid_account_address():
    s = _settings(
        exchange_mode="mainnet",
        vault_tracking_address="",
        hyperliquid_account_address="0xCCCC000000000000000000000000000000000003",
    )
    assert (
        s.effective_vault_tracking_address
        == "0xcccc000000000000000000000000000000000003"
    )


def test_testnet_does_not_fall_back():
    """Testnet wallet isn't on mainnet, so the fallback would just
    return an address that has no vault holdings — better to return
    empty so the UI shows the empty-state instead of a misleading 'no
    holdings' for the wrong wallet."""
    s = _settings(
        exchange_mode="testnet",
        vault_tracking_address="",
        hyperliquid_account_address="0xCCCC000000000000000000000000000000000003",
    )
    assert s.effective_vault_tracking_address == ""


def test_paper_does_not_fall_back():
    s = _settings(
        exchange_mode="paper",
        vault_tracking_address="",
        hyperliquid_account_address="0xCCCC000000000000000000000000000000000003",
    )
    assert s.effective_vault_tracking_address == ""


def test_empty_when_neither_set_on_mainnet():
    s = _settings(
        exchange_mode="mainnet",
        vault_tracking_address="",
        hyperliquid_account_address="",
    )
    assert s.effective_vault_tracking_address == ""


def test_normalizes_whitespace_and_case():
    s = _settings(
        exchange_mode="mainnet",
        vault_tracking_address="  0xDDDD000000000000000000000000000000000004  ",
        hyperliquid_account_address="",
    )
    assert (
        s.effective_vault_tracking_address
        == "0xdddd000000000000000000000000000000000004"
    )
