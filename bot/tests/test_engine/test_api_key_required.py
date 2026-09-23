"""The bot API refuses to start unauthenticated on testnet/mainnet.

analysis-2026-09-15 § 5, Low. `_require_auth` treats an empty
`settings.api_key` as "auth disabled" and waves every request through,
control routes included — pause, resume, flat-all, strategy toggle,
leverage, kill-switch. That default is there so `paper` stays trivial
to run locally, where the worst case is a corrupted simulation. On
testnet or mainnet it means anything that can reach the port can
flatten positions or re-leverage the account.

Orchestrator-spawned bots always get a generated key, so this should
be unreachable in production. That is the reason to assert it rather
than the reason to skip it: an unreachable safety default that
silently stops being unreachable is how this kind of thing ships.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hypertrade import api as api_module


def _set(mode: str, api_key: str):
    """Patch both settings fields the guard reads.

    `is_paper` / `is_testnet` / `is_mainnet` are properties derived
    from `exchange_mode`, so setting that one field is enough.
    """
    return (
        patch.object(api_module.settings, "exchange_mode", mode),
        patch.object(api_module.settings, "api_key", api_key),
    )


@pytest.mark.parametrize("mode", ["testnet", "mainnet"])
def test_assert_raises_on_live_mode_without_key(mode: str):
    mode_patch, key_patch = _set(mode, "")
    with mode_patch, key_patch:
        with pytest.raises(api_module.ApiKeyRequiredError) as exc:
            api_module._assert_api_key_set()
    message = str(exc.value)
    assert "API_KEY" in message
    assert mode in message


@pytest.mark.parametrize("mode", ["testnet", "mainnet"])
def test_assert_passes_on_live_mode_with_key(mode: str):
    mode_patch, key_patch = _set(mode, "a-real-key")
    with mode_patch, key_patch:
        api_module._assert_api_key_set()  # must not raise


def test_assert_allows_paper_without_key(caplog):
    """Paper keeps the open default — but says so."""
    mode_patch, key_patch = _set("paper", "")
    with mode_patch, key_patch:
        with caplog.at_level("WARNING"):
            api_module._assert_api_key_set()
    assert any("UNAUTHENTICATED" in r.message for r in caplog.records)


def test_error_message_does_not_leak_the_key():
    """The guard names the variable, never a value."""
    mode_patch, key_patch = _set("mainnet", "")
    with mode_patch, key_patch:
        with pytest.raises(api_module.ApiKeyRequiredError) as exc:
            api_module._assert_api_key_set()
    # Nothing secret exists in this path, but pin the shape so a future
    # "helpful" f-string doesn't start interpolating settings values.
    assert "settings.api_key" not in str(exc.value)


async def test_start_api_server_refuses_to_bind_without_key():
    """The guard runs before the socket, not per request."""
    mode_patch, key_patch = _set("testnet", "")
    with mode_patch, key_patch:
        with patch.object(api_module.web, "AppRunner") as runner_cls:
            with pytest.raises(api_module.ApiKeyRequiredError):
                await api_module.start_api_server(
                    port=18999,
                    control=MagicMock(),
                    exchange=MagicMock(),
                    strategies=[],
                )
            # Never got as far as constructing the runner, so nothing
            # was ever listening.
            runner_cls.assert_not_called()


async def test_start_api_server_binds_when_the_key_is_set():
    """The happy path still reaches AppRunner + TCPSite."""
    mode_patch, key_patch = _set("testnet", "a-real-key")
    runner = MagicMock()
    runner.setup = AsyncMock()
    site = MagicMock()
    site.start = AsyncMock()
    with mode_patch, key_patch:
        with patch.object(api_module.web, "AppRunner", return_value=runner):
            with patch.object(api_module.web, "TCPSite", return_value=site):
                result = await api_module.start_api_server(
                    port=18999,
                    control=MagicMock(),
                    exchange=MagicMock(),
                    strategies=[],
                )
    assert result is runner
    runner.setup.assert_awaited_once()
    site.start.assert_awaited_once()
