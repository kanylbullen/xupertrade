# Sidebar navigation refactor

## Goal

Replace the current top-bar `Nav` + global `?mode=` query toggle with a
left sidebar where mode is **route-bound** for the Overview
(`/overview/[mode]`) and **mode-agnostic** for cross-mode pages (Trades,
Strategies, HODL, Vaults). The mode pill (`mode-switch.tsx`) is retired
entirely. Account-level routes (Credentials, Bots, Settings) move under
the existing user avatar dropdown (`UserMenu`). The Status page is
folded into the Bots page as per-bot runtime info. Net effect:
navigation no longer mutates global state, deep links are stable, and
operators stop accidentally toggling mainnet while reviewing testnet.

## Decisions (all confirmed 2026-05-13)

1. **Trades default mode filter.** **Default to "All modes"**,
   sticky-per-session in `localStorage` (key `trades.modeFilter`).
   Rationale: cross-mode default matches the new sidebar mental model
   ("Trades is mode-agnostic"); session-stickiness keeps an operator
   who's actively debugging mainnet from re-selecting on every visit.
   Operator-pick at top of the page via existing pill style (rebuilt
   as a filter, not nav).

2. **Strategies / HODL / Vaults mode filters.** Decided
   2026-05-13 — operator says HODL + Vaults are mainnet-only by
   design (long-term holding signals + on-chain vault data both only
   make sense against the real-money chain). No mode picker on
   either page; `tenantBots` lookup hardcodes `mode='mainnet'`. If
   the mainnet bot isn't running, render the empty state today's
   "bot offline" branch already shows. Strategies stays hardcoded
   descriptive cards, no `?mode` read either.

3. **Status → Bots integration.** **Integrate into each `BotCard`** (not a separate Runtime tab). Heartbeat age,
   paused/disabled flags, last-trade time, equity, open-position count
   render below the existing start/stop/restart/delete row inside the
   same card. Reuses the per-bot context the operator already has eyes
   on; no extra navigation. The `LiveLog` (currently on Status) moves
   to a single collapsible "Recent events" panel below the bot cards
   (it's tenant-wide via Redis pub/sub, not per-bot — so one panel,
   not three).

4. **Sidebar collapsibility.** **shadcn `Sidebar` with
   `collapsible="icon"` on desktop**, sheet-overlay on mobile** (the
   shadcn default). Persist collapsed state via the cookie the shadcn
   primitive sets out of the box. No custom logic.

## Pre-flight (must be true before merging any sub-PR)

- [ ] `cd dashboard && pnpm test` (vitest) passes on `master` baseline
      so regressions are attributable.
- [ ] `cd dashboard && pnpm exec tsc --noEmit` clean on baseline.
- [ ] Image rebrand (`hypertrade-` → `xupertrade-`, CLAUDE.md "Out of
      scope") is **NOT** in flight on the same branch — independent
      change, independent risk surface.
- [ ] No open feature branch touching `proxy.ts` PUBLIC_PATHS (merge
      ordering matters — see Risks).
- [ ] Operator confirms decisions 1–4 above.

## Sub-PR breakdown

### PR A — shadcn Sidebar primitive + skeleton `/overview/[mode]` routes (NON-BREAKING)

Install the sidebar primitive and add the new route shape **alongside**
the existing nav. Top-bar `Nav` and `ModeSwitch` keep working. Old
`/?mode=...` URLs still render the existing overview unchanged. New
`/overview/[mode]` URLs render the same overview, sourcing mode from
the route param instead of the query string.

**Files:**

- New: `dashboard/src/components/ui/sidebar.tsx` (+ deps: `sheet.tsx`,
  `tooltip.tsx`, `input.tsx`, `skeleton.tsx`) via
  `npx shadcn@latest add sidebar`. Verify shadcn-cli compatibility with
  Next 16.2.6 / React 19.2 first; if the CLI errors, copy from the
  upstream registry source manually rather than downgrade.
- New: `dashboard/src/components/app-sidebar.tsx` — the
  dashboard-specific composition (mode-bound Overview links,
  mode-agnostic page links, `BotStatusIndicator` in the header).
  **Renders nothing if `pathname === "/login"`** (mirrors `nav.tsx:29`).
- New: `dashboard/src/app/overview/[mode]/page.tsx` — thin wrapper that
  validates `params.mode ∈ {paper,testnet,mainnet}` (404 otherwise) and
  renders the same component currently in `app/page.tsx`. To avoid
  duplication, **extract the existing overview body** from
  `app/page.tsx` into a new `app/overview/_overview-view.tsx` shared
  module that takes `mode` as a prop. `app/page.tsx` keeps reading
  `?mode` and renders `<OverviewView mode={mode} />`. The new
  `overview/[mode]/page.tsx` reads the route param and renders the
  same.
- Edit: `dashboard/src/app/layout.tsx` — wrap `<main>` in shadcn
  `<SidebarProvider>` + `<AppSidebar />` + `<SidebarInset>`. **Keep
  `<Nav />` rendered for now** (parallel mode for one PR cycle); flag
  the deprecation in a code comment.

**Tests:**

- New unit: `dashboard/src/lib/__tests__/overview-route.test.ts` —
  assert `/overview/foo` 404s, `/overview/paper` resolves, mode prop is
  plumbed.
- Manual smoke: load `/`, `/?mode=testnet`, `/overview/paper`,
  `/overview/mainnet` — all four render correctly. Sidebar collapses +
  persists across reload.

**Out:** `Nav` not yet removed; `ModeSwitch` still present; account
links still in `UserMenu`; no Trades / HODL / Vaults / Status changes.

### PR B — Route-bind mode-aware pages, drop top-bar mode pill from new pages

Switch the cross-mode pages to mode-agnostic with optional inline
filters. Mode-bound pages source from route params. `Nav` and
`ModeSwitch` remain in code (still rendered above the sidebar) but the
new pages stop listening to `?mode`.

**Files:**

- Edit: `dashboard/src/app/trades/page.tsx` — drop `searchParams.mode`.
  Read `?mode=` from URL only as a one-shot legacy fallback
  (server-side: if present, 308 redirect to `/trades?filter=<mode>` so
  bookmarks survive). New behavior: query without mode = all modes
  (queries.ts: `getRecentTrades` overload that omits the `mode`
  clause). Add a small client `<TradesModeFilter />` pill
  (paper / testnet / mainnet / all) that writes to URL `?filter=` and
  `localStorage`. Default = `localStorage` value or `all`.
- Edit: `dashboard/src/lib/queries.ts` — change
  `getRecentTrades(tenantId, limit, mode?)` to make `mode` optional;
  when undefined, omit the `eq(trades.mode, mode)` predicate. Same for
  `getDailyPnl` if needed for a future "all-modes overview" — but keep
  mode-bound for now since overview is per-mode.
- Edit: `dashboard/src/app/strategies/page.tsx` — remove the `?mode=`
  read at line 491/696 (it's only a label string today; nothing
  functional to change beyond dropping the searchParams plumbing).
- Edit: `dashboard/src/app/hodl/page.tsx` — drop the `?mode=` read.
  Hardcode the `tenantBots` lookup to `mode='mainnet'` (HODL signals
  are mainnet-only by design — see Decision 2). Existing "bot
  offline" branch renders when no mainnet bot is running.
- Edit: `dashboard/src/app/vaults/page.tsx` — same treatment: drop
  `?mode=`, hardcode `mode='mainnet'`. Vaults are an on-chain mainnet
  concept; testnet/paper bots have nothing to scan.
- Edit: `dashboard/src/components/app-sidebar.tsx` — Trades /
  Strategies / HODL / Vaults links now go to the bare path with no
  `?mode` suffix.
- **DO NOT** touch `lib/bot-api.ts:parseMode` yet — it still reads
  `?mode` from request URL on bot-proxy routes
  (`/api/tenant/me/bots/...?mode=foo`). Those callers are different
  code paths and migrate separately.

**Tests:**

- Update `dashboard/src/lib/__tests__/queries.test.ts` (if exists; if
  not, add) — assert `getRecentTrades` without mode returns rows from
  all modes for the tenant.
- Update `dashboard/src/lib/__tests__/bot-api.test.ts` — confirm
  `parseMode` behavior unchanged (we're explicitly NOT migrating it).
- Manual smoke: from Paper Overview, click Trades → no mode flicker,
  filter pill defaults to "all". Switch to "testnet", reload — pill
  stays on testnet. Click HODL → defaults to running-bot mode.

### PR C — Cut over: kill top-nav, kill mode toggle, merge Status into Bots, redirect legacy URLs

The cleanup. After this lands, `nav.tsx`, `mode-switch.tsx`, and
`use-mode.ts` are gone.

**Files:**

- Delete: `dashboard/src/components/nav.tsx`,
  `dashboard/src/components/mode-switch.tsx`.
- Delete: `dashboard/src/lib/use-mode.ts` after grepping that no
  consumer remains. Verify with
  `grep -r "use-mode\|useMode\|withMode" dashboard/src` — must be zero
  hits.
- Edit: `dashboard/src/app/layout.tsx` — remove `<Nav />` import +
  render. Remove the `<Suspense>` wrapping it (was needed because Nav
  read searchParams).
- Edit: `dashboard/src/components/user-menu.tsx` — drop the `suffix`
  prop entirely (no more mode propagation). Update the Settings link
  from `/options${suffix}` to `/options` (URL consolidation to
  `/settings` is out of scope). Bots link kept above Settings.
  Settings stays tenant-accessible (Decision 4 confirmed
  2026-05-13 — settings are per-tenant prefs, not operator-only
  config).
- Edit: `dashboard/src/app/page.tsx` — convert to a 308 redirect to
  `/overview/${mode ?? "paper"}`, preserving the legacy `?mode=` query
  for bookmarks. Delete `app/overview/_overview-view.tsx` extraction
  added in PR A and inline back into `app/overview/[mode]/page.tsx` if
  cleaner — judgment call.
- Edit: `dashboard/src/app/settings/bots/bots-client.tsx` — extend
  `BotCard` to render heartbeat / paused state / last-trade time /
  open-position count under the start/stop row. Fetch from existing
  `/api/tenant/me/bots/[id]/status` (or add it; the bot's
  `/api/control/heartbeat` is already there per CLAUDE.md — wire
  dashboard-side aggregation). Add the `LiveLog` component once at the
  bottom of the page.
- Delete: `dashboard/src/app/status/page.tsx`. Add a server-side 308
  from `/status` → `/settings/bots` (Next 16: `app/status/page.tsx`
  becomes a `redirect("/settings/bots")` one-liner, OR add to
  `proxy.ts` as a redirect). Keep a redirect rather than a hard 404 —
  operators will have it bookmarked.
- Edit: `dashboard/src/proxy.ts` PUBLIC_PATHS — **no additions needed**
  for `/overview/*` (already covered by the catch-all auth gate which
  is what we want — they're authenticated routes). Audit confirms
  current `PUBLIC_PATHS` set is correct; just verify after merge.
- Update tests covering `/status` → ensure they hit the redirect or
  are removed.

**Tests:**

- Manual smoke: load `/?mode=testnet` → 308 to `/overview/testnet`.
  Load `/status` → 308 to `/settings/bots`. Navigate sidebar → Trades
  → no nav-bar visible. UserMenu shows Credentials / Bots / Settings
  (operator-only) / Sign out.
- vitest: full suite green.
- tsc clean.
- Grep audit: `grep -rE "use-mode|ModeSwitch|withMode|nav\.tsx" dashboard/src`
  returns nothing.

## Risks + mitigations

1. **`tenantBotFetch` / `parseMode` in `lib/bot-api.ts:39` still reads
   `?mode=` from request URL.** API route handlers for bot-proxy calls
   (e.g. `/api/tenant/me/bots/.../signals?mode=mainnet`) depend on
   this. **Mitigation**: gradual migration — page-level callers stop
   sending `?mode=` in PR B for cross-mode pages, but `OverviewView`
   (PR A) keeps sending `?mode=` on its internal `fetch` calls, and
   HODL + Vaults send `?mode=mainnet` literally (Decision 2). Removing
   `parseMode` is a separate cleanup not in this plan.
2. **Auth-gate path list miss.** `proxy.ts` uses a default-deny model
   with `/((?!_next/static|...).*)` matcher — new `/overview/[mode]`
   routes are auto-gated correctly. Verified above. Risk is low;
   mitigation is the manual smoke list in each PR.
3. **Bookmarks pointing at `/?mode=mainnet`.** PR C 308-redirects
   `/?mode=*` → `/overview/<mode>` server-side, preserving the mode
   value. Bookmarks survive transparently. Same treatment for
   `/status`.
4. **shadcn Sidebar + Next 16 / React 19.2 compatibility unknown.**
   PR A bullet calls this out — verify
   `npx shadcn@latest add sidebar` succeeds before relying on it. If
   the registry version trips on React 19.2 peer ranges, copy the
   source files manually (they're plain TSX with no runtime version
   pinning; the CLI is just a copier).
5. **Settings gating non-issue.** Operator confirmed Settings is
   tenant-accessible (per-tenant preferences). Keep the unconditional
   render in UserMenu. Operator-only routes that currently live under
   `/options` (TLS, auth-config) need to be **page-level
   `requireOperator`-gated** independently — that's not introduced by
   this refactor but worth a quick audit before PR C ships. List
   today: `/options/auth`, `/options/tls`. Confirm those routes
   already gate at the route level (they do per the M-2 / requireOperator
   work) before merging PR C.
6. **`page.tsx` extraction in PR A creates a temporary shared file
   deleted in PR C.** Slightly awkward but keeps PR A non-breaking.
   Acceptable cost.
7. **HODL/Vaults defaulting logic queries `tenantBots` server-side on
   every render.** Trivial cost (already done on the existing pages)
   — no concern.

## Test plan summary

Each PR ends with: vitest green, `tsc --noEmit` green, the listed
manual smoke walk performed against the local dev server. No deploy
between A and B; deploy after B to verify route-binding live; deploy
again after C with a brief watch on `/status` → `/settings/bots`
redirect hits in Caddy logs.

## Out of scope (do not let scope-creep land in these PRs)

- Image rebrand `hypertrade-` → `xupertrade-` (CLAUDE.md backlog).
- Beta-tenant invite flow.
- Removing `parseMode` / `?mode=` from `lib/bot-api.ts` (separate
  cleanup PR; needs all internal `fetch` callers audited).
- `/options` → `/settings` URL consolidation. Tempting; resist.
- New Strategies-page mode filter (the page is hardcoded today; making
  it data-driven is a separate "Open — Low" backlog item).
- Volatility-adjusted sizing, drawdown auto-scaling, anything in
  CLAUDE.md backlog § 5.
