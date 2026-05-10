/**
 * Schema sanity for the multi-tenancy Drizzle additions (Phase 2b).
 *
 * No live DB is touched — these tests inspect the table objects via
 * Drizzle's `getTableConfig` to confirm column names, types, and FK
 * relationships match what alembic 0009 emits in Python. If the
 * Python schema and the Drizzle mirror drift, these tests fail loudly.
 */

import { describe, expect, it } from "vitest";
import { getTableConfig } from "drizzle-orm/pg-core";

import {
  tenantAuditLog,
  tenantBots,
  tenantSecrets,
  tenants,
} from "../db";


function columnMap(table: ReturnType<typeof getTableConfig>) {
  return Object.fromEntries(
    table.columns.map((c) => [c.name, c.getSQLType()]),
  );
}

describe("tenants table", () => {
  it("matches alembic 0009 column shape", () => {
    const cfg = getTableConfig(tenants);
    expect(cfg.name).toBe("tenants");
    const cols = columnMap(cfg);
    expect(cols.id).toBe("uuid");
    expect(cols.authentik_sub).toBe("varchar(128)");
    expect(cols.email).toBe("varchar(255)");
    expect(cols.passphrase_salt).toBe("bytea");
    expect(cols.passphrase_verifier).toBe("bytea");
    expect(cols.is_active).toBe("boolean");
    expect(cols.is_operator).toBe("boolean");
    expect(cols.multi_bot_enabled).toBe("boolean");
  });

  it("declares the unique index on authentik_sub", () => {
    const cfg = getTableConfig(tenants);
    const idx = cfg.indexes.find((i) => i.config.name === "idx_tenants_authentik_sub");
    expect(idx).toBeDefined();
    expect(idx?.config.unique).toBe(true);
  });
});

describe("tenant_bots table", () => {
  it("has the expected columns and FK to tenants", () => {
    const cfg = getTableConfig(tenantBots);
    expect(cfg.name).toBe("tenant_bots");
    const cols = columnMap(cfg);
    expect(cols.id).toBe("uuid");
    expect(cols.tenant_id).toBe("uuid");
    expect(cols.mode).toBe("varchar(16)");
    expect(cols.container_id).toBe("varchar(64)");
    expect(cols.container_name).toBe("varchar(128)");
    expect(cols.is_running).toBe("boolean");
    expect(cols.telegram_webhook_secret).toBe("varchar(64)");

    // FK on tenant_id with cascade delete
    const fk = cfg.foreignKeys[0];
    expect(fk).toBeDefined();
    expect(fk.onDelete).toBe("cascade");
  });

  it("has the unique (tenant_id, mode) index", () => {
    const cfg = getTableConfig(tenantBots);
    const uq = cfg.indexes.find((i) => i.config.name === "uq_tenant_bots_mode");
    expect(uq).toBeDefined();
    expect(uq?.config.unique).toBe(true);
  });

  it("declares the partial index on running bots", () => {
    const cfg = getTableConfig(tenantBots);
    const idx = cfg.indexes.find((i) => i.config.name === "idx_tenant_bots_running");
    expect(idx).toBeDefined();
    // The WHERE predicate is stored on the index config; we just
    // assert it's set (Drizzle stores SQL fragments opaquely).
    expect(idx?.config.where).toBeDefined();
  });
});

describe("tenant_secrets table", () => {
  it("matches alembic 0009 shape", () => {
    const cfg = getTableConfig(tenantSecrets);
    expect(cfg.name).toBe("tenant_secrets");
    const cols = columnMap(cfg);
    expect(cols.tenant_id).toBe("uuid");
    expect(cols.key).toBe("varchar(64)");
    expect(cols.ciphertext).toBe("bytea");
    expect(cols.nonce).toBe("bytea");
  });

  it("FK to tenants cascades on delete", () => {
    const cfg = getTableConfig(tenantSecrets);
    expect(cfg.foreignKeys[0].onDelete).toBe("cascade");
  });
});

describe("tenant_audit_log table", () => {
  it("matches alembic 0009 shape", () => {
    const cfg = getTableConfig(tenantAuditLog);
    expect(cfg.name).toBe("tenant_audit_log");
    const cols = columnMap(cfg);
    expect(cols.id).toBe("serial");
    expect(cols.tenant_id).toBe("uuid");
    expect(cols.actor).toBe("varchar(16)");
    expect(cols.action).toBe("varchar(64)");
    expect(cols.context_json).toBe("text");
  });

  it("indexes (tenant_id, ts) for the recent-activity query", () => {
    const cfg = getTableConfig(tenantAuditLog);
    const idx = cfg.indexes.find(
      (i) => i.config.name === "idx_tenant_audit_log_tenant_ts",
    );
    expect(idx).toBeDefined();
  });
});
