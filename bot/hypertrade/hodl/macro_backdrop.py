"""Macro backdrop signal — global liquidity / dollar / recession context.

Distinct from btc_accumulation_zone (which uses Bitcoin-specific on-chain
data) — this signal asks "is the broader macro environment supportive
for risk assets right now?" When green, BTC tailwinds are present even
if on-chain doesn't yet say bottom. When red, even a "perfect" on-chain
setup can fail because the sea is going out.

v1 has only DXY. Will grow with Global Liquidity and Recession when
those HARs are extracted.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from hypertrade.data import roots_local
from hypertrade.hodl.base import Check, Signal, SignalState
from hypertrade.hodl.registry import register

logger = logging.getLogger(__name__)


@register
class MacroBackdropSignal(Signal):
    name = "macro_backdrop"
    asset = "BTC"
    description = (
        "Macro environment for risk-on positioning. DXY weakness, ample "
        "global liquidity, and no recession all support adding to BTC. "
        "When most checks fail, defer additions even if on-chain says bottom."
    )
    threshold = 0.5  # ≥ half of present checks must pass

    # DXY thresholds
    dxy_weak_threshold: float = 100.0   # < 100 = USD relatively weak
    dxy_strong_threshold: float = 105.0 # > 105 = strong dollar headwind

    # Global liquidity thresholds (90-day rate of change)
    liquidity_growth_lookback_days: int = 90
    liquidity_expanding_threshold: float = 0.0   # > 0 = expanding

    # Yield curve threshold — below this = inverted = recession warning
    yield_curve_threshold: float = 0.0

    # Latest macro readings can lag a few weeks (monthly data) — accept up to:
    macro_max_age_days: int = 60

    def _verdict(self, score: float) -> str:
        if score >= 0.8:
            return "Tailwind — strongly risk-on macro"
        if score >= self.threshold:
            return "Mild tailwind — broadly supportive"
        if score >= 0.3:
            return "Mixed — neutral macro"
        return "Headwind — macro fights BTC"

    async def evaluate(self) -> SignalState:
        checks: list[Check] = []
        notes_parts: list[str] = []

        # DXY check
        try:
            dxy_series = roots_local.load_dxy()
            if dxy_series:
                latest = roots_local.latest(dxy_series)
                if latest:
                    dxy_date, dxy_val = latest
                    age = (datetime.now(timezone.utc).date() - dxy_date).days
                    is_weak = dxy_val < self.dxy_weak_threshold
                    is_strong = dxy_val > self.dxy_strong_threshold
                    state_label = (
                        "weak" if is_weak else "strong" if is_strong else "neutral"
                    )
                    checks.append(Check(
                        name="DXY weak (USD risk-on)",
                        passed=is_weak,
                        value=f"DXY = {dxy_val:.2f} ({state_label}, {age}d old)",
                        threshold=f"< {self.dxy_weak_threshold:.0f}",
                    ))
                    if is_strong:
                        notes_parts.append(
                            f"⚠ DXY {dxy_val:.1f} > {self.dxy_strong_threshold:.0f} "
                            f"= active strong-dollar headwind."
                        )
        except Exception:
            logger.exception("macro_backdrop: DXY load failed")

        # Global liquidity 90d rate of change
        try:
            gl_series = roots_local.load_global_liquidity()
            if gl_series:
                sorted_dates = sorted(gl_series)
                latest_date = sorted_dates[-1]
                latest_val = gl_series[latest_date]
                # find a date approximately 90 days before latest
                from datetime import timedelta
                target = latest_date - timedelta(days=self.liquidity_growth_lookback_days)
                # pick the earliest date >= target
                past_date = next((d for d in sorted_dates if d >= target), None)
                if past_date and past_date != latest_date:
                    past_val = gl_series[past_date]
                    pct_change = (latest_val - past_val) / past_val
                    age = (datetime.now(timezone.utc).date() - latest_date).days
                    is_expanding = pct_change > self.liquidity_expanding_threshold
                    checks.append(Check(
                        name="Global liquidity expanding",
                        passed=is_expanding,
                        value=(
                            f"${latest_val/1000:.1f}T, "
                            f"{pct_change*100:+.2f}% over "
                            f"{self.liquidity_growth_lookback_days}d ({age}d old)"
                        ),
                        threshold=f"90d change > {self.liquidity_expanding_threshold*100:+.0f}%",
                    ))
        except Exception:
            logger.exception("macro_backdrop: global liquidity load failed")

        # Yield curve (10y - 2y)
        try:
            yc_series = roots_local.load_yield_curve_10y2y()
            if yc_series:
                latest = roots_local.latest(yc_series)
                if latest:
                    yc_date, yc_val = latest
                    age = (datetime.now(timezone.utc).date() - yc_date).days
                    not_inverted = yc_val > self.yield_curve_threshold
                    checks.append(Check(
                        name="Yield curve not inverted",
                        passed=not_inverted,
                        value=(
                            f"10y-2y = {yc_val:+.2f}pp "
                            f"({'normal' if not_inverted else 'INVERTED'}, "
                            f"{age}d old)"
                        ),
                        threshold=f"> {self.yield_curve_threshold:+.2f}pp",
                    ))
                    if not not_inverted:
                        notes_parts.append(
                            f"⚠ Yield curve inverted at {yc_val:.2f}pp — "
                            f"historical recession lead time ~18 months."
                        )
        except Exception:
            logger.exception("macro_backdrop: yield curve load failed")

        # Recession active flag
        try:
            rec_series = roots_local.load_recession_active()
            if rec_series:
                # Use the most recent date that's <= today
                today_date = datetime.now(timezone.utc).date()
                live_dates = [d for d in rec_series if d <= today_date]
                if live_dates:
                    last_date = max(live_dates)
                    rec_val = rec_series[last_date]
                    in_recession = rec_val >= 0.5
                    age = (today_date - last_date).days
                    checks.append(Check(
                        name="No active recession",
                        passed=not in_recession,
                        value=(
                            f"{'IN RECESSION' if in_recession else 'expansion'}"
                            f" ({age}d old)"
                        ),
                        threshold="recession flag = 0",
                    ))
        except Exception:
            logger.exception("macro_backdrop: recession flag load failed")

        if not checks:
            notes_parts.append(
                "No macro data available yet. Extract HAR exports from "
                "/dxy, /global-liquidity, /recession to populate checks."
            )

        # Use _build_state to get standard score calc + verdict mapping
        return self._build_state(checks, notes=" ".join(notes_parts))
