# Invite-only onboarding

`hypertrade` multi-tenant deployments use **Authentik group
membership** as the registration gate. There is no self-service
sign-up form, no email-verification flow, no public registration
endpoint. The operator decides who gets in by adding their
Authentik account to the `hypertrade-users` group.

This document covers the operator-side workflow (adding a new user)
and the user-side workflow (first sign-in through bot-running).

---

## Why invite-only

The decision was made during multi-tenancy planning
([`docs/plans/multi-tenancy.md`](plans/multi-tenancy.md) §1
decision 5). Reasoning:

- **Resource cap** (`MAX_TENANTS=10` default) — per-user containers
  add up; operator wants to know who they're sharing the host with
- **No abuse / spam infrastructure needed** — invite-only sidesteps
  email-verification, captcha, brute-force protection, content
  moderation, etc.
- **Identity already federated** — Authentik (which the operator
  is already running for the dashboard's OIDC) is the natural
  trust anchor; no separate user database

Anything that wants to look like "open registration" should be
designed around how Authentik exposes a self-service signup flow
with operator approval, NOT around adding email/password to the
dashboard.

---

## Operator workflow: adding a new tenant

### One-time setup (per Authentik instance)

1. In Authentik admin → **Directory → Groups** → create a group
   named exactly `hypertrade-users`. Anyone in this group can
   sign in to the dashboard.
2. In **Applications → Providers** → ensure the OIDC provider
   that backs the dashboard restricts access to that group via a
   policy:
   - Policy type: **Group Membership**
   - Group: `hypertrade-users`
   - Bind to the application's **Outpost** or use a group-based
     access policy

   Without this policy, *any* Authentik user can sign in. With
   it, only group members can.

### Inviting a new user

1. Verify the user already has an Authentik account on your
   instance. If not, use Authentik's normal user-creation flow
   (admin-created or via Authentik's enrollment flow with
   operator approval).
2. Add their account to the `hypertrade-users` group:
   - Authentik admin → **Directory → Users → [their account]
     → Groups → Add → hypertrade-users**
3. Send them the dashboard URL + a copy of [`USER_GUIDE.md`](#)
   (TODO — to be created during Phase 8 closed-beta).

That's it on the operator side. The user can now sign in and
the dashboard will auto-create their `tenants` row on first sign-
in.

### Removing a tenant

1. **Stop their bot first** to avoid orphan containers:
   - Dashboard → admin view → Tenants → Stop bot
   - Or via API: `POST /api/admin/tenants/<id>/stop-all-bots`
2. Optional: remove from the `hypertrade-users` group (revokes
   future sign-ins; keeps DB rows)
3. To actually delete their data:
   - Dashboard → admin view → Tenants → Delete (cascades to
     their bots, secrets, audit log, and all per-tenant data
     rows via the FK CASCADE chains established in alembic 0009)
   - This is irreversible. Confirm carefully.

### Operator visibility

The operator role can:

- List all tenants + their bot status
- Stop any tenant's bot (e.g. spam-trading mitigation)
- Disable a tenant (sets `tenants.is_active = false`, blocks
  future sign-in even if still in the Authentik group)
- See aggregate metrics (total tenants, total bots, total trades)

The operator role **CANNOT**:

- Read any tenant's secrets at rest (encrypted with the tenant's
  passphrase-derived key K, which is never persisted)
- See into a tenant's secret values via the dashboard UI
- Impersonate a tenant for /api/tenant/me/* calls

(See the v1 caveat in
[`docs/plans/multi-tenancy.md`](plans/multi-tenancy.md) §4 about
what "cannot read" actually protects against — operator with host
root + `docker inspect` can still see env vars of running
containers.)

---

## User workflow: first sign-in

What a freshly-invited user experiences:

### 1. Sign in

- Browse to the dashboard URL (operator provides this)
- Get redirected to Authentik for OIDC sign-in
- Authentik authenticates them; checks `hypertrade-users`
  membership; redirects back to dashboard with a session cookie
- Dashboard auto-creates a `tenants` row keyed on their
  Authentik `sub` claim. They are now tenant N.

### 2. Set passphrase

The first thing the dashboard prompts for is a **passphrase**
(separate from their Authentik password). This passphrase is
never sent to the operator and is the encryption key for all
their stored secrets.

- Minimum 12 characters
- **No recovery flow** — write it down. If you lose it, your
  stored secrets become unreadable forever and you'll have to
  re-enter every secret from scratch.

The dashboard derives a key K from the passphrase via Argon2id,
stores only a salt and an HMAC verifier, and never persists K
itself.

### 3. Add HyperLiquid + Telegram secrets

The dashboard's settings page has a per-secret form for each
key the bot might need:

- `HYPERLIQUID_PRIVATE_KEY` — your HL trading wallet's private
  key (or API-wallet private key in the recommended pattern)
- `HYPERLIQUID_ACCOUNT_ADDRESS` — your HL main wallet address
  (only required for API-wallet pattern; leave empty if signer
  IS the trading wallet)
- `TELEGRAM_BOT_TOKEN` — your dedicated Telegram bot's token
  (create one via [@BotFather](https://t.me/BotFather))
- `TELEGRAM_CHAT_ID` — the Telegram chat ID where notifications
  go (your own user ID via [@userinfobot](https://t.me/userinfobot))

Each value is encrypted client-side under K and stored as
`tenant_secrets`. The operator with raw DB access sees only
ciphertext + nonce.

### 4. Unlock + create bot

To create a bot, the dashboard needs your passphrase ONCE
per session to decrypt the secrets and inject them into the
bot container.

- Click **Create bot** on the dashboard
- Enter passphrase → unlock
- Pick mode: **paper** / **testnet** / **mainnet**
  - For non-operator tenants: only one bot at a time. To switch
    mode, stop the existing bot first (deletes the `tenant_bots`
    row + container).
- Click **Start**

Behind the scenes:

- Dashboard derives K from passphrase, validates against verifier
- Caches K in Redis with TTL = session lifetime (default 7d)
- Provisions a per-tenant Postgres role (`tenant_<32hex>`) with
  RLS-enforced access to your tenant_id rows only
- Builds a `DATABASE_URL` with that role's credentials
- Creates + starts a Docker container with your decrypted
  secrets in env

### 5. Operate

Once the bot is running, the dashboard shows:

- Live container status
- Open positions
- Recent trades
- Equity curve
- Per-strategy enable/disable toggles
- Pause / flat-all / kill-switch controls (mode-scoped to your
  bot only — you cannot affect other tenants' bots)

You can also operate via Telegram if you set those secrets:
your bot replies only to your configured `TELEGRAM_CHAT_ID`.

### 6. Lock / sign out

- **Lock**: explicitly clears the cached K from Redis. Bot keeps
  running with its in-process env vars; only new bot-create or
  passphrase-protected actions need re-unlock.
- **Sign out**: ends the dashboard session entirely. Same effect
  on K-cache.
- **Bot survives** both — it has its decrypted secrets in env
  and continues trading. To stop the bot, explicitly delete it.

---

## Common operator questions

**Q: A tenant is spam-trading. How do I stop them?**

A: Either stop their bot (preserves their data) or disable them
(`is_active=false`, blocks future sign-in too). Both are
admin-only actions. The bot's existing trade-rate alarm + Phase
8 beta will surface this loud and early.

**Q: A tenant lost their passphrase. Can I recover their data?**

A: No. By design (trust model B). Their secrets are
unrecoverable. They can re-set passphrase + re-enter every
secret to get a fresh start, but historical-trade history
remains as is (it's not encrypted; just the credentials are).
The operator can NOT decrypt the lost data — that's the entire
point of B's at-rest protection.

**Q: A tenant wants to delete their account (GDPR).**

A: Admin endpoint cascades the `tenants` row → tenant_bots,
tenant_secrets, tenant_audit_log, and all per-tenant data rows
via FK CASCADE chains established in alembic 0009. After
deletion, no tenant data remains; aggregate-only data is
preserved (e.g. operator-level metrics).

**Q: How many tenants can my host handle?**

A: Soft cap via `MAX_TENANTS` env var (default 10). Per-bot
defaults: 1 CPU, 512MB RAM. Bumpable per-tenant by operator if
needed. With multi_bot_enabled tenants the count is per-bot, so
3 multi-bot tenants ≈ 9 containers worth of resources.

**Q: Can I invite from a separate Authentik instance?**

A: Today the dashboard is configured against a single OIDC
issuer. Multi-issuer (federated cross-Authentik) is not yet
supported but should be a small change if needed.

---

## Phase 8 (closed-beta) checklist

Before inviting your first non-operator user:

- [ ] Phase 6 cutover complete (operator now uses tenant 1 with
      `multi_bot_enabled=true`)
- [ ] `alembic upgrade head` applied to live Postgres
- [ ] `MAX_TENANTS` set explicitly in deploy env
- [ ] `hypertrade-users` Authentik group exists + has policy
- [ ] Dashboard reachable over HTTPS (Caddy + valid cert)
- [ ] Run `npm run test:integration` against the prod-Postgres
      mirror to confirm RLS policies are live and working
- [ ] Tested invite + first-sign-in + create-bot end-to-end
      with a throwaway test account in the `hypertrade-users`
      group
