/**
 * Passphrase-derived key (KDF) helpers — multi-tenancy Phase 2a.
 *
 * Trust model B (per docs/plans/multi-tenancy.md): the user's secrets
 * are encrypted at rest with a key K derived from their passphrase
 * via Argon2id. K is never persisted; it lives only in process memory
 * (or a session-scoped Redis cache, in Phase 2c).
 *
 * Argon2id parameters chosen for "cheap on a server, expensive on an
 * attacker's GPU": memory=64MB, iterations=3, parallelism=4. Same
 * tuning the Argon2 RFC suggests for interactive logins on modern
 * hardware (2024+). Tweak via `KDF_PARAMS` if hardware grows much
 * faster.
 */

import { hashRaw } from "@node-rs/argon2";
import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";

export const SALT_BYTES = 16;
export const KEY_BYTES = 32;            // 256-bit key for AES-256-GCM
export const VERIFIER_BYTES = 32;       // HMAC-SHA-256 output

/**
 * KDF parameters for Phase 2 v1.
 *
 * **These values are IMMUTABLE once any tenant has set their passphrase.**
 * The salt + verifier we persist do not encode the params; if we ever
 * change `memoryCost`/`timeCost`/`parallelism` here, every existing
 * tenant's verifier will fail to validate (and their secrets become
 * undecryptable) without a migration path.
 *
 * Future hardening (deferred): persist `kdf_version` on each tenant
 * row, route `deriveKey` through a version table, and allow background
 * "re-encrypt with new params" jobs after a successful unlock. Until
 * we need it, treat these constants as a forever-decision.
 *
 * `hashRaw` defaults to algorithm=Argon2id; we don't pass it explicitly
 * because @node-rs/argon2 ships `Algorithm` as a `const enum`, which
 * we can't dereference under Next's `isolatedModules: true`.
 */
const KDF_PARAMS = {
  memoryCost: 64 * 1024,                // 64 MiB
  timeCost: 3,                          // 3 iterations
  parallelism: 4,
  outputLen: KEY_BYTES,
} as const;

const VERIFIER_DOMAIN = Buffer.from("hypertrade-passphrase-verifier-v1");

/** Cryptographically random 16-byte salt. Store alongside the verifier. */
export function generateSalt(): Buffer {
  return randomBytes(SALT_BYTES);
}

/**
 * Derive K from a passphrase + per-tenant salt. Returns 32 bytes
 * suitable for AES-256-GCM. Always async because Argon2 is CPU-heavy
 * and the binding offloads to a worker thread.
 */
export async function deriveKey(
  passphrase: string,
  salt: Buffer,
): Promise<Buffer> {
  if (salt.length !== SALT_BYTES) {
    throw new RangeError(
      `salt must be ${SALT_BYTES} bytes, got ${salt.length}`,
    );
  }
  // hashRaw returns the raw KDF output bytes directly (vs `hash()`
  // which returns the encoded `$argon2id$...` string). We want bytes
  // for the AES key — no encoding round-trip.
  return Buffer.from(
    await hashRaw(passphrase, { salt, ...KDF_PARAMS }),
  );
}

/**
 * Build a verifier blob from K. Stored in `tenants.passphrase_verifier`.
 * On subsequent login attempts, we re-derive K from the entered
 * passphrase and check `verify(K, stored_verifier)` to decide whether
 * the user typed the right passphrase — without ever decrypting any
 * actual secret (the decrypt would fail too, but with much worse UX).
 */
export function makeVerifier(key: Buffer): Buffer {
  if (key.length !== KEY_BYTES) {
    throw new RangeError(
      `key must be ${KEY_BYTES} bytes, got ${key.length}`,
    );
  }
  return createHmac("sha256", key).update(VERIFIER_DOMAIN).digest();
}

/**
 * Constant-time compare of `makeVerifier(key)` against a stored verifier.
 * Returns `true` only when the buffers match in both length and bytes.
 */
export function verify(key: Buffer, storedVerifier: Buffer): boolean {
  if (key.length !== KEY_BYTES) return false;
  if (storedVerifier.length !== VERIFIER_BYTES) return false;
  const computed = makeVerifier(key);
  return timingSafeEqual(computed, storedVerifier);
}
