import {
  pgTable,
  serial,
  varchar,
  doublePrecision,
  boolean,
  text,
  timestamp,
  uuid,
  customType,
  index,
  uniqueIndex,
  bigint,
} from "drizzle-orm/pg-core";
import { sql } from "drizzle-orm";
import { drizzle } from "drizzle-orm/postgres-js";
import postgres from "postgres";

// Postgres BYTEA — Drizzle has no built-in, so we declare a custom
// type. Reads/writes are Node `Buffer` so callers can pass crypto
// outputs directly.
const bytea = customType<{ data: Buffer; default: false }>({
  dataType() {
    return "bytea";
  },
});

const connectionString = process.env.DATABASE_URL || "postgresql://postgres:postgres@localhost:5432/hypertrade";
const client = postgres(connectionString);
export const db = drizzle(client);

// Mirror the Python SQLAlchemy models

// tenant_id is NOT NULL on all data tables as of alembic 0011 (Phase 6c
// PR β). The Drizzle schema mirrors that on the 5 tables this module
// exposes (trades / positions / equity_snapshots / funding_payments /
// strategy_configs). Other tenant-scoped tables in alembic 0011
// (backtest_runs / manual_onchain_levels / hodl_purchases /
// user_vault_entries) are not declared here because the dashboard
// doesn't query them via Drizzle today — they're read through the
// bot's API. Add them here if/when a server-component query needs
// them, with the same notNull tenantId column.
//
// Every read site MUST filter by tenantId or it leaks across tenants.
// queries.ts requires a tenantId arg on every exported function;
// server components must pass it.

export const trades = pgTable("trades", {
  id: serial("id").primaryKey(),
  tenantId: uuid("tenant_id").notNull(),
  orderId: varchar("order_id", { length: 64 }).notNull(),
  strategyName: varchar("strategy_name", { length: 64 }).notNull(),
  symbol: varchar("symbol", { length: 16 }).notNull(),
  side: varchar("side", { length: 8 }).notNull(),
  size: doublePrecision("size").notNull(),
  price: doublePrecision("price").notNull(),
  fee: doublePrecision("fee").default(0),
  pnl: doublePrecision("pnl"),
  reason: text("reason").default(""),
  isPaper: boolean("is_paper").default(true),
  mode: varchar("mode", { length: 16 }).default("paper"),
  timestamp: timestamp("timestamp", { withTimezone: true }).defaultNow(),
});

export const positions = pgTable("positions", {
  id: serial("id").primaryKey(),
  tenantId: uuid("tenant_id").notNull(),
  strategyName: varchar("strategy_name", { length: 64 }).notNull(),
  symbol: varchar("symbol", { length: 16 }).notNull(),
  side: varchar("side", { length: 8 }).notNull(),
  size: doublePrecision("size").notNull(),
  entryPrice: doublePrecision("entry_price").notNull(),
  exitPrice: doublePrecision("exit_price"),
  pnl: doublePrecision("pnl"),
  isOpen: boolean("is_open").default(true),
  isPaper: boolean("is_paper").default(true),
  mode: varchar("mode", { length: 16 }).default("paper"),
  openedAt: timestamp("opened_at", { withTimezone: true }).defaultNow(),
  closedAt: timestamp("closed_at", { withTimezone: true }),
});

export const equitySnapshots = pgTable("equity_snapshots", {
  id: serial("id").primaryKey(),
  tenantId: uuid("tenant_id").notNull(),
  totalEquity: doublePrecision("total_equity").notNull(),
  availableBalance: doublePrecision("available_balance").notNull(),
  unrealizedPnl: doublePrecision("unrealized_pnl").default(0),
  isPaper: boolean("is_paper").default(true),
  mode: varchar("mode", { length: 16 }).default("paper"),
  timestamp: timestamp("timestamp", { withTimezone: true }).defaultNow(),
});

export const fundingPayments = pgTable("funding_payments", {
  id: serial("id").primaryKey(),
  tenantId: uuid("tenant_id").notNull(),
  timestamp: timestamp("timestamp", { withTimezone: true }).notNull(),
  hash: varchar("hash", { length: 80 }).notNull(),
  coin: varchar("coin", { length: 16 }).notNull(),
  usdc: doublePrecision("usdc").notNull(),
  szi: doublePrecision("szi"),
  fundingRate: doublePrecision("funding_rate"),
  strategyName: varchar("strategy_name", { length: 64 }),
  isPaper: boolean("is_paper").default(false),
  mode: varchar("mode", { length: 16 }).default("testnet"),
});

export const strategyConfigs = pgTable("strategy_configs", {
  id: serial("id").primaryKey(),
  tenantId: uuid("tenant_id").notNull(),
  // .unique() mirrors the global UNIQUE on `name` from alembic 0001
  // (still in effect — see `\d strategy_configs` on prod). This is
  // technically wrong for multi-tenancy: two tenants using the same
  // strategy name would collide. The proper fix is an alembic
  // migration to drop the global unique and replace with a
  // (tenant_id, name) composite — tracked as a follow-up to Phase 6c.
  // The table is empty in prod today (0 rows pre-cutover) so the
  // collision risk is theoretical until a tenant actually inserts.
  name: varchar("name", { length: 64 }).notNull().unique(),
  symbol: varchar("symbol", { length: 16 }).notNull(),
  timeframe: varchar("timeframe", { length: 8 }).notNull(),
  enabled: boolean("enabled").default(true),
  paramsJson: text("params_json").default("{}"),
  createdAt: timestamp("created_at", { withTimezone: true }).defaultNow(),
  updatedAt: timestamp("updated_at", { withTimezone: true }).defaultNow(),
});

// ─────────────────────────────────────────────────────────────────────
// Multi-tenancy (Phase 1 alembic migration 0009; Phase 2b mirror)
//
// Mirrors the four tables added by `bot/alembic/versions/
// 0009_multi_tenancy_schema.py`. Authoritative schema lives in the
// Python alembic migration; this is the Drizzle view that the
// dashboard's API routes use to read/write the rows.
//
// Trust model B per docs/plans/multi-tenancy.md:
// - `tenants.passphrase_salt` + `passphrase_verifier` are Argon2id
//   outputs used to gate unlock attempts (see crypto/passphrase.ts)
// - `tenant_secrets.ciphertext + nonce` is AES-256-GCM under K, where
//   K is derived per-session from the user's passphrase (never
//   persisted)
// - The operator (DB-root) cannot decrypt either without the user's
//   passphrase.
// ─────────────────────────────────────────────────────────────────────

export const tenants = pgTable(
  "tenants",
  {
    // No `.defaultRandom()` here — alembic 0009 emits `id UUID PRIMARY
    // KEY` with no server default (PR #36 review removed the
    // `gen_random_uuid()` default to avoid the pgcrypto extension
    // dependency). Application code must supply UUIDs at insert time
    // via `crypto.randomUUID()` or similar. Drizzle therefore treats
    // `id` as REQUIRED on insert; if you forget it, the type checker
    // catches it before runtime.
    id: uuid("id").primaryKey(),
    authentikSub: varchar("authentik_sub", { length: 128 }).notNull(),
    email: varchar("email", { length: 255 }).notNull(),
    displayName: varchar("display_name", { length: 128 }),
    createdAt: timestamp("created_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
    // 16-byte Argon2id salt; null = passphrase not set yet
    passphraseSalt: bytea("passphrase_salt"),
    // 32-byte HMAC-SHA-256 verifier (see crypto/passphrase.ts:makeVerifier)
    passphraseVerifier: bytea("passphrase_verifier"),
    isActive: boolean("is_active").notNull().default(true),
    isOperator: boolean("is_operator").notNull().default(false),
    multiBotEnabled: boolean("multi_bot_enabled").notNull().default(false),
    lastLoginAt: timestamp("last_login_at", { withTimezone: true }),
  },
  (t) => ({
    authentikSubIdx: uniqueIndex("idx_tenants_authentik_sub").on(t.authentikSub),
  }),
);

export const tenantBots = pgTable(
  "tenant_bots",
  {
    // Same as tenants.id — application supplies UUID, no server default.
    id: uuid("id").primaryKey(),
    tenantId: uuid("tenant_id")
      .notNull()
      .references(() => tenants.id, { onDelete: "cascade" }),
    mode: varchar("mode", { length: 16 }).notNull(),  // paper | testnet | mainnet
    containerId: varchar("container_id", { length: 64 }),
    containerName: varchar("container_name", { length: 128 }),
    isRunning: boolean("is_running").notNull().default(false),
    // Anti-forge token for the Telegram webhook receiver
    // (low-sensitivity per multi-tenancy plan §8 — the actual bot
    // token stays encrypted in tenant_secrets).
    telegramWebhookSecret: varchar("telegram_webhook_secret", { length: 64 }),
    createdAt: timestamp("created_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
    lastStartedAt: timestamp("last_started_at", { withTimezone: true }),
    lastStoppedAt: timestamp("last_stopped_at", { withTimezone: true }),
  },
  (t) => ({
    tenantIdx: index("idx_tenant_bots_tenant").on(t.tenantId),
    // UNIQUE(tenant_id, mode) — one bot per (tenant, mode); enforced
    // at the DB layer too. Application-level multi_bot_enabled gate
    // is documented in the plan (§6 bot-create flow).
    tenantModeUq: uniqueIndex("uq_tenant_bots_mode").on(t.tenantId, t.mode),
    // Partial index matching alembic 0009: speeds up "list all
    // running bots" queries without indexing the (typically larger)
    // pile of stopped bots.
    runningIdx: index("idx_tenant_bots_running")
      .on(t.isRunning)
      .where(sql`is_running = true`),
  }),
);

export const tenantSecrets = pgTable(
  "tenant_secrets",
  {
    tenantId: uuid("tenant_id")
      .notNull()
      .references(() => tenants.id, { onDelete: "cascade" }),
    key: varchar("key", { length: 64 }).notNull(),
    // AES-256-GCM ciphertext WITH the auth tag appended (final 16
    // bytes of `ciphertext` per crypto/secrets.ts:encryptSecret).
    ciphertext: bytea("ciphertext").notNull(),
    // 12-byte GCM nonce (random per encryption).
    nonce: bytea("nonce").notNull(),
    updatedAt: timestamp("updated_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
  },
  (t) => ({
    // Composite PK = (tenant_id, key); same key for two tenants is fine,
    // same key twice for one tenant is not.
    pk: uniqueIndex("tenant_secrets_pkey").on(t.tenantId, t.key),
  }),
);

export const tenantAuditLog = pgTable(
  "tenant_audit_log",
  {
    id: serial("id").primaryKey(),
    tenantId: uuid("tenant_id")
      .notNull()
      .references(() => tenants.id, { onDelete: "cascade" }),
    actor: varchar("actor", { length: 16 }).notNull(),     // 'tenant' | 'operator'
    action: varchar("action", { length: 64 }).notNull(),   // e.g. 'secret.set', 'bot.start'
    contextJson: text("context_json").default("{}"),
    ts: timestamp("ts", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => ({
    tenantTsIdx: index("idx_tenant_audit_log_tenant_ts").on(t.tenantId, t.ts),
  }),
);

// PR 3a — maps a tenant to their linked Telegram chat for unlock
// notifications. Created via the /link <6-digit-code> command in
// the Telegram bot (proves chat ownership) after the dashboard
// minted the code (proves Authentik ownership). 1:1 mapping.
//
// `telegram_chat_id` is bigint because Telegram chat IDs are
// negative for groups (-100xxxxx for supergroups) and exceed INT32
// for some users post-2024.
//
// `telegram_username` is display-only — users can change theirs,
// so don't use this as an identifier.
export const tenantTelegramLinks = pgTable(
  "tenant_telegram_links",
  {
    tenantId: uuid("tenant_id")
      .primaryKey()
      .references(() => tenants.id, { onDelete: "cascade" }),
    // `mode: "bigint"` keeps full precision: Telegram supergroup
    // IDs (-100xxxxxxxxx) are within Number.MAX_SAFE_INTEGER today
    // but the type promise that BIGINT round-trips losslessly is
    // worth keeping. API responses serialize to string (JSON has
    // no native bigint, and a JSON number could silently round-trip
    // through a client's `parseInt`).
    telegramChatId: bigint("telegram_chat_id", { mode: "bigint" }).notNull(),
    telegramUsername: varchar("telegram_username", { length: 64 }),
    linkedAt: timestamp("linked_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
    lastUnlockAt: timestamp("last_unlock_at", { withTimezone: true }),
  },
  (t) => ({
    chatIdx: index("idx_tenant_telegram_links_chat").on(t.telegramChatId),
  }),
);
