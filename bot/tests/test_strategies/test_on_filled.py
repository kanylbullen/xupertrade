"""on_filled re-anchors %SL/TP brackets to the ACTUAL exchange fill.

Origin: investigations/oleg-paper-loop-2026-05-14.md "Fix C". At signal
time a strategy sets _entry_price from the closed bar's close; the real
fill differs (slippage / market fill / paper fill-at-current-price), so
percentage-derived SL/TP/trail are slightly wrong. on_filled corrects
them from the real fill price.
"""

from __future__ import annotations

import pandas as pd

from hypertrade.strategies.bb_short import BBShortStrategy
from hypertrade.strategies.btc_mean_reversion import BTCMeanReversionStrategy
from hypertrade.strategies.oleg_aryukov import OlegAryukovStrategy

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
