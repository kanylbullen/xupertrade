"""Optimized BTC Mean Reversion (RSI 20/65) — Pine v5 port.

Direct Python port of the TradingView source. Mean-reversion strategy
using RSI extremes with Stochastic confirmation and an EMA position filter.
Fixed % stop loss and take profit.

Long entry (all must be true on a closed candle, position must be flat):
    rsi(14) < 20
    AND stoch_k(14) < 25                     # raw fast %K (no smoothing)
    AND close > ema(close, 200) * 0.9        # not too far below trend

Short entry (all must be true on a closed candle, position must be flat):
    rsi(14) > 65
    AND stoch_k(14) > 75
    AND close < ema(close, 200)              # below trend

Exit (managed bar-by-bar inside the strategy, mirrors strategy.exit):
    Long:  stop = entry × (1 − 4%),  limit = entry × (1 + 6%)
    Short: stop = entry × (1 + 4%),  limit = entry × (1 − 6%)
    Whichever is hit first by the bar's high/low closes the trade.

Source uses 100% of equity at 1x leverage. Set leverage=1 here so the
runner's position-size formula `MAX_POSITION_SIZE_USD * leverage` matches
that. The user can scale risk via MAX_POSITION_SIZE_USD.
"""

import pandas as pd
import pandas_ta as pta

from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.strategies.base import Strategy
from hypertrade.strategies.registry import register


@register
class BTCMeanReversionStrategy(Strategy):
    name = "btc_mean_reversion"
    symbol = "BTC"
    timeframe = "15m"
    leverage = 1

    # Indicator parameters (mirror source)
    ema_length: int = 200
    rsi_period: int = 14
    rsi_bull_level: float = 20.0
    rsi_bear_level: float = 65.0
    stoch_length: int = 14
    stoch_smooth_d: int = 3  # only used for the (unused) signal line
    stoch_overbought: float = 75.0
    stoch_oversold: float = 25.0

    # Risk
    stop_loss_pct: float = 0.04
    take_profit_pct: float = 0.06

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._position_side: str | None = None
        self._entry_price: float | None = None
        self._stop_loss: float | None = None
        self._take_profit: float | None = None

    def restore_state(self, side: str, entry_price: float) -> None:
        self._position_side = side
        self._entry_price = entry_price
        if side == "long":
            self._stop_loss = entry_price * (1 - self.stop_loss_pct)
            self._take_profit = entry_price * (1 + self.take_profit_pct)
        else:
            self._stop_loss = entry_price * (1 + self.stop_loss_pct)
            self._take_profit = entry_price * (1 - self.take_profit_pct)

    def export_state(self) -> dict | None:
        if self._position_side is None:
            return None
        return {
            "position_side": self._position_side,
            "entry_price": self._entry_price,
            "stop_loss": self._stop_loss,
            "take_profit": self._take_profit,
        }

    def restore_from_json(
        self, side: str, entry_price: float, state: dict
    ) -> None:
        self._position_side = state.get("position_side", side)
        self._entry_price = state.get("entry_price", entry_price)
        self._stop_loss = state.get("stop_loss")
        self._take_profit = state.get("take_profit")

    def _reset_position_state(self) -> None:
        self._position_side = None
        self._entry_price = None
        self._stop_loss = None
        self._take_profit = None

    def reset_state(self) -> None:
        self._reset_position_state()

    async def on_candle(self, candles: pd.DataFrame) -> Signal | None:
        if len(candles) < self.ema_length + 5:
            return None

        latest = candles.iloc[-1]
        high = float(latest["high"])
        low = float(latest["low"])
        close = float(latest["close"])

        # ----- Manage open position first -----
        if self._position_side is not None and self._entry_price is not None:
            sl = self._stop_loss
            tp = self._take_profit
            if self._position_side == "long":
                if sl is not None and low <= sl:
                    self._reset_position_state()
                    return Signal(
                        action=SignalAction.CLOSE_LONG,
                        symbol=self.symbol,
                        strategy_name=self.name,
                        reason=f"SL hit at ${sl:,.2f} (low ${low:,.2f})",
                    )
                if tp is not None and high >= tp:
                    self._reset_position_state()
                    return Signal(
                        action=SignalAction.CLOSE_LONG,
                        symbol=self.symbol,
                        strategy_name=self.name,
                        reason=f"TP hit at ${tp:,.2f} (high ${high:,.2f})",
                    )
            else:  # short
                if sl is not None and high >= sl:
                    self._reset_position_state()
                    return Signal(
                        action=SignalAction.CLOSE_SHORT,
                        symbol=self.symbol,
                        strategy_name=self.name,
                        reason=f"SL hit at ${sl:,.2f} (high ${high:,.2f})",
                    )
                if tp is not None and low <= tp:
                    self._reset_position_state()
                    return Signal(
                        action=SignalAction.CLOSE_SHORT,
                        symbol=self.symbol,
                        strategy_name=self.name,
                        reason=f"TP hit at ${tp:,.2f} (low ${low:,.2f})",
                    )
            return None

        # ----- Flat: look for entry -----
        df = candles.copy()
        df["ema"] = pta.ema(df["close"], length=self.ema_length)
        df["rsi"] = pta.rsi(df["close"], length=self.rsi_period)
        # Source uses raw fast %K (no smoothing): smooth_k=1
        stoch_df = pta.stoch(
            df["high"], df["low"], df["close"],
            k=self.stoch_length, d=self.stoch_smooth_d, smooth_k=1,
        )
        if stoch_df is None:
            return None
        k_col = next((c for c in stoch_df.columns if c.startswith("STOCHk_")), None)
        if k_col is None:
            return None
        df["stoch_k"] = stoch_df[k_col]

        latest = df.iloc[-1]
        for col in ("ema", "rsi", "stoch_k"):
            if pd.isna(latest[col]):
                return None

        ema_val = float(latest["ema"])
        rsi_val = float(latest["rsi"])
        stoch_k = float(latest["stoch_k"])

        long_signal = (
            rsi_val < self.rsi_bull_level
            and stoch_k < self.stoch_oversold
            and close > ema_val * 0.9
        )
        short_signal = (
            rsi_val > self.rsi_bear_level
            and stoch_k > self.stoch_overbought
            and close < ema_val
        )

        if long_signal:
            self._position_side = "long"
            self._entry_price = close
            self._stop_loss = close * (1 - self.stop_loss_pct)
            self._take_profit = close * (1 + self.take_profit_pct)
            return Signal(
                action=SignalAction.OPEN_LONG,
                symbol=self.symbol,
                strategy_name=self.name,
                stop_loss=self._stop_loss,
                take_profit=self._take_profit,
                reason=(
                    f"Mean reversion long: RSI {rsi_val:.1f}<20, %K {stoch_k:.1f}<25, "
                    f"close ${close:,.2f} > EMA200×0.9 (${ema_val * 0.9:,.2f})"
                ),
            )

        if short_signal:
            self._position_side = "short"
            self._entry_price = close
            self._stop_loss = close * (1 + self.stop_loss_pct)
            self._take_profit = close * (1 - self.take_profit_pct)
            return Signal(
                action=SignalAction.OPEN_SHORT,
                symbol=self.symbol,
                strategy_name=self.name,
                stop_loss=self._stop_loss,
                take_profit=self._take_profit,
                reason=(
                    f"Mean reversion short: RSI {rsi_val:.1f}>65, %K {stoch_k:.1f}>75, "
                    f"close ${close:,.2f} < EMA200 (${ema_val:,.2f})"
                ),
            )

        return None
