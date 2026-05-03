"""Verify all strategies implement reset_state() correctly.

Reconcile calls reset_state() when it closes a DB position outside the
normal signal path. If a strategy keeps _in_position=True in RAM after
its DB row has been orphan-closed, the next signal won't open (already
in position) and the next restart re-enters duplicately.
"""

from hypertrade.strategies.registry import get_strategy, list_strategies, load_all


def test_all_strategies_reset_state_clears_position():
    """For every strategy that supports restore_state, reset_state must
    clear it back to the uninitialized state."""
    load_all()
    for name in list_strategies():
        strat = get_strategy(name)
        # Simulate having an open position
        try:
            strat.restore_state("long", 100.0)
        except Exception:
            # Strategies that can't go long (e.g. bb_short, vvv_hedge): try short
            try:
                strat.restore_state("short", 100.0)
            except Exception:
                continue  # stateless strategy

        # Now reset
        strat.reset_state()

        # Verify any common state flags are cleared
        for attr in (
            "_in_position", "_in_long", "_in_short",
            "_position_side", "_entry_price", "_stop_loss",
            "_take_profit", "_sl", "_tp", "_entry",
        ):
            if hasattr(strat, attr):
                val = getattr(strat, attr)
                assert val in (False, None, 0.0), (
                    f"{name}.{attr} = {val!r} after reset_state() — should be False/None/0"
                )
