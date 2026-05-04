"""BTC accumulation zone — operationalizes the Roots framework.

Hard signal: which accumulation zone is BTC currently in? Used to flag
when to add to a long-term spot stack, and (via record_purchase.py) to
log purchases against the zone for K4 cost-basis tracking.

Zones (priority order, highest match wins):
  - DEEP RED — drawdown >50% from ATH AND price < CVDD (or proxy 0.85×SMA1400)
  - RED      — price < Realized Price (or proxy SMA1400) AND price < SMA200
  - YELLOW   — price < STH cost basis (or proxy SMA155) AND price < SMA200
  - GREEN    — risk-on, no special action

Two data paths for STH/LTH/Realized/CVDD:
  1. Manual override from manual_onchain_levels table (if recorded ≤14d ago)
  2. Proxy approximations from HL daily candles when manual is stale or absent

Manual is preferred because price-based proxies for STH cost basis can
diverge 20-30% during volatile periods. Proxies are good enough for zone
classification but should not be trusted for tight thresholds.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import pandas_ta as pta

from hypertrade.data.feed import fetch_candles
from hypertrade.data import roots_local
from hypertrade.db.repo import Repository
from hypertrade.hodl.base import Check, Signal, SignalState
from hypertrade.hodl.registry import register

logger = logging.getLogger(__name__)


def _zone_for(
    price: float, sth: float, realized: float, cvdd: float, ath_drawdown_pct: float,
) -> str:
    """Classify zone from price + on-chain levels + drawdown context."""
    if price < cvdd or (ath_drawdown_pct > 50.0 and price < realized):
        return "deep"
    if price < realized:
        return "red"
    if price < sth:
        return "yellow"
    return "green"


@register
class BtcAccumulationZoneSignal(Signal):
    name = "btc_accumulation_zone"
    asset = "BTC"
    description = (
        "Roots-inspired BTC zone classifier. Uses manual STH/LTH/CVDD "
        "from the most recent newsletter entry when fresh (≤14d), "
        "otherwise falls back to SMA-based proxies. Triggers when BTC "
        "is in yellow zone or worse — i.e. below STH cost basis."
    )
    threshold = 0.5  # any zone other than green flips score over threshold
    manual_max_age_days: int = 14

    # Proxy lengths (used when manual data is stale)
    sth_proxy_len: int = 155
    realized_proxy_len: int = 1400
    btc_long_sma: int = 200
    cvdd_proxy_factor: float = 0.85   # CVDD ≈ 0.85 × Realized Price (rough)

    # 90d change of Realized Price: when |value| < this threshold, RP is
    # essentially flat → historically a bottom-formation signal (per Roots).
    rp_change_flat_threshold: float = 0.10

    # STH cost basis Z-score: bottom-zone when ≤ this value. Roots' historical
    # bottoms have all been at -1.0 to -2.0, with cycle bottoms getting
    # progressively shallower (current cycle projected ≈ -0.25).
    sth_zscore_bottom_threshold: float = -0.5

    def _verdict(self, score: float) -> str:
        # We override score-to-verdict mapping in evaluate(); this is
        # only used when build_state runs without explicit verdict override.
        return super()._verdict(score)

    async def evaluate(self) -> SignalState:
        try:
            btc = await fetch_candles(
                "BTC", "1d",
                limit=max(self.realized_proxy_len + 50, 1500),
            )
        except Exception as e:
            logger.exception("btc_accumulation_zone: candle fetch failed")
            return self._build_state([], error=f"candle fetch failed: {e}")

        if btc is None or btc.empty or len(btc) < self.btc_long_sma + 1:
            return self._build_state([], error="not enough BTC candles")

        price = float(btc["close"].iloc[-1])
        ath = float(btc["high"].max())
        ath_drawdown_pct = (ath - price) / ath * 100.0

        # Compute proxies
        sma_short = pta.sma(btc["close"], length=self.btc_long_sma)
        sma_sth = pta.sma(btc["close"], length=self.sth_proxy_len)
        sma_realized = pta.sma(btc["close"], length=self.realized_proxy_len)

        sma_short_now = float(sma_short.iloc[-1])
        sma_sth_now = float(sma_sth.iloc[-1])
        # Realized proxy may have NaN if we don't have 1400 days yet
        sma_realized_now = (
            float(sma_realized.iloc[-1])
            if sma_realized is not None and not pd.isna(sma_realized.iloc[-1])
            else sma_sth_now * 0.7  # crude fallback if not enough history
        )
        cvdd_proxy_now = sma_realized_now * self.cvdd_proxy_factor

        # Data source priority:
        #   1. Local Roots CSV export (most accurate, may be days old)
        #   2. Manual newsletter snapshot from DB (≤14d old)
        #   3. SMA-based proxies (fallback)
        manual_age_days: float | None = None
        manual_source = "proxy"
        sth = sma_sth_now
        realized = sma_realized_now
        cvdd = cvdd_proxy_now
        manual_notes = ""
        rp_90d_change: float | None = None
        rp_90d_change_age_days: int | None = None
        sth_zscore: float | None = None
        sth_zscore_age_days: int | None = None
        lth_cost_basis: float | None = None
        lth_cost_basis_age_days: int | None = None

        # Layer 1: local Roots CSVs (if extracted)
        try:
            rp_series = roots_local.load_realized_price()
            if rp_series:
                latest_rp = roots_local.latest(rp_series)
                if latest_rp:
                    rp_date, rp_val = latest_rp
                    age = (datetime.now(timezone.utc).date() - rp_date).days
                    if age <= 7:
                        realized = rp_val
                        manual_source = f"roots_csv ({age}d old)"

            sth_series = roots_local.load_sth_cost_basis()
            if sth_series:
                latest_sth = roots_local.latest(sth_series)
                if latest_sth:
                    sth = latest_sth[1]
                    if manual_source == "proxy":
                        manual_source = "roots_csv"

            lth_series = roots_local.load_lth_cost_basis()
            if lth_series:
                latest_lth = roots_local.latest(lth_series)
                if latest_lth:
                    lth_date, lth_val = latest_lth
                    lth_cost_basis = lth_val
                    lth_cost_basis_age_days = (
                        datetime.now(timezone.utc).date() - lth_date
                    ).days

            zscore_series = roots_local.load_sth_zscore()
            if zscore_series:
                latest_zs = roots_local.latest(zscore_series)
                if latest_zs:
                    zs_date, zs_val = latest_zs
                    sth_zscore = zs_val
                    sth_zscore_age_days = (
                        datetime.now(timezone.utc).date() - zs_date
                    ).days

            cvdd_series = roots_local.load_cvdd()
            if cvdd_series:
                latest_cvdd = roots_local.latest(cvdd_series)
                if latest_cvdd:
                    cvdd = latest_cvdd[1]

            change_series = roots_local.load_rp_90d_change()
            if change_series:
                latest_change = roots_local.latest(change_series)
                if latest_change:
                    change_date, change_val = latest_change
                    rp_90d_change = change_val
                    rp_90d_change_age_days = (
                        datetime.now(timezone.utc).date() - change_date
                    ).days
        except Exception:
            logger.exception("btc_accumulation_zone: Roots CSV lookup failed")

        # Layer 2: DB-backed manual snapshot (overrides only fields we don't
        # already have from Roots CSV)
        try:
            repo = Repository()
            try:
                level = await repo.latest_onchain_level()
                if level is not None and level.recorded_at is not None:
                    age = datetime.now(timezone.utc) - level.recorded_at
                    manual_age_days = age.total_seconds() / 86400.0
                    if manual_age_days <= self.manual_max_age_days and manual_source == "proxy":
                        if level.sth_cost_basis_usd:
                            sth = float(level.sth_cost_basis_usd)
                        if level.realized_price_usd:
                            realized = float(level.realized_price_usd)
                        if level.cvdd_usd:
                            cvdd = float(level.cvdd_usd)
                        manual_source = level.source or "manual"
                        manual_notes = level.notes or ""
            finally:
                await repo.close()
        except Exception:
            logger.exception("btc_accumulation_zone: manual level lookup failed")
            # carry on with proxies

        zone = _zone_for(price, sth, realized, cvdd, ath_drawdown_pct)

        # Build checks (informational — score reflects zone severity, not check pass-rate)
        checks = [
            Check(
                name="Below STH cost basis (yellow gate)",
                passed=price < sth,
                value=f"BTC ${price:,.0f} {'<' if price < sth else '≥'} STH ${sth:,.0f}",
                threshold="enter yellow zone",
            ),
            Check(
                name="Below Realized Price (red gate)",
                passed=price < realized,
                value=f"BTC ${price:,.0f} {'<' if price < realized else '≥'} RP ${realized:,.0f}",
                threshold="enter red zone",
            ),
            Check(
                name="Below CVDD or 50%+ drawdown (deep gate)",
                passed=price < cvdd or (ath_drawdown_pct > 50.0 and price < realized),
                value=(
                    f"BTC ${price:,.0f} vs CVDD ${cvdd:,.0f}, "
                    f"drawdown {ath_drawdown_pct:.1f}% from ATH ${ath:,.0f}"
                ),
                threshold="enter deep red zone",
            ),
            Check(
                name="Below 200d SMA (regime confirmation)",
                passed=price < sma_short_now,
                value=f"BTC ${price:,.0f} {'<' if price < sma_short_now else '≥'} SMA200 ${sma_short_now:,.0f}",
                threshold="risk-off regime",
            ),
        ]

        # Optional 5th check: Realized Price 90d-change (Roots' bottom-formation
        # signal). Only present if we have local Roots CSV data.
        if rp_90d_change is not None:
            is_flat = abs(rp_90d_change) <= self.rp_change_flat_threshold
            checks.append(Check(
                name="RP 90d change near zero (bottom-formation)",
                passed=is_flat,
                value=(
                    f"{rp_90d_change*100:+.1f}% over 90d"
                    + (f" ({rp_90d_change_age_days}d old data)"
                       if rp_90d_change_age_days else "")
                ),
                threshold=f"|change| ≤ {self.rp_change_flat_threshold*100:.0f}%",
            ))

        # Optional 6th check: STH cost basis Z-score (Roots' standard-deviation
        # oscillator from the bottom panel of /sth-costbasis chart). Bottoms
        # historically at -1 to -2; current cycle projected ≈ -0.25.
        if sth_zscore is not None:
            in_bottom_zone = sth_zscore <= self.sth_zscore_bottom_threshold
            checks.append(Check(
                name="STH Z-score in bottom zone",
                passed=in_bottom_zone,
                value=(
                    f"Z = {sth_zscore:+.2f}σ"
                    + (f" ({sth_zscore_age_days}d old data)"
                       if sth_zscore_age_days else "")
                ),
                threshold=f"≤ {self.sth_zscore_bottom_threshold:+.2f}σ",
            ))

        # Optional 7th check: BTC below LTH cost basis. Historically only
        # touched at deep cycle bottoms (2018-12, 2020-03, 2022-11). When BTC
        # < LTH, even long-term holders are underwater — strongest single
        # bottom signal in Roots' framework.
        if lth_cost_basis is not None:
            below_lth = price < lth_cost_basis
            checks.append(Check(
                name="BTC below LTH cost basis (deep bottom)",
                passed=below_lth,
                value=(
                    f"BTC ${price:,.0f} {'<' if below_lth else '≥'} LTH ${lth_cost_basis:,.0f}"
                    + (f" ({lth_cost_basis_age_days}d old data)"
                       if lth_cost_basis_age_days else "")
                ),
                threshold="BTC < LTH",
            ))

        # Score: 0 in green, 0.5 in yellow, 0.75 in red, 1.0 in deep
        score_map = {"green": 0.0, "yellow": 0.5, "red": 0.75, "deep": 1.0}
        score = score_map[zone]
        verdict_map = {
            "green": "Green — normal DCA, no action",
            "yellow": "Yellow — increase DCA 1.5×",
            "red": "Red — activate layer-2 reserve",
            "deep": "Deep red — full layer-2 + layer-3 deployment",
        }

        notes_parts = []
        if manual_source.startswith("roots_csv"):
            notes_parts.append(f"Realized Price from {manual_source}.")
        elif manual_source == "proxy":
            notes_parts.append(
                "Using SMA-based proxies — extract Roots CSVs via "
                "`scripts/import_roots_har.py` or record manual snapshots."
            )
        else:
            age_str = f"{manual_age_days:.1f}d old" if manual_age_days is not None else "fresh"
            notes_parts.append(f"Using manual levels from {manual_source} ({age_str}).")
            if manual_notes:
                notes_parts.append(f"Note: {manual_notes}")
        if manual_age_days is not None and manual_age_days > self.manual_max_age_days and not manual_source.startswith("roots_csv"):
            notes_parts.append(
                f"⚠ Manual levels are {manual_age_days:.1f}d old — exceeds "
                f"{self.manual_max_age_days}d freshness window; using proxies."
            )

        # Build state directly (custom verdict, not the default mapping)
        state = SignalState(
            name=self.name,
            asset=self.asset,
            description=self.description,
            triggered=zone != "green",
            score=score,
            threshold=self.threshold,
            verdict=verdict_map[zone],
            checks=checks,
            notes=" ".join(notes_parts),
        )
        return state
