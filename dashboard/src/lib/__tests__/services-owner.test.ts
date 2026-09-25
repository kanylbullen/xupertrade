/**
 * Tests for lib/services-owner.ts (roadmap NU-7): which bot owns the side
 * services, and the operator's vault-tracking address.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  _resetServicesOwnerWarningsForTests,
  containerIsServicesOwner,
  isServicesOwner,
  operatorVaultTrackingAddress,
  SERVICES_OWNER_LABEL,
  servicesOwnerMode,
  servicesOwnerModeFor,
} from "../services-owner";

const ORIG_ENV = { ...process.env };

beforeEach(() => {
  delete process.env.HYPERTRADE_SERVICES_OWNER_MODE;
  delete process.env.VAULT_TRACKING_ADDRESS;
  _resetServicesOwnerWarningsForTests();
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  process.env = { ...ORIG_ENV };
  vi.mocked(console.warn).mockRestore();
});

describe("servicesOwnerMode", () => {
  it("defaults to paper when unset or blank (decision 5.12)", () => {
    expect(servicesOwnerMode()).toBe("paper");
    process.env.HYPERTRADE_SERVICES_OWNER_MODE = "  ";
    expect(servicesOwnerMode()).toBe("paper");
    expect(console.warn).not.toHaveBeenCalled();
  });

  it.each(["paper", "testnet", "mainnet", " Mainnet "])(
    "accepts %j",
    (raw) => {
      process.env.HYPERTRADE_SERVICES_OWNER_MODE = raw;
      expect(servicesOwnerMode()).toBe(raw.trim().toLowerCase());
    },
  );

  it("falls back to paper on an unknown value and warns once per value", () => {
    process.env.HYPERTRADE_SERVICES_OWNER_MODE = "mainet";
    expect(servicesOwnerMode()).toBe("paper");
    expect(servicesOwnerMode()).toBe("paper");
    expect(console.warn).toHaveBeenCalledOnce();
  });

  it("isServicesOwner is true for exactly one mode", () => {
    process.env.HYPERTRADE_SERVICES_OWNER_MODE = "testnet";
    const owners = (["paper", "testnet", "mainnet"] as const).filter((m) =>
      isServicesOwner(m, { isOperator: true }),
    );
    expect(owners).toEqual(["testnet"]);
  });
});

describe("servicesOwnerModeFor", () => {
  it("applies the configured mode to the operator only", () => {
    expect(servicesOwnerModeFor({ isOperator: true })).toBe("paper");
    process.env.HYPERTRADE_SERVICES_OWNER_MODE = "testnet";
    expect(servicesOwnerModeFor({ isOperator: true })).toBe("testnet");
  });

  it.each([{ isOperator: false }, { isOperator: null }, {}])(
    "keeps mainnet for any other tenant (%j): a paper bot never owns their Telegram",
    (tenant) => {
      process.env.HYPERTRADE_SERVICES_OWNER_MODE = "paper";
      expect(servicesOwnerModeFor(tenant)).toBe("mainnet");
      expect(isServicesOwner("paper", tenant)).toBe(false);
    },
  );
});

describe("containerIsServicesOwner", () => {
  it("reads the label buildSpec sets", () => {
    const labels = { "hypertrade.mode": "mainnet" };
    expect(containerIsServicesOwner({ ...labels, [SERVICES_OWNER_LABEL]: "true" })).toBe(true);
    expect(containerIsServicesOwner({ ...labels, [SERVICES_OWNER_LABEL]: "false" })).toBe(false);
  });

  it("treats a pre-NU-7 container (no label) as the owner only on mainnet", () => {
    expect(containerIsServicesOwner({ "hypertrade.mode": "mainnet" })).toBe(true);
    expect(containerIsServicesOwner({ "hypertrade.mode": "paper" })).toBe(false);
  });
});

describe("operatorVaultTrackingAddress", () => {
  // Placeholder, never a real wallet (CLAUDE.md § 0).
  const ADDR = "0xAbCd000000000000000000000000000000000001";

  it("is null when unset", () => {
    expect(operatorVaultTrackingAddress()).toBeNull();
  });

  it("returns a well-formed address, trimmed", () => {
    process.env.VAULT_TRACKING_ADDRESS = `  ${ADDR} `;
    expect(operatorVaultTrackingAddress()).toBe(ADDR);
  });

  it.each(["0x1234", "not-an-address", `${ADDR}00`])(
    "ignores malformed %j without logging the value",
    (bad) => {
      process.env.VAULT_TRACKING_ADDRESS = bad;
      expect(operatorVaultTrackingAddress()).toBeNull();
      const logged = vi.mocked(console.warn).mock.calls.flat().join(" ");
      expect(logged).toContain("VAULT_TRACKING_ADDRESS");
      expect(logged).not.toContain(bad);
    },
  );
});
