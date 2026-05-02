"""Strategy registry for discovery and instantiation."""

from hypertrade.strategies.base import Strategy

_REGISTRY: dict[str, type[Strategy]] = {}


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
