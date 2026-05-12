# PR 3 — Telegram unlock-link flow

## Problem

After PR 1+2, tenants store HL keys + Telegram tokens in
`tenant_secrets` encrypted under a passphrase-derived K. The K-cache
in Redis is session-scoped (7-day TTL); when the session expires OR
when a host reboot loses everything (Redis is in compose, not
persisted across image rebuilds), no K exists → bot cannot decrypt
`tenant_secrets` → bot has no HL keys → won't trade.

Today the only way to unlock is for the tenant to open the web
dashboard and POST `/api/tenant/me/unlock`. That's bad UX for:
- Host reboot at 3am — bot stays locked until tenant logs in
- Tenant-bot crash → `decryptAndStart` re-runs decrypt with cached
  K (currently fine), but a recreate (e.g. after image upgrade)
  loses K-cache if session expired
- Beta tenants who use Telegram more than the dashboard

## Design (chat 2026-05-12)

**Telegram is a notification + deeplink channel, NOT an
auth-credentials channel.** Passphrase never travels through
Telegram (E2E-issue avoided).

Flow when a bot starts without an unlocked K:

1. Bot detects locked state (tenant_secrets rows exist but
   `requireUnlockedKey` would 401)
2. Bot DMs the tenant's linked chat:
   > 🔒 Bot offline after restart. Click to unlock:
   > https://$DEPLOY_HOST/unlock?token=<HMAC-signed>
3. Token is short-lived (5 min), signed with a shared
   bot↔dashboard secret, payload = `{tenant_id, bot_id, mode,
   issued_at}`
4. Tenant clicks link → `/unlock` page on dashboard
5. Page validates token signature + expiry → shows passphrase input
6. User enters passphrase → dashboard POSTs to existing
   `/api/tenant/me/unlock` → K cached in Redis
7. Dashboard signals the locked bot via internal endpoint
   (`POST <bot>/api/internal/k-available`) so bot retries
   decrypt + starts trading

## DB schema

New table `tenant_telegram_links`:

```sql
CREATE TABLE tenant_telegram_links (
  tenant_id UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
  telegram_chat_id BIGINT NOT NULL,
  telegram_username VARCHAR(64),  -- for display only, can change
  linked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_unlock_at TIMESTAMPTZ
);
CREATE INDEX idx_tenant_telegram_links_chat ON tenant_telegram_links(telegram_chat_id);
```

Note: 1:1 mapping. A tenant has at most one linked Telegram chat
(simpler than 1:many; can revisit if beta users want multi-device).

## Linking flow

Initial linking proves the user owns both the Authentik account
AND the Telegram chat:

1. Tenant goes to `/settings/credentials` (extended) or new
   `/settings/telegram`
2. Clicks "Link Telegram"
3. Dashboard generates a 6-digit code, stores in Redis with TTL
   10 min, key `tg-link:<code>` → tenant_id
4. UI shows "Send `/link 123456` to @YourBot in Telegram"
5. Tenant DMs the bot `/link 123456`
6. Bot looks up the code in Redis → finds tenant_id → fetches
   `chat.id` and `from.username` from the Telegram message →
   inserts row into `tenant_telegram_links`
7. Bot replies: "✅ Linked! You'll get unlock notifications here."
8. Polling endpoint on dashboard sees the row exist → UI updates
   to "Linked as @username"

Inverse flow (Telegram first, web confirms) is also possible but
gives only one proof-of-control. The above gives two.

## Locked-state detection in bot

Bot startup currently fetches creds from env or (if PR 4 lands)
from `tenant_secrets`. Add a startup phase:

```python
# In main.py, before exchange init
async def _wait_for_unlock_if_locked(tenant_id, telegram, repo):
    """If tenant_secrets has rows but we can't decrypt them
       (no K available), enter locked state: DM the unlock
       deeplink, then poll an internal signal until K arrives.
       Skip entirely for operator if env-injection still active."""
    if not _tenant_has_secrets(tenant_id):
        return  # nothing to unlock
    if _try_decrypt_one(tenant_id):
        return  # already unlocked (K-cache hit)
    # Send unlock-link DM (rate-limited: max 1/5min/tenant)
    token = _make_unlock_token(tenant_id, bot_id, mode)
    deeplink = f"{PUBLIC_URL}/unlock?token={token}"
    await telegram.send_unlock_link(deeplink, mode)
    # Block startup until /api/internal/k-available is called
    await _wait_for_k_available(timeout=3600)  # 1h
```

The bot's existing `restart: unless-stopped` is fine — if it
times out waiting for unlock, container exits, docker restarts,
new DM goes out (rate limit prevents spam).

## Bot `/link <code>` command

Add to `_commands` dict in `notify/telegram.py`:
- Validate code format (`^\d{6}$`)
- Lookup Redis `tg-link:<code>` → tenant_id (or 404)
- Get chat_id from message context
- INSERT (or UPSERT on conflict) into `tenant_telegram_links`
- Delete Redis code (one-shot use)
- Reply with success

Edge cases:
- Group chats: refuse, tell user to DM the bot directly
- Already linked elsewhere: overwrite (with audit log)
- Code expired: tell user to retry from dashboard

## Bot internal endpoint

New aiohttp route on bot's HTTP API: `POST /api/internal/k-available`

- Requires X-Api-Key (operator-only)
- Body: `{tenant_id: uuid}`
- Action: signal the bot's startup-loop (via asyncio.Event or
  Redis pubsub) that K-cache should now have a value → retry
  decrypt + proceed to normal startup
- 404 if the bot's not in waiting-for-unlock state

## Unlock-page on dashboard

New route `/unlock`:

- Parses `?token=...` from query string
- Validates HMAC signature + expiry
- If valid: shows passphrase input + tenant identity ("Unlock
  bot for paper@you.com")
- On submit:
  1. POST `/api/tenant/me/unlock` (existing endpoint) with the
     passphrase
  2. POST `/api/internal/signal-k-available` (new dashboard
     endpoint that calls the bot's internal API)
  3. Show "✅ Bot unlocked, trading resumed"
- 401 with helpful message on expired/invalid token

Token format: base64url(HMAC-SHA256-truncated to 16 bytes ||
payload). Same shared secret as session cookies
(`SESSION_SECRET`).

## Rate limiting + audit

- Telegram DM unlock-link: max 1 per 5 min per tenant
  (Redis SETNX with 300s TTL)
- Unlock attempts: max 5 per 15 min per tenant; soft lockout 1h
  on exceed
- All `/link`, `/unlock`, K-available signals go to
  `tenant_audit_log` with the IP from the request (no
  passphrase content, of course)

## Sub-PR breakdown

Big scope — split into smaller PRs that each ship independently:

### PR 3a — DB + link API (this PR's first sub-PR)
- Alembic migration 0012 for `tenant_telegram_links`
- Drizzle table definition
- `POST /api/tenant/me/telegram/link` (creates code)
- `GET /api/tenant/me/telegram/link` (returns linked status)
- `DELETE /api/tenant/me/telegram/link` (unlinks)
- Unit tests for the API

Mergeable on its own. Linking won't work end-to-end yet (no
bot `/link` command), but the schema + dashboard surface are
ready.

### PR 3b — Bot `/link` command + UI for linking
- `/link <code>` in `notify/telegram.py`
- `/settings/credentials` or new `/settings/telegram` shows
  "Link Telegram" button + code display
- Linking now works end-to-end

### PR 3c — Locked-state detection + DM + unlock-page
- Bot startup wait-for-unlock loop
- `/api/internal/k-available` on bot
- `/unlock?token=` page + signed-token helper
- `/api/internal/signal-k-available` on dashboard

This is the big PR. Touches both bot AND dashboard.

### PR 3d — Rate limiting + audit + polish
- Rate limit logic
- Audit log writes
- Edge case handling (group chat, expired codes, re-link)

## Out of scope for PR 3

- Per-tenant Telegram routing (today: testnet bot owns all
  Telegram across all 3 modes). PR 3 keeps this as-is. Beta
  feedback may change priorities.
- Auto-detect host reboot vs. session expiry — both go through
  the same locked-state path.
- Re-encrypt-on-passphrase-change. Still deferred (it touches
  every `tenant_secrets` row and needs its own design).

## Test plan

Each sub-PR has its own test plan, but cumulatively:

- [ ] Tenant can link Telegram via 6-digit code (round trip
      proves both auth + chat ownership)
- [ ] Bot restart with no K-cache → DM lands in linked chat
- [ ] `/unlock?token=` accepts valid token, rejects expired,
      rejects bad signature
- [ ] After passphrase entry, bot resumes trading within ~5s
- [ ] Rate limiting: triggering 6 unlock-link DMs in 5min only
      sends 1
- [ ] Operator (env-injected creds) skips locked-state path
      entirely — bot starts normally without waiting
