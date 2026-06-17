"""Tests for the Strategy base class.

Covers the `params` per-instance fix (audit M1, 2026-05-09): the
class-level mutable default was being shared across instances, so
any unknown kwarg leaked into every other strategy's `params` dict.
"""

from __future__ import annotations

from hypertrade.strategies.base import Strategy


class _Dummy(Strategy):
    name = "dummy"

    async def on_candle(self, candles):  # type: ignore[override]
        return None


def test_params_is_per_instance_not_shared_class_state():
    """Two instances must NOT see each other's unknown kwargs."""
    a = _Dummy(some_unknown_a="va")
    b = _Dummy(some_unknown_b="vb")
    assert a.params == {"some_unknown_a": "va"}
    assert b.params == {"some_unknown_b": "vb"}
    # Class-level state should not have either
    assert "some_unknown_a" not in getattr(_Dummy, "params", {})
    assert "some_unknown_b" not in getattr(_Dummy, "params", {})


def test_configure_creates_params_when_missing():
    """If subclass skipped super().__init__, configure must still work."""
    s = _Dummy.__new__(_Dummy)  # bypass __init__
    s.configure({"newkey": 42})
    assert s.params == {"newkey": 42}


def test_configure_setattr_for_known_attrs():
    """Known class attrs are set directly, not into params."""
    s = _Dummy()
    s.configure({"leverage": 3, "unknown": "x"})
    assert s.leverage == 3
    assert s.params == {"unknown": "x"}


# ----- on_filled hook (Fix C from oleg paper-loop investigation) -----

_TOL = 1e-9


def test_on_filled_default_sets_entry_price_when_attr_exists():
    """Base default: anchor _entry_price to the real fill if the strategy
    tracks one (re-anchoring covers vol_breakout, which recomputes its
    ATR-SL from _entry_price every bar)."""

    class _WithEntry(Strategy):
        name = "with_entry"

        def __init__(self, **kw):
            super().__init__(**kw)
            self._entry_price = 100.0

        async def on_candle(self, candles):  # type: ignore[override]
            return None

    s = _WithEntry()
    s.on_filled("long", 101.5)
    assert abs(s._entry_price - 101.5) < _TOL


def test_on_filled_default_noop_when_no_entry_attr():
    """Strategies that don't track entry (e.g. rsi_momentum) are unaffected."""
    s = _Dummy()
    s.on_filled("long", 123.0)  # must not raise, must not invent the attr
    assert not hasattr(s, "_entry_price")
