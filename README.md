# Xupertrade

Automated crypto trading bot targeting [HyperLiquid](https://hyperliquid.xyz) with a real-time monitoring dashboard, multi-environment support (paper / testnet / mainnet), and Telegram control.

> The internal codename `hypertrade` is still used in container
> names, the Postgres database name, the Phase secrets app, the
> source-tree path, and most code references. Only the user-facing
> brand is **Xupertrade** — kept distinct from HyperLiquid's name to
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

- **Three modes side-by-side** — `paper`, `testnet`, and `mainnet` run as independent bot containers with isolated state, so you can A/B test strategies in paper while testnet handles canary trades and mainnet runs production.
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
│  ├── bot/             Python trading engine                            │
│  │   ├── strategies/  Strategy implementations + registry              │
│  │   ├── exchange/    PaperExchange + HyperLiquidExchange              │
│  │   ├── engine/      Runner loop, control state, indicator status     │
│  │   ├── data/        WebSocket feed + REST candles + indicators       │
│  │   ├── events/      Redis pub/sub + typed event schemas              │
│  │   ├── notify/      Telegram notifier + command handler              │
│  │   ├── db/          SQLAlchemy models + repository                   │
│  │   └── api.py       aiohttp HTTP API for dashboard control           │
│  ├── dashboard/       Next.js 16 + shadcn/ui                           │
│  └── docker-compose.yml                                                │
└────────────────────────────────────────────────────────────────────────┘
        │                                  │
        ▼                                  ▼
   bot-paper:8000      bot-testnet:8001          bot-mainnet:8002 (opt-in)
        │                  │                            │
        └──────────────────┼────────────────────────────┘
                           │
                  ┌────────┴────────┐
                  │   PostgreSQL    │  trades, positions, equity_snapshots
                  │      Redis      │  control state (per-mode), pub/sub events
                  └─────────────────┘
                           │
                           ▼
              dashboard:3000  (Next.js, ?mode= picks bot)
                           │
                           ▼
               Telegram bot (subscribes to all 3 modes)
```

Each bot container is **fully isolated**:
- Own exchange instance (PaperExchange / HyperLiquid testnet / HyperLiquid mainnet).
- Own Redis state under `hypertrade:{mode}:control:*` keys.
- Own event channel `hypertrade:{mode}:events`.
- Trades persisted with `mode` column so DB queries can filter per-mode.

## Strategies

Six strategies implemented from [Minara AI's backtesting study](https://x.com/minara/status/2044432012002635843) of 236 TradingView strategies tested under HyperLiquid fees:

| Strategy | Pair | Timeframe | Backtest APR | Default leverage | Description |
|----------|------|-----------|--------------|------------------|-------------|
| `btc_mean_reversion` | BTC/USDT | 15m | +204.6% | 6× | **#1 ranked.** RSI(14) cross-below 20 entry, cross-above 65 exit. Sharpe 4+, 16 trades / 90 days — highest risk-adjusted returns in the study. |
| `volatility_breakout` | ETH/USDT | 1h | +124.6% | 8× | Toby Crabel-style breakout: range × 0.6 above prev close. Fixed 2% stop / 3% take-profit (defined risk → higher leverage acceptable). |
| `bb_short` | SOL/USDT | 1h | +48.1% | 2× | Shorts when price breaks 2% above upper Bollinger Band (20, 2σ). Exits at 2% profit. 100% historical win rate over 49 trades. |
| `supertrend` | BTC/USDT | 1d | +35.6% | 1× | ATR-based trend following (period 10, multiplier 8.5). Long on bullish flip, exit on bearish. ~4 trades / 4 years. No stop loss. |
| `rsi_momentum` | BTC/USDT | 4h | +24.3% | 5× | Buys when RSI(14) crosses above 70 (momentum continuation). Sharpe 1.85, 14.8% max DD. |
| `sma_rsi` | ETH/USDT | 1d | +23.5% | 3× | Long when price > SMA50 & SMA200 and smoothed RSI(21,9) > 57. Beat buy-and-hold by +117pp during a losing period. |

Leverage defaults are chosen by historical max drawdown and whether the strategy has a hard stop loss. They can be overridden per-mode from the dashboard or Telegram. The bot computes the maximum leverage needed per coin across active strategies and pushes that to HyperLiquid at startup (HyperLiquid leverage is per-coin, not per-position).

## Quick Start

### Prerequisites

- Docker & Docker Compose
- For testnet/mainnet: a HyperLiquid wallet that has previously deposited on mainnet (testnet faucet requires it)

### Local development

```bash
git clone <repo-url> && cd hypertrade
docker compose up -d
# Dashboard: http://localhost:3000
# bot-paper API:    http://localhost:8000
# bot-testnet API:  http://localhost:8001  (needs HYPERLIQUID_PRIVATE_KEY in .env)
# bot-mainnet API:  http://localhost:8002  (only with --profile mainnet)
```

### Configuration (`.env`)

```env
# Testnet wallet — bot signs orders with API_KEY, executes on ACCOUNT_ADDRESS's behalf
HYPERLIQUID_PRIVATE_KEY=0x...           # API wallet's private key (trade-only, can't withdraw)
HYPERLIQUID_ACCOUNT_ADDRESS=0x...       # Main wallet (where the funds live)

# Mainnet (only used when bot-mainnet starts)
HYPERLIQUID_MAINNET_PRIVATE_KEY=0x...
HYPERLIQUID_MAINNET_ACCOUNT_ADDRESS=0x...

# Telegram (optional but recommended)
TELEGRAM_BOT_TOKEN=...                  # from @BotFather
TELEGRAM_CHAT_ID=...                    # your numeric chat id
TELEGRAM_EVENTS=signal.generated,trade.executed,position.closed,error
```

Per-bot env (set in `docker-compose.yml`, override via env):

| Var | Default | Description |
|-----|---------|-------------|
| `EXCHANGE_MODE` | `paper`/`testnet`/`mainnet` | Which exchange this bot talks to. Set per-service. |
| `MAX_POSITION_SIZE_USD` | `200` | Margin per trade. Notional = this × strategy.leverage. |
| `MAX_DAILY_LOSS_USD` | `100` | Trading halts when daily PnL drops below this. |
| `POLL_INTERVAL_SECONDS` | `60` | How often the runner ticks. |
| `KILL_SWITCH` | `false` | Emergency stop (set without restart via dashboard). |
| `TELEGRAM_ENABLED` | `false` (paper/mainnet), `true` (testnet) | Only one bot should run the Telegram poller. |

## Setting up HyperLiquid testnet

1. Open <https://app.hyperliquid-testnet.xyz/drip>, connect your **mainnet** wallet (must have prior deposit history) and claim 1000 mock USDC.
2. Open <https://app.hyperliquid-testnet.xyz/API>, generate an **API wallet** (gives a fresh address + private key, valid 180 days). Authorize it with your main wallet — this lets the bot sign orders on your account's behalf without ever being able to withdraw funds.
3. Set both keys in `.env`:
   ```env
   HYPERLIQUID_PRIVATE_KEY=<API wallet private key>
   HYPERLIQUID_ACCOUNT_ADDRESS=<main wallet address>
   ```
4. `docker compose up -d bot-testnet` and verify with:
   ```bash
   curl http://localhost:8001/api/hyperliquid/diagnostic
   # → { "ok": true, "network": "testnet", "api_wallet_mode": true,
   #     "account_value_usd": 999.0, ... }
   ```
5. Open the dashboard, switch to `?mode=testnet`, and click **Resume** when ready.

## Going live on mainnet

Same flow but with mainnet wallet keys. Mainnet bot only starts when explicitly requested:

```bash
docker compose --profile mainnet up -d bot-mainnet
```

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
4. Restart the testnet bot (which is the one that owns Telegram): `docker compose up -d --force-recreate bot-testnet`. You should receive a startup ping within ~10 seconds: `🔵 TESTNET 🚀 Xupertrade started`.

### Notifications

By default the bot forwards these event types from all three modes (paper/testnet/mainnet) to the same chat:
- `signal.generated` — a strategy generated a signal that's about to execute
- `trade.executed` — order filled
- `position.closed` — position closed with realized PnL
- `error` — bot-side error in a strategy or the engine

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

Currently commands operate on the bot instance that runs Telegram (the testnet bot). Cross-mode commands like `/status mainnet` are a planned future addition.

## Bot architecture

### Exchange abstraction

```python
class Exchange(ABC):
    async def place_order(symbol, side, size, order_type, price) -> Order
    async def cancel_order(order_id) -> bool
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

Dark-themed Next.js 16 (App Router, Turbopack) app on port 3000. The 3-way mode toggle in the top nav routes everything (controls, indicator status, trades view) to the correct bot.

| Page | Contents |
|------|----------|
| `/` (Overview) | TradingView ticker, total equity, P&L stat cards, equity curve, indicator status grid (per strategy: signal + distance to trigger), open positions, recent trades, embedded BTC chart |
| `/trades` | Filtered trade history per mode |
| `/strategies` | Detailed per-strategy cards (logic, strengths, weaknesses, parameters), per-strategy on/off + leverage input, embedded TradingView charts with strategy-specific indicators preloaded |
| `/status` | Pause/Resume button, "Close All Positions" with confirmation dialog, per-strategy toggles, live event log via Server-Sent Events from Redis |

Each component reads `?mode=` from the URL via `useMode()` and prefixes its API calls accordingly so a single dashboard handles all three environments.

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
- PostgreSQL 16 — historical trades, positions, equity snapshots (`mode` column for per-env filtering)
- Redis 7 — runtime state + event pub/sub
- Docker Compose — three bot services (`bot-paper`, `bot-testnet`, `bot-mainnet` under profile) + dashboard + Postgres + Redis

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

3. Add to `strategies = [...]` in `bot/hypertrade/main.py`.

4. (Optional) Add an entry to `bot/hypertrade/engine/indicators_status.py` so the dashboard shows distance-to-trigger.

5. (Optional) Add a card on `dashboard/src/app/strategies/page.tsx` for documentation.

6. `docker compose build && docker compose up -d`.

## Project origin

This project was bootstrapped from [Minara AI's research](https://x.com/minara/status/2044432012002635843) backtesting 236 public TradingView strategies under HyperLiquid's real fee structure. Of 236 tested, only 21 cleared 10% annualized return after fees — and the survivors mixed mean reversion, momentum, and trend following with no single approach dominating. The five strategies shipped here represent different archetypes from that Tier 1 list.

## License

MIT
