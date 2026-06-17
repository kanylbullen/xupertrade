"""on_filled re-anchors %SL/TP brackets to the ACTUAL exchange fill.

Origin: investigations/oleg-paper-loop-2026-05-14.md "Fix C". At signal
time a strategy sets _entry_price from the closed bar's close; the real
fill differs (slippage / market fill / paper fill-at-current-price), so
percentage-derived SL/TP/trail are slightly wrong. on_filled corrects
them from the real fill price.
"""

from __future__ import annotations

import pandas as pd
import pytest

from hypertrade.strategies.bb_short import BBShortStrategy
from hypertrade.strategies.btc_mean_reversion import BTCMeanReversionStrategy
from hypertrade.strategies.oleg_aryukov import OlegAryukovStrategy
from hypertrade.strategies.supertrend import SuperTrendStrategy
from hypertrade.strategies.vvv_hedge import VVVHedgeStrategy

_TOL = 1e-6


def test_oleg_on_filled_rederives_brackets_from_fill():
    s = OlegAryukovStrategy()
    # Signal-time estimate (bar close) X = 2000; brackets derived from it.
    estimate = 2000.0
    s._position_side = "short"
    s._entry_price = estimate
    s._recompute_brackets()
    s._entry_bar_ts = pd.Timestamp("2026-05-14T15:00:00Z")
    est_sl = s._stop_loss

    # Real fill Y = 2010 differs from the bar close.
    fill = 2010.0
    s.on_filled("short", fill)

    sl_pct = s.stop_loss_percent / 100.0
    tp_pct = s.take_profit_percent / 100.0
    assert abs(s._entry_price - fill) < _TOL
    # short: SL above, TP below entry — derived from the FILL, not the estimate
    assert abs(s._stop_loss - fill * (1 + sl_pct)) < _TOL
    assert abs(s._take_profit - fill * (1 - tp_pct)) < _TOL
    assert abs(s._trail_extreme - fill) < _TOL
    # Sanity: it actually moved off the estimate-derived value
    assert abs(s._stop_loss - est_sl) > 1.0
    # Entry-bar timestamp (set at open) must survive on_filled so the
    # same-bar manage-skip guard keeps working.
    assert s._entry_bar_ts == pd.Timestamp("2026-05-14T15:00:00Z")


def test_oleg_on_filled_long_side():
    s = OlegAryukovStrategy()
    fill = 2500.0
    s.on_filled("long", fill)
    sl_pct = s.stop_loss_percent / 100.0
    tp_pct = s.take_profit_percent / 100.0
    assert s._position_side == "long"
    assert abs(s._stop_loss - fill * (1 - sl_pct)) < _TOL
    assert abs(s._take_profit - fill * (1 + tp_pct)) < _TOL


def test_btc_mean_reversion_on_filled_rederives_brackets():
    s = BTCMeanReversionStrategy()
    s._position_side = "long"
    s._entry_price = 50_000.0
    s.restore_state("long", 50_000.0)
    est_sl = s._stop_loss

    fill = 50_250.0
    s.on_filled("long", fill)
    assert abs(s._entry_price - fill) < _TOL
    assert abs(s._stop_loss - fill * (1 - s.stop_loss_pct)) < _TOL
    assert abs(s._take_profit - fill * (1 + s.take_profit_pct)) < _TOL
    assert abs(s._stop_loss - est_sl) > 1.0


def test_bb_short_on_filled_rederives_tp_level():
    s = BBShortStrategy()
    s.restore_state("short", 150.0)
    est_tp = s._tp_level

    fill = 151.0
    s.on_filled("short", fill)
    assert abs(s._entry_price - fill) < _TOL
    assert abs(s._tp_level - fill * (1 - s.take_profit_pct)) < _TOL
    assert abs(s._tp_level - est_tp) > 0.5


def test_vvv_hedge_on_filled_reanchors_hard_sl():
    # vvv_hedge is short-only with a hard SL = entry * (1 + hard_sl_pct).
    # Open at the signal-time close X, then fill at Y != X — SL must re-derive
    # from Y, not stay anchored to the stale bar close.
    s = VVVHedgeStrategy()
    signal_close = 7.00
    s._in_short = True
    s._entry_price = signal_close
    s._recompute_sl()
    est_sl = s._sl
    assert est_sl == pytest.approx(signal_close * (1 + s.hard_sl_pct), abs=_TOL)

    fill = 7.05  # slipped above the bar close
    s.on_filled("short", fill)
    assert s._in_short is True
    assert s._entry_price == pytest.approx(fill, abs=_TOL)
    assert s._sl == pytest.approx(fill * (1 + s.hard_sl_pct), abs=_TOL)
    # Sanity: SL actually moved off the estimate-derived value.
    assert abs(s._sl - est_sl) > _TOL


def test_supertrend_on_filled_shifts_brackets_by_delta():
    # supertrend SL/TP are ATR-DISTANCE bands fixed at open. on_filled must
    # shift every level by the same delta the entry moved so the ATR distance
    # (which doesn't change on slippage) is preserved exactly.
    s = SuperTrendStrategy()
    entry = 60_000.0
    sl_dist = 1_200.0
    tp_dist = sl_dist * s.tp_rr
    s._position_side = "long"
    s._entry_price = entry
    s._stop_loss = entry - sl_dist
    s._take_profit = entry + tp_dist
    s._trail_extreme = entry

    delta = 150.0
    s.on_filled("long", entry + delta)
    assert s._entry_price == pytest.approx(entry + delta, abs=_TOL)
    assert s._stop_loss == pytest.approx(entry - sl_dist + delta, abs=_TOL)
    assert s._take_profit == pytest.approx(entry + tp_dist + delta, abs=_TOL)
    assert s._trail_extreme == pytest.approx(entry + delta, abs=_TOL)
    # ATR distance preserved.
    assert (s._entry_price - s._stop_loss) == pytest.approx(sl_dist, abs=_TOL)
    assert (s._take_profit - s._entry_price) == pytest.approx(tp_dist, abs=_TOL)
