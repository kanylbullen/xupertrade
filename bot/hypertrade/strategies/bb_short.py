"""BB Upper breakout Short +2% — Pine v5 port (by @DrZiuber).

Direct Python port of the TradingView source. Shorts when price spikes
above the upper Bollinger Band by a configurable buffer, then takes a
fixed % profit when price retraces.

Entry (short, only when flat):
    ref_price = high (or close, configurable)
    Trigger when ref_price > upper_BB × (1 + breakout_pct)

Where:
    upper_BB = SMA(close, 20) + 2.0 × stdev(close, 20)

Exit (take-profit only — no stop loss):
    Limit fill at entry × (1 − take_profit_pct).
    Bar's low touching that level closes the trade at the limit price.

Source uses fixed $10k cash per trade on $40k initial capital (~25% of
equity, 1× leverage). Match leverage = 1 here.
"""

import pandas as pd
import pandas_ta as pta

from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.strategies.base import Strategy
from hypertrade.strategies.registry import register


@register
class BBShortStrategy(Strategy):
    name = "bb_short"
    symbol = "SOL"
    timeframe = "1h"
    leverage = 1

    bb_period: int = 20
    bb_std: float = 2.0
    breakout_pct: float = 0.02  # 2% above upper band
    take_profit_pct: float = 0.02  # 2% profit target
    use_high: bool = True  # if False, fall back to close-based trigger

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._entry_price: float | None = None
        self._tp_level: float | None = None

    def restore_state(self, side: str, entry_price: float) -> None:
        self._entry_price = entry_price
        self._tp_level = entry_price * (1 - self.take_profit_pct)

    async def on_candle(self, candles: pd.DataFrame) -> Signal | None:
        if len(candles) < self.bb_period + 5:
            return None

        latest = candles.iloc[-1]
        high = float(latest["high"])
        low = float(latest["low"])
        close = float(latest["close"])

        # ----- Manage open short: TP via limit fill -----
        if self._entry_price is not None and self._tp_level is not None:
            if low <= self._tp_level:
                tp = self._tp_level
                entry = self._entry_price
                self._entry_price = None
                self._tp_level = None
                return Signal(
                    action=SignalAction.CLOSE_SHORT,
                    symbol=self.symbol,
                    price=tp,  # limit fill at exact TP level
                    strategy_name=self.name,
                    reason=f"TP −{self.take_profit_pct:.0%} filled at ${tp:,.4f} (entry ${entry:,.4f}, low ${low:,.4f})",
                )
            return None

        # ----- Flat: look for breakout -----
        bb = pta.bbands(candles["close"], length=self.bb_period, std=self.bb_std)
        if bb is None:
            return None
        upper_col = next((c for c in bb.columns if c.startswith("BBU_")), None)
        if upper_col is None:
            return None
        upper = bb[upper_col].iloc[-1]
        if pd.isna(upper):
            return None
        upper = float(upper)

        ref_price = high if self.use_high else close
        threshold = upper * (1 + self.breakout_pct)

        if ref_price > threshold:
            # Source uses strategy.entry at bar close → fills at next bar's open in PineScript.
            # We approximate: entry fills at close of the trigger bar.
            self._entry_price = close
            self._tp_level = close * (1 - self.take_profit_pct)
            return Signal(
                action=SignalAction.OPEN_SHORT,
                symbol=self.symbol,
                strategy_name=self.name,
                take_profit=self._tp_level,
                reason=(
                    f"BB upper breakout: {'high' if self.use_high else 'close'} ${ref_price:,.4f} > "
                    f"upper ${upper:,.4f} × {1 + self.breakout_pct:.2f} (=${threshold:,.4f})"
                ),
            )

        return None
