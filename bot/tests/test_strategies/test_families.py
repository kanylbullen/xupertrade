"""Strategy `family` attribute — correlation grouping invariants.

Backlog "Correlation grouping": cdc_macd and macd_zero are mathematically
near-identical (EMA12/26 cross ≡ MACD zero-cross) and must never stack.
Every registered strategy therefore declares a non-empty `family`; the
engine's allow_multi_coin=False gate refuses a second same-family position
across all coins (behaviour tested in test_engine/test_family_exclusion.py).
"""

from hypertrade.strategies.base import Strategy
from hypertrade.strategies.registry import (
    get_strategy_family,
    list_strategies,
    load_all,
)


def test_every_registered_strategy_declares_a_family():
    """New registrations must think about correlation grouping: the invariant
    fails until a `family` is declared (a strategy without one would silently
    opt out of the family-level allow_multi_coin exclusion)."""
    load_all()
    for name in list_strategies():
        family = get_strategy_family(name)
        assert family, f"strategy '{name}' must declare a non-empty family"


def test_cdc_macd_and_macd_zero_share_a_family():
    """The mandated grouping: both are the EMA12/26 cross ≡ MACD-zero-cross
    on 1d — holding both is effectively one trade in duplicate."""
    load_all()
    assert get_strategy_family("cdc_macd") == "macd_zero_cross"
    assert get_strategy_family("macd_zero") == "macd_zero_cross"


def test_supertrend_variants_share_a_family():
    load_all()
    families = {
        get_strategy_family(n)
        for n in ("supertrend", "hash_supertrend", "pivot_supertrend")
    }
    assert families == {"supertrend"}


def test_keltner_channel_variants_share_a_family():
    load_all()
    families = {
        get_strategy_family(n) for n in ("keltner_breakout", "volatility_breakout")
    }
    assert families == {"keltner_channel"}


def test_bollinger_variants_share_a_family():
    load_all()
    families = {get_strategy_family(n) for n in ("bb_short", "bb_rsi_scalper")}
    assert families == {"bollinger_band"}


def test_ema_crossover_is_not_grouped_with_macd_zero_cross():
    """ema_crossover is an EMA cross too, but 7/19 on 1h with structural-SL
    exits — correlated, not near-identical. Grouping it with the 12/26 1d
    pair would over-block genuinely different trades."""
    load_all()
    assert get_strategy_family("ema_crossover") == "ema_cross"
    assert get_strategy_family("ema_crossover") != get_strategy_family("cdc_macd")


def test_unknown_strategy_name_has_no_family():
    assert get_strategy_family("definitely_not_a_strategy") is None


def test_base_family_default_is_none():
    """The base class must not invent a family — only explicit declarations
    opt a strategy into family-level exclusion."""

    class _Anonymous(Strategy):
        name = "anonymous_family_test"

        async def on_candle(self, candles):
            return None

    assert _Anonymous.family is None
