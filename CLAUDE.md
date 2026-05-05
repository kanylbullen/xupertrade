# HyperTrade — Agent Development Framework

This file is the operating manual for any Claude agent working on this repo.
The mission is to ship and maintain a **production-grade autonomous crypto
trader** that executes its 14 strategies faithfully, recovers from failure,
and never silently diverges from exchange reality.

The agent acts independently: investigates issues, fixes them, writes tests,
deploys, and verifies. It only stops to ask the user when an action is
destructive or genuinely ambiguous.

---

## 0. Personal info & secrets policy — READ EVERY COMMIT

**This repository is public.** Every commit goes to GitHub where anyone can
read it forever — even if you delete the file in a later commit, the value
lives in history. **Never commit any of these:**

| Type | Example | Where it goes instead |
|---|---|---|
| Telegram bot token | `8639592584:AAGj…` (digits, colon, 35 base64 chars) | `.env` on the deploy host |
| HyperLiquid private key | `0x` + 64 hex chars | `.env` on the deploy host |
| Cloudflare API token | 40-char base64 | Redis (`dashboard:tls:cf_token`) via Options page |
| OIDC client secret | provider-specific | Redis (`dashboard:auth:oidc_client_secret`) |
| `API_KEY` for the bot HTTP API | random string | `.env` on the deploy host |
| Personal email used live | `you@yourdomain.com` | `.env` (`TELEGRAM_*`, OIDC config); generic placeholder in docs (`you@example.com`) |
| Telegram chat ID | 8-12 digit number used as your identity | `.env` on the deploy host |
| Server IP / private LAN | `192.168.x.x`, `10.x.x.x` | `~/.ssh/config`, env vars (`$DEPLOY_HOST`, `$DEPLOY_IP`) |
| Real production hostname | `mybot.example.com` | local env / SSH config; use `$DEPLOY_HOST` placeholder in docs |
| Wallet addresses (HL trading account) | `0x` + 40 hex | not needed in repo; bot reads from env |
| Holdings / position sizes that identify you | "I have 400 VVV" in commit msg | discuss in chat, not in commits |

**Before EVERY commit**, a pre-commit hook (see § Setup) blocks the diff
when known secret-shaped strings appear. **Never bypass with `--no-verify`**
unless the match is genuinely a false positive AND you've manually verified
the value isn't sensitive.

**Even with the hook, you (or a future agent) are still responsible.** The
hook catches known patterns, not novel ones. When writing docs, prefer:

- `$DEPLOY_HOST`, `$DEPLOY_IP`, `$YOUR_DOMAIN` placeholders
- `you@example.com`, `1234567890:your-actual-token-here` for examples
- `~/.ssh/<keyname>` for SSH key paths (not `/home/<user>/.ssh/...`)

**If you discover a secret that's already in history:**
1. **Rotate the credential immediately** (Telegram: `/revoke` to BotFather; HL: regenerate API wallet; CF: revoke the token in CF dashboard).
2. Mask the value in `HEAD` and commit + push the mask.
3. Optionally: rewrite history with `git filter-repo --replace-text` to scrub from older commits. Destructive — coordinate before doing it. Even then, anyone who already cloned has the old value.
4. Add the leaked pattern to the pre-commit hook's blocklist so it can't recur.

### Setup the hook (one-time, per local clone)

```bash
git config --local core.hooksPath .githooks
chmod +x .githooks/pre-commit
```

The hook is checked into `.githooks/pre-commit`. Setup is per-clone because
git ignores `core.hooksPath` from a checked-in `.git/config`.

---

## 1. Mission and definition of done

**Mission:** Build and maintain a HyperLiquid autotrader that
1. Executes the 14 implemented strategies with byte-fidelity to their TradingView ports.
2. Survives network outages, exchange errors, DB hiccups, and process restarts without losing position state.
3. Produces accurate trade records, equity snapshots, and PnL — DB always matches exchange reality.
4. Surfaces problems via Telegram and the dashboard before they become silent losses.

**The agent cannot guarantee a strategy is profitable.** It *can* and *must*:
- Guarantee the strategy logic matches the source PineScript.
- Detect and remove strategies that are demonstrably broken (e.g. SL=0 bug, sign flips, unit confusion).
- Recommend disabling strategies whose live behavior diverges from their backtest archetype, with evidence (live trade history, signal-vs-execution comparison).

**Definition of done for any task:**
- Code change is committed with a clear message.
- Pushed to `origin/master`.
- Deployed to the testnet server (`root@$DEPLOY_HOST`, `/opt/hypertrade/`).
- Verified live: relevant logs are clean, dashboard shows expected state, or `curl` to the bot API confirms behavior.
- If a bug was fixed: a test or runtime check exists that would have caught it.

---

## 2. Repo layout (the parts that matter)

```
/home/xup/hypertrade/
├── bot/
│   ├── hypertrade/
│   │   ├── main.py                  # entry point — auto-instantiates every registered strategy
│   │   ├── config.py                # pydantic-settings (.env), tolerates unknown vars
│   │   ├── api.py                   # aiohttp HTTP API: control, auth, tls, positions, indicator-status, ...
│   │   ├── engine/
│   │   │   ├── runner.py            # tick loop: heartbeat, periodic reconcile (5min), funding poll (30min), flip-detect, signal exec
│   │   │   ├── control.py           # Redis-backed state: paused, disabled, leverage, allow_multi_coin, heartbeat, auth, tls
│   │   │   └── indicators_status.py # per-strategy live "what is each strategy seeing right now"
│   │   ├── exchange/
│   │   │   ├── base.py              # Exchange interface (Position, Balance, Order, OrderType)
│   │   │   ├── paper.py             # in-memory simulated exchange
│   │   │   └── hyperliquid.py       # live HL via SDK + tenacity retry on reads only
│   │   ├── strategies/              # 21 strategies (14 Pine ports + 6 new ports + vvv_hedge custom)
│   │   ├── data/                    # candle feed (REST + WS) with retry/backoff
│   │   ├── events/                  # Redis pub/sub event bus
│   │   ├── notify/
│   │   │   ├── telegram.py          # notifier + command bot (/status /strategies /positions /eval /kelly /today /flat)
│   │   │   └── caddy_admin.py       # builds + applies Caddy JSON config (HTTPS or self-signed)
│   │   ├── reports/
│   │   │   └── weekly_eval.py       # /eval and /kelly engine; CLI: `python -m hypertrade.reports.weekly_eval`
│   │   ├── backtest/
│   │   │   ├── runner.py            # replays candles, simulates fills+fees, returns BacktestResult
│   │   │   ├── metrics.py           # pure functions: Sharpe, APR, max DD, periods/year
│   │   │   └── __main__.py          # CLI: `python -m hypertrade.backtest --strategy X --days N`
│   │   └── db/
│   │       ├── models.py            # Trade, PositionRecord, EquitySnapshot, StrategyConfig, FundingPayment, BacktestRun
│   │       └── repo.py              # all SQL + reconcile_positions (closes orphans both sides)
│   ├── tests/                       # pytest, pytest-asyncio (97 passing, 3 xfailed)
│   ├── alembic/versions/            # 0001 initial, 0002 state_json, 0003 funding_payments, 0004 backtest_runs
│   ├── scripts/migrate.sh
│   └── Dockerfile
│
├── dashboard/                       # Next.js 16 (App Router, Turbopack)
│   └── src/
│       ├── app/                     # /, /trades, /strategies, /options, /status, /login, /api/...
│       ├── proxy.ts                 # Next 16 proxy.ts (was middleware.ts) — auth gate
│       ├── components/              # PositionCard, IndicatorStatus, BotControls, AuthConfig, TlsConfig, MultiCoinToggle, ...
│       └── lib/
│           ├── bot-api.ts           # mode-aware bot API proxy
│           ├── db.ts                # Drizzle, same Postgres as bot
│           ├── queries.ts           # PnL aggregations: per-strategy, daily, totals
│           ├── auth.ts              # HMAC-signed session cookies, fetchAuthConfig with 30s cache
│           └── oidc.ts              # openid-client config + state-bundle codec + resolveRedirectUri
│
├── caddy/
│   ├── Dockerfile                   # caddy:2-builder + caddy-dns/cloudflare plugin
│   └── Caddyfile                    # bootstrap: self-signed HTTPS for $CADDY_HOST + HTTP→HTTPS redirect
│
├── tv-source/                       # 1:1 with strategies — <name>.pine for source-vs-port audits
├── docker-compose.yml               # postgres + redis + 3 bot containers + dashboard + caddy
├── README.md                        # user-facing docs
├── .env.example
└── CLAUDE.md                        # this file
```

**Three bot containers run side-by-side:** `bot-paper` (simulated), `bot-testnet` (HL testnet, real orders, fake money), `bot-mainnet` (opt-in via `--profile mainnet`). They share the Postgres + Redis, separated by `mode='paper'/'testnet'/'mainnet'`. **Telegram lives on the testnet bot only** — it subscribes to events from all three modes.

---

## 3. Operating environment

- **Local working tree:** `/home/xup/hypertrade/` (Linux, x86_64).
- **Remote server:** `root@$DEPLOY_HOST` at `$DEPLOY_IP`, code at `/opt/hypertrade/`. SSH key: `~/.ssh/hypertrade`. Always log in as `root`. Concrete values for the maintainer's deployment are in their local `~/.bashrc` / SSH config — **never commit them here**.
- **Git remote:** `https://github.com/kanylbullen/hypertrade.git`, branch `master`.
- **Postgres:** runs in compose, exposed on `:5432`.
- **Redis:** runs in compose, exposed on `:6379`.
- **Bot APIs:** paper `:8000`, testnet `:8001`, mainnet `:8002` (direct on host — should be removed once Caddy is verified).
- **Dashboard:** `:3000` (direct, dev/debug only) and via Caddy at `:443` (production).
- **Caddy reverse proxy:** `:80` (HTTP→HTTPS redirect), `:443` (HTTPS), `:443/udp` (HTTP/3). Admin API on `:2019` (internal Docker network only).
- **HTTPS:** `https://$DEPLOY_HOST/` — Let's Encrypt cert auto-renewed by Caddy via Cloudflare DNS-01.
- **Auth:** username + password (basic) or OIDC. Configured under Options → Authentication. Bcrypt hashes + HMAC-signed session cookies stored in Redis.

### Standard deploy command

```bash
ssh -i ~/.ssh/hypertrade root@$DEPLOY_HOST \
  "cd /opt/hypertrade && \
   git fetch origin && git reset --hard origin/master && \
   docker compose build && docker compose up -d"
```

Build only what changed for speed (e.g. `docker compose build bot-testnet bot-paper dashboard`).

### Standard "is the bot OK?" check

```bash
# DB ↔ exchange parity
ssh -i ~/.ssh/hypertrade root@$DEPLOY_HOST \
  "echo '=== Exchange ==='; curl -s http://localhost:8001/api/positions; echo; \
   echo '=== DB ==='; cd /opt/hypertrade && \
   docker compose exec -T postgres psql -U postgres -d hypertrade \
   -c \"SELECT strategy_name, symbol, side, size, entry_price FROM positions WHERE mode='testnet' AND is_open=true;\""

# Recent errors
ssh -i ~/.ssh/hypertrade root@$DEPLOY_HOST \
  "docker logs hypertrade-bot-testnet --since 1h 2>&1 | grep -iE 'error|warning' | tail -50"
```

---

## 4. Subagent strategy

Use subagents aggressively when the work is parallelizable, isolated, or
context-heavy. Spawn them with the lightest model that can do the job.

### When to delegate (and to which model)

| Task type | Subagent type | Model | Why |
|---|---|---|---|
| "Find every place where X happens" | `Explore` | sonnet (default) | Read-only, parallelizable, keeps main context lean |
| "Plan the implementation for Y" | `Plan` | sonnet | Architectural thinking before coding |
| "Run the test suite, report failures" | `general-purpose` | haiku | Mechanical, fast, cheap |
| "Code review this diff" | `general-purpose` | sonnet | Needs judgment but bounded scope |
| "Backtest strategy X on stored OHLCV" | `general-purpose` | sonnet | Self-contained Python work |
| "Audit all 14 strategies vs their PineScript sources" | `general-purpose` | sonnet, in parallel (one per strategy) | Embarrassingly parallel |
| "Investigate this production incident" | `general-purpose` | sonnet | Open-ended, needs flexibility |

**Defaults:**
- Use `sonnet` for anything that touches code or makes recommendations.
- Use `haiku` only for very mechanical tasks (running commands, parsing logs into a structured summary).
- Reserve `opus` for hard architectural decisions or hairy debugging across many files.

### Parallelization rule

If you need 3+ independent investigations, fire them in parallel in one message. Example: auditing 14 strategies → 14 parallel `general-purpose` agents, one per strategy file.

### Subagent prompt requirements

Subagents start cold. Always include:
1. The exact file paths involved.
2. What "done" looks like (a short report? a code change? a test run?).
3. Any constraints (don't deploy, don't write to DB, etc.).
4. The expected output format (bullet points, table, diff).

Bad: `"check the strategies"`. Good: `"Audit /home/xup/hypertrade/bot/hypertrade/strategies/*.py against the TradingView source linked in each strategy's docstring. Report any logic divergences with file:line and the source line. Do not edit anything."`

---

## 5. Backlog

The agent owns this list. Update it as bugs are found and fixed. Move items
between sections as their state changes. Never delete an entry — fixed items
stay in **Done** with the commit hash so the agent has institutional memory.

### Open — Critical (blocks safe operation)

None currently.

### Open — High (impacts trading correctness)

(none currently)

### Open — Medium

- [ ] **Volatility-adjusted sizing (option C from Kelly discussion).** Replace fixed `MAX_POSITION_SIZE_USD` with ATR-normalized sizing: `notional = RISK_BUDGET_USD / (atr × atr_mult)` so every trade has roughly the same dollar-risk regardless of asset volatility. Industry standard, no statistical estimation needed. Add `RISK_BUDGET_USD` config; keep `MAX_POSITION_SIZE_USD` as a hard cap. ~3-4h work, defensive change. Pair with the Kelly report for guidance on the budget level.
- [ ] **Drawdown-based auto-scaling (option B from Kelly discussion).** Add `MAX_STRATEGY_DRAWDOWN_PCT` per strategy. When 80% of cap reached → halve effective margin until 7-day rolling PnL > 0. Limits exposure on degrading strategies without requiring stationary distribution assumptions like Kelly does.

### Open — Low

- [ ] **Trades page filters/pagination.** Currently `LIMIT 50`. Add strategy filter, date range, paging.
- [ ] **Correlation grouping.** `cdc_macd` and `macd_zero` are mathematically near-identical. Tag strategies with a `family` attribute and let `allow_multi_coin=False` extend to family-level conflicts.
- [ ] **Optimize `oleg_aryukov` for backtest.** Nadaraya-Watson kernel + RCI loops are O(n²). Fine for live (one call per tick) but a 4k-bar backtest hangs >30 min. Vectorize NW using rolling weighted convolution; replace per-bar RCI loop with a vectorized rank-correlation.
- [ ] **Drop direct port exposure once HTTPS is verified.** Dashboard `:3000` and bot `:8001/:8002` are still bound on the host. Caddy is the only path that should be reachable externally. Remove the host-port mappings from `docker-compose.yml` for those services and let Caddy handle all ingress.
- [ ] **Make `/strategies` page data-driven.** Currently a hardcoded array of 21 strategy descriptors. Should pull names+symbol+timeframe from the bot's `/strategies` endpoint and read description/strengths/weaknesses from a metadata file colocated with each strategy module.
- [ ] **Surface backtest history in dashboard.** `backtest_runs` table now persists every CLI run. A `/backtests` page would let users compare runs, filter by strategy, and see how parameter changes affect APR/Sharpe over time.
- [ ] **Suppress Telegram noise on transient HL-fetch failures.** Bot currently emits `ErrorOccurred` on every strategy tick whose `fetch_candles` fails after retries — this spams Telegram during HL outages even though the bot recovers automatically. Filter by error type before publishing.

### Done

> Last full sync: 2026-05-02. Earlier items first; recent work grouped
> by theme below the original list.

- [x] Cross-strategy position lock (`allow_multi_coin` Redis flag) — commit `4435c46`.
- [x] Dashboard shows exchange positions, not stale DB — commit `4435c46`.
- [x] Fast `/api/control/config` endpoint to avoid blocking on exchange calls — commit `ccd7c2a`.
- [x] `Repository.reconcile_positions()` on startup closes orphans — commit `b2bc62a`.
- [x] Manual DB cleanup of 2 ETH orphans + BTC size correction (testnet, 2026-04-29).
- [x] EMA-crossover `_sl=0.0` instant-close bug fixed with `None` sentinel.
- [x] Keltner-breakout missing-SL-after-restart fixed.
- [x] Telegram HTML escaping for reason/message strings — commit `ceb3a74`.
- [x] Unreachable `logger.info()` in `HyperLiquidExchange.__init__` — commit `b4ea004`.
- [x] `.env.example` and `dashboard/.env.example` complete — commit `b4ea004`.
- [x] Initial Alembic migration; bot still uses `create_all` for live, Alembic is manual.
- [x] Network retry/backoff via `tenacity` on HL reads + candle fetcher — commit `7731061`.
- [x] DB-driven close-size with sanity-clamp to exchange — commit `7731061`.
- [x] Heartbeat written every tick + `/api/control/heartbeat` endpoint — commit `7731061`.
- [x] Periodic reconcile every 5 minutes from runner loop — commit `8263c20`.
- [x] Daily PnL Telegram digest at 23:00 + `/today` command — commit `8263c20`.
- [x] `MAX_TOTAL_EXPOSURE_USD` cap across all open positions — commit `180f3b1`.
- [x] Reconcile size-mismatch threshold (no spam on HL rounding) — commit `180f3b1`.
- [x] `btc_mean_reversion` restore_state now sets `_take_profit` (was missing) — commit `c8c1e50`.
- [x] `supertrend` restore_state lazily recomputes SL/TP (was running unprotected) — commit `c8c1e50` + ATR-scope fix in `b8ab902`.
- [x] Test coverage 14/14 strategies (was 4/14) — commit `b8ab902`. 51 passed, 2 xfailed.
- [x] None-sentinel migration for hash_momentum, pivot_supertrend, moon_phases — commit `fdedff3`.
- [x] Strategy-state DB persistence (positions.state_json + export_state/restore_from_json on base; hash_momentum implements both) — commit `fdedff3`. Eliminates SL drift across restart.
- [x] Funding-cost tracking: `funding_payments` table, periodic poller (30 min), best-effort strategy attribution — commit `c19da09`. Verified live: first poll captured SOL short funding payment.
- [x] Weekly strategy evaluation report: `hypertrade.reports.weekly_eval`, scheduled Sunday 18:00 Telegram digest, on-demand `/eval [days]` command — commit `c19da09`.
- [x] Settings tolerates unknown env vars (`extra="ignore"`) — commit `c19da09`.
- [x] Overview: detailed P&L breakdown (totals, per-day with bars, per-strategy with W/L) — commit `39d1175`.
- [x] Funding cost merged into Daily P&L net (visualized as net = realized + funding) — commit `3b9c5e1`.
- [x] State persistence (export_state/restore_from_json) on all 8 stateful strategies — commit `3b9c5e1`. Restart now restores SL/TP/trail/entry verbatim from DB instead of recomputing.
- [x] Half-Kelly sizing report (advisory only — does NOT change live sizing). New `/kelly [days]` Telegram command + `weekly_eval --kelly` CLI flag. 6 unit tests cover edge cases (too few trades, no losses, classic 60% / 1:1, negative edge clamp, 40% / 3:1, avg_win/avg_loss).
- [x] Telegram /kelly HTML escape for `<10` literal — commit `ebc93ff`.
- [x] Dashboard authentication phase 1 (basic): bcrypt-hashed username/password stored in Redis, HMAC-signed session cookies (no external deps), Next.js 16 proxy.ts gates non-public routes, Options page UI, Sign Out in nav. — commit `2bcda54`. OIDC fields persist but enforcement reserved for phase 2.
- [x] OIDC phase 2: openid-client-based authorization-code flow with PKCE+state, /api/auth/oidc/start + /callback, mode toggle on Options, friendly error mapping, session subject from claims.email > preferred_username > sub. — commit `71f64cd`.
- [x] Engine flip-detection: when a strategy emits OPEN_X while DB shows opposite side, synthesize CLOSE-then-OPEN. — commit `92e68c2`.
- [x] Backtest framework with metrics (Sharpe / APR / max DD / win rate), CLI (`python -m hypertrade.backtest --all --days 180`), 14 unit tests. — commit `c338c03`. First 180d run: bb_short +5.5%, moon_phases +5.0%, volatility_breakout flat, 11 others negative — strong evidence ema_crossover (-12.4%) and penguin_volatility (-10.3%) are over-trading.

#### Audits & port fixes (2026-04-30 → 05-01)
- [x] Source-vs-Python audit of all 14 original strategies via parallel subagent. Found 4 HIGH-prio port bugs — commit `feb12d2`:
  - `ema_crossover` had a phantom reverse-on-opposite-cross block. Pine source only enters on signal, exits on SL. Removed the reversal → 180d backtest -12.4% → +5.2% APR (190 → 1 trades).
  - `penguin_volatility` hardcoded `use_timing_filter=True`; Pine default is False (state-based entries). Added the toggle, defaulted False to match Pine.
  - `volatility_breakout` latched SL from entry-time ATR; Pine recomputes every bar. Now matches Pine.
  - `hash_momentum` missed Pine's opposite-signal-close block. Added.
- [x] tv-source files renamed to `<strategy_name>.pine` for 1:1 mapping with bot/hypertrade/strategies/<name>.py — commit `feb12d2`.
- [x] Reconcile auto-closes exchange-side orphans — commit `a697242`. Without this, an untracked exchange position would silently absorb a strategy's next OPEN order via netting, corrupting both DB and exchange state. Now closes via market order with a dust threshold.

#### New strategies (2026-04-30 → 05-01)
- [x] 6 new ports via 3 parallel subagents — commit `7b44ecc`: `daily_long_0830` (15m time-of-day), `kalman_breakout` (Kalman+ATR bands, 1h ETH), `bb_rsi_scalper` (BB+RSI+EMA+Fib, 15m BTC), `hash_supertrend` (no-SL flip, 1h BTC), `oleg_aryukov` (6-indicator ensemble, 1h ETH), `qullamagi_breakout` (multi-MA breakout, 1h ETH). 21 new tests. None showed clear edge in 180d window.
- [x] `main.py` now auto-instantiates every registered strategy via `list_strategies()` — commit `3a8394d`. Was hardcoded to 14, so newly-registered strategies weren't actually active.
- [x] `vvv_hedge` — custom defensive hedge for staked VVV holdings — commits `db46764` + `03f7bea`. EMA-bearish mandatory filter; emits `Signal(size=400)` to bypass engine notional calc; symmetric exit on EMA flip. 5 unit tests. Backtest: 2 round trips on 144d uptrend (vs. 5 before EMA filter) — defensive by design.

#### Backtest persistence (2026-05-01)
- [x] `backtest_runs` table + Alembic migration 0004 — commit `36500a5`. CLI auto-saves every run with all summary metrics (APR, Sharpe, max DD, win rate, trade counts) plus input params (position_size, fee_rate, slippage_bps). Use `--no-save` to opt out. 37 historical runs persisted.

#### Dashboard authentication (2026-04-30 → 05-01)
- [x] Bitwarden / password-manager autofill: switched login + auth-config forms to uncontrolled refs and `autoComplete="current-password"` so password-manager DOM injection isn't discarded by React's controlled-input pattern. Form has `name="username"`/`name="password"` so managers identify the fields. — commits `b1707e6`, `b9c9014`, `927c366`.
- [x] Login redirect-to-overview after sign-in (sanitize `next` target) — commit `bd1aaf5`. `next` param falls back to `/` when missing, points at `/login`, or points at an `/api/*` route.
- [x] Logout 405 fix: AuthConfig "Sign out" was a `<a href>` triggering GET on a POST-only route. Replaced with a button that POSTs; added GET fallback that clears cookie and 302-redirects. — commit `347b64a`.
- [x] OIDC phase 2 with full PKCE+state flow — commit `71f64cd`. `openid-client` v6, `/api/auth/oidc/start` + `/callback` + Options-page mode toggle.
- [x] OIDC `redirect_uri` correctness in containerized prod — commit `8fada21`. Introduced `PUBLIC_URL` env (falls back to `DASHBOARD_URL`); `resolveRedirectUri()` helper used by start AND callback so token-exchange `redirect_uri` matches what was sent at auth time. Bot exposes scoped `GET /api/auth/oidc-secret` (API_KEY-only) so dashboard can fetch the OIDC client secret server-side without leaking it.
- [x] Basic-auth fallback when OIDC misbehaves — commits `3c9c4b0`, `7289ffb`. Login page renders an "OIDC not working? Sign in with username + password" link when basic creds are configured; `/login?fallback=basic` forces it. Bot's `/api/auth/verify` and dashboard's `/api/auth/login` both accept basic creds whenever a basic user exists, regardless of active mode.
- [x] All OIDC/auth redirects use PUBLIC_URL, not container hostname — commit `7f6fbf9`. Callback success+error, proxy login redirect, logout fallback, oidc-start error all resolved against PUBLIC_URL. Was redirecting users to `https://<docker-id>:3000/` after OIDC login.

#### HTTPS via Caddy + Let's Encrypt (2026-05-01)
- [x] Caddy reverse proxy with Cloudflare DNS-01 plugin (xcaddy build) — commit `53864ae`. New `caddy/` directory with custom Dockerfile and bootstrap Caddyfile. Admin API on `:2019` for dynamic config push.
- [x] Bot endpoints `GET /api/tls/config` (public, no token leak), `POST /api/tls/configure` (API_KEY-required) — commit `53864ae`. Generates Caddy JSON config with DNS-01 challenge using stored CF token; POSTs to admin API on `/load`.
- [x] Self-signed HTTPS as default after deploy — commit `89b6662`. Caddy issues a local-CA cert immediately, browser warns once, user accepts. No window of plain HTTP cookies/credentials.
- [x] `CADDY_HOST` env to bind self-signed cert to actual hostname — commit `36d0ef8`. `:443 { tls internal }` had no SNI context and failed handshake; now uses `{$CADDY_HOST:localhost}`.
- [x] TLS UI: domain auto-normalization (strip `http://`, trailing slash, port) — commit `0f7e892`. Plus frontend validation when toggling enable=true with empty fields, and server-state-driven badge (not local toggle state) — commits `7289ffb`, `5efa413`.
- [x] `build_internal_https_config` requires `subjects` — commit `7e9b2e6`. The internal-CA fallback config had no subjects in the TLS policy; toggling LE off after deploy made TLS handshake fail (ERR_SSL_PROTOCOL_ERROR) and locked users out. Now requires the `domain` arg or falls back to `CADDY_HOST` env.

#### Misc (2026-04-30 → 05-01)
- [x] TradingView chart routes VVV to `COINBASE:VVVUSD` (not on Binance) — commit `12d6a45`.
- [x] Dashboard `/strategies` page now lists all 21 strategies with descriptions — commit `22d7dd7`. Still hardcoded; should be data-driven (see Open — Low).

#### Reconcile / state-sync hardening (2026-05-03 → 05-04)
- [x] PaperExchange persists `{balance, positions}` to Redis after every fill; `load_state()` runs at startup before reconcile — commit `526d3cc`. Without this, every container restart wiped paper state, then startup-reconcile orphan-closed every DB row, then strategies re-entered duplicately on the next signal. Penguin_volatility paper showed 13 such cascade-entries on 2026-05-01.
- [x] `Strategy.reset_state()` on base + override on all 13 stateful strategies; `repo.reconcile_positions(on_strategy_close=cb)` callback wired to `runner._reset_strategy_state` — commit `526d3cc`. When 5-min runtime reconcile orphan-closes a DB row, strategy `_in_position` is brought into sync. Eliminates "DB closed but strategy thinks it's still in" desync.
- [x] DB cleanup of 28 orphan-closed positions + 28 buy-only trades from before fix — manual SQL on 2026-05-03. Real PnL trades preserved.
- [x] `rsi_momentum` adds `_in_position` flag — commit `494aec8`. Was emitting CLOSE_LONG every tick where exit cond was true, even when flat. Engine ignored each but logged WARNING per tick (~50/hour). Closes Open-Medium item.

#### Vault scanner Phase 1 (2026-05-05) — first PR-flow feature
- [x] HyperLiquid vault scanner: daily catalogue poll → coarse pre-filter → per-vault `vaultDetails` fetch → Sharpe/max-DD/multi-period ROI → quality filter → `vault_snapshots` row + `vault_nav_history` append. Telegram fires `vault.qualified` / `vault.disqualified` events on state change with 24h debounce per vault. New `/vaults` dashboard page (sorted by Sharpe) and `vault_picks` HODL signal alongside the others. Owned by the testnet bot only (single owner; Telegram lives there). Quality filter defaults: age ≥ 180d, AUM \$200k–\$20M, ROI 90/180d > 0%, max DD ≤ 25%, Sharpe(180d) > 1.5, manager equity ≥ 5%, fee ≤ 15%. ROI 365d waived for vaults < 365d. — squash-merge `23dd0bf` (PR #1, branch `feat/vault-scanner`). Plan: `docs/plans/vault-scanner.md`. API research: `docs/hyperliquid-vaults-api.md`. 28 new pytest cases (filters / metrics / poller); full suite 133 passed. **First PR-flow feature**: Copilot found 11 issues on first review (catalogue dropouts not disqualified, NAV history not merged into metrics, full-microsecond `snapshot_at` defeating upsert, unguarded casts in `fetch_details`, 48h cutoff hiding everything on missed poll, per-mode duplicate scanning, cooldown bumped on failure, compose `TELEGRAM_EVENTS` overriding .env update, "—d" rendering for null age, follower count under-reports capped vaults, coarse `apr ≤ 0` filter dropping legit qualifiers); all addressed in commit on the branch before merge.

---

## 6. Working principles

### Investigation before code

Don't fix what you don't understand. The order is always:
1. Reproduce or observe the problem (log line, DB row, dashboard screenshot).
2. Read the relevant code top-to-bottom — no skimming.
3. Write the fix and a check that would have caught it.
4. Deploy. Verify.

If a fix is "obvious" without step 1, the fix is probably wrong.

### Keep DB and exchange in lockstep

The single most dangerous failure mode in this system is DB ↔ exchange divergence (we already lived through it). Any new feature that opens or closes positions must:
- Write to DB **before** sending the order, OR record the order ID and reconcile.
- Tolerate the case where the order succeeds but the DB write fails.
- Be idempotent on retry.

The reconcile function is the safety net, not the strategy. Don't lean on it for correctness.

### Don't add features without a need

We have 14 strategies and a decent UI. Resist the urge to add more strategies, more pages, more abstractions. The backlog is the product roadmap.

### Logs are the API

Every non-trivial action — open, close, skip, reconcile, error — logs a line that includes: strategy name, symbol, side, size, price, and the *reason*. If you can't tell from the logs why something happened, the logging is the bug.

### Telegram is for humans

Don't spam Telegram. Forward `trade.executed`, `position.closed`, and `error`. Skip `signal.generated` (it duplicates `trade.executed`) and any verbose tick-level events.

### Money handling

- All sizes use `position.size` from DB at close time, never recomputed.
- All prices are floats; never compare floats with `==`. Use tolerance windows (`abs(a - b) < 1e-6`).
- Fees are subtracted at trade-record time, not at signal time.
- Leverage applies to notional, not margin. `notional = margin * leverage`. Stop-loss distances are on price, not notional.

### Testing

Every strategy has a unit test in `bot/tests/test_strategies/test_strategies.py` covering: warmup guard, entry signal fires, restore_state doesn't instant-close, SL exit fires. New strategies must add tests in the same shape. Run with `cd bot && uv run pytest`.

The `paper` mode is the integration test — it runs the same code against a simulated exchange. Use it to validate end-to-end behavior before promoting to testnet.

---

## 7. Workflow per task

**As of 2026-05-05, all new features go through a feature branch + PR
flow.** Direct pushes to master are reserved for emergency fixes and
trivial doc tweaks. Hotfixes can also branch (`fix/<short-name>`) when
the change has any risk of regression.

### Standard flow (feature work)

```
1. State the task in one sentence. Update the backlog if new.
2. Investigate (read code, logs, DB rows).
3. Plan — write to docs/plans/<feature>.md for non-trivial work.
   Get user sign-off on the plan before coding.
4. Create branch: git switch -c <type>/<short-name>
   Types: feat | fix | docs | refactor | chore
   Examples: feat/vault-scanner, fix/penguin-restart, docs/api-readme
5. Implement on the branch.
6. Test locally (pytest, type-check, dry-run if possible).
7. Commit with Conventional-Commit-style message + Co-Authored-By line.
   Multiple commits per branch are fine — they get squashed at merge.
8. Push: git push -u origin <branch>
9. Open PR: gh pr create --fill --base master
   Use a HEREDOC body with: ## Summary, ## Test plan, ## Notes for reviewer.
10. Wait for GitHub Copilot's automated review to post.
    - Read every comment Copilot leaves.
    - Address what's worth addressing (real bugs, security, clarity).
    - Reply or push fixes; Copilot re-reviews on each push.
11. After Copilot review is clean (or all comments resolved), merge:
    gh pr merge --squash --delete-branch
12. Pull master locally; deploy to server with standard command.
13. Verify (logs clean, dashboard correct, parity check).
14. Mark backlog item Done with the merged-PR's squash-commit hash.
```

### Direct-to-master allowed (skip the PR)

Only these:
- README typos, comment fixes, single-word doc edits.
- Reverting a freshly-broken master commit (use `git revert`, not force-push).
- True emergencies where the bot is down and waiting on review costs money.

When unsure: branch + PR. The five extra minutes are negligible vs. the
cost of a regression in master.

### Branch naming

- `feat/<short-name>` — new feature or signal
- `fix/<short-name>` — bug fix
- `docs/<short-name>` — docs-only changes (README, CLAUDE.md, plans)
- `refactor/<short-name>` — internal restructuring, no behavior change
- `chore/<short-name>` — dependency bumps, gitignore, CI

Keep names short but specific: `feat/vault-scanner`, not
`feat/new-vault-thing`. Use kebab-case.

### PR description template

```markdown
## Summary
- 1-3 bullets: what this changes and why

## Test plan
- [ ] pytest passes (97+ tests)
- [ ] specific manual checks (e.g. "/hodl page renders new card")
- [ ] deploy verified (or "deploy after merge")

## Notes for reviewer
- Anything subtle, intentional trade-offs, or follow-up work
```

### Commit message style

```
<type>: <imperative summary, <72 chars>

<paragraph explaining WHY — the diff shows what>

Co-Authored-By: Claude <model> <noreply@anthropic.com>
```

`<type>` matches the branch type prefix (feat/fix/docs/refactor/chore).
Multiple commits on a branch don't need to be perfectly clean —
`gh pr merge --squash` collapses them into one with the PR title +
description as the merged commit.

### Working with Copilot review

GitHub Copilot's PR review (auto-enabled on this repo) posts within
~60s of opening or pushing to a PR. Treat it as a pair-programmer:

- **Real bugs** — fix immediately, push, Copilot re-reviews.
- **Style nits** — fix or dismiss with reasoning in a reply.
- **Spurious flags** (e.g. "this could throw" on already-handled cases)
  — leave a one-line reply explaining; don't waste cycles arguing.
- **Don't merge with unaddressed bug-flag comments** even if you
  disagree — at minimum reply explaining why you're proceeding.

If Copilot finds nothing in 5 min, it's done — proceed to merge.

### When to ask the user vs. just do it

**Just do it (still requires PR):**
- Code fixes, refactors, new tests.
- Adding logging, retry logic, reconcile improvements.
- Updating docs, `.env.example`, README.
- New HODL signals or features that fit existing architecture.
- Recommending strategy disable based on evidence.

**Ask first (and write a plan in `docs/plans/`):**
- Anything that modifies production DB rows beyond the migration tooling.
- Disabling a strategy in live config (recommend, then ask).
- Sending real funds, mainnet trading.
- `git reset --hard` on shared branches, force-push.
- Changing the architecture in a way that touches >5 files.
- Removing strategies, columns, or endpoints (vs. deprecating).
- New features that don't fit existing architecture (e.g. vault scanner —
  sit between exchange/ and hodl/).

When in doubt, write the plan first and surface it for review.

---

## 8. Strategy evaluation policy

Every Sunday (or on demand), an agent should:

1. Pull the past 7 days of trades per strategy from `mode='testnet'`.
2. Compute per strategy: number of trades, win rate, total realized PnL, average PnL per trade, max consecutive loss.
3. Compare to the strategy's archetype (e.g. mean-reversion strategies should have ~50%+ win rate; momentum follow-throughs should have <40% win rate but bigger wins).
4. Flag for review:
   - Strategies with 0 trades for 14+ days (data feed broken? signal logic dead?).
   - Strategies with realized PnL more than 2σ below their backtest expectation.
   - Strategies that consistently lose to fees+funding (gross-positive but net-negative).
5. Write findings to `bot/reports/weekly-YYYY-MM-DD.md` and post a one-paragraph summary to Telegram.

The agent **recommends** disable, the user **decides** disable. Disable is done via the dashboard `/options` page or the `/api/control/strategy/{name}/toggle` endpoint, never by editing strategy code.

---

## 9. Common pitfalls — read before debugging

- **`asyncio.to_thread` + `asyncio.run` deadlock with asyncpg in Python 3.13.** This burned us with Alembic. Don't call `asyncio.run` from inside a thread that's already in an event loop. Use `loop.run_in_executor` instead, or run blocking code in a real subprocess.
- **HyperLiquid position is netted per-coin.** If two strategies open opposing sides on the same coin, the exchange shows the net. The DB will diverge unless `allow_multi_coin=False` is enforced (which it now is by default).
- **HyperLiquid sizes have per-asset `szDecimals`.** BTC is 5 decimals, SOL is 2. Round before submitting orders or you get cryptic 422s. The exchange wrapper already does this — don't bypass it.
- **HyperLiquid prices have a 5-significant-figures rule.** Same wrapper handles it.
- **Telegram HTML parse mode breaks on raw `<` and `>`** in dynamic strings. Always `html.escape()` user-supplied or model-supplied text. Already wrapped in `notify/telegram.py`.
- **`docker compose exec -T`** is required for non-TTY commands over SSH. Forgetting `-T` makes the command hang.
- **Postgres `is_open == True` in SQLAlchemy** must be `== True` (not `is True` or just `is_open`). Drizzle and SQLAlchemy both lift `True` to `1` in the SQL but only with explicit comparison.
- **Settings are loaded once at process start.** Changing `.env` requires a container restart. Runtime overrides go through Redis (`BotControl`), not env vars.
- **`req.url` inside Docker returns the container's bound hostname**, not the public hostname users see through the reverse proxy. Any redirect built from `req.url` (NextResponse.redirect, OIDC redirect_uri, login redirects) will send users to `https://<docker-id>:3000/`. Always resolve through `PUBLIC_URL` (or `DASHBOARD_URL` as fallback). Pattern: `lib/oidc.ts:resolveRedirectUri()`, `proxy.ts` login fallback, `app/api/auth/oidc/callback/route.ts:publicLoginUrl()`.
- **Caddy JSON config requires explicit `subjects` in TLS automation policies.** A bare `:443 { tls internal }` Caddyfile block works (Caddy infers from the host directive), but the equivalent JSON config without `subjects` causes TLS handshake to fail (ERR_SSL_PROTOCOL_ERROR) because Caddy doesn't know which SNI to issue for. Same applies to host-match on HTTPS routes — without it any non-matching SNI returns 404.
- **Bitwarden / password managers and React controlled inputs.** Bitwarden injects values via direct DOM property assignment, which doesn't trigger React's `onChange` event. The injected value is silently overwritten on next render. Workaround: use `useRef` + `defaultValue` for password and username inputs (uncontrolled), read at submit time. Required `name` attributes for the manager to identify fields, plus `autoComplete="current-password"` (or `"new-password"` only when truly setting up).
- **Caddy admin API on `:2019` is open inside the Docker network.** Never publish it externally. The bot reaches it via `http://caddy:2019/load` for dynamic config push.
- **Caddy's `local_certs` global directive doesn't help when `subjects` is missing.** It just sets the issuer policy default; subjects still required.
- **OIDC `redirect_uri` mismatch is silent.** If start sends one URL and the provider's allowlist contains another, the user gets a generic auth error from the provider — no log on our side until you bisect. Always verify with `docker logs hypertrade-dashboard-1 | grep oidc` and the provider's audit log.
- **`PUBLIC_URL` must include the scheme.** `$DEPLOY_HOST` won't work — must be `https://$DEPLOY_HOST` (or `http://...` for dev). Used as base for redirect URLs throughout.
- **Caddy `http_only_config` is gone.** The bot's old `build_http_only_config()` is now an alias for `build_internal_https_config()` — there is no plain-HTTP fallback any more by design. If TLS breaks, fix it; don't disable TLS.

---

## 10. Glossary

- **Mode:** one of `paper` / `testnet` / `mainnet`. Selected by `EXCHANGE_MODE` env. Each bot container picks one at startup; the dashboard switches between them via `?mode=` query param.
- **Signal:** a `Signal` dataclass returned by a strategy's `on_candle()`. Has `action` (OPEN_LONG / OPEN_SHORT / CLOSE_LONG / CLOSE_SHORT), symbol, strategy_name, optional reason.
- **Tick:** one iteration of the engine runner loop. Default every 60s. Each tick: fetch candles, ask each enabled strategy `on_candle`, execute resulting signals.
- **Position:** a row in the `positions` table representing one strategy's view of its open exposure. The exchange may show a different (netted) view — see § 9.
- **Trade:** a row in `trades` table representing a single fill (open or close).
- **Equity snapshot:** total account value at a point in time, written every tick.
- **Reconcile:** the operation that compares DB open positions to exchange positions and closes DB orphans. See `Repository.reconcile_positions()`.
- **Allow_multi_coin:** Redis flag. When false (default), only one strategy can hold a position per coin. Enforced in `runner._execute_signal()`.
- **Flip-detect:** Engine logic that synthesizes a CLOSE signal when a strategy emits OPEN_X while DB shows the opposite side open for that strategy. Prevents HL netting from leaving a partial-direction position.
- **Export_state / restore_from_json:** Strategy-level methods that serialize internal state (SL, TP, trail, entry) to `positions.state_json` at signal time and read it back verbatim on bot restart. Eliminates SL drift across restarts. Implemented on all 8 stateful strategies.
- **Self-signed default:** When TLS is enabled but Let's Encrypt isn't configured (or fails), Caddy issues a cert from its internal CA for `CADDY_HOST`. Browser warns once, user accepts. Auth cookies never cross the network in cleartext.
- **PUBLIC_URL:** Env var on the dashboard container. The canonical user-facing URL (e.g. `https://$DEPLOY_HOST`). Every redirect that the user's browser follows must be built from this — never from `req.url`.
- **CADDY_HOST:** Env var on the Caddy container. The hostname Caddy issues a self-signed bootstrap cert for. Set to match `PUBLIC_URL`'s host so browsers don't get a name-mismatch warning on top of the self-signed warning.
- **vvv_hedge:** Custom defensive hedge strategy for staked VVV holdings. NOT a Pine port — designed in-house. Mandatory EMA-bearish filter + 2-of-3 supplementary indicators, fixed `holding_vvv` size, hard 10% SL.
- **Backtest CLI:** `cd bot && uv run python -m hypertrade.backtest --strategy <name> --days N`. Auto-saves to `backtest_runs` table; use `--no-save` to opt out. Use `--all` to sweep every registered strategy.

---

This file is the source of truth. When in doubt, update it rather than build
folklore. If a workflow described here is wrong, fix the workflow first and
the code second.
