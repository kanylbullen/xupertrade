/**
 * Tests for lib/tls-config.ts (PR 4a).
 *
 * Mocks Redis to verify env-first override semantics + the
 * SET/DEL pipeline behavior of setTlsConfig.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { getTlsConfig, setTlsConfig } from "../tls-config";

const ORIG_ENV = { ...process.env };

function makeRedisStub(values: (string | null)[]) {
  const mget = vi.fn().mockResolvedValue(values);
  const pipe = {
    set: vi.fn().mockReturnThis(),
    del: vi.fn().mockReturnThis(),
    exec: vi.fn().mockResolvedValue([]),
  };
  const pipeline = vi.fn(() => pipe);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return { client: { mget, pipeline } as any, pipe };
}

afterEach(() => {
  process.env = { ...ORIG_ENV };
  vi.clearAllMocks();
});

describe("getTlsConfig", () => {
  it("returns disabled defaults when Redis empty + no env", async () => {
    delete process.env.TLS_ENABLED_ENV;
    delete process.env.TLS_DOMAIN;
    delete process.env.TLS_EMAIL;
    delete process.env.TLS_CF_API_TOKEN;

    const { client } = makeRedisStub([null, null, null, null]);
    const cfg = await getTlsConfig(client);
    expect(cfg).toEqual({
      enabled: false,
      domain: "",
      email: "",
      cf_token: "",
    });
  });

  it("env wins over Redis", async () => {
    process.env.TLS_ENABLED_ENV = "1";
    process.env.TLS_DOMAIN = "envdomain.example.com";

    const { client } = makeRedisStub([
      "0",
      "redisdomain.example.com",
      "ops@example.com",
      "redis-token",
    ]);
    const cfg = await getTlsConfig(client);
    expect(cfg.enabled).toBe(true); // env wins
    expect(cfg.domain).toBe("envdomain.example.com");
    // Redis still supplies fields not env-overridden
    expect(cfg.email).toBe("ops@example.com");
    expect(cfg.cf_token).toBe("redis-token");
  });

  it("falls back to Redis when env is empty", async () => {
    process.env.TLS_ENABLED_ENV = "";
    process.env.TLS_DOMAIN = "";

    const { client } = makeRedisStub([
      "1",
      "redis.example.com",
      null,
      "tok",
    ]);
    const cfg = await getTlsConfig(client);
    expect(cfg.enabled).toBe(true);
    expect(cfg.domain).toBe("redis.example.com");
  });
});

describe("setTlsConfig", () => {
  it("writes enabled as '1' or '0'", async () => {
    const { client, pipe } = makeRedisStub([]);
    await setTlsConfig({ enabled: true }, client);
    expect(pipe.set).toHaveBeenCalledWith("dashboard:tls:enabled", "1");

    pipe.set.mockClear();
    await setTlsConfig({ enabled: false }, client);
    expect(pipe.set).toHaveBeenCalledWith("dashboard:tls:enabled", "0");
  });

  it("DELs string fields when set to empty string", async () => {
    const { client, pipe } = makeRedisStub([]);
    await setTlsConfig({ domain: "", cf_token: "" }, client);
    expect(pipe.del).toHaveBeenCalledWith("dashboard:tls:domain");
    expect(pipe.del).toHaveBeenCalledWith("dashboard:tls:cf_token");
  });

  it("only touches keys explicitly present", async () => {
    const { client, pipe } = makeRedisStub([]);
    await setTlsConfig({ domain: "x.example.com" }, client);
    expect(pipe.set).toHaveBeenCalledTimes(1);
    expect(pipe.del).not.toHaveBeenCalled();
  });
});
