"""Base strategy interface."""

from abc import ABC, abstractmethod

import pandas as pd

from hypertrade.engine.signals import Signal


class Strategy(ABC):
    name: str = "unnamed"
    symbol: str = "BTC"
    timeframe: str = "4h"
    # Leverage applied on the exchange for this strategy's positions.
    # 1 = no leverage. Position notional = MAX_POSITION_SIZE_USD * leverage.
    # Bot sets HyperLiquid leverage per coin to the max across strategies
    # touching that coin. Can be overridden at runtime via dashboard.
    leverage: int = 1
    params: dict = {}

    def __init__(self, **kwargs: object) -> None:
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                self.params[key] = value

    @abstractmethod
    async def on_candle(self, candles: pd.DataFrame) -> Signal | None:
        """Called with latest candle data. Return a Signal or None to hold."""
        ...

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

    def export_state(self) -> dict | None:
        """Return a JSON-serializable dict of internal state at signal time.

        Strategies that maintain SL/TP state should override this to capture
        the exact values used when the position was opened. The runner stores
        the result in PositionRecord.state_json. On restart, restore_from_json()
        receives the same dict back.

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
        for key, value in params.items():
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                self.params[key] = value

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.symbol} {self.timeframe}>"
