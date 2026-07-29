"""Tests for strategy metadata loading.

The metadata files are documentation, so the guiding rule is that bad
documentation must never stop the bot from booting or trading — every
failure mode degrades to "no prose for this strategy" rather than
raising.
"""

from __future__ import annotations

import json

import pytest

from hypertrade.strategies import meta_loader
from hypertrade.strategies.meta_loader import (
    load_all_metadata,
    metadata_for,
)
from hypertrade.strategies.registry import list_strategies, load_all


@pytest.fixture(autouse=True)
def _clear_cache():
    load_all_metadata.cache_clear()
    yield
    load_all_metadata.cache_clear()


def test_every_registered_strategy_has_metadata():
    """Not required by the loader — an undocumented strategy still
    lists, just without prose — but true today, and this is the reminder
    when someone adds a strategy without docs.

    `load_all()` first so the registry is fully populated regardless of
    which other tests ran before this one. Without it the assertion
    silently weakens to "every strategy imported so far", which is how
    `ath_breakout` stayed undocumented: the dashboard's hardcoded page
    listed 21 strategies while the bot had 22 registered.
    """
    load_all()
    documented = set(load_all_metadata())
    registered = set(list_strategies())
    missing = registered - documented
    assert not missing, f"strategies with no meta/<name>.json: {sorted(missing)}"


def test_metadata_never_carries_live_fields():
    """name/symbol/timeframe are owned by the registered strategy
    object. If a JSON file could supply them, stale documentation would
    be able to rename or re-symbol a running strategy in the UI."""
    for name, meta in load_all_metadata().items():
        for forbidden in ("name", "symbol", "timeframe"):
            assert forbidden not in meta, f"{name} leaked {forbidden}"


def test_known_strategy_has_the_documented_shape():
    meta = metadata_for("bb_short")
    assert meta["summary"]
    assert isinstance(meta["logic"], list) and meta["logic"]
    assert isinstance(meta["strengths"], list)
    assert isinstance(meta["weaknesses"], list)
    assert isinstance(meta["params"], dict)
    assert set(meta["stats"]) == {
        "apr",
        "sharpe",
        "maxDrawdown",
        "winRate",
        "trades",
    }


def test_undocumented_strategy_returns_empty_not_error():
    assert metadata_for("no_such_strategy") == {}


def test_unknown_keys_are_dropped(tmp_path, monkeypatch):
    (tmp_path / "fake.json").write_text(
        json.dumps({"summary": "ok", "sneaky": "should not pass through"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(meta_loader, "_META_DIR", tmp_path)
    load_all_metadata.cache_clear()
    assert metadata_for("fake") == {"summary": "ok"}


def test_malformed_json_is_skipped_not_raised(tmp_path, monkeypatch):
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "good.json").write_text(
        json.dumps({"summary": "fine"}), encoding="utf-8"
    )
    monkeypatch.setattr(meta_loader, "_META_DIR", tmp_path)
    load_all_metadata.cache_clear()
    loaded = load_all_metadata()
    # The broken file must not take the good one down with it.
    assert "broken" not in loaded
    assert loaded["good"] == {"summary": "fine"}


def test_non_object_json_is_skipped(tmp_path, monkeypatch):
    (tmp_path / "list.json").write_text("[1, 2, 3]", encoding="utf-8")
    monkeypatch.setattr(meta_loader, "_META_DIR", tmp_path)
    load_all_metadata.cache_clear()
    assert load_all_metadata() == {}


def test_missing_directory_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(meta_loader, "_META_DIR", tmp_path / "does-not-exist")
    load_all_metadata.cache_clear()
    assert load_all_metadata() == {}
