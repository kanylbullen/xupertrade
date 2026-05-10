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

    # Multi-tenancy (audit Phase 3b). When set (UUID string from env
    # `TENANT_ID`, injected by the dashboard's bot-orchestrator), the
    # repository tags every INSERT with this tenant_id and scopes
    # SELECTs/UPDATEs/DELETEs to it. When NULL (operator's current
    # 3-mode deploy until Phase 6 cutover), repository falls back to
    # today's tenant-agnostic behavior. `BOT_ID` is the corresponding
    # `tenant_bots.id` UUID — used for Redis key scoping when two
    # bots from the same tenant share a coin (multi-bot tenants).
    tenant_id: str | None = None
    bot_id: str | None = None

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

    # `signal.size`-override safety ceiling (audit H8). Strategies that
    # emit `Signal(size=N)` (e.g. vvv_hedge) bypass _calculate_size so
    # MAX_POSITION_SIZE_USD doesn't bind. The bot rejects opens whose
    # `size × current_price` exceeds this multiplier × MAX_POSITION_SIZE_USD.
    # Default 10× = a $200 cap allows up to $2k notional on a sized signal,
    # which covers vvv_hedge's design (400 VVV × $5 = $2k) without
    # accommodating an accidental 10× param bump.
    signal_size_max_multiplier: float = 10.0

    # Mainnet allowlist (audit C3). Comma-separated list of strategy names
    # that are allowed to trade on mainnet. EMPTY = no strategies trade —
    # explicit fail-closed on first mainnet deploy. Operator must add the
    # one strategy they're piloting (e.g. `bb_short`) to .env and restart
    # before mainnet trades anything. Ignored on paper/testnet — both run
    # the full registered set as before. Setting this is a HARD upper bound;
    # Redis-side `disabled` toggles can subset further but cannot enable
    # beyond the allowlist.
    mainnet_enabled_strategies: str = ""

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
