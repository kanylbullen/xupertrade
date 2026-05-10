from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        # Tolerate unknown env vars (e.g. legacy HYPERLIQUID_TESTNET that some
        # dev shells still export). Strict mode would crash on container
        # startup against any old environment file.
        "extra": "ignore",
    }

    # Exchange
    # "paper"   = simulated locally, no exchange contact
    # "testnet" = live orders against HyperLiquid testnet (fake money)
    # "mainnet" = live orders against HyperLiquid mainnet (REAL money)
    exchange_mode: str = "paper"
    hyperliquid_private_key: str = ""
    # If set, uses API-wallet pattern: orders are signed by
    # hyperliquid_private_key but executed on this account's behalf.
    # Leave empty to trade on the signing wallet's own account.
    hyperliquid_account_address: str = ""
    # Mainnet wallet address (read-only) used to query the user's vault
    # equities via HL's `userVaultEquities` endpoint. Public address only —
    # no private key needed since vault equities are public on-chain. Leave
    # empty to disable the "My vault positions" panel. Distinct from
    # hyperliquid_account_address because that's the trading account on
    # whichever network this bot runs on (often a testnet wallet), but
    # vaults only exist on mainnet.
    vault_tracking_address: str = ""

    # Comma-separated list of portfolio providers to query. Each shows
    # up as its own card on /portfolio. The matching provider's
    # connection vars (below) must also be filled in.
    #
    #   ""                     → page disabled
    #   "rotki"                → only Rotki
    #   "rotki,ghostfolio"     → both, side-by-side
    #   "rotki,ghostfolio,coinstats" → all three
    #
    # Special value "*" enables every provider whose creds are set.
    portfolio_providers: str = ""

    # CoinStats portfolio (read-only). Requires Degen plan subscription.
    # 8 credits per request — page caches 5 min in Redis. Get share token
    # from CoinStats app → portfolio → share. See coinstats.app/docs/sharetoken
    coinstats_api_key: str = ""
    coinstats_share_token: str = ""
    # Optional: set if your portfolio is passcode-protected on CoinStats.
    coinstats_passcode: str = ""

    # Rotki — open-source self-hosted portfolio tracker (https://rotki.com).
    # Run a Rotki backend (e.g. via Docker) and point us at it. Auth is
    # session-based: we POST username+password to log in, then read
    # balances. Premium subscription not required for the local instance.
    rotki_url: str = ""           # e.g. "http://rotki:5042"
    rotki_username: str = ""
    rotki_password: str = ""

    # Ghostfolio — open-source self-hosted multi-asset portfolio tracker
    # (https://ghostfol.io). Broader asset universe than Rotki — supports
    # stocks, ETFs, funds, plus crypto. Auth is via Bearer token (or the
    # scoped "API key" in newer versions). The token is generated from
    # Ghostfolio settings → Membership → "Security token".
    ghostfolio_url: str = ""      # e.g. "http://ghostfolio:3333"
    ghostfolio_token: str = ""    # security token / API token

    @property
    def is_paper(self) -> bool:
        return self.exchange_mode == "paper"

    @property
    def is_testnet(self) -> bool:
        return self.exchange_mode == "testnet"

    @property
    def is_mainnet(self) -> bool:
        return self.exchange_mode == "mainnet"

    @property
    def is_live(self) -> bool:
        return self.exchange_mode in ("testnet", "mainnet")

    # Paper trading
    paper_initial_balance: float = 10_000.0

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/hypertrade"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Engine
    default_symbol: str = "BTC"
    default_timeframe: str = "4h"
    poll_interval_seconds: int = 60
    api_port: int = 8000

    # Risk management
    max_position_size_usd: float = 1_000.0  # margin per single position
    max_daily_loss_usd: float = 500.0
    # Total open margin (sum across all open positions) cap. New opens are
    # blocked if going through would cross this. 0 disables the cap.
    max_total_exposure_usd: float = 5_000.0
    kill_switch: bool = False
    taker_fee_rate: float = 0.00045

    # Trade-rate anomaly alarm. When a strategy starts spam-trading
    # (e.g. due to a stale-bar SL bug like the 2026-05-09 hash_momentum
    # incident), this catches it within ~5 min and auto-pauses the
    # offender via Redis + emits a Telegram alert.
    #
    # Triggers when EITHER:
    #   (a) hourly trade count > baseline_multiplier × 7d-avg-per-hour
    #       AND > min_hourly_floor (prevents 5×0=0 false pass)
    #   (b) hourly trade count > absolute_ceiling regardless of baseline
    #       (catches first-time-active strategies bypass)
    trade_rate_alarm_enabled: bool = True
    trade_rate_alarm_check_interval_seconds: int = 300
    trade_rate_alarm_baseline_multiplier: float = 5.0
    trade_rate_alarm_min_hourly_floor: int = 5
    trade_rate_alarm_absolute_ceiling: int = 20

    # HyperLiquid SDK timeouts (audit M2). Without these, a hung HL API
    # call would block the executor thread → block the runner tick →
    # stop the heartbeat → freeze risk-cap checks. The SDK uses
    # `requests` internally with no explicit timeouts. Reads are short
    # because tenacity retries them (3×); writes get a more generous
    # window since order placement isn't retried (would risk duplicate
    # fills) and HL's match engine can take a few seconds under load.
    hl_read_timeout_seconds: float = 5.0
    hl_order_timeout_seconds: float = 15.0

    # HyperLiquid init retry. The SDK's HLExchange constructor fetches
    # meta + spot_meta synchronously, so a HL outage at bot startup
    # would crash __init__ → container exit → docker restart-loop until
    # HL recovers (4.5h on 2026-05-09). Retry with exponential backoff
    # buys us through brief glitches. Sleeps happen BETWEEN attempts —
    # with attempts=5, we sleep 4 times (2s + 4s + 8s + 16s = 30s
    # total) before declaring HL down on the 5th failure.
    hl_init_retry_attempts: int = 5
    hl_init_retry_backoff_seconds: float = 2.0

    # API authentication
    api_key: str = ""  # if set, all POST endpoints require X-Api-Key header

    # Telegram notifications (optional). Only enable on ONE bot instance
    # in multi-mode setups — that single notifier subscribes to all 3 modes'
    # event channels and routes commands per-mode internally.
    telegram_enabled: bool = True
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # Comma-separated list of event types to forward. Empty = no filtering
    # (everything except heartbeat). Heartbeat is never sent (too noisy).
    # signal.generated is excluded by default — trade.executed already
    # carries the reason and only fires when an order is actually placed.
    telegram_events: str = (
        "trade.executed,position.closed,error,"
        "vault.qualified,vault.disqualified"
    )


settings = Settings()
