import { describe, expect, it } from "vitest";
import { randomBytes } from "node:crypto";
import {
  AUTH_TAG_BYTES,
  KEY_BYTES,
  NONCE_BYTES,
  decryptSecret,
  encryptSecret,
} from "../secrets";

const TEST_KEY = () => randomBytes(KEY_BYTES);

describe("AES-GCM secret roundtrip", () => {
  it("encrypts to a non-empty ciphertext + 12-byte nonce", () => {
    const { ciphertext, nonce } = encryptSecret(
      TEST_KEY(),
      "0xdeadbeef-private-key",
    );
    expect(nonce.length).toBe(NONCE_BYTES);
    // ciphertext = encrypted bytes (>=1) + auth tag (16)
    expect(ciphertext.length).toBeGreaterThanOrEqual(AUTH_TAG_BYTES + 1);
  });

  it("decrypt reproduces the original plaintext", () => {
    const key = TEST_KEY();
    const plaintext = "TELEGRAM_BOT_TOKEN=12345:abc";
    const blob = encryptSecret(key, plaintext);
    expect(decryptSecret(key, blob.ciphertext, blob.nonce)).toBe(plaintext);
  });

  it("decrypt with the WRONG key throws (auth tag mismatch)", () => {
    const blob = encryptSecret(TEST_KEY(), "secret");
    const wrong = TEST_KEY();
    expect(() =>
      decryptSecret(wrong, blob.ciphertext, blob.nonce),
    ).toThrow();
  });

  it("decrypt with the WRONG nonce throws", () => {
    const key = TEST_KEY();
    const blob = encryptSecret(key, "secret");
    const wrongNonce = randomBytes(NONCE_BYTES);
    expect(() =>
      decryptSecret(key, blob.ciphertext, wrongNonce),
    ).toThrow();
  });

  it("decrypt with TAMPERED ciphertext throws (auth tag detects)", () => {
    const key = TEST_KEY();
    const blob = encryptSecret(key, "secret");
    const tampered = Buffer.from(blob.ciphertext);
    tampered[0] ^= 0xff;
    expect(() =>
      decryptSecret(key, tampered, blob.nonce),
    ).toThrow();
  });

  it("encrypting the SAME plaintext twice yields different ciphertexts", () => {
    const key = TEST_KEY();
    const a = encryptSecret(key, "the same secret");
    const b = encryptSecret(key, "the same secret");
    expect(a.nonce.equals(b.nonce)).toBe(false);
    expect(a.ciphertext.equals(b.ciphertext)).toBe(false);
  });

  it("handles the empty string", () => {
    const key = TEST_KEY();
    const blob = encryptSecret(key, "");
    expect(decryptSecret(key, blob.ciphertext, blob.nonce)).toBe("");
  });

  it("handles unicode plaintext (utf-8 round-trip)", () => {
    const key = TEST_KEY();
    const plaintext = "passphrase med åäö 🦄 中文";
    const blob = encryptSecret(key, plaintext);
    expect(decryptSecret(key, blob.ciphertext, blob.nonce)).toBe(plaintext);
  });

  it("rejects wrong-length key on encrypt", () => {
    expect(() =>
      encryptSecret(Buffer.alloc(31), "x"),
    ).toThrow(/key must be 32 bytes/);
  });

  it("rejects wrong-length key on decrypt", () => {
    const blob = encryptSecret(TEST_KEY(), "x");
    expect(() =>
      decryptSecret(Buffer.alloc(31), blob.ciphertext, blob.nonce),
    ).toThrow(/key must be 32 bytes/);
  });

  it("rejects wrong-length nonce on decrypt", () => {
    const key = TEST_KEY();
    const blob = encryptSecret(key, "x");
    expect(() =>
      decryptSecret(key, blob.ciphertext, Buffer.alloc(11)),
    ).toThrow(/nonce must be 12 bytes/);
  });

  it("rejects ciphertext shorter than the auth tag", () => {
    const key = TEST_KEY();
    expect(() =>
      decryptSecret(key, Buffer.alloc(8), Buffer.alloc(NONCE_BYTES)),
    ).toThrow(/ciphertext shorter than/);
  });
});
