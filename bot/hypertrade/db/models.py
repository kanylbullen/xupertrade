"""SQLAlchemy models for trade history and strategy state."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Boolean,
    Text,
    UniqueConstraint,
)
from sqlalchemy import JSON, Uuid
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


# ──────────────────────────────────────────────────────────────────────
# Multi-tenancy (audit-tracking PR #35 plan, Phase 1)
#
# Tables added Phase 1; tenant_id columns added to existing tables but
# kept NULLABLE for backwards compat. Phase 6 (operator cutover)
# backfills operator as tenant 1 and converts to NOT NULL.
# ──────────────────────────────────────────────────────────────────────


class Tenant(Base):
    """One row per authenticated user. The operator is tenant 1, with
    `is_operator=true` and `multi_bot_enabled=true`. All other users
    default to single-bot mode (one mode per tenant).

    Secrets at rest are encrypted with a key derived from the user's
    passphrase via Argon2id; this row stores only the salt + verifier.
    The derived key K is never persisted.
    """

    __tablename__ = "tenants"

    id = Column(
        Uuid(), primary_key=True, default=uuid.uuid4
    )
    authentik_sub = Column(String(128), unique=True, nullable=False, index=True)
    email = Column(String(255), nullable=False)
    display_name = Column(String(128), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    # Argon2id outputs — set after the user picks their passphrase.
    # Both NULL = no passphrase set yet (user must complete onboarding).
    passphrase_salt = Column(LargeBinary(16), nullable=True)
    passphrase_verifier = Column(LargeBinary(32), nullable=True)
    # is_active: false = login disabled (operator-disabled or self-deactivated)
    is_active = Column(Boolean, default=True, nullable=False)
    # is_operator: true grants admin endpoints (stop-any-bot, list-tenants);
    # operator role bypasses tenant RLS but still cannot decrypt secrets
    is_operator = Column(Boolean, default=False, nullable=False)
    # multi_bot_enabled: true allows up to 3 bots (one per mode); false
    # gates bot-create at the application layer to max 1
    multi_bot_enabled = Column(Boolean, default=False, nullable=False)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    # alembic 0016 — operator-set policy controls (NULL = no cap / allowlist).
    max_active_bots = Column(Integer, nullable=True)
    max_active_strategies = Column(Integer, nullable=True)
    # ARRAY on Postgres; JSON variant on SQLite (test runtime only).
    allowed_strategies = Column(
        ARRAY(Text).with_variant(JSON, "sqlite"), nullable=True
    )


class TenantBot(Base):
    """A single bot belonging to a tenant. Up to 3 per tenant (one per
    mode), enforced by UNIQUE (tenant_id, mode). Single-bot tenants
    have at most 1 row; multi-bot tenants (operator) can have all 3.

    container_id/container_name are written when the dashboard's bot
    lifecycle endpoint starts a docker container; cleared on stop.
    """

    __tablename__ = "tenant_bots"

    id = Column(
        Uuid(), primary_key=True, default=uuid.uuid4
    )
    tenant_id = Column(
        Uuid(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    mode = Column(String(16), nullable=False)  # paper | testnet | mainnet
    container_id = Column(String(64), nullable=True)
    container_name = Column(String(128), nullable=True)
    is_running = Column(Boolean, default=False, nullable=False, index=True)
    # Telegram webhook anti-forge token (low-sensitivity per PR #35 §8;
    # plaintext OK so dashboard can validate webhooks without unlock).
    # The actual bot token (which can impersonate) lives encrypted in
    # `tenant_secrets`.
    telegram_webhook_secret = Column(String(64), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    last_started_at = Column(DateTime(timezone=True), nullable=True)
    last_stopped_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "mode", name="uq_tenant_bots_mode"),
    )


class TenantSecret(Base):
    """One encrypted secret per (tenant, key). AES-GCM ciphertext +
    nonce; no plaintext is ever stored. Key is derived per-session from
    the tenant's passphrase via Argon2id (see tenants.passphrase_salt).

    `key` examples: HYPERLIQUID_PRIVATE_KEY, HYPERLIQUID_ACCOUNT_ADDRESS,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, etc.
    """

    __tablename__ = "tenant_secrets"

    tenant_id = Column(
        Uuid(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    key = Column(String(64), primary_key=True)
    ciphertext = Column(LargeBinary, nullable=False)
    nonce = Column(LargeBinary(12), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    # Operator-set rotation deadline for HL private keys (alembic 0015).
    # NULL = no expiry tracking. Only the dashboard credentials UI
    # writes this, and only for HYPERLIQUID_{,MAINNET_}PRIVATE_KEY.
    expires_at = Column(DateTime(timezone=True), nullable=True)


class TenantAuditLog(Base):
    """Per-tenant audit trail (PR #35 §11.5). Phase 5 starts writing
    to this table; created in Phase 1 so Phase 5 doesn't need another
    migration. Append-only. Indexed on (tenant_id, ts) for the common
    "show me my recent activity" query.
    """

    __tablename__ = "tenant_audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(
        Uuid(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Who took the action: 'tenant' (the user themselves) or 'operator'
    actor = Column(String(16), nullable=False)
    # What happened: e.g. 'secret.set', 'bot.start', 'passphrase.changed',
    # 'tenant.disabled' (operator action)
    action = Column(String(64), nullable=False)
    # Free-form context: which secret key was set, which bot id was
    # started, etc. Plaintext but never the secret values themselves.
    context_json = Column(Text, default="{}")
    ts = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )


class TenantTelegramLink(Base):
    """Maps a tenant to their linked Telegram chat (1:1). PR 3a
    created the table via alembic 0012; this model lets the bot's
    /link handler upsert + lookup."""

    __tablename__ = "tenant_telegram_links"

    tenant_id = Column(
        Uuid(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # BIGINT — Telegram chat IDs are negative for groups and can
    # exceed INT32 for some user IDs post-2024.
    telegram_chat_id = Column(BigInteger, nullable=False)
    telegram_username = Column(String(64))
    linked_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    last_unlock_at = Column(DateTime(timezone=True))


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Multi-tenancy: nullable in Phase 1 for backwards compat; backfilled
    # to operator's tenant in Phase 6, made NOT NULL after that.
    tenant_id = Column(
        Uuid(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    order_id = Column(String(64), unique=True, nullable=False)
    strategy_name = Column(String(64), nullable=False, index=True)
    symbol = Column(String(16), nullable=False, index=True)
    side = Column(String(8), nullable=False)  # buy/sell
    size = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    fee = Column(Float, default=0.0)
    pnl = Column(Float, nullable=True)
    reason = Column(Text, default="")
    is_paper = Column(Boolean, default=True, index=True)  # legacy: True for paper, False for testnet/mainnet
    mode = Column(String(16), default="paper", index=True)  # paper | testnet | mainnet
    timestamp = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )


class PositionRecord(Base):
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(
        Uuid(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    strategy_name = Column(String(64), nullable=False, index=True)
    symbol = Column(String(16), nullable=False, index=True)
    side = Column(String(8), nullable=False)  # long/short
    size = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=True)
    pnl = Column(Float, nullable=True)
    is_open = Column(Boolean, default=True, index=True)
    is_paper = Column(Boolean, default=True, index=True)
    mode = Column(String(16), default="paper", index=True)
    # Strategy-internal state at signal time (JSON: e.g. {"sl": 1.23, "tp": 4.56,
    # "entry": 2.0, "trail_extreme": null}). On restart, restore_state reads
    # these exact values to eliminate SL drift across restarts. Strategies
    # that don't persist state leave this None — fallback is recompute.
    state_json = Column(Text, nullable=True)
    opened_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    closed_at = Column(DateTime(timezone=True), nullable=True)


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(
        Uuid(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    total_equity = Column(Float, nullable=False)
    available_balance = Column(Float, nullable=False)
    unrealized_pnl = Column(Float, default=0.0)
    is_paper = Column(Boolean, default=True, index=True)
    mode = Column(String(16), default="paper", index=True)
    timestamp = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )


class FundingPayment(Base):
    """A single funding payment received or paid by the account.

    Pulled periodically from HL's user_funding_history. usdc is signed:
    positive = received, negative = paid. Attribution to a strategy is
    best-effort: at insertion time, we look up the open position on this
    coin and tag with that strategy_name. NULL if no DB position covered
    the payment time (rare — typically only for orphan positions).
    """

    __tablename__ = "funding_payments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(
        Uuid(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # HL's funding event time (epoch ms → DateTime)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    # HL hash for idempotency. user_funding_history can return overlapping
    # ranges so we dedupe on this.
    hash = Column(String(80), unique=True, nullable=False)
    coin = Column(String(16), nullable=False, index=True)
    usdc = Column(Float, nullable=False)
    szi = Column(Float, nullable=True)  # signed position size at funding time
    funding_rate = Column(Float, nullable=True)
    strategy_name = Column(String(64), nullable=True, index=True)
    is_paper = Column(Boolean, default=False, index=True)
    mode = Column(String(16), default="testnet", index=True)


class BacktestRun(Base):
    """Persisted backtest result. One row per CLI invocation per strategy.
    Lets us compare runs over time, see how parameter changes affect APR,
    and surface results in the dashboard. Trades and equity-curve are
    NOT stored (would explode the table); just the summary metrics."""

    __tablename__ = "backtest_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(
        Uuid(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    strategy_name = Column(String(64), nullable=False, index=True)
    symbol = Column(String(16), nullable=False)
    timeframe = Column(String(8), nullable=False)
    leverage = Column(Integer, nullable=False, default=1)
    period_start = Column(DateTime(timezone=True), nullable=False)
    period_end = Column(DateTime(timezone=True), nullable=False)
    days = Column(Float, nullable=False)
    initial_equity = Column(Float, nullable=False)
    final_equity = Column(Float, nullable=False)
    total_return_pct = Column(Float, nullable=False)
    apr = Column(Float, nullable=False)
    sharpe = Column(Float, nullable=False)
    max_drawdown_pct = Column(Float, nullable=False)
    num_trades = Column(Integer, nullable=False)
    num_round_trips = Column(Integer, nullable=False)
    wins = Column(Integer, nullable=False)
    losses = Column(Integer, nullable=False)
    win_rate = Column(Float, nullable=False)
    fees_paid = Column(Float, nullable=False)
    position_size_usd = Column(Float, nullable=False)
    fee_rate = Column(Float, nullable=False)
    slippage_bps = Column(Float, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )


class ManualOnchainLevel(Base):
    """Manually-recorded on-chain level snapshot (e.g. from Roots' weekly newsletter).

    HODL signals can use these as ground-truth when fresh (≤14 days old) and
    fall back to proxy approximations otherwise. One row per reading.
    Insert via `record_levels.py` CLI or future dashboard form.
    """

    __tablename__ = "manual_onchain_levels"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(
        Uuid(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    recorded_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )
    sth_cost_basis_usd = Column(Float, nullable=True)
    lth_cost_basis_usd = Column(Float, nullable=True)
    realized_price_usd = Column(Float, nullable=True)
    cvdd_usd = Column(Float, nullable=True)
    source = Column(String(64), default="roots_newsletter")
    notes = Column(Text, default="")


class HodlPurchase(Base):
    """Manually-logged spot accumulation purchase, separate from algo trades.

    Tracks anskaffningsvärde (SEK cost basis) per purchase for K4 reporting,
    plus cold-storage status. Not touched by the bot — purely human-entered
    via record_purchase.py CLI.
    """

    __tablename__ = "hodl_purchases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(
        Uuid(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    purchased_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )
    asset = Column(String(16), nullable=False, default="BTC", index=True)
    exchange = Column(String(32), default="kraken")
    amount_local = Column(Float, nullable=False)        # how much local currency spent
    local_currency = Column(String(8), default="SEK")
    btc_amount = Column(Float, nullable=False)          # how much BTC received
    btc_price_usd = Column(Float, nullable=False)       # spot at purchase time
    btc_price_local = Column(Float, nullable=True)      # spot in local currency
    fx_rate = Column(Float, nullable=True)              # local per USD at purchase
    zone = Column(String(16), nullable=True)            # green/yellow/red/deep at the time
    cold_storage_at = Column(DateTime(timezone=True), nullable=True)
    cold_storage_address = Column(String(128), nullable=True)
    notes = Column(Text, default="")


class Vault(Base):
    """A HyperLiquid vault we've discovered. Static-ish metadata only;
    metrics that change daily live in `vault_snapshots`."""

    __tablename__ = "vaults"

    address = Column(String(42), primary_key=True)
    name = Column(String(128), nullable=True)
    leader_address = Column(String(42), nullable=True, index=True)
    description = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), nullable=True)
    profit_share_pct = Column(Float, nullable=True)
    relationship_type = Column(String(16), default="normal")
    first_seen_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class VaultSnapshot(Base):
    """Daily snapshot of a vault's risk-adjusted metrics. Each row is one
    poll; we only insert when something changed (or once per day)."""

    __tablename__ = "vault_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    vault_address = Column(
        String(42),
        ForeignKey("vaults.address", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    snapshot_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )
    aum_usd = Column(Float, nullable=True)
    nav = Column(Float, nullable=True)
    leader_equity_pct = Column(Float, nullable=True)
    depositor_count = Column(Integer, nullable=True)
    apr = Column(Float, nullable=True)
    age_days = Column(Integer, nullable=True)
    roi_7d = Column(Float, nullable=True)
    roi_30d = Column(Float, nullable=True)
    roi_90d = Column(Float, nullable=True)
    roi_180d = Column(Float, nullable=True)
    roi_365d = Column(Float, nullable=True)
    max_drawdown_pct = Column(Float, nullable=True)
    sharpe_180d = Column(Float, nullable=True)
    qualified = Column(Boolean, default=False, index=True)
    # JSON: {filter_name: {"passed": bool, "value": str, "threshold": str}}
    # Lets the dashboard show which filter caused a vault to fail.
    filter_breakdown_json = Column(Text, default="{}")
    allow_deposits = Column(Boolean, default=True)
    is_closed = Column(Boolean, default=False)

    __table_args__ = (
        UniqueConstraint("vault_address", "snapshot_at", name="uq_vault_snapshot"),
    )


class UserVaultEntry(Base):
    """The user's stake in a HyperLiquid vault. Composite PK on
    (user_address, vault_address). Refreshed daily from HL's
    `vaultDetails.followerState`, which is the source of truth for
    "what is my position worth?" — no need to track first-seen / last-seen
    diffs ourselves since HL gives us entry time + lifetime P&L directly.
    """

    __tablename__ = "user_vault_entries"

    tenant_id = Column(
        Uuid(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    user_address = Column(String(42), primary_key=True)
    vault_address = Column(
        String(42),
        ForeignKey("vaults.address", ondelete="CASCADE"),
        primary_key=True,
    )
    # Current value of the user's stake (HL's `vaultEquity`).
    vault_equity_usd = Column(Float, nullable=False, default=0.0)
    # Currently-unrealized P&L on this stake (HL's `pnl`).
    unrealized_pnl_usd = Column(Float, nullable=False, default=0.0)
    # Lifetime P&L on this stake including any realized portion (HL's
    # `allTimePnl`). This is what the user actually cares about for
    # "have I made money on this vault?".
    all_time_pnl_usd = Column(Float, nullable=False, default=0.0)
    # When the user first followed the vault (HL's `vaultEntryTime`).
    entered_at = Column(DateTime(timezone=True), nullable=True)
    days_following = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime(timezone=True), nullable=True)
    last_seen_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    # Equity ≈ 0 (full withdrawal) → marked exited; row kept for history.
    exited_at = Column(DateTime(timezone=True), nullable=True)


class VaultNavPoint(Base):
    """One historical NAV+PnL observation. Backfilled from HL on first
    encounter, appended daily thereafter. Composite PK = (address, ts).

    `nav` is total account value (deposits + withdrawals + cumulative
    pnl). `pnl_cum` is cumulative net PnL since vault inception. We
    store both so period returns can be computed as `(pnl_cum_t -
    pnl_cum_{t-1}) / nav_{t-1}` — flow-neutral, unlike NAV deltas.
    Pre-pnl-aware rows have pnl_cum=0 and the metric layer falls back
    to NAV-delta returns for them."""

    __tablename__ = "vault_nav_history"

    vault_address = Column(
        String(42),
        ForeignKey("vaults.address", ondelete="CASCADE"),
        primary_key=True,
    )
    timestamp = Column(DateTime(timezone=True), primary_key=True)
    nav = Column(Float, nullable=False)
    # NULL = we don't know the cumulative PnL for this point (legacy row
    # from before the pnl-aware schema, or HL didn't ship a matching
    # pnlHistory timestamp). Distinct from a real cumulative-PnL of zero.
    pnl_cum = Column(Float, nullable=True)


class StrategyConfig(Base):
    __tablename__ = "strategy_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(
        Uuid(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # Multi-tenant: name is unique PER TENANT, not globally. Phase 6
    # cutover replaces this UNIQUE with a composite once all rows have
    # tenant_id. For now we keep the column-level UNIQUE so Phase 1
    # is purely additive and doesn't break the operator's deploy.
    name = Column(String(64), unique=True, nullable=False)
    symbol = Column(String(16), nullable=False)
    timeframe = Column(String(8), nullable=False)
    enabled = Column(Boolean, default=True)
    params_json = Column(Text, default="{}")
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
