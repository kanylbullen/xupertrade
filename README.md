# xupertrade

Automated crypto trading bot targeting [HyperLiquid](https://hyperliquid.xyz) with a real-time monitoring dashboard, multi-environment support (paper / testnet / mainnet), and Telegram control.

> The internal codename `hypertrade` is still used in container
> names, the Postgres database name, the Phase secrets app, the
> source-tree path, and most code references. Only the user-facing
> brand is **xupertrade** (lowercase) — kept distinct from HyperLiquid's name to
> avoid confusion. No internal rename was done because moving every
> container + volume + secret exceeds the benefit.

> ⚠️ **Disclaimer — read before using with real money**
>
> This software is provided **as-is, with no warranty of any kind**, for educational and research purposes. It is **not financial advice**.
>
> Running this bot in `mainnet` mode places **real orders with real funds** on HyperLiquid. Backtested APR figures shown in the strategy table below are historical results from a third-party study and **do not predict future performance** — strategies that worked in past market regimes can and do lose money going forward. Live trading involves risk of total loss, including from bugs in this code, exchange outages, network failures, and mis-configured risk limits.
>
> You are solely responsible for any funds you trade with. Test thoroughly in `paper` and `testnet` modes first, set conservative `MAX_POSITION_SIZE_USD` / `MAX_DAILY_LOSS_USD` limits, and never trade more than you can afford to lose.
>
> Also: change `POSTGRES_PASSWORD` in [docker-compose.yml](docker-compose.yml) before deploying anywhere reachable from the internet — the default value is intended for local development only.

## Highlights

- **Multi-tenant, orchestrator-spawned bots** — the dashboard spawns one
  Docker container per tenant per mode (`paper`, `testnet`, `mainnet`) via
  `lib/bot-orchestrator.ts`, each with isolated state, so you can A/B test
  strategies in paper while testnet handles canary trades and mainnet runs
  production. Secrets are decrypted server-side from a tenant's passphrase
  and injected into that tenant's bot containers only — see "Security
  model" below.
- **HyperLiquid API-wallet pattern** — bot signs orders with a trade-only key while funds stay on a separate main wallet (cannot be withdrawn even if the bot is compromised).
- **Real-time data** — HyperLiquid WebSocket feed for live prices and per-strategy candle subscriptions, plus REST candle snapshots for indicator computation.
- **Per-strategy leverage and on/off** — defaults baked into each strategy, overridable from the dashboard or Telegram.
- **Live runtime controls** — pause/resume, flat-all positions, per-strategy toggle, all without restarting the bot.
- **Telegram bot** — startup pings, signal/trade notifications, and interactive `/status`, `/positions`, `/strategies`, `/pause`, `/resume`, `/flat` commands; every message tagged with mode badge.
- **Position-aware indicator status** — dashboard shows whether each strategy is `flat`, `ready_long`, `ready_short`, or `holding` based on actual DB position state plus current indicator readings.
- **TradingView charts embedded** — per strategy with the relevant indicators preloaded.
- **Risk limits** — `MAX_POSITION_SIZE_USD`, `MAX_DAILY_LOSS_USD`, plus a kill switch.

## Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│  hypertrade/                                                           │
│  ├── bot/             Python trading engine (one image, spawned per    │
│  │   │                tenant per mode by the dashboard's orchestrator) │
│  │   ├── strategies/  Strategy implementations + registry              │
│  │   ├── hodl/        Advisory accumulation signals (not orders)       │
│  │   ├── vaults/      HyperLiquid vault scanner (read-only)            │
│  │   ├── exchange/    PaperExchange + HyperLiquidExchange              │
│  │   ├── engine/      Runner loop, control state, portfolio, indicator │
│  │   │                status                                          │
│  │   ├── data/        WebSocket feed + REST candles + indicators       │
│  │   ├── events/      Redis pub/sub + typed event schemas              │
│  │   ├── notify/      Telegram notifier + command handler              │
│  │   ├── db/          SQLAlchemy models + repository                   │
│  │   └── api.py       aiohttp HTTP API for dashboard control           │
│  ├── dashboard/       Next.js 16 + shadcn/ui + bot-orchestrator.ts     │
│  │                    (Docker socket → per-tenant container lifecycle) │
│  └── docker-compose.yml   postgres + redis + dashboard + caddy +       │
│                           cloudflared (profile `public`) + bot-image   │
│                           (profile `build`, builds xupertrade-bot:latest│
│                           — never runs as a service itself)            │
└────────────────────────────────────────────────────────────────────────┘
                           │
              dashboard spawns one container per
              (tenant, mode) from xupertrade-bot:latest —
              xupertrade-bot-<tenant-short-id>-<mode>,
              no published host ports, reached by
              container name over the compose network
                           │
                  ┌────────┴────────┐
                  │   PostgreSQL    │  trades, positions, equity_snapshots,
                  │      Redis      │  tenant_bots row per running container
                  │                 │  control state (per-mode), pub/sub events
                  └─────────────────┘
                           │
                           ▼
              dashboard:3000  (Next.js — /overview/[mode] picks bot)
                           │
                           ▼
       Telegram (env-driven per bot instance via TELEGRAM_ENABLED;
       the orchestrator currently enables it on the mainnet bot only)
```

Each tenant's bot container is **fully isolated**:
- Own exchange instance (PaperExchange / HyperLiquid testnet / HyperLiquid mainnet), with that tenant's decrypted credentials injected at spawn time.
- Own Redis state under `hypertrade:{mode}:control:*` keys.
- Own event channel `hypertrade:{mode}:events`.
- Own per-bot API key (generated by the orchestrator, stored in Redis) — one tenant's key cannot reach another tenant's bot.
- Trades persisted with `mode` (and `tenant_id`) columns so DB queries can filter per-mode / per-tenant.

## Strategies

The registry currently holds **22 registered strategies** — 6 of them
seeded from [Minara AI's backtesting study](https://x.com/minara/status/2044432012002635843)
of 236 TradingView strategies tested under HyperLiquid fees, plus later
ports and two in-house designs (`vvv_hedge`, `ath_breakout`). The
`/strategies` dashboard page is the source of truth for the current
roster: it's data-driven, reading live name/symbol/timeframe from the bot's
`/strategies` endpoint and merging in prose from
`bot/hypertrade/strategies/meta/<name>.json` (one file per registered
strategy — count them with `ls bot/hypertrade/strategies/meta/*.json | wc -l`).
A hardcoded table here would drift the same way the old one did (see
docs/CHANGELOG.md, "Dashboard: trades-page filters + data-driven /strategies").

The six from the original Minara seed, for context on where this started:

| Strategy | Pair | Timeframe | Backtest APR | Default leverage | Description |
|----------|------|-----------|--------------|------------------|-------------|
| `btc_mean_reversion` | BTC/USDT | 15m | +204.6% | 6× | **#1 ranked.** RSI(14) cross-below 20 entry, cross-above 65 exit. Sharpe 4+, 16 trades / 90 days — highest risk-adjusted returns in the study. |
| `volatility_breakout` | ETH/USDT | 1h | +124.6% | 8× | Toby Crabel-style breakout: range × 0.6 above prev close. Fixed 2% stop / 3% take-profit (defined risk → higher leverage acceptable). |
| `bb_short` | SOL/USDT | 1h | +48.1% | 2× | Shorts when price breaks 2% above upper Bollinger Band (20, 2σ). Exits at 2% profit. 100% historical win rate over 49 trades. |
| `supertrend` | BTC/USDT | 1d | +35.6% | 1× | ATR-based trend following (period 10, multiplier 8.5). Long on bullish flip, exit on bearish. ~4 trades / 4 years. No stop loss. |
| `rsi_momentum` | BTC/USDT | 4h | +24.3% | 5× | Buys when RSI(14) crosses above 70 (momentum continuation). Sharpe 1.85, 14.8% max DD. |
| `sma_rsi` | ETH/USDT | 1d | +23.5% | 3× | Long when price > SMA50 & SMA200 and smoothed RSI(21,9) > 57. Beat buy-and-hold by +117pp during a losing period. |

Leverage defaults are chosen by historical max drawdown and whether the strategy has a hard stop loss. They can be overridden per-mode from the dashboard or Telegram. The bot computes the maximum leverage needed per coin across active strategies and pushes that to HyperLiquid at startup (HyperLiquid leverage is per-coin, not per-position).

## Documentation

- **[docs/USER_GUIDE.md](docs/USER_GUIDE.md)** — for people invited to
  someone else's instance: sign-in, passphrase, HyperLiquid API wallet,
  first bot, and what the beta does not do yet.
- **[docs/INVITE_ONBOARDING.md](docs/INVITE_ONBOARDING.md)** — operator
  side: Authentik group gating, inviting and removing tenants, admin UI.
- **[docs/CLOUDFLARE_TUNNEL.md](docs/CLOUDFLARE_TUNNEL.md)** — public
  access without opening inbound ports.

## Security model — credentials at rest

Tenants paste HyperLiquid private keys (and Telegram tokens) into the dashboard. The plaintext travels from the browser to the dashboard server over TLS, the server encrypts it in memory under a key `K` derived from the tenant's passphrase, and only the encrypted blob (ciphertext + nonce) hits the database. Plaintext never touches durable storage — not the DB, not Phase, not the log files, not container images. An operator with DB-only access (Postgres credentials but no host shell) cannot read tenant secrets without also knowing each tenant's passphrase.

If a tenant forgets the passphrase, the keys are unrecoverable: there is no reset, no recovery email. They re-enter the keys (and rotate the wallets, since their plaintext was last seen on whichever device they used originally).

If an attacker steals the database, they hold ciphertext that can only be cracked by brute-forcing the passphrase through Argon2id — intentionally tuned so that takes years on dedicated hardware for any decent passphrase.

> **Trust model boundary.** This is not end-to-end encryption. The dashboard server sees plaintext during write (to encrypt) and during decrypt (to hand the bot its key). Anyone with root on the dashboard host can read those values out of memory while a tenant is unlocked. What we protect against is data-at-rest exposure: stolen DB dumps, stolen Phase exports, log scrapes, and operators whose access is bounded to durable storage rather than running processes.

### How it works (technical detail)

| Layer | Primitive | Notes |
|---|---|---|
| **Key derivation** | Argon2id, 64 MiB memory, 3 iterations, 4-way parallelism, 16-byte per-tenant salt | Output is a 32-byte key `K`. Memory-hard so GPUs/ASICs don't help an attacker. Salt is per-tenant, persisted on the `tenants` row. |
| **At-rest encryption** | AES-256-GCM, fresh 12-byte nonce per write | Stored in `tenant_secrets(ciphertext, nonce)`. K never persisted. Same plaintext encrypts to different ciphertexts. GCM auth tag fails loudly on tamper or wrong key. |
| **Verifier** | HMAC-SHA-256(K, fixed domain string) | Stored in `tenants.passphrase_verifier`. Lets us check "did the user type the right passphrase?" without decrypting any actual secret, in constant time. We never store the passphrase itself, hashed or otherwise. |
| **Session cache** | Redis `dashboard:k-cache:<tenant>:<session>`, 24h TTL | After unlock, K lives in Redis tied to the session ID so subsequent actions in the same session don't re-prompt. Cleared on logout / explicit Lock / TTL expiry. |
| **In transit** | TLS terminated at Caddy (Let's Encrypt via Cloudflare DNS-01, or self-signed bootstrap) | The passphrase crosses the wire on initial setup (once to `/api/tenant/me/passphrase` to register the salt + verifier, then once to `/api/tenant/me/unlock` to derive and cache K) and again on each subsequent unlock after the K-cache TTL expires. Secret values cross the wire on each Save. None of these reach durable storage in plaintext. |

**What this does not protect against.** An operator with root access to the host running the bot containers can read decrypted secrets out of running-process memory or container env vars. The bot needs the plaintext private key to sign HyperLiquid orders — that's unavoidable for any non-custodial trading system. The relevant guarantees are:

1. No decrypted secret ever touches durable storage (DB, Phase, log files, container images).
2. No operator can read secrets without the tenant's passphrase before bot start.
3. Rotating a compromised key invalidates the old ciphertext on the next save (each value is independently re-encrypted with a fresh nonce).
4. A 24-hour cache TTL on K means a stolen session cookie buys at most 24h of decryption capability before the next passphrase prompt.

Source of truth (read it):

- `dashboard/src/lib/crypto/passphrase.ts` — Argon2id parameters + verifier construction
- `dashboard/src/lib/crypto/secrets.ts` — AES-256-GCM encrypt/decrypt
- `dashboard/src/lib/crypto/k-cache.ts` — Redis-backed session cache for K
- `dashboard/src/app/api/tenant/me/secrets/[key]/route.ts` — the only place plaintext touches the encrypt/decrypt boundary

The Argon2id parameters are immutable once a tenant has set their passphrase (they're not encoded in the verifier, so changing them would lock everyone out). A future hardening pass could add a `kdf_version` column and a re-encrypt-on-unlock migration path; the [security audit from 2026-05-12](docs/security-audit-2026-05-12.md) tracks this and other deferred items.

## Quick Start

### Prerequisites

- Docker & Docker Compose
- For testnet/mainnet: a HyperLiquid wallet that has previously deposited on mainnet (testnet faucet requires it)

### Local development

```bash
git clone <repo-url> && cd hypertrade
docker compose up -d
# Dashboard: http://localhost:3000
#
# Published ports are bound to 127.0.0.1, so these work from the host
# itself (or over an SSH tunnel) but are NOT reachable from the LAN.
# Redis in particular runs without a password, so its reachability is
# the access control -- don't republish it on 0.0.0.0.
#
# Tenant bots are spawned by the dashboard orchestrator and publish no
# host ports at all. The bot image has no wget/curl, and most endpoints
# require the bot's per-tenant X-Api-Key (generated by the orchestrator,
# stored in Redis — see docs/runbooks/health-check.md). To query one:
#   docker exec <bot-container> python -c \
#     'import urllib.request,sys; req=urllib.request.Request(sys.argv[1], headers={"X-Api-Key": sys.argv[2]}); print(urllib.request.urlopen(req).read().decode())' \
#     http://localhost:8001/api/positions "$API_KEY"
```

### Configuration (`.env`)

In production, a tenant enters these values through **Settings →
Credentials** in the dashboard, not a local `.env` file — see "Security
model" below for how they're encrypted at rest and injected into that
tenant's bot container at spawn time by `bot-orchestrator.ts`. The
variable names below are the same ones the orchestrator injects; this
`.env` shape is mainly useful for running the bot directly against a
single mode without the dashboard (local development).

```env
# Testnet wallet — bot signs orders with API_KEY, executes on ACCOUNT_ADDRESS's behalf
HYPERLIQUID_PRIVATE_KEY=0x...           # API wallet's private key (trade-only, can't withdraw)
HYPERLIQUID_ACCOUNT_ADDRESS=0x...       # Main wallet (where the funds live)

# Mainnet (only used when the bot runs in mainnet mode)
HYPERLIQUID_MAINNET_PRIVATE_KEY=0x...
HYPERLIQUID_MAINNET_ACCOUNT_ADDRESS=0x...

# Telegram (optional but recommended)
TELEGRAM_BOT_TOKEN=...                  # from @BotFather
TELEGRAM_CHAT_ID=...                    # your numeric chat id
TELEGRAM_EVENTS=signal.generated,trade.executed,position.closed,error
```

Per-bot env (injected per-container by the orchestrator at spawn time, or set directly if running the bot standalone):

| Var | Default | Description |
|-----|---------|-------------|
| `EXCHANGE_MODE` | `paper`/`testnet`/`mainnet` | Which exchange this bot talks to. Set per-container by the orchestrator when it spawns a tenant's bot — not a compose-service setting. |
| `MAX_POSITION_SIZE_USD` | `200` | Margin per trade. Notional = this × strategy.leverage. |
| `MAX_DAILY_LOSS_USD` | `100` | Trading halts when daily PnL drops below this. |
| `POLL_INTERVAL_SECONDS` | `60` | How often the runner ticks. |
| `KILL_SWITCH` | `false` | Emergency stop (set without restart via dashboard). |
| `TELEGRAM_ENABLED` | `true` (mainnet), `false` (paper/testnet) | Only one bot should run the Telegram poller. |

## Setting up HyperLiquid testnet

1. Open <https://app.hyperliquid-testnet.xyz/drip>, connect your **mainnet** wallet (must have prior deposit history) and claim 1000 mock USDC.
2. Open <https://app.hyperliquid-testnet.xyz/API>, generate an **API wallet** (gives a fresh address + private key, valid 180 days). Authorize it with your main wallet — this lets the bot sign orders on your account's behalf without ever being able to withdraw funds.
3. Set both keys in `.env`:
   ```env
   HYPERLIQUID_PRIVATE_KEY=<API wallet private key>
   HYPERLIQUID_ACCOUNT_ADDRESS=<main wallet address>
   ```
4. In the dashboard, paste both keys under **Settings → Credentials**, then start the testnet bot from **Settings → Bots** (or `POST /api/tenant/me/bots` to create it, then `POST /api/tenant/me/bots/<id>/start`). Verify it came up with:
   ```bash
   docker exec $(docker ps --format '{{.Names}}' | grep -E '^xupertrade-bot-.*-testnet$') \
     python -c 'import urllib.request,sys; req=urllib.request.Request(sys.argv[1], headers={"X-Api-Key": sys.argv[2]}); print(urllib.request.urlopen(req).read().decode())' \
     http://localhost:8001/api/hyperliquid/diagnostic "$API_KEY"
   # → { "ok": true, "network": "testnet", "api_wallet_mode": true,
   #     "account_value_usd": 999.0, ... }
   ```
   (`$API_KEY` is the bot's per-tenant key — the dashboard already knows it and attaches it automatically for you when you use the UI; this raw check is only needed for manual debugging.)
5. Open the dashboard at `/overview/testnet`, and click **Resume** when ready.

## Going live on mainnet

Same flow but with mainnet wallet keys. The mainnet bot only starts when
explicitly requested — there is no compose profile for it any more; it's
just another tenant bot in `mainnet` mode, started from **Settings → Bots**
in the dashboard (or `POST /api/tenant/me/bots/<id>/start` after creating
it with `mode: "mainnet"`).

**Strongly recommended before flipping to mainnet:**
- Run the same strategies on testnet for at least a few weeks and confirm the trades execute as expected.
- Sanity-check `MAX_POSITION_SIZE_USD` and per-strategy leverage; the equity at risk is `MAX_POSITION_SIZE_USD × max_leverage_per_coin`, not just the size.
- Verify Telegram pings on testnet first.
- Have the dashboard open and pre-confirm the **Pause** and **Close All Positions** buttons work.

## Telegram bot setup

The bot pushes notifications **and** accepts interactive commands.

### One-time setup

1. **Create a bot** — message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`, follow the prompts. BotFather replies with a token (numeric ID, colon, then ~35 random characters).
2. **Get your chat ID** — open the new bot's chat, send `/start`, then visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` and copy the numeric `chat.id` from the response.
3. **Set in `.env`** (these are secrets — never commit them):
   ```env
   TELEGRAM_BOT_TOKEN=<paste-from-botfather>
   TELEGRAM_CHAT_ID=<your-numeric-chat-id>
   ```
4. Telegram is env-driven per bot instance (`TELEGRAM_ENABLED`), and the
   orchestrator currently sets it `true` only for the **mainnet** bot
   (`false` on paper/testnet) — only one bot instance should ever run the
   Telegram poller. Restart the mainnet bot to pick up new
   `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` values: **Settings → Bots →
   restart** in the dashboard, or `POST /api/tenant/me/bots/<id>/stop` then
   `/start`. You should receive a startup ping within ~10 seconds:
   `🟢 MAINNET 🚀 xupertrade started`.

### Notifications

By default the bot forwards these event types to the configured chat (`Settings.telegram_events`, comma-separated, overridable via `TELEGRAM_EVENTS`):
- `trade.executed` — order filled
- `position.closed` — position closed with realized PnL
- `error` — bot-side error in a strategy or the engine
- `vault.qualified` / `vault.disqualified` — HyperLiquid vault scanner state changes
- `hodl.verdict_changed` — a HODL accumulation signal flipped

`signal.generated` is excluded by default (it duplicates `trade.executed`,
which already carries the reason and only fires when an order is actually
placed).

Each message is prefixed with a mode badge (🟡 PAPER / 🔵 TESTNET / 🟢 MAINNET) so cross-mode events stay distinguishable.

Configure which events to forward via `TELEGRAM_EVENTS` env (comma-separated). Heartbeats are intentionally never forwarded.

### Commands

All commands are restricted to the configured `TELEGRAM_CHAT_ID` — nobody else can control the bot.

| Command | What it does |
|---------|--------------|
| `/help` or `/start` | Show command list |
| `/status` | Mode, paused state, equity, open positions, active strategies |
| `/strategies` | All strategies with on/off, leverage, signal status, distance to trigger |
| `/positions` | Open positions with unrealized PnL |
| `/pause` | Pause the bot — no new signals execute |
| `/resume` | Resume |
| `/flat` | Show how many positions would close (asks for confirmation) |
| `/flat confirm` | Actually close every open position with market orders |

Currently commands operate on the bot instance that runs Telegram (the mainnet bot, per the orchestrator's `TELEGRAM_ENABLED` rule above). Cross-mode commands like `/status mainnet` are a planned future addition.

## Bot architecture

### Exchange abstraction

```python
class Exchange(ABC):
    async def place_order(symbol, side, size, order_type, price) -> Order
    async def cancel_order(order_id, symbol) -> bool
    async def get_positions() -> list[Position]
    async def get_balance() -> Balance
    async def get_current_price(symbol) -> float
    async def update_leverage(symbol, leverage, is_cross=True) -> bool
```

Two implementations:
- `PaperExchange` — simulated fills with HyperLiquid's fee model (0.015% maker / 0.045% taker). Tracks cash, positions, and unrealized PnL with proper accounting (no double-counting).
- `HyperLiquidExchange` — live execution via `hyperliquid-python-sdk`, with API-wallet support. Sync SDK calls offloaded to a `ThreadPoolExecutor` so they don't block the asyncio event loop.

### Strategy framework

```python
@register
class MyStrategy(Strategy):
    name = "my_strategy"
    symbol = "BTC"
    timeframe = "4h"
    leverage = 3   # default; overridable from dashboard

    async def on_candle(self, candles: pd.DataFrame) -> Signal | None:
        # candles excludes the forming/in-progress candle — only closed bars
        ...
        return Signal(action=SignalAction.OPEN_LONG, symbol=self.symbol, ...)
```

Strategies receive **closed candles only** (the runner drops the forming candle before calling `on_candle`) so signals don't fire repeatedly during a single bar. The runner additionally enforces idempotency: it won't fire `OPEN_LONG`/`OPEN_SHORT` if there's already an open DB position for that `(strategy, symbol)` pair.

### Engine loop

```
Every POLL_INTERVAL_SECONDS:
  1. Process pending flat-all request (if any)
  2. Read pause + disabled-strategies + leverage-overrides from Redis
  3. If not paused, for each enabled strategy:
       a. Fetch OHLCV via REST (forming candle dropped)
       b. strategy.on_candle(closed_candles) → Signal | None
       c. If signal AND no existing open position:
            - Calculate size: MAX_POSITION_SIZE_USD * strategy.leverage / price
            - Submit order via Exchange
            - Record trade + position to Postgres (mode-tagged)
            - Publish events to Redis (per-mode channel)
       d. Publish tick.completed event
  4. Update unrealized PnL for all open positions
  5. Snapshot equity
  6. Publish heartbeat
```

### Runtime control state

Stored in Redis under per-mode keys, so the same Redis can serve all three modes without collision:

| Key | Type | Purpose |
|-----|------|---------|
| `hypertrade:{mode}:control:paused` | string `0`/`1` | Pause flag |
| `hypertrade:{mode}:control:disabled` | set | Strategy names that are off |
| `hypertrade:{mode}:control:leverage` | hash | strategy_name → leverage int |
| `hypertrade:{mode}:control:flat_request_id` | string | Token; bot acts on each new value |

State survives bot restart, so a paused bot stays paused until you explicitly resume.

### Risk management

- **Per-trade size cap** — `MAX_POSITION_SIZE_USD` is the margin per position. Notional exposure = margin × leverage.
- **Daily loss limit** — `MAX_DAILY_LOSS_USD` halts trading when realized + unrealized PnL drops below the threshold.
- **Idempotency** — runner checks DB for an existing open position before issuing OPEN_LONG/OPEN_SHORT.
- **Closed-candle evaluation** — strategies don't react to live forming-candle prices, preventing intra-bar re-trigger spam.
- **Kill switch** — `KILL_SWITCH=true` env, OR `/pause` from Telegram, OR Pause button on dashboard. Closing all positions is a separate `/flat` action.

## Dashboard

Dark-themed Next.js 16 (App Router, Turbopack) app on port 3000, route-bound
per mode rather than a single `?mode=` toggle.

| Page | Contents |
|------|----------|
| `/overview/[mode]` | TradingView ticker, total equity, P&L stat cards, equity curve, indicator status grid (per strategy: signal + distance to trigger), open positions, recent trades |
| `/trades` | Filterable trade history (strategy, date range, pagination — URL-driven and shareable) |
| `/strategies` | Data-driven strategy reference: live name/symbol/timeframe from the bot's registry merged with prose from `bot/hypertrade/strategies/meta/*.json`, embedded TradingView charts |
| `/backtests` | Filters, APR/Sharpe trend chart, and a pager over the `backtest_runs` table |
| `/hodl` | Advisory accumulation signals, manual on-chain levels, purchase log |
| `/vaults` | Qualified HyperLiquid vaults sorted by Sharpe, plus the tenant's own vault positions |
| `/settings/bots` | Per-bot start/stop/restart, live status, and the live event log (formerly `/status`, which now 308-redirects here) |
| `/settings/credentials` | Tenant HyperLiquid keys and Telegram token, encrypted at rest (see "Security model" below) |
| `/unlock` | Passphrase prompt that derives the session's decryption key before secrets can be used |
| `/admin/server`, `/admin/[tenantId]` | Operator-only: host stats and per-tenant administration |

Each page resolves the tenant from the session (never from the URL) and
scopes its data and bot-API calls accordingly, so one dashboard instance
serves every tenant's paper/testnet/mainnet bots in isolation.

## Tech Stack

### Bot
- Python 3.13, asyncio
- `hyperliquid-python-sdk` + `eth_account` — exchange API + wallet signing
- `pandas` + `pandas-ta` — data + indicators
- `sqlalchemy` + `asyncpg` — Postgres ORM
- `redis` — control state + event pub/sub
- `aiohttp` — HTTP server (`/api/control/*`, `/api/indicator-status`, `/api/hyperliquid/diagnostic`) + REST data feeds + Telegram client
- `pydantic-settings` — env config

### Dashboard
- Next.js 16 (App Router, Turbopack)
- shadcn/ui + Tailwind CSS
- `recharts` — equity curve
- `drizzle-orm` — Postgres queries
- `ioredis` — Redis subscriber for SSE
- TradingView embed widgets

### Infrastructure
- PostgreSQL 16 — historical trades, positions, equity snapshots (`mode`/`tenant_id` columns for per-env, per-tenant filtering)
- Redis 7 — runtime state + event pub/sub + per-bot API keys
- Docker Compose — `postgres` + `redis` + `dashboard` + `caddy` + `cloudflared` (profile `public`) + `bot-image` (profile `build`, produces the `xupertrade-bot:latest` image only — never runs as a service)
- Dashboard-driven orchestration — the dashboard binds the host's Docker socket (`lib/docker.ts`) and spawns one bot container per tenant per mode from `xupertrade-bot:latest` (`lib/bot-orchestrator.ts`); there's no fixed set of bot containers in the compose file

## Adding a strategy

1. Create `bot/hypertrade/strategies/my_strategy.py`:

```python
import pandas as pd
from hypertrade.data.indicators import rsi
from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.strategies.base import Strategy
from hypertrade.strategies.registry import register

@register
class MyStrategy(Strategy):
    name = "my_strategy"
    symbol = "BTC"
    timeframe = "4h"
    leverage = 2  # default; can be overridden per-mode in UI

    async def on_candle(self, candles: pd.DataFrame) -> Signal | None:
        df = rsi(candles.copy(), 14)
        cur = df["rsi"].iloc[-1]
        prev = df["rsi"].iloc[-2]
        if prev > 30 and cur <= 30:
            return Signal(
                action=SignalAction.OPEN_LONG,
                symbol=self.symbol,
                strategy_name=self.name,
                reason=f"RSI crossed below 30 ({cur:.1f})",
            )
        return None
```

2. Register import in `bot/hypertrade/strategies/registry.py::load_all()`.
   `main.py` auto-instantiates every registered strategy via
   `list_strategies()` — there's no separate list to add the name to.

3. (Optional) Add an entry to `bot/hypertrade/engine/indicators_status.py` so the dashboard shows distance-to-trigger.

4. (Optional) Add `bot/hypertrade/strategies/meta/<name>.json` for
   documentation — the `/strategies` dashboard page is data-driven and
   reads from these files (see "Strategies" above); there's no page
   component to edit by hand.

5. Rebuild the bot image and restart running bots to pick it up: `GIT_SHA=$(git rev-parse HEAD) docker compose --profile build build --no-cache --pull bot-image`, then restart each running bot from **Settings → Bots** in the dashboard (or `POST /api/tenant/me/bots/<id>/stop` then `/start`) — a bot only picks up new code when it's (re)started. `GIT_SHA` stamps the image, so the bot's card on **Settings → Bots** shows which commit it runs (`unknown` without it).

## Project origin

This project was bootstrapped from [Minara AI's research](https://x.com/minara/status/2044432012002635843) backtesting 236 public TradingView strategies under HyperLiquid's real fee structure. Of 236 tested, only 21 cleared 10% annualized return after fees — and the survivors mixed mean reversion, momentum, and trend following with no single approach dominating. The five strategies shipped here represent different archetypes from that Tier 1 list.

## License

MIT
