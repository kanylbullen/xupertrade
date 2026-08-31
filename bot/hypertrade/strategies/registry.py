"""Strategy registry for discovery and instantiation."""

from hypertrade.strategies.base import Strategy

_REGISTRY: dict[str, type[Strategy]] = {}

# Correlation families (backlog "Correlation grouping"): strategies whose
# signal math is near-identical share a `family` (see Strategy.family) so
# the engine's allow_multi_coin=False gate can refuse to stack them — at
# most one strategy per family may hold a position, across all coins.
# Grouped families:
#   macd_zero_cross — cdc_macd, macd_zero (EMA12/26 cross ≡ MACD zero cross)
#   supertrend      — supertrend, hash_supertrend, pivot_supertrend
#   keltner_channel — keltner_breakout, volatility_breakout
#   bollinger_band  — bb_short, bb_rsi_scalper
# A strategy with no known near-duplicate declares its own name as family,
# which keeps the "every registered strategy has a family" invariant
# meaningful without inventing artificial groupings. ema_crossover is
# deliberately NOT in macd_zero_cross: it is an EMA-cross too, but 7/19 on
# 1h with structural-SL exits — correlated, not near-identical, and the two
# trade different assets at different timescales.


def register(cls: type[Strategy]) -> type[Strategy]:
    """Decorator to register a strategy class."""
    _REGISTRY[cls.name] = cls
    return cls


def get_strategy(name: str, **kwargs: object) -> Strategy:
    """Instantiate a strategy by name."""
    if name not in _REGISTRY:
        available = ", ".join(_REGISTRY.keys())
        raise ValueError(f"Unknown strategy '{name}'. Available: {available}")
    return _REGISTRY[name](**kwargs)


def list_strategies() -> list[str]:
    """List all registered strategy names."""
    return list(_REGISTRY.keys())


def get_strategy_family(name: str) -> str | None:
    """Correlation family for a registered strategy, or None when unknown.

    Used by the runner's allow_multi_coin=False gate: when the flag is off,
    at most one strategy per family may hold a position, across all coins
    (see Strategy.family). Names not in the registry return None — such
    signals keep the legacy per-coin behaviour only.
    """
    cls = _REGISTRY.get(name)
    if cls is None:
        return None
    return getattr(cls, "family", None)


# Import strategy modules to trigger registration
def load_all() -> None:
    """Import all strategy modules so they register themselves."""
    import hypertrade.strategies.supertrend  # noqa: F401
    import hypertrade.strategies.rsi_momentum  # noqa: F401
    import hypertrade.strategies.bb_short  # noqa: F401
    import hypertrade.strategies.sma_rsi  # noqa: F401
    import hypertrade.strategies.volatility_breakout  # noqa: F401
    import hypertrade.strategies.btc_mean_reversion  # noqa: F401
    import hypertrade.strategies.hash_momentum  # noqa: F401
    import hypertrade.strategies.ema_crossover  # noqa: F401
    import hypertrade.strategies.cdc_macd  # noqa: F401
    import hypertrade.strategies.keltner_breakout  # noqa: F401
    import hypertrade.strategies.pivot_supertrend  # noqa: F401
    import hypertrade.strategies.macd_zero  # noqa: F401
    import hypertrade.strategies.moon_phases  # noqa: F401
    import hypertrade.strategies.penguin_volatility  # noqa: F401
    import hypertrade.strategies.daily_long_0830  # noqa: F401
    import hypertrade.strategies.kalman_breakout  # noqa: F401
    import hypertrade.strategies.bb_rsi_scalper  # noqa: F401
    import hypertrade.strategies.hash_supertrend  # noqa: F401
    import hypertrade.strategies.oleg_aryukov  # noqa: F401
    import hypertrade.strategies.qullamagi_breakout  # noqa: F401
    import hypertrade.strategies.vvv_hedge  # noqa: F401
    import hypertrade.strategies.ath_breakout  # noqa: F401
    # golden_cross — backtested 5y on BTC/ETH/SOL/DOGE/XRP/AVAX/SUI 1d via
    # Binance dump on 2026-05-04. All positive APR but lags buy-and-hold by
    # huge margin (BTC +1.5% vs hold +150%, SOL +4.3% vs hold +700%). Late
    # signal misses major uptrend phases. Not registered — file kept for
    # historical reference and tv-source/golden_cross.pine for the source.
