"""Base strategy interface."""

from abc import ABC, abstractmethod

import pandas as pd

from hypertrade.engine.signals import Signal


def bars_elapsed(earlier, later, candle_before_later) -> int:
    """Closed bars from bar timestamp `earlier` to `later`.

    0 when they are the same bar, otherwise at least 1. One bar's duration
    is `later - candle_before_later` (the last two candle timestamps).
    Counting elapsed bars rather than "the timestamp changed" is what keeps
    a restored bar counter honest: the counter in a snapshot is as old as
    the snapshot, and the bars since then are derived here on the first
    candle after the restart. When the timestamps can't be subtracted, a
    changed timestamp counts as one bar.
    """
    if later == earlier:
        return 0
    try:
        bar = later - candle_before_later
        return max(1, int(round((later - earlier) / bar)))
    except Exception:
        return 1


class Strategy(ABC):
    name: str = "unnamed"
    symbol: str = "BTC"
    timeframe: str = "4h"
    # Correlation family (backlog "Correlation grouping"): strategies whose
    # signal math is near-identical share a `family` so the engine can refuse
    # to stack them — cdc_macd and macd_zero are both the EMA12/26 cross
    # (≡ MACD crossing zero) and would otherwise take effectively the same
    # trade in duplicate. When the allow_multi_coin Redis flag is False, the
    # runner blocks an OPEN not only when another strategy holds the same
    # coin (legacy rule) but also when another strategy of the same family
    # holds a position on ANY coin. A strategy with no known near-duplicate
    # declares its own name as family; None = no family-level exclusion
    # (names unknown to the registry keep the legacy per-coin behaviour).
    family: str | None = None
    # Leverage applied on the exchange for this strategy's positions.
    # 1 = no leverage. Position notional = MAX_POSITION_SIZE_USD * leverage.
    # Bot sets HyperLiquid leverage per coin to the max across strategies
    # touching that coin. Can be overridden at runtime via dashboard.
    leverage: int = 1
    # The split between position state and cooldown state. Attributes named
    # here are NOT position state: re-entry cooldown counters and the
    # bar-tracking baselines they advance from (audit M6 — the reason a flat
    # strategy has a Redis snapshot at all). `reset_position()` clears
    # everything `reset_state()` clears EXCEPT these. Default: none, i.e.
    # everything `reset_state()` touches is position state. A tuple, so the
    # class-level default is immutable.
    cooldown_attrs: tuple[str, ...] = ()
    # NOTE: do NOT use a class-level mutable default for `params` —
    # `params: dict = {}` would be shared across every instance, so any
    # strategy receiving an unknown kwarg would leak the value into
    # every other strategy's `params`. Initialize per-instance in
    # __init__ instead. Audit M1 (2026-05-09).

    def __init__(self, **kwargs: object) -> None:
        self.params: dict = {}
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                self.params[key] = value

    @abstractmethod
    async def on_candle(self, candles: pd.DataFrame) -> Signal | None:
        """Called with latest candle data. Return a Signal or None to hold."""
        ...

    def on_filled(self, side: str, fill_price: float) -> None:
        """Called by the engine AFTER an OPEN order fills on the exchange.

        Re-anchors the strategy's entry to the ACTUAL fill price rather than
        the closed-bar `close` it estimated at signal time. Slippage,
        market-order fills, and paper fill-at-current-price all make the real
        fill differ from the bar close, so any percentage/ATR bracket derived
        from the estimate is slightly wrong (origin: the 2026-05-14 oleg
        paper-loop investigation, "Fix C").

        Default: set ``_entry_price = fill_price`` if the strategy tracks one,
        else no-op. Strategies whose SL/TP/trail are derived from entry at
        OPEN time (not recomputed every bar from ``_entry_price``) must
        override to re-derive those brackets from ``fill_price``.

        Called only on OPEN fills; closes don't need it. The engine wraps the
        call in try/except — a bug here cannot break the execution path.
        """
        if hasattr(self, "_entry_price"):
            self._entry_price = fill_price

    def restore_state(self, side: str, entry_price: float) -> None:
        """Restore in-memory position state after restart. Override in stateful strategies."""

    def reset_state(self) -> None:
        """Clear in-memory position state. Called by the engine when a position
        is closed outside the normal signal path (reconcile orphan close,
        manual flat, exchange-side liquidation). Without this, _in_position
        stays True in RAM while DB shows closed, and the strategy refuses to
        re-enter (or worse, the next restart re-enters duplicately).

        Default no-op. Stateful strategies must override and reset their
        flags (_in_position, _stop_loss, etc.) to a clean uninitialized state.
        """

    def reset_position(self) -> None:
        """Clear position state but keep the `cooldown_attrs`.

        `reset_state()` is free to clear cooldown too (hash_momentum's
        does); this is the variant for "the DB says you are flat, but
        whatever cooldown you were in still applies".
        """
        kept = {
            attr: getattr(self, attr)
            for attr in self.cooldown_attrs
            if hasattr(self, attr)
        }
        self.reset_state()
        for attr, value in kept.items():
            setattr(self, attr, value)

    def holds_position(self) -> bool:
        """Whether in-memory state says this strategy holds a position.

        Generic over the flag conventions the strategies use: a non-None
        `_position_side`, or a truthy `_in_position` / `_in_long` /
        `_in_short`. A strategy that tracks its position some other way
        must override (tests/test_strategies/test_restore_flat.py checks
        every registered strategy that exports state).
        """
        if getattr(self, "_position_side", None) is not None:
            return True
        return any(
            bool(getattr(self, attr, False))
            for attr in ("_in_position", "_in_long", "_in_short")
        )

    def restore_cooldown_only(self, state: dict) -> bool:
        """Restore a flat strategy from its Redis snapshot.

        The startup path for a strategy with NO open DB row. The snapshot
        exists for the cooldown fields (audit M6), but it is written after
        every executed signal — including OPENs — and was never cleared when
        a position closed outside the strategy's own signal. Every
        `restore_from_json` prefers the dict's position flags over the
        `side` argument, so restoring it as-is brought a flat strategy back
        believing it held a position it would never re-enter or close.

        Restores the dict, then clears position state with
        `reset_position()`, so the strategy always ends FLAT with its
        cooldown fields kept. Position state is cleared even if
        `restore_from_json` raises. Returns True when the snapshot had put
        the strategy in a position that was discarded.
        """
        held = False
        try:
            self.restore_from_json("flat", 0.0, state)
            held = self.holds_position()
        finally:
            self.reset_position()
        return held

    def export_state(self) -> dict | None:
        """Return a JSON-serializable dict of internal state at signal time.

        Strategies that maintain SL/TP state should override this to capture
        the exact values used when the position was opened. The runner stores
        the result in PositionRecord.state_json. On restart, restore_from_json()
        receives the same dict back.

        The runner also snapshots it to Redis after every executed signal
        and every engine-driven reset; None deletes that snapshot. A flat
        strategy is restored from the snapshot via `restore_cooldown_only`,
        which keeps only its `cooldown_attrs`.

        Default returns None — strategies without state need not override.
        """
        return None

    def restore_from_json(
        self, side: str, entry_price: float, state: dict
    ) -> None:
        """Restore exact state from previously-exported JSON dict.

        Falls back to recompute-style restore_state if the strategy doesn't
        override this. Override for strategies where SL/TP at signal time
        differs from the recomputed-from-entry value.
        """
        self.restore_state(side, entry_price)

    def configure(self, params: dict) -> None:
        """Update strategy parameters."""
        # Defensive: subclasses might override __init__ without calling
        # super().__init__(); ensure per-instance params dict exists.
        if not hasattr(self, "params"):
            self.params = {}
        for key, value in params.items():
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                self.params[key] = value

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.symbol} {self.timeframe}>"
