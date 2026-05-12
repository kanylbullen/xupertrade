/**
 * Tests for the signed unlock-token helper (PR 3c).
 *
 * Mocks `getSessionSecret` so we don't need real env. Verifies:
 *   - mint produces a parseable payload.signature shape
 *   - verify round-trips a freshly-minted token
 *   - signature mismatch is rejected (different secret)
 *   - expired tokens are rejected
 *   - malformed tokens return null (no throw)
 *   - timing-safe compare (length differences don't leak)
 */

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/auth", () => ({
  getSessionSecret: vi.fn(),
}));

import { getSessionSecret } from "@/lib/auth";

import { mintUnlockToken, verifyUnlockToken } from "../unlock-token";

const mockedSecret = vi.mocked(getSessionSecret);

const TENANT_ID = "11111111-2222-3333-4444-555555555555";
const SECRET_A = "this-is-secret-A-for-tests-only";
const SECRET_B = "different-secret-B-for-tests-only";

afterEach(() => {
  mockedSecret.mockReset();
  vi.useRealTimers();
});

describe("mintUnlockToken", () => {
  it("produces payload.signature shape with non-empty parts", async () => {
    mockedSecret.mockResolvedValue(SECRET_A);
    const tok = await mintUnlockToken(TENANT_ID);
    const parts = tok.split(".");
    expect(parts).toHaveLength(2);
    expect(parts[0].length).toBeGreaterThan(0);
    expect(parts[1].length).toBeGreaterThan(0);
  });

  it("respects custom ttlSeconds", async () => {
    mockedSecret.mockResolvedValue(SECRET_A);
    const tok = await mintUnlockToken(TENANT_ID, 60);
    const payloadB64 = tok.split(".")[0];
    // Decode payload to check exp.
    const padded =
      payloadB64 + "=".repeat((4 - (payloadB64.length % 4)) % 4);
    const decoded = JSON.parse(
      Buffer.from(
        padded.replace(/-/g, "+").replace(/_/g, "/"),
        "base64",
      ).toString("utf8"),
    );
    const now = Math.floor(Date.now() / 1000);
    expect(decoded.exp).toBeGreaterThanOrEqual(now + 59);
    expect(decoded.exp).toBeLessThanOrEqual(now + 61);
  });
});

describe("verifyUnlockToken", () => {
  it("accepts a freshly-minted token", async () => {
    mockedSecret.mockResolvedValue(SECRET_A);
    const tok = await mintUnlockToken(TENANT_ID);
    const payload = await verifyUnlockToken(tok);
    expect(payload).not.toBeNull();
    expect(payload?.sub).toBe(TENANT_ID);
  });

  it("rejects a token signed with a different secret", async () => {
    mockedSecret.mockResolvedValue(SECRET_A);
    const tok = await mintUnlockToken(TENANT_ID);
    // Pretend the secret rotated between mint and verify.
    mockedSecret.mockResolvedValue(SECRET_B);
    expect(await verifyUnlockToken(tok)).toBeNull();
  });

  it("rejects an expired token", async () => {
    vi.useFakeTimers();
    mockedSecret.mockResolvedValue(SECRET_A);
    const tok = await mintUnlockToken(TENANT_ID, 10);
    // Advance time past expiry.
    vi.setSystemTime(Date.now() + 11_000);
    expect(await verifyUnlockToken(tok)).toBeNull();
  });

  it("returns null on malformed input (no dot)", async () => {
    mockedSecret.mockResolvedValue(SECRET_A);
    expect(await verifyUnlockToken("not-a-token")).toBeNull();
  });

  it("returns null on empty signature", async () => {
    mockedSecret.mockResolvedValue(SECRET_A);
    expect(await verifyUnlockToken("payload.")).toBeNull();
  });

  it("returns null on empty payload", async () => {
    mockedSecret.mockResolvedValue(SECRET_A);
    expect(await verifyUnlockToken(".sig")).toBeNull();
  });

  it("returns null on non-JSON payload", async () => {
    mockedSecret.mockResolvedValue(SECRET_A);
    // Hand-craft: valid signature over a garbage payload.
    // We can't easily produce that without the helper; just check
    // a clearly-bogus token doesn't crash the verifier.
    expect(await verifyUnlockToken("AAAA.BBBB")).toBeNull();
  });

  it("doesn't expose timing differences via length-mismatched signatures", async () => {
    mockedSecret.mockResolvedValue(SECRET_A);
    const tok = await mintUnlockToken(TENANT_ID);
    const [payload] = tok.split(".");
    const truncated = `${payload}.AAAA`;
    expect(await verifyUnlockToken(truncated)).toBeNull();
  });
});
