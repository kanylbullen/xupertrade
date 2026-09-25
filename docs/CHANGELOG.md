# Changelog — fixed backlog items

The **Done** history that used to live in `CLAUDE.md` § 5, moved here on
2026-09-24 so the agent manual stays short. Newest first. Entries are
verbatim, except for the "Fixed:" references added when an Open item
moved here and the PR number (#170) filled in where an entry said
`#<n>`. A pointer inside an entry ("above", "§ 3", "Open — Medium")
refers to `CLAUDE.md` as it was when the entry was written.

The PR that fixes an Open item in `CLAUDE.md` § 5 moves its entry here,
at the top, ticked and citing the PR number, so the entry lands together
with the fix. Never delete an entry.

PRs that closed no backlog item are not listed; `git log --oneline` has
them all.

## Open items closed by the 2026-09-23 hardening PRs (moved from Open 2026-09-24)
- [x] **Reconcile flattens the book on a transient HyperLiquid read failure.**
  `hyperliquid.py`'s `get_positions()` catches every exception (including a
  bare HL `502 Bad Gateway`) and returns `[]`; the reconcile pass then reads
  that as "no exchange positions" and closes every open DB row as an orphan
  (`pnl=0`, no `Trade` row), and on its next 5-minute pass market-closes the
  still-real exchange position it now sees with no DB owner — again with no
  `Trade` row. Confirmed live: every `closed orphan` log line in the last two
  weeks of the testnet log is immediately preceded by an HL 502 traceback;
  48 of 155 testnet closes since 2026-05-29 (31%) match this pattern. See
  `bot/reports/analysis-2026-09-15.md` § 2 for the full trace and fix
  sketch. Fix in progress on `fix/reconcile-read-failure`. — Fixed: squash-merge `c7cffc8` (PR #167, merged 2026-09-23). A failed exchange read now raises `ExchangeReadError` and reconcile writes and orders nothing on it.
- [x] **Engine money-path hardening (analysis-2026-09-15 § 4, not covered by `fix/reconcile-read-failure`):**
  - `Order.size` on the HL exchange wrapper is the *requested* size, not the rounded size actually submitted — DB row and fee are computed from the wrong number (this is the live 247× "BTC size mismatch" reconcile warning).
  - `main.py` sets `repo = None` on a DB error at boot and enters the trading loop anyway; every `if self.repo` gate downstream (flip-detect, same-side dedup, coin/family gate, `MAX_TOTAL_EXPOSURE_USD`) silently no-ops, and `_check_parity_after_trade` then returns `True` unconditionally.
  - Kill-switch read failure and `set_daily_pnl` persist failure (`engine/portfolio.py`) both fail open — a Redis blip loses the daily-loss counter or lets a kill-switched bot keep trading.
  - `PositionRecord` (the `positions` table) has no `leverage` column; the exposure-cap check does `getattr(p, "leverage", 1)`, mixing full-notional and margin-divided-by-leverage units in the same cap.
  - A failed `meta()` fetch at HL-exchange construction leaves `_sz_decimals` empty and every coin silently rounds to 4dp; the bot boots "successfully" anyway.
  - A failed `update_leverage` push before an open is logged only — the open proceeds at whatever leverage HL already has for that coin.
  - The HL exchange's read-retry predicate keys off exception *type*, so 4xx `ServerError` gets retried like a transient 5xx (reads only, no double-submit risk, but wasted retry budget on a permanent error).
  - Fixed by squash-merges `eb11542` (PR #170: `Order.size` is the filled size, `meta`/`spotMeta` fetched and validated at construction, status-based read-retry predicate) and `2cfb13e` (PR #172: the runner books `order.size`, testnet/mainnet refuse to boot without a database, kill switch and daily-PnL persistence fail closed, the exposure cap counts notional on both sides, a failed leverage push aborts the open), both merged 2026-09-23.
- [x] **Dashboard auth/tenant-isolation hardening (analysis-2026-09-15 § 5)** — auth-mode fail-open when the Redis key is absent, an OIDC open-redirect (`safeNext` blocks `//` but not `/\`), and a few lower-severity gaps. Fix in progress on `fix/dashboard-auth-hardening`. — Fixed: squash-merge `15ebf4f` (PR #168: a missing auth mode resolves to `locked`, `safeNext` resolves against our own origin, Caddy strips `CF-Connecting-IP`) and the review follow-ups in `9b0b103` (PR #171), both merged 2026-09-23.

## HyperLiquid exchange-wrapper hardening (2026-09-23, analysis-2026-09-15 § 4)
- [x] Wrapper reports the filled size: `Order.size` from the HL wrapper is the fill's `totalSz` (a readable 0 is REJECTED), else the szDecimals-rounded size that was submitted, on both the immediate-fill and the timeout-poll path — never the unrounded request. Runner consumption (`size = order.size` in `_execute_signal`) is tracked in the engine-safety PR; until it lands the 247× "BTC size mismatch" drift continues, so the Open — Medium `Order.size` sub-bullet stays open. — (PR #170, squash-merge `eb11542`)
- [x] HL-exchange construction fetches `meta` and `spotMeta` itself inside the init retry loop, validates both, and passes them into the SDK `Info` and `Exchange` constructors; an empty, failed or non-JSON answer is retried and then raises, instead of booting with an empty szDecimals map that rounded every coin to 4 dp. `place_order` refuses a coin with no szDecimals rather than guessing. The `/api/hyperliquid/diagnostic` endpoint builds the exchange in a worker thread so that blocking construction can't freeze the event loop. — (PR #170, squash-merge `eb11542`)
- [x] HL read retry uses the `_is_retryable_server_error` predicate (transient network errors including `requests`' own ConnectionError/Timeout, malformed 200s, and HTTP 408/429/500/502/503/504 read from `status_code`, with a code-less 4xx body kept as its status on reads) instead of an exception-type tuple; other 4xx/5xx fail on the first attempt. Writes still never retry. — (PR #170, squash-merge `eb11542`)
- [x] A non-JSON 200 on an HL read (the SDK returns `{"error": …}` as the answer) is `_MalformedResponseError`, retried like a 502 and then `ExchangeReadError` — `get_positions`/`get_balance` no longer read an error page as a flat book and $0. — (PR #170, squash-merge `eb11542`)
- [x] An order POST whose outcome is unknown — our deadline, the SDK's own `requests` timeout, a dropped connection, a 504 — gets the audit-H2 delayed-fill poll instead of being reported REJECTED while HL may have filled it. — (PR #170, squash-merge `eb11542`)
- [x] `cancel_order` calls the SDK as `cancel(coin, oid)` — it passed only the id, so every call was a TypeError swallowed as False — and returns True only when HL's per-order status is `"success"`. `Exchange.cancel_order` now takes the symbol. — (PR #170, squash-merge `eb11542`)
- [x] An order HL leaves resting is cancelled with a log line and reported CANCELLED, or PENDING with an error log when the cancel is not confirmed. Unreachable while every order is IOC market; it is the guard for the first GTC limit order. Follow-up for whoever adds limit orders: a GTC order that fills partly before resting has that part booked nowhere (see `_cancel_resting`). — (PR #170, squash-merge `eb11542`)

## Full-stack analysis (2026-09-15)
- [x] Full-stack analysis 2026-09-15 — live production state (operator tenant, all three modes), strategy performance since the 2026-05-29 evaluation, a money-path review of the bot engine, a tenant-isolation review of the dashboard, local quality gates, and documentation drift. Read-only; nothing on the server, in Redis, or in the DB was changed. Surfaced the reconcile read-failure bug (now Open — Critical above), the engine/dashboard hardening items (now Open — Medium above), and this file's drift (fixed by this PR). Report: `bot/reports/analysis-2026-09-15.md` (PR #163).

## Correlation grouping, oleg_aryukov vectorization, backtest history page (2026-09)
- [x] **Correlation grouping.** `family` attribute on `Strategy`, resolved via `registry.get_strategy_family()`; `allow_multi_coin=False` now refuses to stack two strategies in the same family (e.g. `cdc_macd`/`macd_zero`, both an EMA12/26-cross ≡ MACD-zero-cross signal) across any coin, not just the same coin. — squash-merge `3b6433a` (PR #157, `feat: strategy family grouping and family-level multi-coin gate`).
- [x] **Optimize `oleg_aryukov` for backtest.** Vectorized the Nadaraya-Watson kernel and per-bar RCI loop (behavior-preserving — live signal output unchanged). — squash-merge `37e490d` (PR #160, `refactor: vectorize oleg_aryukov`).
- [x] **Surface backtest history in dashboard.** New `/backtests` page: filters, trend chart, pager over the `backtest_runs` table. — squash-merge `0be8a2f` (PR #159, `feat(dashboard): /backtests page with filters, trend chart, pager`).

## Telegram noise: transient HL-fetch failures on strategy ticks (2026-08-31)
- [x] **Suppress Telegram noise on transient HL-fetch failures (strategy ticks).** Replaces both failure modes the strategy-tick path had: per-strategy-per-tick `ErrorOccurred` publishing (~22 events/min during the 2026-05-09 HL outage) and the PR-#24 error-type filter's full suppression (which left a multi-hour outage Telegram-silent — the empty-DataFrame signature `fetch_candles` returns after its tenacity retries never even reached the filter). Now both failure signatures (empty candles in `_run_strategy`, transient exceptions in the tick catch-all via the reused `_is_transient_network_error` predicate) feed an outage-window aggregator in `EngineRunner._settle_fetch_outage()`: a window clearing before `FETCH_OUTAGE_ALERT_SECONDS` (default 600) never notifies; a window persisting ≥ threshold emits exactly ONE `ErrorOccurred` (`strategy="candle-fetch"`) summarizing affected strategies; recovery closes the window log-only. Non-transient errors still publish immediately. Companion fix for HODL verdict-recovery noise was `fix/vault-picks-error-event` (PR #98). Tests: `bot/tests/test_engine/test_fetch_outage_alert.py` (10 cases). — squash-merge `1fb9db1` (PR #158, branch `fix/fetch-outage-dedup`), merged 2026-08-31.

## Dashboard: trades-page filters + data-driven /strategies (2026-07-29)
- [x] Trades page filters/pagination — was `LIMIT 50`. Strategy filter, date-range picker, and pagination on `/trades`; filter state is URL-driven so views are shareable/bookmarkable. — squash-merge `2c37e79` (PR #150, branch `feat/trades-filters-pagination`).
- [x] Make `/strategies` page data-driven — page no longer carries its hardcoded 21-descriptor array (which had already drifted: `ath_breakout` shipped and traded but was never documented). Strategy prose moved to `bot/hypertrade/strategies/meta/<name>.json` colocated with each module, read via `meta_loader.py`; the bot's `/strategies` endpoint merges metadata with the live registry (live name/symbol/timeframe always win; unreadable meta files are skipped and an undocumented strategy still lists). Coverage test asserts every registered strategy has a meta file. — squash-merge `97ff1d1` (PR #152, branch `refactor/data-driven-strategies`).

## Host port exposure closed (2026-07-29)
- [x] **Every published port moved to loopback except Caddy's 80/443.** `docker-compose.yml` published `postgres:5432`, `redis:6379` and `dashboard:3000` on `0.0.0.0`, i.e. on the LAN. The bot ports named in the original backlog item were a non-issue — orchestrator-spawned tenant bots have never published host ports (`PortBindings: null`); the § 3 line claiming otherwise was stale and is now corrected. The severe one was **Redis**: `redis:7-alpine` runs with an empty `requirepass`, so any LAN host could read `dashboard:auth:session_secret` — the HMAC key signing session cookies — and forge an admin session outright, plus the OIDC client secret and the CF API token. `dashboard:3000` bypassed both real ingress paths (CF tunnel and Caddy each reach the container over the compose network), so it also sat outside Cloudflare's rate limiting and bot management. Bound to `127.0.0.1` rather than removed, which closes the LAN exposure while keeping `localhost` dev access and SSH-tunnelled DB clients working. Verified no external client was connected to any of the three beforehand.

## Vault scanner Phase 1 (2026-05-05) — first PR-flow feature
- [x] HyperLiquid vault scanner: daily catalogue poll → coarse pre-filter → per-vault `vaultDetails` fetch → Sharpe/max-DD/multi-period ROI → quality filter → `vault_snapshots` row + `vault_nav_history` append. Telegram fires `vault.qualified` / `vault.disqualified` events on state change with 24h debounce per vault. New `/vaults` dashboard page (sorted by Sharpe) and `vault_picks` HODL signal alongside the others. Owned by the mainnet bot only (single owner; vaults are mainnet-only on HL and the `/vaults` dashboard is pinned to mainnet — moved from testnet 2026-05-13). Quality filter defaults: age ≥ 180d, AUM \$200k–\$20M, ROI 90/180d > 0%, max DD ≤ 25%, Sharpe(180d) > 1.5, manager equity ≥ 5%, fee ≤ 15%. ROI 365d waived for vaults < 365d. — squash-merge `23dd0bf` (PR #1, branch `feat/vault-scanner`). Plan: `docs/plans/vault-scanner.md`. API research: `docs/hyperliquid-vaults-api.md`. 28 new pytest cases (filters / metrics / poller); full suite 133 passed. **First PR-flow feature**: Copilot found 11 issues on first review (catalogue dropouts not disqualified, NAV history not merged into metrics, full-microsecond `snapshot_at` defeating upsert, unguarded casts in `fetch_details`, 48h cutoff hiding everything on missed poll, per-mode duplicate scanning, cooldown bumped on failure, compose `TELEGRAM_EVENTS` overriding .env update, "—d" rendering for null age, follower count under-reports capped vaults, coarse `apr ≤ 0` filter dropping legit qualifiers); all addressed in commit on the branch before merge.

## Reconcile / state-sync hardening (2026-05-03 → 05-04)
- [x] PaperExchange persists `{balance, positions}` to Redis after every fill; `load_state()` runs at startup before reconcile — commit `526d3cc`. Without this, every container restart wiped paper state, then startup-reconcile orphan-closed every DB row, then strategies re-entered duplicately on the next signal. Penguin_volatility paper showed 13 such cascade-entries on 2026-05-01.
- [x] `Strategy.reset_state()` on base + override on all 13 stateful strategies; `repo.reconcile_positions(on_strategy_close=cb)` callback wired to `runner._reset_strategy_state` — commit `526d3cc`. When 5-min runtime reconcile orphan-closes a DB row, strategy `_in_position` is brought into sync. Eliminates "DB closed but strategy thinks it's still in" desync.
- [x] DB cleanup of 28 orphan-closed positions + 28 buy-only trades from before fix — manual SQL on 2026-05-03. Real PnL trades preserved.
- [x] `rsi_momentum` adds `_in_position` flag — commit `494aec8`. Was emitting CLOSE_LONG every tick where exit cond was true, even when flat. Engine ignored each but logged WARNING per tick (~50/hour). Closes Open-Medium item.

## Misc (2026-04-30 → 05-01)
- [x] TradingView chart routes VVV to `COINBASE:VVVUSD` (not on Binance) — commit `12d6a45`.
- [x] Dashboard `/strategies` page now lists all 21 strategies with descriptions — commit `22d7dd7`. Hardcoded at the time (21 was the live count then); made data-driven by PR #152, see the 2026-07-29 entry below. Registry is at 22 as of 2026-09 (see § 2).

## HTTPS via Caddy + Let's Encrypt (2026-05-01)
- [x] Caddy reverse proxy with Cloudflare DNS-01 plugin (xcaddy build) — commit `53864ae`. New `caddy/` directory with custom Dockerfile and bootstrap Caddyfile. Admin API on `:2019` for dynamic config push.
- [x] Bot endpoints `GET /api/tls/config` (public, no token leak), `POST /api/tls/configure` (API_KEY-required) — commit `53864ae`. Generates Caddy JSON config with DNS-01 challenge using stored CF token; POSTs to admin API on `/load`.
- [x] Self-signed HTTPS as default after deploy — commit `89b6662`. Caddy issues a local-CA cert immediately, browser warns once, user accepts. No window of plain HTTP cookies/credentials.
- [x] `CADDY_HOST` env to bind self-signed cert to actual hostname — commit `36d0ef8`. `:443 { tls internal }` had no SNI context and failed handshake; now uses `{$CADDY_HOST:localhost}`.
- [x] TLS UI: domain auto-normalization (strip `http://`, trailing slash, port) — commit `0f7e892`. Plus frontend validation when toggling enable=true with empty fields, and server-state-driven badge (not local toggle state) — commits `7289ffb`, `5efa413`.
- [x] `build_internal_https_config` requires `subjects` — commit `7e9b2e6`. The internal-CA fallback config had no subjects in the TLS policy; toggling LE off after deploy made TLS handshake fail (ERR_SSL_PROTOCOL_ERROR) and locked users out. Now requires the `domain` arg or falls back to `CADDY_HOST` env.

## Dashboard authentication (2026-04-30 → 05-01)
- [x] Bitwarden / password-manager autofill: switched login + auth-config forms to uncontrolled refs and `autoComplete="current-password"` so password-manager DOM injection isn't discarded by React's controlled-input pattern. Form has `name="username"`/`name="password"` so managers identify the fields. — commits `b1707e6`, `b9c9014`, `927c366`.
- [x] Login redirect-to-overview after sign-in (sanitize `next` target) — commit `bd1aaf5`. `next` param falls back to `/` when missing, points at `/login`, or points at an `/api/*` route.
- [x] Logout 405 fix: AuthConfig "Sign out" was a `<a href>` triggering GET on a POST-only route. Replaced with a button that POSTs; added GET fallback that clears cookie and 302-redirects. — commit `347b64a`.
- [x] OIDC phase 2 with full PKCE+state flow — commit `71f64cd`. `openid-client` v6, `/api/auth/oidc/start` + `/callback` + Options-page mode toggle.
- [x] OIDC `redirect_uri` correctness in containerized prod — commit `8fada21`. Introduced `PUBLIC_URL` env (falls back to `DASHBOARD_URL`); `resolveRedirectUri()` helper used by start AND callback so token-exchange `redirect_uri` matches what was sent at auth time. Bot exposes scoped `GET /api/auth/oidc-secret` (API_KEY-only) so dashboard can fetch the OIDC client secret server-side without leaking it.
- [x] Basic-auth fallback when OIDC misbehaves — commits `3c9c4b0`, `7289ffb`. Login page renders an "OIDC not working? Sign in with username + password" link when basic creds are configured; `/login?fallback=basic` forces it. Bot's `/api/auth/verify` and dashboard's `/api/auth/login` both accept basic creds whenever a basic user exists, regardless of active mode.
- [x] All OIDC/auth redirects use PUBLIC_URL, not container hostname — commit `7f6fbf9`. Callback success+error, proxy login redirect, logout fallback, oidc-start error all resolved against PUBLIC_URL. Was redirecting users to `https://<docker-id>:3000/` after OIDC login.

## Backtest persistence (2026-05-01)
- [x] `backtest_runs` table + Alembic migration 0004 — commit `36500a5`. CLI auto-saves every run with all summary metrics (APR, Sharpe, max DD, win rate, trade counts) plus input params (position_size, fee_rate, slippage_bps). Use `--no-save` to opt out. 37 historical runs persisted.

## New strategies (2026-04-30 → 05-01)
- [x] 6 new ports via 3 parallel subagents — commit `7b44ecc`: `daily_long_0830` (15m time-of-day), `kalman_breakout` (Kalman+ATR bands, 1h ETH), `bb_rsi_scalper` (BB+RSI+EMA+Fib, 15m BTC), `hash_supertrend` (no-SL flip, 1h BTC), `oleg_aryukov` (6-indicator ensemble, 1h ETH), `qullamagi_breakout` (multi-MA breakout, 1h ETH). 21 new tests. None showed clear edge in 180d window.
- [x] `main.py` now auto-instantiates every registered strategy via `list_strategies()` — commit `3a8394d`. Was hardcoded to 14, so newly-registered strategies weren't actually active.
- [x] `vvv_hedge` — custom defensive hedge for staked VVV holdings — commits `db46764` + `03f7bea`. EMA-bearish mandatory filter; emits `Signal(size=400)` to bypass engine notional calc; symmetric exit on EMA flip. 5 unit tests. Backtest: 2 round trips on 144d uptrend (vs. 5 before EMA filter) — defensive by design.

## Audits & port fixes (2026-04-30 → 05-01)
- [x] Source-vs-Python audit of all 14 original strategies via parallel subagent. Found 4 HIGH-prio port bugs — commit `feb12d2`:
  - `ema_crossover` had a phantom reverse-on-opposite-cross block. Pine source only enters on signal, exits on SL. Removed the reversal → 180d backtest -12.4% → +5.2% APR (190 → 1 trades).
  - `penguin_volatility` hardcoded `use_timing_filter=True`; Pine default is False (state-based entries). Added the toggle, defaulted False to match Pine.
  - `volatility_breakout` latched SL from entry-time ATR; Pine recomputes every bar. Now matches Pine.
  - `hash_momentum` missed Pine's opposite-signal-close block. Added.
- [x] tv-source files renamed to `<strategy_name>.pine` for 1:1 mapping with bot/hypertrade/strategies/<name>.py — commit `feb12d2`.
- [x] Reconcile auto-closes exchange-side orphans — commit `a697242`. Without this, an untracked exchange position would silently absorb a strategy's next OPEN order via netting, corrupting both DB and exchange state. Now closes via market order with a dust threshold.

## Initial build (2026-04-29 → 05-01)
- [x] Backtest framework with metrics (Sharpe / APR / max DD / win rate), CLI (`python -m hypertrade.backtest --all --days 180`), 14 unit tests. — commit `c338c03`. First 180d run: bb_short +5.5%, moon_phases +5.0%, volatility_breakout flat, 11 others negative — strong evidence ema_crossover (-12.4%) and penguin_volatility (-10.3%) are over-trading.
- [x] Engine flip-detection: when a strategy emits OPEN_X while DB shows opposite side, synthesize CLOSE-then-OPEN. — commit `92e68c2`.
- [x] OIDC phase 2: openid-client-based authorization-code flow with PKCE+state, /api/auth/oidc/start + /callback, mode toggle on Options, friendly error mapping, session subject from claims.email > preferred_username > sub. — commit `71f64cd`.
- [x] Dashboard authentication phase 1 (basic): bcrypt-hashed username/password stored in Redis, HMAC-signed session cookies (no external deps), Next.js 16 proxy.ts gates non-public routes, Options page UI, Sign Out in nav. — commit `2bcda54`. OIDC fields persist but enforcement reserved for phase 2.
- [x] Telegram /kelly HTML escape for `<10` literal — commit `ebc93ff`.
- [x] Half-Kelly sizing report (advisory only — does NOT change live sizing). New `/kelly [days]` Telegram command + `weekly_eval --kelly` CLI flag. 6 unit tests cover edge cases (too few trades, no losses, classic 60% / 1:1, negative edge clamp, 40% / 3:1, avg_win/avg_loss).
- [x] State persistence (export_state/restore_from_json) on all 8 stateful strategies — commit `3b9c5e1`. Restart now restores SL/TP/trail/entry verbatim from DB instead of recomputing.
- [x] Funding cost merged into Daily P&L net (visualized as net = realized + funding) — commit `3b9c5e1`.
- [x] Overview: detailed P&L breakdown (totals, per-day with bars, per-strategy with W/L) — commit `39d1175`.
- [x] Settings tolerates unknown env vars (`extra="ignore"`) — commit `c19da09`.
- [x] Weekly strategy evaluation report: `hypertrade.reports.weekly_eval`, scheduled Sunday 18:00 Telegram digest, on-demand `/eval [days]` command — commit `c19da09`.
- [x] Funding-cost tracking: `funding_payments` table, periodic poller (30 min), best-effort strategy attribution — commit `c19da09`. Verified live: first poll captured SOL short funding payment.
- [x] Strategy-state DB persistence (positions.state_json + export_state/restore_from_json on base; hash_momentum implements both) — commit `fdedff3`. Eliminates SL drift across restart.
- [x] None-sentinel migration for hash_momentum, pivot_supertrend, moon_phases — commit `fdedff3`.
- [x] Test coverage 14/14 strategies (was 4/14) — commit `b8ab902`. 51 passed, 2 xfailed.
- [x] `supertrend` restore_state lazily recomputes SL/TP (was running unprotected) — commit `c8c1e50` + ATR-scope fix in `b8ab902`.
- [x] `btc_mean_reversion` restore_state now sets `_take_profit` (was missing) — commit `c8c1e50`.
- [x] Reconcile size-mismatch threshold (no spam on HL rounding) — commit `180f3b1`.
- [x] `MAX_TOTAL_EXPOSURE_USD` cap across all open positions — commit `180f3b1`.
- [x] Daily PnL Telegram digest at 23:00 + `/today` command — commit `8263c20`.
- [x] Periodic reconcile every 5 minutes from runner loop — commit `8263c20`.
- [x] Heartbeat written every tick + `/api/control/heartbeat` endpoint — commit `7731061`.
- [x] DB-driven close-size with sanity-clamp to exchange — commit `7731061`.
- [x] Network retry/backoff via `tenacity` on HL reads + candle fetcher — commit `7731061`.
- [x] Initial Alembic migration; bot still uses `create_all` for live, Alembic is manual.
- [x] `.env.example` and `dashboard/.env.example` complete — commit `b4ea004`.
- [x] Unreachable `logger.info()` in `HyperLiquidExchange.__init__` — commit `b4ea004`.
- [x] Telegram HTML escaping for reason/message strings — commit `ceb3a74`.
- [x] Keltner-breakout missing-SL-after-restart fixed.
- [x] EMA-crossover `_sl=0.0` instant-close bug fixed with `None` sentinel.
- [x] Manual DB cleanup of 2 ETH orphans + BTC size correction (testnet, 2026-04-29).
- [x] `Repository.reconcile_positions()` on startup closes orphans — commit `b2bc62a`.
- [x] Fast `/api/control/config` endpoint to avoid blocking on exchange calls — commit `ccd7c2a`.
- [x] Dashboard shows exchange positions, not stale DB — commit `4435c46`.
- [x] Cross-strategy position lock (`allow_multi_coin` Redis flag) — commit `4435c46`.
