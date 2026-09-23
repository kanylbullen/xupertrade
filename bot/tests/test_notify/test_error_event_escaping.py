"""The error-event formatter escapes the strategy field.

Telegram's HTML parse mode rejects the whole message on a stray `<` or
`&`, so an unescaped value does not just render oddly — the alert is
lost (`bot/reports/analysis-2026-09-15.md` § 4, Low). The message field
was already escaped; the strategy field was not.
"""

from __future__ import annotations

from hypertrade.notify.telegram import _format_event


def test_error_strategy_is_html_escaped():
    text = _format_event({
        "type": "error",
        "mode": "testnet",
        "strategy": "parity/<BTC> & co",
        "message": "a < b",
    })
    assert "parity/&lt;BTC&gt; &amp; co" in text
    assert "<BTC>" not in text
    assert "a &lt; b" in text


def test_error_without_strategy_renders_empty_not_none():
    text = _format_event({"type": "error", "mode": "testnet", "message": "m"})
    assert "None" not in text
    assert text.endswith(": m")
