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


def load_cvdd() -> dict[date, float] | None:
    return _load_csv("cvdd.csv")


def latest(series: dict[date, float] | None) -> tuple[date, float] | None:
    """Return (date, value) of the most recent point or None."""
    if not series:
        return None
    d = max(series)
    return d, series[d]
