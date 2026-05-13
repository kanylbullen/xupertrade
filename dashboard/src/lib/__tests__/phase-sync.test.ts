/**
 * Tests for lib/phase-sync.ts (PR feat/phase-auth-autosync).
 *
 * Mocks the Redis client so we don't need real infra. Covers:
 *   - all 5 env vars set → pipeline writes all 5 keys
 *   - some env vars set, others empty/whitespace → only set ones written
 *   - no env vars set → no pipeline opened, written=0
 *   - Redis pipeline.exec rejects → logged WARN, redisError=true, doesn't throw
 *   - isPhaseManagingAuth env-presence detection (drives the UI banner)
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { isPhaseManagingAuth, syncPhaseAuthConfig } from "../phase-sync";

const ORIG_ENV = { ...process.env };

const SYNC_ENV_KEYS = [
  "OIDC_ISSUER",
  "OIDC_CLIENT_ID",
  "OIDC_CLIENT_SECRET",
  "OIDC_SCOPES",
  "AUTH_MODE",
] as const;

function clearSyncEnv() {
  for (const k of SYNC_ENV_KEYS) delete process.env[k];
}

function makeRedisStub(execImpl?: () => Promise<unknown>) {
  const pipe = {
    set: vi.fn().mockReturnThis(),
    exec: vi.fn(execImpl ?? (async () => [])),
  };
  const pipeline = vi.fn(() => pipe);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return { client: { pipeline } as any, pipe };
}

beforeEach(() => {
  clearSyncEnv();
  // Silence the INFO/WARN logs the function emits so test output stays
  // clean — we still assert on the values via the return value.
  vi.spyOn(console, "log").mockImplementation(() => {});
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  process.env = { ...ORIG_ENV };
  vi.restoreAllMocks();
});

describe("syncPhaseAuthConfig", () => {
  it("writes all 5 keys when every env var is set", async () => {
    process.env.OIDC_ISSUER = "https://auth.example.com/realms/x";
    process.env.OIDC_CLIENT_ID = "xupertrade";
    process.env.OIDC_CLIENT_SECRET = "shh-secret";
    process.env.OIDC_SCOPES = "openid profile email";
    process.env.AUTH_MODE = "oidc";

    const { client, pipe } = makeRedisStub();
    const result = await syncPhaseAuthConfig(client);

    expect(result).toEqual({ written: 5, total: 5, redisError: false });
    expect(pipe.set).toHaveBeenCalledTimes(5);
    expect(pipe.set).toHaveBeenCalledWith(
      "dashboard:auth:oidc:issuer",
      "https://auth.example.com/realms/x",
    );
    expect(pipe.set).toHaveBeenCalledWith(
      "dashboard:auth:oidc:client_id",
      "xupertrade",
    );
    expect(pipe.set).toHaveBeenCalledWith(
      "dashboard:auth:oidc:client_secret",
      "shh-secret",
    );
    expect(pipe.set).toHaveBeenCalledWith(
      "dashboard:auth:oidc:scopes",
      "openid profile email",
    );
    expect(pipe.set).toHaveBeenCalledWith("dashboard:auth:mode", "oidc");
    expect(pipe.exec).toHaveBeenCalledOnce();
  });

  it("writes only the env vars that are non-empty (after trim)", async () => {
    process.env.OIDC_ISSUER = "  https://issuer  ";
    process.env.OIDC_CLIENT_ID = "client-1";
    // OIDC_CLIENT_SECRET intentionally unset
    process.env.OIDC_SCOPES = "   "; // whitespace-only → treated as empty
    process.env.AUTH_MODE = "oidc";

    const { client, pipe } = makeRedisStub();
    const result = await syncPhaseAuthConfig(client);

    expect(result).toEqual({ written: 3, total: 5, redisError: false });
    expect(pipe.set).toHaveBeenCalledTimes(3);
    // Trimmed value, not the raw env value
    expect(pipe.set).toHaveBeenCalledWith(
      "dashboard:auth:oidc:issuer",
      "https://issuer",
    );
    expect(pipe.set).toHaveBeenCalledWith(
      "dashboard:auth:oidc:client_id",
      "client-1",
    );
    expect(pipe.set).toHaveBeenCalledWith("dashboard:auth:mode", "oidc");
  });

  it("does nothing when no env vars are set", async () => {
    const { client, pipe } = makeRedisStub();
    const result = await syncPhaseAuthConfig(client);

    expect(result).toEqual({ written: 0, total: 5, redisError: false });
    // No pipeline opened at all — short-circuit before touching Redis.
    expect(client.pipeline).not.toHaveBeenCalled();
    expect(pipe.set).not.toHaveBeenCalled();
    expect(pipe.exec).not.toHaveBeenCalled();
  });

  it("logs WARN and returns redisError=true when Redis rejects, doesn't throw", async () => {
    process.env.AUTH_MODE = "oidc";

    const warn = vi.spyOn(console, "warn");
    const { client } = makeRedisStub(async () => {
      throw new Error("ECONNREFUSED 127.0.0.1:6379");
    });

    // Must NOT throw — instrumentation hook can't crash the process.
    const result = await syncPhaseAuthConfig(client);

    expect(result).toEqual({ written: 0, total: 5, redisError: true });
    expect(warn).toHaveBeenCalled();
    const msg = warn.mock.calls[0][0] as string;
    expect(msg).toContain("[phase-sync]");
    expect(msg).toContain("Redis unreachable");
  });
});

describe("isPhaseManagingAuth (drives the UI banner)", () => {
  it("returns false when no env vars are set (banner hidden)", () => {
    expect(isPhaseManagingAuth()).toBe(false);
  });

  it("returns true when any single env var is non-empty (banner shown)", () => {
    process.env.OIDC_ISSUER = "https://issuer";
    expect(isPhaseManagingAuth()).toBe(true);
  });

  it("returns true with AUTH_MODE alone", () => {
    process.env.AUTH_MODE = "disabled";
    expect(isPhaseManagingAuth()).toBe(true);
  });

  it("treats whitespace-only as empty (banner hidden)", () => {
    process.env.OIDC_CLIENT_ID = "   ";
    process.env.OIDC_SCOPES = "\t\n";
    expect(isPhaseManagingAuth()).toBe(false);
  });
});
