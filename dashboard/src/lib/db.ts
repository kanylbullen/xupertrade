import { pgTable, serial, varchar, doublePrecision, boolean, text, timestamp } from "drizzle-orm/pg-core";
import { drizzle } from "drizzle-orm/postgres-js";
import postgres from "postgres";

const connectionString = process.env.DATABASE_URL || "postgresql://postgres:postgres@localhost:5432/hypertrade";
const client = postgres(connectionString);
export const db = drizzle(client);

// Mirror the Python SQLAlchemy models

export const trades = pgTable("trades", {
  id: serial("id").primaryKey(),
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
  totalEquity: doublePrecision("total_equity").notNull(),
  availableBalance: doublePrecision("available_balance").notNull(),
  unrealizedPnl: doublePrecision("unrealized_pnl").default(0),
  isPaper: boolean("is_paper").default(true),
  mode: varchar("mode", { length: 16 }).default("paper"),
  timestamp: timestamp("timestamp", { withTimezone: true }).defaultNow(),
});

export const fundingPayments = pgTable("funding_payments", {
  id: serial("id").primaryKey(),
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
  name: varchar("name", { length: 64 }).notNull().unique(),
  symbol: varchar("symbol", { length: 16 }).notNull(),
  timeframe: varchar("timeframe", { length: 8 }).notNull(),
  enabled: boolean("enabled").default(true),
  paramsJson: text("params_json").default("{}"),
  createdAt: timestamp("created_at", { withTimezone: true }).defaultNow(),
  updatedAt: timestamp("updated_at", { withTimezone: true }).defaultNow(),
});
