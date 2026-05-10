# Invite-only onboarding

`hypertrade` multi-tenant deployments use **Authentik group
membership** as the registration gate. There is no self-service
sign-up form, no email-verification flow, no public registration
endpoint. The operator decides who gets in by adding their
Authentik account to the `hypertrade-users` group.

This document covers the operator-side workflow (adding a new user)
and the user-side workflow (first sign-in through bot-running).

> **Status note (2026-05-10):** This doc was written ahead of
> Phase 6 + Phase 8. Feature status is marked inline:
> - **AVAILABLE NOW** = implemented + on `master`
> - **PLANNED** = part of a later phase, not yet implemented
> Where the operator workflow currently relies on raw DB / SSH
> rather than a UI/API, that's called out explicitly.

---

## Why invite-only

The decision was made during multi-tenancy planning
([`docs/plans/multi-tenancy.md`](plans/multi-tenancy.md) §1
decision 5). Reasoning:

- **Resource sanity** — per-user containers add up; operator wants
  to know who they're sharing the host with. (A `MAX_TENANTS`
  enforcement env-var is **PLANNED** but not yet wired; for now
  the operator self-polices via Authentik group membership.)
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
3. Send them the dashboard URL + the user-workflow steps from this
   doc (USER_GUIDE.md is **PLANNED** — first beta user audience
   will tell us what they need before we write a dedicated guide).

That's it on the operator side. The user can now sign in and
the dashboard will auto-create their `tenants` row on first sign-
in.

### Removing a tenant

**AVAILABLE NOW** (manual SSH/SQL path):

1. **Stop their bot first** to avoid orphan containers:
   ```bash
   # Find the container
   ssh root@$DEPLOY_HOST 'docker ps --filter label=hypertrade.tenant_id=<uuid>'
   # Stop + remove
   ssh root@$DEPLOY_HOST 'docker rm -f hypertrade-bot-<short_id>-<mode>'
   ```
2. Optional: remove their Authentik account from the
   `hypertrade-users` group — revokes future sign-ins; their DB
   rows stay intact.
3. To delete their data entirely:
   ```sql
   -- Connect to postgres as superuser
   DELETE FROM tenants WHERE id = '<uuid>';
   -- Cascades to tenant_bots, tenant_secrets, tenant_audit_log
   -- and all per-tenant data rows via FK CASCADE (alembic 0009).
   -- Also drop the per-tenant PG role (Phase 5b created it):
   DROP ROLE IF EXISTS tenant_<32hex>;
   ```
   Irreversible. Confirm against `git log` to be sure you're
   removing the right tenant.

**PLANNED** (Phase 6+):

- `GET/POST/DELETE /api/admin/tenants[/<id>]` — operator-scoped HTTP
  endpoints for the above. No admin dashboard UI in v1; operators
  use curl + jq.
- A `delete_tenant(tenant_id)` helper that wraps the SQL cascade
  + Postgres-role DROP into one atomic operation.

### Operator visibility

**AVAILABLE NOW** (raw access):

- Operator's bot containers connect as the `postgres` superuser
  and bypass RLS naturally — so SELECT-ing across the whole
  database from any operator-tier bot's DB session shows all
  tenants' rows. Manual SQL queries via `docker compose exec -T
  postgres psql -U postgres -d hypertrade ...` is the current
  "admin UI."
- `docker ps --filter label=hypertrade.tenant_id=<uuid>` lists
  running containers per tenant (labels set by the orchestrator
  in Phase 3a).

**PLANNED** (admin-tier dashboard endpoints, Phase 6+):

- List all tenants + bot status via `/api/admin/tenants`
- Stop any tenant's bot via `/api/admin/tenants/<id>/bots/<bot_id>/stop`
- Disable a tenant via `/api/admin/tenants/<id>/disable`
  (this should set `tenants.is_active = false` AND have the
  tenant resolver enforce it — currently `tenants.is_active`
  exists in the schema but **NOT enforced** by
  `lib/tenant.ts:getCurrentTenant`. Bug or feature gap; tracked
  for Phase 6.)
- Aggregate metrics page

The operator role currently **CANNOT** (by design, multi-tenancy
trust model B):

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

A: **Today (manual)**: SSH in and `docker rm -f
hypertrade-bot-<short_id>-<mode>`. Their bot is dead; their data
stays. They can recreate via the dashboard if they unlock again,
so for repeat offenders also remove their account from the
`hypertrade-users` Authentik group.

**Planned (Phase 6+)**: admin endpoint
`POST /api/admin/tenants/<id>/disable` will set
`tenants.is_active = false`, and the tenant resolver will be
updated to deny new sign-ins for inactive tenants. Today the
column exists but isn't enforced — file an issue if this bites
before Phase 6 lands.

The bot's trade-rate alarm + parity check (audit M2 + M3) will
surface a misbehaving bot loudly via Telegram regardless.

**Q: A tenant lost their passphrase. Can I recover their data?**

A: No. By design (trust model B). Their secrets are
unrecoverable. They can re-set passphrase + re-enter every
secret to get a fresh start, but historical-trade history
remains as is (it's not encrypted; just the credentials are).
The operator can NOT decrypt the lost data — that's the entire
point of B's at-rest protection.

**Q: A tenant wants to delete their account (GDPR).**

A: **Today (manual)**: Operator runs the SQL cascade described
in "Removing a tenant" above. `DELETE FROM tenants WHERE id =
'<uuid>'` cascades to tenant_bots, tenant_secrets,
tenant_audit_log, and all per-tenant data rows via the FK CASCADE
chains established in alembic 0009. Operator also drops the
per-tenant Postgres role manually. **Planned (Phase 6+)**:
admin HTTP endpoint that wraps both into one call.

After deletion, no tenant data remains in the DB; aggregate-only
operator metrics are preserved.

**Q: How many tenants can my host handle?**

A: No hard cap is enforced today. Per-bot defaults
(`bot-orchestrator.ts`): **1 CPU, 512 MB RAM**, hardcoded — not
yet env-overridable per tenant. Roughly: 10 single-bot tenants
≈ 10 containers ≈ 5 GB RAM + 10 CPU shares plus the operator's
own paper/testnet/mainnet bots.

**Planned (Phase 6+)**: `MAX_TENANTS` env var enforced at bot-
create time + per-tenant resource overrides via admin endpoint.
Until then, the operator polices headcount via the
`hypertrade-users` Authentik group and watches host metrics
(`docker stats`).

**Q: Can I invite from a separate Authentik instance?**

A: Today the dashboard is configured against a single OIDC
issuer. Multi-issuer (federated cross-Authentik) is not yet
supported but should be a small change if needed.

---

## Phase 8 (closed-beta) checklist

Before inviting your first non-operator user:

- [ ] Phase 6 cutover complete (operator now uses tenant 1 with
      `multi_bot_enabled=true`)
- [ ] `alembic upgrade head` applied to live Postgres so 0010 RLS
      is in effect (Phase 5a infrastructure)
- [ ] `hypertrade-users` Authentik group exists + has a Group
      Membership policy on the dashboard's OIDC provider
- [ ] Dashboard reachable over HTTPS (Caddy + valid cert)
- [ ] `npm run test:integration` green against an ephemeral
      Postgres (testcontainers) — proves the RLS policies still
      enforce isolation
- [ ] Test the invite flow end-to-end with a throwaway Authentik
      account: sign in → set passphrase → add HL testnet key →
      unlock → create paper bot → bot ticks without error
- [ ] Operator headcount sanity: < 5 tenants planned for first
      beta wave (no `MAX_TENANTS` enforcement yet — see FAQ
      above)
- [ ] Decide ahead of time how to revoke if needed: SSH access
      working, Authentik group remove documented, manual SQL
      DELETE-from-tenants understood (operator dry-runs the SQL
      against testnet first)
