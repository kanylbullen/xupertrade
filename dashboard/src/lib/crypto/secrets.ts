/**
 * Secret encryption helpers — multi-tenancy Phase 2a.
 *
 * AES-256-GCM with random per-message nonce. K is the 32-byte key
 * derived from the user's passphrase via Argon2id (see passphrase.ts).
 *
 * `tenant_secrets` (DB) stores ciphertext + nonce; this module is the
 * only place that touches plaintext secrets in the dashboard process.
 *
 * GCM authenticates the ciphertext: tampering with either ciphertext
 * or nonce makes `decryptSecret` throw — no garbled-but-accepted
 * plaintext. Decryption with the wrong key (e.g. user typed wrong
 * passphrase) throws as well, so the API layer can return
 * "passphrase is wrong" instead of letting bot startup fail with
 * mysterious garbage.
 */

import { createCipheriv, createDecipheriv, randomBytes } from "node:crypto";

export const NONCE_BYTES = 12;            // GCM standard
export const AUTH_TAG_BYTES = 16;
export const KEY_BYTES = 32;              // AES-256 (re-exported so callers/tests don't duplicate the magic number)

export type SecretBlob = {
  /** ciphertext WITH the auth tag appended (GCM standard layout) */
  ciphertext: Buffer;
  nonce: Buffer;
};

/**
 * Encrypt a UTF-8 string under K. Returns ciphertext (which includes
 * the GCM auth tag as the final 16 bytes) plus the random nonce that
 * must be stored alongside.
 *
 * Each call produces a fresh nonce, so encrypting the same plaintext
 * twice yields different ciphertexts — desirable for stored secrets
 * (an attacker with DB read can't tell which secrets share a value).
 */
export function encryptSecret(key: Buffer, plaintext: string): SecretBlob {
  if (key.length !== KEY_BYTES) {
    throw new RangeError(
      `key must be ${KEY_BYTES} bytes, got ${key.length}`,
    );
  }
  const nonce = randomBytes(NONCE_BYTES);
  const cipher = createCipheriv("aes-256-gcm", key, nonce);
  const enc = Buffer.concat([
    cipher.update(plaintext, "utf8"),
    cipher.final(),
  ]);
  const tag = cipher.getAuthTag();
  return { ciphertext: Buffer.concat([enc, tag]), nonce };
}

/**
 * Decrypt under K. Throws if the auth tag fails (tampered ciphertext,
 * wrong nonce, or wrong key). Callers should distinguish "wrong key"
 * (user error → 401) from "corrupted data" (server error → 500), but
 * the GCM check returns the same error class for both — that's
 * acceptable since both mean "we can't read this secret."
 */
export function decryptSecret(
  key: Buffer,
  ciphertext: Buffer,
  nonce: Buffer,
): string {
  if (key.length !== KEY_BYTES) {
    throw new RangeError(
      `key must be ${KEY_BYTES} bytes, got ${key.length}`,
    );
  }
  if (nonce.length !== NONCE_BYTES) {
    throw new RangeError(
      `nonce must be ${NONCE_BYTES} bytes, got ${nonce.length}`,
    );
  }
  if (ciphertext.length < AUTH_TAG_BYTES) {
    throw new RangeError(
      `ciphertext shorter than ${AUTH_TAG_BYTES}-byte auth tag`,
    );
  }
  const enc = ciphertext.subarray(0, ciphertext.length - AUTH_TAG_BYTES);
  const tag = ciphertext.subarray(ciphertext.length - AUTH_TAG_BYTES);
  const decipher = createDecipheriv("aes-256-gcm", key, nonce);
  decipher.setAuthTag(tag);
  return Buffer.concat([decipher.update(enc), decipher.final()]).toString(
    "utf8",
  );
}
