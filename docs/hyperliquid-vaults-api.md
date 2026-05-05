# HyperLiquid Vaults API — verified shapes

Researched 2026-05-05 against mainnet.

## 1. Vault catalog (discovery)

```
GET https://stats-data.hyperliquid.xyz/Mainnet/vaults
```

No auth, ~14 MB JSON, list of **9 449** vaults. Cache locally, refresh
≤ 1×/day.

Each entry:

```jsonc
{
  "apr": 5.35,                 // annualized return (decimal: 5.35 = 535 %)
  "pnls": [                    // 4 buckets: day/week/month/allTime,
                               // ~10–13 sparse points each — too coarse
                               // for Sharpe; use vaultDetails for that
    ["day",     ["0.0", "0.0", ...]],
    ["week",    [...]],
    ["month",   [...]],
    ["allTime", [...]]
  ],
  "summary": {
    "name":             "Long LINK Short XRP",
    "vaultAddress":     "0x73ce82fb...",
    "leader":           "0x506657af...",
    "tvl":              "1181408.0",      // string-encoded float USD
    "isClosed":         false,
    "relationship":     {"type": "normal"},  // normal | parent | child
    "createTimeMillis": 1759387218453
  }
}
```

`relationship.type` distribution: `normal` 9441, `child` 7, `parent` 1.
The `parent` is HLP. Filter to `normal` for Phase 1; ignore parent/child
nesting.

## 2. Vault details (per-vault deep)

```
POST https://api.hyperliquid.xyz/info
Content-Type: application/json

{"type": "vaultDetails", "vaultAddress": "0x..."}
```

Optional `"user": "0x..."` adds the requester's `followerState`. Omit
for read-only polling.

Returns `null` for invalid/missing addresses.

```jsonc
{
  "name":             "...",
  "vaultAddress":     "0x...",
  "leader":           "0x...",
  "description":      "...",
  "portfolio": [                          // 8 buckets
    ["day",         {"accountValueHistory": [[ts_ms, "nav_usd"], ...],
                     "pnlHistory":          [[ts_ms, "pnl_usd"], ...],
                     "vlm": "0.0"}],
    ["week",        {...}],
    ["month",       {...}],
    ["allTime",     {...}],
    ["perpDay",     {...}],               // perp-only sub-vault
    ["perpWeek",    {...}],
    ["perpMonth",   {...}],
    ["perpAllTime", {...}]
  ],
  "apr":               -0.0004,           // decimal
  "followerState":     null,              // present only when user= passed
  "leaderFraction":    0.969,             // manager equity share — "skin in game"
  "leaderCommission":  0.10,              // profit-share fee (decimal)
  "followers":         [...],             // capped at 100 in response
  "maxDistributable":  589906.28,         // USD
  "maxWithdrawable":   0.0,
  "isClosed":          false,
  "relationship":      {"type": "normal"},
  "allowDeposits":     true,
  "alwaysCloseOnWithdraw": false
}
```

### NAV-history resolution

Variable per vault. Empirically `allTime` returns ~50–90 points spanning
the vault's lifetime — that's roughly 1 sample every 5–10 days for a
year-old vault. **Not daily.** Sharpe and max-DD must use these
samples; we persist them and append daily snapshots to build our own
higher-resolution history over time.

`day` bucket gives ~60 hourly-ish points. `week`/`month` show
day-grain rollups.

## 3. Quality-filter source mapping

| Filter           | Source field                                  |
|------------------|-----------------------------------------------|
| Age              | `summary.createTimeMillis` (catalog)          |
| TVL              | `summary.tvl`                                 |
| ROI 7/30/90/180/365d | derived from `accountValueHistory`        |
| Max drawdown     | derived from `accountValueHistory`            |
| Sharpe           | derived from `accountValueHistory`            |
| Manager equity % | `leaderFraction` (vaultDetails)               |
| Profit-share fee | `leaderCommission` (vaultDetails)             |
| Open to deposits | `allowDeposits` && `!isClosed` (vaultDetails) |

## 4. Polling strategy

1. GET catalog once (~14 MB, ~3s)
2. Coarse-filter to candidates: `tvl ∈ [200k, 20M]`, `age ≥ 180d`,
   `apr > 0`, `!isClosed`, `relationship.type == "normal"`
   — reduces 9 449 → ~20–30 vaults
3. For each candidate, POST vaultDetails (parallelized with bounded
   concurrency: 5 at a time, ~5–10s total)
4. Compute risk metrics from `allTime.accountValueHistory`
5. Apply full quality filter; persist snapshot

Total daily cost: ~14 MB + ~30 small POSTs. Cheap.

## 5. Quick local test

```python
import urllib.request, json
url = "https://stats-data.hyperliquid.xyz/Mainnet/vaults"
data = json.loads(urllib.request.urlopen(url).read())
# 9449 entries, ~25 pass coarse filter as of 2026-05-05
```
