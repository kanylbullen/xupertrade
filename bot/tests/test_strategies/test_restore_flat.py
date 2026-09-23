"""A strategy with no open DB row restores FLAT, keeping only its cooldown.

Production, 2026-09-23 (testnet restarted on 9b0b103): DB and exchange
agreed the bot held two positions, yet seven of fifteen enabled strategies
restored as holding one — btc_mean_reversion short @78834 (closed
2026-09-09), hash_supertrend short @78424 (closed 2026-09-10),
keltner_breakout, pivot_supertrend, kalman_breakout, bb_rsi_scalper and
hash_momentum; paper did the same for qullamagi_breakout and
daily_long_0830. The runner snapshots `export_state()` to Redis after every
executed signal, OPENs included, and nothing overwrote that snapshot when
the position closed outside the strategy's own signal. At startup every
flat strategy got `restore_from_json("flat", 0.0, snapshot)`, and every
`restore_from_json` prefers the dict's position flags over `side`.

`Strategy.restore_cooldown_only` is the startup path now: restore, then
`reset_position()`, which clears everything `reset_state()` clears except
the strategy's declared `cooldown_attrs`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hypertrade.strategies.registry import get_strategy, list_strategies, load_all

load_all()
ALL = sorted(list_strategies())

# The ones production restored into phantom positions on 2026-09-23
# (testnet + paper).
PRODUCTION_AFFECTED = [
    "btc_mean_reversion", "hash_supertrend", "keltner_breakout",
    "pivot_supertrend", "kalman_breakout", "bb_rsi_scalper", "hash_momentum",
    "qullamagi_breakout", "daily_long_0830",
]

# A snapshot claiming a position under every flag convention the
# strategies use. Covers strategies that never export (their restore falls
# back to `restore_state("flat", 0.0)`, which for some means "in position").
CLAIMS_A_POSITION = {
    "in_position": True, "in_long": True, "in_short": False,
    "position_side": "long", "entry_price": 100.0, "entry": 100.0,
    "stop_loss": 90.0, "take_profit": 110.0, "sl": 90.0, "tp": 110.0,
}

COOLDOWN_TS = datetime(2026, 9, 20, 8, tzinfo=timezone.utc)


def _cooldown_sample(default):
    """A value for a cooldown attr that differs from its fresh default and
    survives the export → JSON → restore round trip."""
    if isinstance(default, bool):
        return not default
    if isinstance(default, int):
        return 2
    return COOLDOWN_TS


def _in_position(name: str):
    """A strategy instance holding a position, via its own restore_state."""
    for side in ("long", "short"):
        strat = get_strategy(name)
        strat.restore_state(side, 100.0)
        if strat.holds_position():
            return strat
    return None


def _position_attrs(strat) -> dict:
    """Every private attribute except the declared cooldown ones."""
    return {
        k: v for k, v in vars(strat).items()
        if k.startswith("_") and k not in strat.cooldown_attrs
    }


def _snapshot_in_position(name: str) -> tuple[dict, dict]:
    """(snapshot, cooldown values it carries) captured while in position,
    with every cooldown attr set away from its default first."""
    strat = _in_position(name)
    cooldown: dict = {}
    real: dict = {}
    if strat is not None:
        for attr in strat.cooldown_attrs:
            value = _cooldown_sample(getattr(get_strategy(name), attr))
            setattr(strat, attr, value)
            cooldown[attr] = value
        real = strat.export_state() or {}
    carried = {a: v for a, v in cooldown.items() if a[1:] in real}
    return {**CLAIMS_A_POSITION, **real}, carried


@pytest.mark.parametrize("name", ALL)
def test_restore_cooldown_only_ends_flat_and_keeps_cooldown(name):
    snapshot, carried = _snapshot_in_position(name)

    strat = get_strategy(name)
    strat.restore_cooldown_only(snapshot)

    assert not strat.holds_position(), f"{name} restored into a position"
    # Flat means flat: every non-cooldown field equals a fresh instance's.
    assert _position_attrs(strat) == _position_attrs(get_strategy(name))
    for attr, value in carried.items():
        assert getattr(strat, attr) == value, (
            f"{name}.{attr} from the snapshot was lost"
        )


def test_cooldown_survival_is_actually_exercised():
    """Guard against the test above passing vacuously: the strategies whose
    snapshot carries a cooldown field must be in the set it checks."""
    carrying = {n for n in ALL if _snapshot_in_position(n)[1]}
    assert {"hash_momentum", "supertrend"} <= carrying


@pytest.mark.parametrize("name", PRODUCTION_AFFECTED)
def test_production_strategies_were_restored_in_position_before_the_fix(name):
    """Pins the bug: the old startup call on the same snapshot leaves the
    strategy holding a position; the new one reports it forced it flat."""
    snapshot, _ = _snapshot_in_position(name)

    old = get_strategy(name)
    old.restore_from_json("flat", 0.0, snapshot)
    assert old.holds_position()

    new = get_strategy(name)
    assert new.restore_cooldown_only(snapshot) is True
    assert not new.holds_position()


def test_hash_momentum_cooldown_only_snapshot_is_kept_and_not_reported():
    """The snapshot's real job (audit M6): a flat strategy inside its
    post-close cooldown keeps the cooldown across a restart."""
    closed = get_strategy("hash_momentum")
    closed.restore_state("long", 100.0)
    closed._in_long = False           # what its own SL/TP close does
    closed._bars_since_close = 1
    closed._last_closed_bar_ts = COOLDOWN_TS
    snapshot = closed.export_state()

    strat = get_strategy("hash_momentum")
    assert strat.restore_cooldown_only(snapshot) is False
    assert not strat.holds_position()
    assert strat._bars_since_close == 1
    assert strat._last_closed_bar_ts == COOLDOWN_TS


@pytest.mark.parametrize("name", ALL)
def test_reset_position_keeps_cooldown_attrs_in_memory(name):
    strat = _in_position(name) or get_strategy(name)
    expected = {}
    for attr in strat.cooldown_attrs:
        expected[attr] = _cooldown_sample(getattr(get_strategy(name), attr))
        setattr(strat, attr, expected[attr])

    strat.reset_position()

    assert not strat.holds_position()
    assert {a: getattr(strat, a) for a in strat.cooldown_attrs} == expected


@pytest.mark.parametrize("name", ALL)
def test_cooldown_attrs_name_real_attributes(name):
    strat = get_strategy(name)
    for attr in strat.cooldown_attrs:
        assert hasattr(strat, attr), f"{name}.cooldown_attrs names {attr!r}"


@pytest.mark.parametrize("name", ALL)
def test_holds_position_sees_every_strategys_position(name):
    """`holds_position()` is generic over flag conventions. A strategy
    tracking its position some other way must override it, or the startup
    log and the flatness checks above would not see its position."""
    assert not get_strategy(name).holds_position()
    assert _in_position(name) is not None, (
        f"{name}: restore_state put it in a position holds_position() "
        f"does not see — override holds_position()"
    )
    strat = _in_position(name)
    strat.reset_state()
    assert not strat.holds_position()


def test_restore_cooldown_only_clears_position_even_when_restore_raises():
    strat = get_strategy("btc_mean_reversion")

    def _boom(side, entry_price, state):
        strat._position_side = "short"
        raise ValueError("corrupt snapshot")

    strat.restore_from_json = _boom
    with pytest.raises(ValueError):
        strat.restore_cooldown_only({"position_side": "short"})
    assert not strat.holds_position()
