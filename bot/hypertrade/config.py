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

    # Portfolio provider selector. "" = disabled (default), "coinstats" =
    # third-party paid, "rotki" = open-source self-hosted. The matching
    # provider's connection vars (below) must also be filled in.
    portfolio_provider: str = ""

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
