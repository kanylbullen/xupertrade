"""HODL signal registry."""

from hypertrade.hodl.base import Signal

_REGISTRY: dict[str, type[Signal]] = {}


def register(cls: type[Signal]) -> type[Signal]:
    if not getattr(cls, "name", None) or cls.name == "unnamed":
        raise ValueError(f"Signal {cls!r} must declare a `name` class attribute")
    _REGISTRY[cls.name] = cls
    return cls


def get_signal(name: str) -> Signal:
    if name not in _REGISTRY:
        raise ValueError(f"No HODL signal named {name!r}. Registered: {list(_REGISTRY)}")
    return _REGISTRY[name]()


def list_signals() -> list[str]:
    return sorted(_REGISTRY)


def all_signals() -> list[Signal]:
    return [_REGISTRY[name]() for name in sorted(_REGISTRY)]


def load_all() -> None:
    """Import all signal modules so they self-register."""
    import hypertrade.hodl.hype_accumulation  # noqa: F401
    import hypertrade.hodl.altseason  # noqa: F401
    import hypertrade.hodl.btc_accumulation_zone  # noqa: F401
    import hypertrade.hodl.macro_backdrop  # noqa: F401
    import hypertrade.hodl.vault_picks  # noqa: F401
