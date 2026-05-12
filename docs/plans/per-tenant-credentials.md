# Per-tenant credentials — implementation plan

## Background

Today, `HYPERLIQUID_PRIVATE_KEY`, `HYPERLIQUID_ACCOUNT_ADDRESS`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (etc.) are injected into the
operator's compose-defined bots via Phase → x-bot-env. All bots use the
same operator credentials. To support multi-tenant SaaS use, every
tenant must supply their own HL keys + Telegram bot token.

The substrate already exists (Phase 2c/2d): `tenant_secrets` table,
Argon2id-derived KDF, AES-256-GCM, Redis K-cache, /unlock + secret-CRUD
endpoints. What's missing is the UI, the bot-spawn wire-up, and a
fallback unlock path for unattended bot restarts.

This plan retires env-injection entirely (Path A from chat) — operator
becomes a normal tenant. Big-bang for cleanliness; the unlock-via-
Telegram-link flow (PR 3) means host-reboots don't strand bots.

## Existing infrastructure (do not duplicate)

| What | Where |
|---|---|
| `tenant_secrets` table | `bot/alembic/versions/0009_multi_tenancy_schema.py:125` + `dashboard/src/lib/db.ts:212` |
| Passphrase verifier on `tenants` | `dashboard/src/lib/db.ts:162` (`passphrase_salt`, `passphrase_verifier`) |
| KDF (Argon2id) | `dashboard/src/lib/crypto/passphrase.ts` |
| AES-256-GCM helpers | `dashboard/src/lib/crypto/secrets.ts` |
| K cache (Redis, 7d TTL, session-scoped) | `dashboard/src/lib/crypto/k-cache.ts` |
| `requireUnlockedKey(req, tenant)` guard | `dashboard/src/lib/tenant.ts:127` |
| Set-passphrase endpoint | `POST /api/tenant/me/passphrase` |
| Unlock endpoint | `POST /api/tenant/me/unlock` (verify + cache K), `DELETE` (clear) |
| Secret CRUD | `PUT/DELETE /api/tenant/me/secrets/[KEY]` |
| List secret keys | `GET /api/tenant/me/secrets` |
| Bot orchestrator accepting `decryptedSecrets` | `dashboard/src/lib/bot-orchestrator.ts` |

Secret-key regex: `/^[A-Z0-9_]{1,64}$/`. We'll use these slot names:
`HYPERLIQUID_PRIVATE_KEY`, `HYPERLIQUID_ACCOUNT_ADDRESS`,
`HYPERLIQUID_MAINNET_PRIVATE_KEY`, `HYPERLIQUID_MAINNET_ACCOUNT_ADDRESS`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

## Decisions made (chat 2026-05-12)

1. **Path A** (operator = normal tenant). No env-injection survives
   in the final state; every credential goes through `tenant_secrets`.
2. **Unlock UX**: web modal at session expiry; Telegram
   notification + signed deeplink to `$DEPLOY_HOST/unlock?token=...` for headless
   bot restarts. Passphrase never travels through Telegram.
3. **Onboarding**: walkthrough wizard on first visit (set passphrase →
   paste creds → optional Telegram link → start bots).
4. **Telegram lives where it lives today** — testnet bot is the
   command/notification bot per tenant.

## PR sequence

### PR 1 — Wizard + creds form (this PR)

Scope:
- `GET /api/tenant/me` extended with `passphraseSet: bool` and
  `unlocked: bool` — frontend uses these to pick between wizard,
  unlock-modal, and normal UI.
- `/settings/credentials` page:
  - First visit (no passphrase): wizard step 1 → set passphrase form.
  - Passphrase set, locked: prompt for passphrase → POST /unlock.
  - Unlocked: list of credential slots with "set" / "replace" /
    "delete" buttons and last-updated timestamp.
- Unlock modal: a client component that any page can mount to gate
  passphrase-required actions. Used directly on the credentials page;
  PR 2 will wire it into bot-start flow.
- User-menu gets a "Settings" link (already exists pointing to
  /options — repurpose to /settings/credentials, or add a new entry).

**Out of scope for PR 1:**
- Bot-spawn flow (PR 2)
- Telegram linking (PR 3)
- Removing env-injection (PR 4)

Operator can use the wizard during PR 1 deploy without affecting bot
operation — bots still read env-injected creds. Tenants who fill in
the wizard during PR 1 won't see effects until PR 2 ships.

### PR 2 — Per-tenant bot spawning

The orchestration substrate already exists (Phase 3a):
- `POST /api/tenant/me/bots` already creates a row + decrypts +
  starts a container. Used by tests; no UI yet.
- `DELETE /api/tenant/me/bots/[id]` stops the container and removes
  the row.
- `bot-orchestrator.ts` exposes `startBot`, `stopBot`, `statusBot`
  with full env-precedence rules, RLS-role provisioning, password
  rotation, and compensation logic.

PR 2 just adds:
- `POST /api/tenant/me/bots/[id]/stop` — stop the container but
  keep the DB row (`isRunning=false`, clear `containerId`, set
  `lastStoppedAt`). For temporary pause without losing the slot.
- `POST /api/tenant/me/bots/[id]/start` — for a stopped row,
  re-decrypt secrets and start a new container; update row with
  fresh `containerId`. Required for "stop overnight, restart in
  morning" UX.
- `/settings/bots` page with one card per mode showing:
  - Mode badge (paper/testnet/mainnet)
  - Live status (running / stopped / not-created)
  - Start / Stop / Delete buttons
  - Container ID + last-started/stopped timestamps
- Mounts `<UnlockModal>` when a start action returns 401.

Operator's compose-bots run in parallel during this phase. Tenants
get their own.

### PR 3 — Telegram unlock-link flow

- New `tenant_telegram_links` table: `(tenant_id, chat_id, linked_at)`.
- `/api/tenant/me/telegram/link` → returns 6-digit code valid 10 min.
- Bot's `/link 123456` command — verifies code, persists link.
- Bot startup: if `tenant_secrets` rows exist but no K available,
  enter locked state, DM tenant: "🔒 Bot offline. Unlock:
  https://$DEPLOY_HOST/unlock?token=<HMAC-signed>".
- `/unlock?token=...` page accepts the signed token, validates,
  shows passphrase form, calls existing /unlock endpoint, then signals
  the locked bot to retry decrypt.
- Bot ↔ dashboard internal endpoint for "K is now available, here's
  the decryptedSecrets dict" (signed with API_KEY).
- Rate limit: 5 unlock attempts / 15 min / tenant; bot DM rate
  limited so we never spam.

### PR 4 — Retire env-injection

- Once all real tenants (incl. operator) have moved to tenant_secrets:
- Remove `HYPERLIQUID_*` and `TELEGRAM_*` from x-bot-env in compose.
- Remove env-fallback from `bot/hypertrade/config.py`.
- Remove operator's compose-bots (`bot-paper`, `bot-testnet`,
  `bot-mainnet` services) — orchestrator-spawned bots cover all.
- Cleanup Phase secrets that no longer have a reader.

## PR 1 implementation notes

### `/api/tenant/me` extension

Add fields to the response:
```ts
{
  id, email, displayName, isOperator,           // existing
  passphraseSet: boolean,                        // tenant.passphraseVerifier !== null
  unlocked: boolean,                             // loadKey(tenant.id, sessionId) !== null
}
```

### Onboarding wizard state machine

```
not_authenticated → /login (handled by proxy.ts)
authenticated, !passphraseSet → wizard step 1 (set passphrase)
authenticated, passphraseSet, !unlocked → unlock prompt
authenticated, passphraseSet, unlocked → credentials list
```

Each step is a render branch on `/settings/credentials`. No multi-page
wizard — single page with conditional rendering keeps state simple
and avoids back-button hazards.

### Unlock modal as reusable component

`<UnlockModal>` — props: `onUnlocked: () => void`, `onCancel?`. Posts
to /unlock, on success calls onUnlocked. PR 1 only mounts it on the
credentials page; PR 2 will mount it on bot-start, etc.

### Operator visibility

Operator sees the same UI as any tenant. There's no "you're using
env-injected operator credentials" banner — that note belongs in
PR 4's release notes when env-injection actually goes away. During
PR 1 (and 2 and 3), operator can fill in the wizard but it has no
runtime effect on the compose-bots. That's fine — operator is just
pre-populating before PR 4.

### Validation at save time

PR 1 does no live validation of HL keys / TG tokens at save (that
needs network calls and the decrypt path). It saves the value and
trusts the user. PR 2 will validate at bot-start time (refuse to start
if HL key + address don't match, etc.). PR 3 will optionally validate
on save by decrypting and probing.

## Test plan for PR 1

- [ ] `/api/tenant/me` returns `passphraseSet: false, unlocked: false`
      for fresh tenant
- [ ] Set passphrase → `passphraseSet: true, unlocked: false`
- [ ] Unlock → `unlocked: true`
- [ ] PUT secret while locked → 401
- [ ] PUT secret while unlocked → 200, value retrievable as ciphertext
- [ ] Wizard renders correct branch for each state
- [ ] Existing pages (Overview, Trades, etc.) unaffected
