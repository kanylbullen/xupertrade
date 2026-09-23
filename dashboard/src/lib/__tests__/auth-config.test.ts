/**
 * Tests for lib/auth-config.ts (PR 4a).
 *
 * Mocks the Redis client so we don't need real infra. Verifies:
 *   - getAuthConfig env-first override semantics
 *   - getAuthConfig falls back to Redis values, then defaults
 *   - setAuthConfig pipelines correct DEL/SET ops
 *   - ensureSessionSecret SET NX + re-read pattern
 *   - mode validation rejects garbage Redis values
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ensureSessionSecret,
  getAuthConfig,
  resolveMode,
  setAuthConfig,
} from "../auth-config";

/** Stand-in for the Postgres tenant probe. `getAuthConfig` only
 *  reaches it when no mode is stored anywhere. */
const hasTenants = () => Promise.resolve(true);
const noTenants = () => Promise.resolve(false);

const ORIG_ENV = { ...process.env };

function makeRedisStub(values: (string | null)[]) {
  const mget = vi.fn().mockResolvedValue(values);
  const get = vi.fn();
  const set = vi.fn().mockResolvedValue("OK");
  const pipe = {
    set: vi.fn().mockReturnThis(),
    del: vi.fn().mockReturnThis(),
    exec: vi.fn().mockResolvedValue([]),
  };
  const pipeline = vi.fn(() => pipe);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return { client: { mget, get, set, pipeline } as any, pipe };
}

afterEach(() => {
  process.env = { ...ORIG_ENV };
  vi.clearAllMocks();
});

function clearAuthEnv() {
  delete process.env.AUTH_MODE;
  delete process.env.OIDC_ISSUER;
  delete process.env.OIDC_CLIENT_ID;
  delete process.env.OIDC_CLIENT_SECRET;
  delete process.env.OIDC_SCOPES;
}

describe("getAuthConfig", () => {
  it("locks (not disables) when Redis is empty on a provisioned install", async () => {
    // SECURITY regression guard. This case used to resolve to
    // "disabled", which let proxy.ts wave every request through and
    // tenant-server.ts hand out the operator tenant's data. An empty
    // Redis on an install that has tenants means the config was LOST,
    // not that auth was switched off.
    clearAuthEnv();

    const { client } = makeRedisStub([
      null,
      null,
      null,
      null,
      null,
      null,
      null,
      null,
    ]);
    const cfg = await getAuthConfig(client, hasTenants);
    expect(cfg.mode).toBe("locked");
    expect(cfg.basic_user).toBe("");
    expect(cfg.oidc_scopes).toBe("openid profile email");
  });

  it("allows disabled on a fresh install (no tenants yet)", async () => {
    // A first boot has nothing to leak and must be able to bootstrap.
    clearAuthEnv();

    const { client } = makeRedisStub([
      null,
      null,
      null,
      null,
      null,
      null,
      null,
      null,
    ]);
    const cfg = await getAuthConfig(client, noTenants);
    expect(cfg.mode).toBe("disabled");
  });

  it("falls back to basic when the mode key is gone but a basic user survives", async () => {
    clearAuthEnv();
    const { client } = makeRedisStub([
      null, // mode key gone
      "alice",
      "$2b$12$hash",
      "session-secret",
      null,
      null,
      null,
      null,
    ]);
    const cfg = await getAuthConfig(client, hasTenants);
    expect(cfg.mode).toBe("basic");
  });

  it("falls back to oidc when the mode key is gone but Phase re-seeded OIDC", async () => {
    clearAuthEnv();
    // `idp` rather than `auth` as the subdomain: it says the same
    // thing without needing the gitleaks `operator-service-subdomain`
    // carve-out at all. (That carve-out was dead until the companion
    // commit on this branch fixed it — see `.gitleaks.toml`.)
    process.env.OIDC_ISSUER = "https://idp.example.com/";
    process.env.OIDC_CLIENT_ID = "client-id";
    const { client } = makeRedisStub([
      null, // mode key gone
      null,
      null,
      null,
      null,
      null,
      null,
      null,
    ]);
    const cfg = await getAuthConfig(client, hasTenants);
    expect(cfg.mode).toBe("oidc");
  });

  it("honours an explicitly stored disabled mode", async () => {
    // A value that is PRESENT cannot be the flush we're defending
    // against — the operator stored "disabled" on purpose.
    clearAuthEnv();
    const { client } = makeRedisStub([
      "disabled",
      null,
      null,
      null,
      null,
      null,
      null,
      null,
    ]);
    const cfg = await getAuthConfig(client, hasTenants);
    expect(cfg.mode).toBe("disabled");
  });

  it("honours AUTH_MODE=disabled from env even with no stored config", async () => {
    clearAuthEnv();
    process.env.AUTH_MODE = "disabled";
    const { client } = makeRedisStub([
      null,
      null,
      null,
      null,
      null,
      null,
      null,
      null,
    ]);
    const cfg = await getAuthConfig(client, hasTenants);
    expect(cfg.mode).toBe("disabled");
  });

  it("env wins over Redis (Phase 6c env-first override)", async () => {
    process.env.AUTH_MODE = "oidc";
    process.env.OIDC_ISSUER = "https://auth.example.com/";

    const { client } = makeRedisStub([
      "basic", // Redis says basic
      "alice",
      "$2b$12$dummy",
      "secret",
      "https://old.example.com/", // Redis says old issuer
      "client-id",
      "client-secret",
      null,
    ]);
    const cfg = await getAuthConfig(client);
    expect(cfg.mode).toBe("oidc"); // env wins
    expect(cfg.oidc_issuer).toBe("https://auth.example.com/");
    // Redis still supplies the fields not overridden
    expect(cfg.basic_user).toBe("alice");
    expect(cfg.oidc_client_id).toBe("client-id");
  });

  it("falls back to Redis when env is empty string", async () => {
    process.env.AUTH_MODE = "";
    const { client } = makeRedisStub([
      "basic",
      "alice",
      "$2b$12$h",
      "s",
      null,
      null,
      null,
      null,
    ]);
    const cfg = await getAuthConfig(client);
    expect(cfg.mode).toBe("basic");
  });

  it("clamps a garbage stored mode to 'locked', not 'disabled'", async () => {
    clearAuthEnv();
    const { client } = makeRedisStub([
      "garbage", // invalid mode
      null,
      null,
      null,
      null,
      null,
      null,
      null,
    ]);
    const cfg = await getAuthConfig(client, hasTenants);
    expect(cfg.mode).toBe("locked");
  });

  it("clamps a garbage AUTH_MODE to 'locked'", async () => {
    clearAuthEnv();
    process.env.AUTH_MODE = "disabeld"; // typo
    const { client } = makeRedisStub([
      "oidc",
      null,
      null,
      null,
      null,
      null,
      null,
      null,
    ]);
    const cfg = await getAuthConfig(client, hasTenants);
    expect(cfg.mode).toBe("locked");
  });
});

describe("resolveMode", () => {
  const base = {
    envMode: "",
    storedMode: null as string | null,
    basicConfigured: false,
    oidcConfigured: false,
    hasAnyTenant: hasTenants,
  };

  it("env wins over a stored mode", async () => {
    await expect(
      resolveMode({ ...base, envMode: "basic", storedMode: "oidc" }),
    ).resolves.toBe("basic");
  });

  it("prefers basic over oidc when both survive a lost mode key", async () => {
    await expect(
      resolveMode({ ...base, basicConfigured: true, oidcConfigured: true }),
    ).resolves.toBe("basic");
  });

  it("locks when nothing survives and the DB probe says tenants exist", async () => {
    await expect(resolveMode(base)).resolves.toBe("locked");
  });

  it("locks when the DB probe itself fails (probe answers 'provisioned')", async () => {
    // defaultHasAnyTenant swallows DB errors and returns true; a
    // caller-supplied probe modelling that must still lock.
    await expect(
      resolveMode({ ...base, hasAnyTenant: () => Promise.resolve(true) }),
    ).resolves.toBe("locked");
  });
});

describe("setAuthConfig", () => {
  it("SET for non-empty values, DEL for empty values", async () => {
    const { client, pipe } = makeRedisStub([]);
    await setAuthConfig(
      {
        mode: "oidc",
        basic_user: "",
        oidc_issuer: "https://new.example.com/",
      },
      client,
    );
    expect(pipe.set).toHaveBeenCalledWith(
      "dashboard:auth:mode",
      "oidc",
    );
    expect(pipe.del).toHaveBeenCalledWith("dashboard:auth:basic:user");
    expect(pipe.set).toHaveBeenCalledWith(
      "dashboard:auth:oidc:issuer",
      "https://new.example.com/",
    );
    expect(pipe.exec).toHaveBeenCalledOnce();
  });

  it("only touches keys explicitly present in updates", async () => {
    const { client, pipe } = makeRedisStub([]);
    await setAuthConfig({ basic_user: "alice" }, client);
    // exactly one set, no del
    expect(pipe.set).toHaveBeenCalledTimes(1);
    expect(pipe.del).not.toHaveBeenCalled();
  });
});

describe("ensureSessionSecret", () => {
  it("returns existing value when already set", async () => {
    const { client } = makeRedisStub([]);
    client.get.mockResolvedValue("existing-secret");
    const v = await ensureSessionSecret(client);
    expect(v).toBe("existing-secret");
    expect(client.set).not.toHaveBeenCalled();
  });

  it("generates + writes with NX when missing", async () => {
    const { client } = makeRedisStub([]);
    // First get → null (not set). Set NX. Re-get → returns what
    // the winning caller wrote.
    let stored: string | null = null;
    client.get.mockImplementation(async () => stored);
    client.set.mockImplementation(async (
      _key: string,
      val: string,
      mode: string,
    ) => {
      if (mode === "NX" && stored === null) {
        stored = val;
        return "OK";
      }
      return null;
    });

    const v = await ensureSessionSecret(client);
    expect(v.length).toBeGreaterThan(40); // base64url of 48 bytes
    expect(client.set).toHaveBeenCalledWith(
      "dashboard:auth:session_secret",
      expect.any(String),
      "NX",
    );
  });

  it("loser-of-NX-race re-reads the winner's value", async () => {
    const { client } = makeRedisStub([]);
    // Initial get → null (we think we need to write). NX-set
    // returns null because another caller beat us. Final
    // re-read returns the winner's value.
    let firstGet = true;
    client.get.mockImplementation(async () => {
      if (firstGet) {
        firstGet = false;
        return null;
      }
      return "winner-secret";
    });
    client.set.mockResolvedValue(null); // NX lost the race

    const v = await ensureSessionSecret(client);
    expect(v).toBe("winner-secret");
  });
});
