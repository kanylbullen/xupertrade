import { describe, expect, it } from "vitest";
import {
  KEY_BYTES,
  SALT_BYTES,
  VERIFIER_BYTES,
  deriveKey,
  generateSalt,
  makeVerifier,
  verify,
} from "../passphrase";

describe("passphrase KDF", () => {
  it("generates a 16-byte salt", () => {
    const salt = generateSalt();
    expect(salt).toBeInstanceOf(Buffer);
    expect(salt.length).toBe(SALT_BYTES);
  });

  it("two calls produce different salts (cryptographic randomness)", () => {
    const a = generateSalt();
    const b = generateSalt();
    expect(a.equals(b)).toBe(false);
  });

  it("derives a 32-byte key", async () => {
    const salt = generateSalt();
    const key = await deriveKey("hunter2", salt);
    expect(key).toBeInstanceOf(Buffer);
    expect(key.length).toBe(KEY_BYTES);
  });

  it("derivation is deterministic for the same passphrase + salt", async () => {
    const salt = generateSalt();
    const k1 = await deriveKey("correct horse battery staple", salt);
    const k2 = await deriveKey("correct horse battery staple", salt);
    expect(k1.equals(k2)).toBe(true);
  });

  it("different passphrase yields different key (same salt)", async () => {
    const salt = generateSalt();
    const a = await deriveKey("hunter2", salt);
    const b = await deriveKey("hunter3", salt);
    expect(a.equals(b)).toBe(false);
  });

  it("different salt yields different key (same passphrase)", async () => {
    const a = await deriveKey("hunter2", generateSalt());
    const b = await deriveKey("hunter2", generateSalt());
    expect(a.equals(b)).toBe(false);
  });

  it("rejects wrong-length salt", async () => {
    await expect(
      deriveKey("hunter2", Buffer.alloc(15)),
    ).rejects.toThrow(/salt must be 16 bytes/);
  });
});

describe("passphrase verifier", () => {
  it("makes a 32-byte verifier", async () => {
    const key = await deriveKey("hunter2", generateSalt());
    const v = makeVerifier(key);
    expect(v.length).toBe(VERIFIER_BYTES);
  });

  it("verify returns true for the correct key", async () => {
    const salt = generateSalt();
    const key = await deriveKey("hunter2", salt);
    const stored = makeVerifier(key);

    const replayKey = await deriveKey("hunter2", salt);
    expect(verify(replayKey, stored)).toBe(true);
  });

  it("verify returns false for a wrong key", async () => {
    const salt = generateSalt();
    const stored = makeVerifier(await deriveKey("hunter2", salt));
    const wrong = await deriveKey("WRONG", salt);
    expect(verify(wrong, stored)).toBe(false);
  });

  it("verify returns false on length mismatch (no exception)", async () => {
    const key = await deriveKey("hunter2", generateSalt());
    expect(verify(key, Buffer.alloc(31))).toBe(false);
    expect(verify(Buffer.alloc(31), makeVerifier(key))).toBe(false);
  });

  it("makeVerifier rejects wrong-length key", () => {
    expect(() => makeVerifier(Buffer.alloc(31))).toThrow(/key must be 32 bytes/);
  });
});
