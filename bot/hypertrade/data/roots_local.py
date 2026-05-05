"""Loader for the locally-extracted Roots dataset.

The CSVs under bot/data/private/roots/ are produced by:
    cd bot && uv run python -m scripts.import_roots_har <har-file>

Naming convention after manual rename based on chart inspection:
    realized_price.csv   — Realized Price (USD/BTC)
    rp_90d_change.csv    — 90-day percent change of Realized Price
    sth_cost_basis.csv   — STH cost basis (from a separate HAR export)
    cvdd.csv             — CVDD (likewise)

All CSVs share the schema `date,value` with ISO-8601 dates and float
values. Files are gitignored (private/paid data) so this loader degrades
gracefully when they're absent — returns None and the caller falls back
to proxies.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

# Path is bot/data/private/roots/ relative to repo root; this file is at
# bot/hypertrade/data/roots_local.py so go up two then into data/private.
DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "private" / "roots"


def _load_csv(name: str) -> dict[date, float] | None:
    path = DATA_DIR / name
    if not path.exists():
        return None
    out: dict[date, float] = {}
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                d = date.fromisoformat(row["date"])
                v_raw = (row.get("value") or "").strip()
                if not v_raw:
                    continue
                out[d] = float(v_raw)
            except (KeyError, ValueError):
                continue
    return out or None


def load_rp_90d_change() -> dict[date, float] | None:
    return _load_csv("rp_90d_change.csv")


def load_realized_price() -> dict[date, float] | None:
    return _load_csv("realized_price.csv")


def load_sth_cost_basis() -> dict[date, float] | None:
    return _load_csv("sth_cost_basis.csv")


def load_lth_cost_basis() -> dict[date, float] | None:
    return _load_csv("lth_cost_basis.csv")


def load_sth_zscore() -> dict[date, float] | None:
    """Z-score of STH cost basis (Roots' standard-deviation oscillator).

    Reads from the bottom panel of the /sth-costbasis chart. Values
    typically range -2 (deep bottom) to +8 (euphoric top spike).
    """
    return _load_csv("sth_zscore.csv")


def load_mvrv() -> dict[date, float] | None:
    """MVRV (or MVRV-Z, depending on series). From /mvrv chart.

    Roots' rendered series can dip below zero, suggesting Z-score-style
    normalization. Bottoms typically at or below 0; tops > 5.
    """
    return _load_csv("mvrv.csv")


def load_sth_lth_ratio() -> dict[date, float] | None:
    """STH cost basis / LTH cost basis ratio. From /sth-lth-ratio chart.

    Per Roots' framework: ratio < 1.0 has historically coincided with
    cycle bottoms (STH cohort capitulating below LTH baseline).
    """
    return _load_csv("sth_lth_ratio.csv")


def load_inflow_multiplier() -> dict[date, float] | None:
    """Capital Inflow Multiplier (green line in /multiplier chart).

    Per Roots: > 100 has historically led cycle bottoms by 2-5 months
    (2 mån före 2022, 5 mån före 2018). Reflects how much market cap
    moves per dollar inflow — high values mean illiquid market with
    bottom-conviction holders.
    """
    return _load_csv("inflow_multiplier.csv")


def load_outflow_multiplier() -> dict[date, float] | None:
    """Capital Outflow Multiplier (pink line in /multiplier chart).

    Inverse: spikes during distribution/top phases. Currently exposed
    via API but not yet used as a signal check.
    """
    return _load_csv("outflow_multiplier.csv")


def load_bull_regime() -> dict[date, float] | None:
    """Roots' bull/bear regime classifier from /key-levels chart.

    Values: 3 = "bull" (price above all key levels: SMA200d/SMA21w/STH/RP/LTH),
            0 = anything else (correction or bear).
    """
    return _load_csv("bull_regime.csv")


def load_dxy() -> dict[date, float] | None:
    """U.S. Dollar Index from /dxy chart.

    DXY tracks USD vs basket of major currencies. Strong inverse correlation
    with BTC: DXY > 100 = strong dollar (risk-off), DXY < 95 = weak dollar
    (risk-on, historically good for BTC). Trading-day series — has gaps for
    weekends and holidays.
    """
    return _load_csv("dxy.csv")


def load_cvdd() -> dict[date, float] | None:
    return _load_csv("cvdd.csv")


def latest(series: dict[date, float] | None) -> tuple[date, float] | None:
    """Return (date, value) of the most recent point or None."""
    if not series:
        return None
    d = max(series)
    return d, series[d]
