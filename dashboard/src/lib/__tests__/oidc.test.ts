/**
 * Tests for `getOidcConfig` in `lib/oidc.ts`.
 *
 * Both OIDC routes build on it, so this is where "locked means no OIDC"
 * is decided: when `resolveMode` answers "locked" it must refuse with
 * `auth-locked` before discovery (a locked dashboard never contacts the
 * IdP) and before the client-secret read, whatever OIDC fields happen
 * to survive. The other refusals keep their `oidc-misconfigured` code.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("openid-client", () => ({
  discovery: vi.fn(),
}));

vi.mock("@/lib/auth", () => ({
  fetchAuthConfig: vi.fn(),
}));

vi.mock("@/lib/auth-config", () => ({
  getAuthConfig: vi.fn(),
}));

import * as client from "openid-client";

import { fetchAuthConfig, type AuthConfig } from "@/lib/auth";
import { getAuthConfig } from "@/lib/auth-config";

import { getOidcConfig } from "../oidc";

const mockedDiscovery = vi.mocked(client.discovery);
const mockedFetchCfg = vi.mocked(fetchAuthConfig);
const mockedGetAuthConfig = vi.mocked(getAuthConfig);

const DISCOVERED = { discovered: true } as unknown as client.Configuration;

function cfg(overrides: Partial<AuthConfig> = {}): AuthConfig {
  return {
    mode: "oidc",
    basic_user_set: false,
    oidc_issuer: "https://idp.example.test",
    oidc_client_id: "client-id",
    oidc_scopes: "openid profile email",
    ...overrides,
  };
}

beforeEach(() => {
  mockedFetchCfg.mockResolvedValue(cfg());
  mockedGetAuthConfig.mockResolvedValue({
    oidc_client_secret: "client-secret",
  } as Awaited<ReturnType<typeof getAuthConfig>>);
  mockedDiscovery.mockResolvedValue(DISCOVERED);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("getOidcConfig — locked", () => {
  it("refuses with auth-locked and never runs discovery, with OIDC fully configured", async () => {
    mockedFetchCfg.mockResolvedValueOnce(cfg({ mode: "locked" }));

    await expect(getOidcConfig()).resolves.toEqual({
      ok: false,
      error: "auth-locked",
    });
    expect(mockedDiscovery).not.toHaveBeenCalled();
    // Nor does it go on to read the client secret.
    expect(mockedGetAuthConfig).not.toHaveBeenCalled();
  });

  it("answers auth-locked, not oidc-misconfigured, when no OIDC fields survive", async () => {
    mockedFetchCfg.mockResolvedValueOnce(
      cfg({ mode: "locked", oidc_issuer: "", oidc_client_id: "" }),
    );

    await expect(getOidcConfig()).resolves.toEqual({
      ok: false,
      error: "auth-locked",
    });
    expect(mockedDiscovery).not.toHaveBeenCalled();
  });

  it("decides on the forced (uncached) auth-config read", async () => {
    mockedFetchCfg.mockResolvedValueOnce(cfg({ mode: "locked" }));

    await getOidcConfig();

    expect(mockedFetchCfg).toHaveBeenCalledTimes(1);
    expect(mockedFetchCfg).toHaveBeenCalledWith(true);
  });
});

describe("getOidcConfig — not locked", () => {
  it("runs discovery with the stored issuer, client id and secret", async () => {
    const res = await getOidcConfig();

    expect(res).toEqual({ ok: true, config: DISCOVERED, cfg: cfg() });
    expect(mockedDiscovery).toHaveBeenCalledTimes(1);
    const [issuer, clientId, secret] = mockedDiscovery.mock.calls[0];
    expect(String(issuer)).toBe("https://idp.example.test/");
    expect(clientId).toBe("client-id");
    expect(secret).toBe("client-secret");
  });

  it.each<[string, () => void]>([
    ["auth config unreadable", () => mockedFetchCfg.mockResolvedValueOnce(null)],
    ["issuer missing", () => mockedFetchCfg.mockResolvedValueOnce(cfg({ oidc_issuer: "" }))],
    ["client id missing", () => mockedFetchCfg.mockResolvedValueOnce(cfg({ oidc_client_id: "" }))],
    [
      "issuer not a URL",
      () => mockedFetchCfg.mockResolvedValueOnce(cfg({ oidc_issuer: "not a url" })),
    ],
    [
      "client secret missing",
      () =>
        mockedGetAuthConfig.mockResolvedValueOnce({
          oidc_client_secret: "",
        } as Awaited<ReturnType<typeof getAuthConfig>>),
    ],
  ])("%s → oidc-misconfigured, no discovery", async (_label, arrange) => {
    arrange();

    await expect(getOidcConfig()).resolves.toEqual({
      ok: false,
      error: "oidc-misconfigured",
    });
    expect(mockedDiscovery).not.toHaveBeenCalled();
  });
});
