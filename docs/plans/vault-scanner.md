# Vault Scanner — Implementation Plan

**Status:** Approved 2026-05-05. Build pending context-compact.
**Target branch:** `feat/vault-scanner`

## Goal

Identify HyperLiquid vaults worth depositing into by polling the public
vaults API daily, computing risk-adjusted metrics from NAV history, and
ranking on a quality filter. Initially read-only (research/dashboard);
optional auto-deposit comes later only if the watch-list shows real
edge over 3+ months.

This is **passive allocation research**, not active trading. Mental
model is the same as HODL signals: "is this worth my capital?"

## Why vaults beat copy-trading

| Concern | Copy-trade | HL vault |
|---|---|---|
| Execution lag | Real (you see fills after the fact) | None — same fill |
| Slippage from copy | Real | None |
| Size mismatch | Always | Proportional |
| Manager skin in game | None | ≥5% required by HL |
| Backtestable | No (no follower NAV) | Yes (daily vault NAV) |
| Operational risk | High (your bot must run) | Low (vault runs without you) |

The remaining risks are all manageable: lock-up (1d on HL), capacity
decay, manager dropout, profit-share fee, tail risk. Each gets a
filter check.

## Scope (Phase 1 only — what this plan covers)

**In scope:**
- Daily snapshot of all HL vaults: AUM, manager equity %, ROI 7/30/90/180/365d,
  drawdown, profit-share fee, depositor count, age
- Quality filter (defined below)
- Per-vault NAV history persistence — enables Sharpe + max-DD calc
- New `/vaults` dashboard page listing qualified vaults with metrics
- Telegram notification when:
  - New vault becomes qualified
  - A watched vault drops below filter
- New "vault_picks" HODL signal showing aggregate state ("N qualified
  vaults", "M watched")

**Out of scope (Phase 2/3):**
- Auto-deposit / withdrawal automation
- Capital allocation optimizer (how much to put where)
- Backtest framework for "if I'd put $X in vault Y, where would I be?"
  (worth building but not v1)
- Manager wallet history / cross-vault analysis

## Quality filter (Phase 1)

A vault is **qualified** when ALL of:

| Filter | Threshold | Rationale |
|---|---|---|
| Age | ≥ 180 days | Need enough history to detect skill vs luck |
| ROI 90d | > 0% | Recent positive return |
| ROI 180d | > 0% | Medium-term consistency |
| ROI 365d | > 0% (or N/A if young) | Survives a market cycle |
| Max drawdown | ≤ 25% | Tolerable risk |
| Sharpe (180d) | > 1.5 | Risk-adjusted skill, not raw returns |
| Manager equity | ≥ 5% | Skin in the game |
| AUM | $200k–$20M | Not unstable, not capacity-saturated |
| Profit-share fee | ≤ 15% | Reasonable cut |

A vault is **watched** when it qualified at any point in the last 30
days, even if currently failing one filter (avoid churn from temp dips).

A vault is **disqualified** (alert) when previously qualified but now
fails one of: max drawdown breach, ROI 90d turning negative, Sharpe
drop below 1.0, manager pulled equity below 5%.

## Data sources

HyperLiquid info API (no auth):
- `POST https://api.hyperliquid.xyz/info` with `type: "vaultDetails"` for per-vault
- `POST https://api.hyperliquid.xyz/info` with `type: "vaults"` (or similar) for list
- Per-vault NAV history endpoint TBD — research first call

Need to verify exact endpoint names + response shapes during build —
HL's docs are spotty for vault endpoints, may need to inspect their
frontend's network tab.

## Architecture

### New files

```
bot/hypertrade/vaults/
├── __init__.py
├── api.py             # HL vaults API client (separate from exchange/)
├── models.py          # Vault, VaultSnapshot, VaultNavPoint dataclasses
├── filters.py         # qualified() / watched() / disqualified() pure logic
├── poller.py          # daily polling job
└── metrics.py         # Sharpe, max DD, drawdown duration from NAV
```

### DB schema (new tables, Alembic migration 0006)

```sql
CREATE TABLE vaults (
    address VARCHAR(42) PRIMARY KEY,
    name VARCHAR(128),
    leader_address VARCHAR(42),
    created_at TIMESTAMPTZ,
    profit_share_pct REAL,
    first_seen_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE vault_snapshots (
    id SERIAL PRIMARY KEY,
    vault_address VARCHAR(42) REFERENCES vaults(address),
    snapshot_at TIMESTAMPTZ NOT NULL,
    aum_usd REAL,
    nav REAL,
    leader_equity_pct REAL,
    depositor_count INT,
    roi_7d REAL, roi_30d REAL, roi_90d REAL, roi_180d REAL, roi_365d REAL,
    max_drawdown_pct REAL,
    sharpe_180d REAL,
    qualified BOOLEAN,
    UNIQUE(vault_address, snapshot_at)
);

CREATE TABLE vault_nav_history (
    vault_address VARCHAR(42) REFERENCES vaults(address),
    timestamp TIMESTAMPTZ NOT NULL,
    nav REAL NOT NULL,
    PRIMARY KEY (vault_address, timestamp)
);
```

NAV history is what enables Sharpe + max-DD calc. We backfill from HL
on first encounter (their API exposes historical NAV) and incrementally
append daily.

### Engine integration

Add a `_poll_vaults` method to `runner.py` that runs daily (24h cooldown
in tick loop). Mirrors the `_poll_funding` pattern. Lives in the
mainnet bot (vaults are a mainnet-only concept; moved from testnet 2026-05-13).

Pseudo:
```python
async def _poll_vaults(self) -> None:
    if not self.repo: return
    vaults = await fetch_all_hl_vaults()
    for v in vaults:
        snapshot = await build_snapshot(v)
        await self.repo.save_vault_snapshot(snapshot)
        prev_qual = await self.repo.was_qualified(v.address, days_ago=1)
        if snapshot.qualified and not prev_qual:
            await self.event_bus.publish(VaultQualified(...))
        elif prev_qual and not snapshot.qualified:
            await self.event_bus.publish(VaultDisqualified(...))
```

### Dashboard

New `/vaults` page:
- Table of currently qualified vaults sorted by Sharpe
- Each row clickable → detail panel with NAV chart (recharts), recent
  snapshots, filter pass/fail breakdown
- Filter bar: min AUM, min Sharpe, max profit-share — adjusted live
- "Watch list" = user-toggled subset (stored in Redis like other UI prefs)

### HODL signal

New `vault_picks` signal in `bot/hypertrade/hodl/`:
- Reads latest snapshots
- Verdict based on count of qualified vaults:
  - 0 qualified → "No qualified vaults — wait"
  - 1-3 → "Few candidates — verify before depositing"
  - 4-10 → "Solid pool — pick top 2-3 by Sharpe"
  - 10+ → "Crowded — be selective; capacity-decay risk"
- Checks: "≥1 vault age >365d", "≥1 vault Sharpe >2.0", "no qualified
  vault disqualified in last 7d", etc.

### API endpoints

- `GET /api/vaults` — qualified vaults snapshot
- `GET /api/vaults/<address>` — detail + NAV history
- `GET /api/vaults/<address>/snapshots?days=30` — historical metrics

## Implementation phases

### Phase 1a: Research (½ day, no PR yet)
- Inspect HL frontend Network tab to find vault endpoints
- Document actual API shapes in `docs/hyperliquid-vaults-api.md`
- Sanity test: fetch 5 sample vaults, eyeball metrics

### Phase 1b: Backend (~2 days)
- Branch `feat/vault-scanner`
- DB models + Alembic 0006
- HL vault API client
- Poller (idempotent — safe to run >1×/day)
- Filter logic + Sharpe/DD math + unit tests
- Wire into `runner.py` tick loop
- Telegram event types
- Commit + push

### Phase 1c: Dashboard (~1 day, same branch)
- `/api/vaults*` endpoints in `api.py`
- `/vaults` page + components
- New nav link
- Commit + push

### Phase 1d: HODL signal (~½ day, same branch)
- `vault_picks` signal
- Auto-registers, appears on /hodl page
- Commit + push

### Phase 1e: PR + review + deploy
- Open PR with full test plan
- Address Copilot review
- Merge, deploy, verify

**Total estimate: ~4 days of focused work.**

## Testing strategy

### Unit tests
- `tests/test_vaults/test_filters.py` — qualified/watched/disqualified
  pure logic against synthetic snapshots
- `tests/test_vaults/test_metrics.py` — Sharpe, max-DD, drawdown
  duration on known NAV series (e.g. straight line, V-shape, double-dip)
- `tests/test_vaults/test_poller.py` — idempotent re-run, partial
  failure handling

### Integration test
- Mock HL API response, end-to-end poll → snapshot → filter → DB row
- Verify dashboard `/api/vaults` returns expected shape

### Manual verification (after deploy)
- `/vaults` page shows ≥1 vault
- Telegram notification fires on a qualified-state change
- DB has `vault_snapshots` rows accumulating daily

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| HL vault API undocumented / changes | Document discovered shapes, version-check on poll |
| NAV history endpoint missing | Compute Sharpe from snapshot ROIs as fallback |
| Filter too strict — 0 qualified vaults | Log all candidates with pass/fail per filter so we can tune |
| Filter too loose — too many false positives | Watch over 1 month, raise Sharpe threshold |
| Capacity decay invisible to filter | Add "AUM growth in last 30d" as warning flag (not hard filter) |
| Manager exits suddenly | Disqualified-alert covers this |
| Vault contract bug | Out of scope — this is HL's risk, not ours |

## Definition of done

- [ ] Branch `feat/vault-scanner` merged via squash
- [ ] At least 1 qualified vault visible on `/vaults` page in production
- [ ] Daily poll proven by ≥3 days of `vault_snapshots` rows
- [ ] HODL `vault_picks` signal appears on `/hodl` with non-error verdict
- [ ] Telegram notif fires once during a state change (or simulated)
- [ ] `pytest` adds ≥6 passing test cases (filters + metrics)
- [ ] CLAUDE.md backlog updated with merged-commit hash

## Open questions for the user (before building)

1. **Min vault age**: 180d or 90d? Stricter = fewer false positives,
   but new alpha gets missed. Default: 180d. Override?
2. **Telegram-spam frequency**: per-state-change or daily digest? Default:
   per-change with 24h debounce per vault.
3. **Show paper/testnet/mainnet split**: Vaults are mainnet-only on HL.
   Make the page mainnet-only, or visible in all modes? Default: visible
   everywhere, but indicate "mainnet only" in copy.
