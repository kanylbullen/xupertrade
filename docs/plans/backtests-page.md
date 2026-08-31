# Backtests page (`/backtests`)

## Goal

Close the CLAUDE.md § 5 Open-Low item *"Surface backtest history in
dashboard"*. The `backtest_runs` table (bot Alembic migration 0004,
`tenant_id` NOT NULL since 0011) already persists every CLI backtest run
with APR / Sharpe / max-DD / win-rate / trade counts plus input params
(position_size, fee_rate, slippage_bps). The dashboard has no way to see
any of it — this page adds read-only access via Drizzle against the same
Postgres the bot writes to.

## Scope

- New `/backtests` page in `dashboard/src/app/backtests/`, mirroring the
  `/trades` page patterns from PR #150:
  - URL-driven filters (`?strategy=&from=&to=&page=`) so filtered views
    are linkable; changing any filter resets to page 1.
  - Server component + `requireTenantServer()`; every query takes
    `tenantId` (dashboard connects as superuser, so RLS is not
    enforced at the DB level — the WHERE clause is the only guard).
  - Date range filters the **run date** (`created_at`), not the candle
    window the run covered (`period_start`/`period_end`) — the page
    answers "what did I run lately".
- `backtest_runs` declared in `dashboard/src/lib/db.ts` (was explicitly
  left out until a dashboard query needed it — now it does).
- Query helpers in `dashboard/src/lib/queries.ts`:
  `getBacktestRunsPage` (stable `(created_at DESC, id DESC)` ordering,
  same rationale as the trades pager), `countBacktestRuns`,
  `getBacktestStrategyNames`, `getBacktestTrend`.
- APR / Sharpe trend chart (recharts, dual Y-axis) rendered when a
  strategy filter is active — mixing strategies into one APR line would
  be noise. Shows how parameter changes (position size / fee /
  slippage, visible in the table) moved APR and Sharpe over time.
- Sidebar link under Pages.
- Table renders fractions as percentages (`apr`, `total_return_pct`,
  `win_rate`, `max_drawdown_pct`, `fee_rate` are 0..1 fractions in the
  DB — the CLI multiplies by 100 only for display; slippage is in bps).

## Out of scope

- No write path — the bot CLI owns this table.
- No symbol / timeframe filter (backlog asks for strategy filter;
  symbol is visible in the table).
- Backlog item stays Open in CLAUDE.md until the squash-merge hash
  exists (operator merges).

## Test plan

- `src/lib/__tests__/backtest-queries.test.ts` — SQL-shape tests mocking
  the Drizzle chain (same style as `trades-page-queries.test.ts`):
  tenant predicate on every path, filter composition, stable ordering,
  page window, trend reversal.
- `src/app/backtests/__tests__/filters.test.ts` — malformed URL params
  degrade to "no constraint", never throw (same cases as the trades
  filters test).
- Gates: `npm test` (vitest) and `npm run build` (Next production build).
