/**
 * Tests for lib/oidc-group-gate.ts (roadmap NU-8 point 2).
 *
 * The gate itself is exercised end to end — callback cookie through
 * both tenant resolvers — in
 * app/api/auth/oidc/callback/__tests__/group-gate.test.ts. Here: the
 * match rules, and the boot warning that makes an empty
 * `OIDC_REQUIRED_GROUP` visible instead of silently onboarding every
 * IdP user.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AuthConfig } from "../auth";
import {
  getRequiredOidcGroup,
  hasRequiredGroup,
  oidcGroupGateWarning,
  warnIfOidcGroupGateOff,
} from "../oidc-group-gate";

function cfg(
  mode: AuthConfig["mode"],
  oidc: { issuer?: string; clientId?: string } = {},
): Pick<AuthConfig, "mode" | "oidc_issuer" | "oidc_client_id"> {
  return {
    mode,
    oidc_issuer: oidc.issuer ?? "",
    oidc_client_id: oidc.clientId ?? "",
  };
}

const OIDC_SET = { issuer: "https://idp.example.test", clientId: "client-id" };

let warn: ReturnType<typeof vi.spyOn>;
let log: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  warn = vi.spyOn(console, "warn").mockImplementation(() => {});
  log = vi.spyOn(console, "log").mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe("getRequiredOidcGroup", () => {
  it("is empty when unset", () => {
    vi.stubEnv("OIDC_REQUIRED_GROUP", undefined);
    expect(getRequiredOidcGroup()).toBe("");
  });

  it("trims surrounding whitespace, so whitespace-only means unset", () => {
    vi.stubEnv("OIDC_REQUIRED_GROUP", "  hypertrade-users \n");
    expect(getRequiredOidcGroup()).toBe("hypertrade-users");
    vi.stubEnv("OIDC_REQUIRED_GROUP", "   ");
    expect(getRequiredOidcGroup()).toBe("");
  });
});

describe("hasRequiredGroup", () => {
  it("matches an exact member of the array", () => {
    expect(hasRequiredGroup(["a", "hypertrade-users"], "hypertrade-users")).toBe(true);
  });

  it("is case-sensitive", () => {
    expect(hasRequiredGroup(["Hypertrade-Users"], "hypertrade-users")).toBe(false);
  });

  it("does not trim claim values", () => {
    expect(hasRequiredGroup([" hypertrade-users"], "hypertrade-users")).toBe(false);
  });

  it.each<[string, unknown]>([
    ["undefined", undefined],
    ["null", null],
    ["a string containing the group", "not-hypertrade-users"],
    ["an object", { "hypertrade-users": true }],
  ])("treats %s as no groups", (_label, groups) => {
    expect(hasRequiredGroup(groups, "hypertrade-users")).toBe(false);
  });
});

describe("oidcGroupGateWarning", () => {
  it("is null whenever a group is set", () => {
    expect(oidcGroupGateWarning(cfg("oidc", OIDC_SET), "hypertrade-users")).toBeNull();
    expect(oidcGroupGateWarning(null, "hypertrade-users")).toBeNull();
  });

  it("warns in oidc mode with no group", () => {
    const w = oidcGroupGateWarning(cfg("oidc", OIDC_SET), "");
    expect(w).toMatch(/WARNING/);
    expect(w).toMatch(/OIDC_REQUIRED_GROUP is empty/);
    expect(w).toMatch(/every user the IdP authenticates gets a tenant/);
  });

  it("warns in oidc mode even before the OIDC fields are filled in", () => {
    expect(oidcGroupGateWarning(cfg("oidc"), "")).not.toBeNull();
  });

  it("warns in basic mode when OIDC is configured — the callback serves any mode but locked", () => {
    expect(oidcGroupGateWarning(cfg("basic", OIDC_SET), "")).not.toBeNull();
  });

  it("warns when the config could not be read", () => {
    expect(oidcGroupGateWarning(null, "")).toMatch(/could not be read/);
  });

  it("stays quiet in basic mode with no OIDC configured", () => {
    expect(oidcGroupGateWarning(cfg("basic"), "")).toBeNull();
    expect(oidcGroupGateWarning(cfg("basic", { issuer: OIDC_SET.issuer }), "")).toBeNull();
  });
});

describe("warnIfOidcGroupGateOff", () => {
  it("logs a WARNING at boot in oidc mode with an empty group", async () => {
    vi.stubEnv("OIDC_REQUIRED_GROUP", "");
    const warned = await warnIfOidcGroupGateOff(async () => cfg("oidc", OIDC_SET));
    expect(warned).toBe(true);
    expect(warn).toHaveBeenCalledOnce();
    expect(String(warn.mock.calls[0][0])).toMatch(/OIDC_REQUIRED_GROUP is empty/);
  });

  it("confirms the group instead, without reading the auth config", async () => {
    vi.stubEnv("OIDC_REQUIRED_GROUP", "hypertrade-users");
    const read = vi.fn();
    const warned = await warnIfOidcGroupGateOff(read);
    expect(warned).toBe(false);
    expect(read).not.toHaveBeenCalled();
    expect(warn).not.toHaveBeenCalled();
    expect(String(log.mock.calls[0][0])).toContain('"hypertrade-users"');
  });

  it("stays quiet when OIDC sign-in is not configured", async () => {
    vi.stubEnv("OIDC_REQUIRED_GROUP", "");
    expect(await warnIfOidcGroupGateOff(async () => cfg("basic"))).toBe(false);
    expect(warn).not.toHaveBeenCalled();
  });

  it("never throws: a failing config read still warns", async () => {
    vi.stubEnv("OIDC_REQUIRED_GROUP", "");
    const warned = await warnIfOidcGroupGateOff(async () => {
      throw new Error("redis down");
    });
    expect(warned).toBe(true);
    expect(String(warn.mock.calls[0][0])).toMatch(/could not be read/);
  });
});
