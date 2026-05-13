# HyperTrade / Xupertrade — Security Audit (post PR-4c, multi-tenant)

**Date:** 2026-05-12
**Auditor:** Claude (Opus 4.7)
**Scope:** `bot/`, `dashboard/`, supporting libs. Threat model = malicious
authenticated tenant on the multi-tenant deployment, plus
unauthenticated network attacker for the public surface.
**Out of scope:** operator-as-adversary (operator already has host root +
docker socket).

---

## 1. Executive summary

Multi-tenancy primitives (per-tenant Postgres roles, RLS, tenant-scoped
Drizzle queries, AES-256-GCM secret-at-rest, Argon2id KDF, HMAC session
cookies) are well-designed and the code is unusually disciplined about
defence-in-depth (`requireOperator` strict `=== true`, fail-closed
`fetchAuthConfig`, fail-closed `getSessionSecret`, GETDEL on Telegram
codes, TOCTOU-aware role provision, etc.). I did not find a clear
cross-tenant *data-read* break: every `db.select` on tenant-scoped
tables in the dashboard goes through `queries.ts` with an explicit
`tenantId` arg, every API route resolves the calling tenant via
`requireTenant`, and the bot runs as a per-tenant PG role with RLS.

The notable findings cluster around **(a) malicious tenant escalating
their *own* bot beyond operator-imposed risk caps via the secrets API,
(b) brute-force / replay surfaces with no rate-limit, and (c) the
shared `API_KEY` model collapsing the auth boundary between bots inside
the docker network.** None of these are theoretical — each has a
concrete exploit path described below.

**Top 5 highest-risk findings:**

1. **C-1 — Malicious tenant overrides bot risk caps & mainnet
   allowlist via the secrets API.** Any uppercase env-var name passes
   the `^[A-Z0-9_]{1,64}$` filter, so a tenant can set
   `MAINNET_ENABLED_STRATEGIES`, `MAX_TOTAL_EXPOSURE_USD`,
   `SIGNAL_SIZE_MAX_MULTIPLIER`, `TRADE_RATE_ALARM_*` etc. (NOT in
   `getOrchestratorSystemEnv`, so they aren't overwritten by the
   orchestrator) and bypass every operator-imposed safety limit on
   their own bot. Defeats audit-C3 hardening explicitly.
2. **H-1 — `API_KEY` is a single shared secret across all per-tenant
   bots and the dashboard, with `compose_digest("", "")` returning
   True.** With the default empty `API_KEY`, every tenant bot's HTTP
   API is unauthenticated and reachable on the docker network. Any
   tenant who can run code in their own bot container can pause /
   flat-all / leverage every other tenant's bot.
3. **H-2 — `/api/auth/login` and `/api/tenant/me/unlock` have no
   rate-limit or lockout.** Combined with username enumeration
   (`username !== storedUser` shortcut returns before bcrypt) and
   ~100ms Argon2id, an attacker can mount online brute-force against
   either basic-auth or the per-tenant passphrase from a single IP.
4. **H-3 — Logout does not invalidate the session cookie or clear the
   K-cache.** Stateless HMAC cookies + 7-day TTL + 7-day K-cache TTL
   = a stolen cookie keeps decrypting secrets for a week after the
   user clicks "Sign out". No session-revocation list, no
   per-session secret rotation.
5. **M-1 — Telegram `/link` 6-digit code has only a 5s/global cooldown
   on the bot side.** Combined with no per-chat lockout, an attacker
   who knows that some tenant has an active code can brute-force the
   keyspace and on success, *their* chat_id becomes linked to the
   victim's tenant — receiving all future unlock-link DMs.

Total finding count by severity:

| Severity      | Count |
|---------------|------:|
| Critical      |   1 |
| High          |   4 |
| Medium        |   5 |
| Low           |   4 |
| Informational |   3 |

---

## 2. Findings

### C-1 — Tenant secret-CRUD is an env-injection vector that defeats operator safety caps

- **Severity:** Critical (impact: high — bypasses mainnet allowlist
  and total-exposure cap; likelihood: high — anyone with a tenant
  account + their own passphrase can do this with one curl).
- **Location:**
  `dashboard/src/app/api/tenant/me/secrets/[key]/route.ts:25`
  (KEY pattern `/^[A-Z0-9_]{1,64}$/`),
  `dashboard/src/lib/bot-orchestrator.ts:123-144` (`getOrchestratorSystemEnv`
  — incomplete list),
  `bot/hypertrade/config.py:89-130` (settings the orchestrator
  doesn't override).

- **Description.** A tenant's stored secrets are merged into the
  bot container's env vars in `buildSpec`:

  ```
  envMap = { TENANT_ID, BOT_ID, EXCHANGE_MODE,
             ...decryptedSecrets,        // user-controlled
             ...systemEnv,               // orchestrator-controlled, wins
             API_PORT }
  ```

  This is the right pattern, BUT `getOrchestratorSystemEnv()` only
  enumerates `REDIS_URL`, `PAPER_INITIAL_BALANCE`,
  `POLL_INTERVAL_SECONDS`, `MAX_POSITION_SIZE_USD`,
  `MAX_DAILY_LOSS_USD`, `KILL_SWITCH`, `DASHBOARD_URL`, `API_KEY`.
  The bot's `Settings` reads many more — and a tenant can set any of
  the un-overridden ones because the secret-key validator only
  requires `[A-Z0-9_]{1,64}`. Concretely, a malicious tenant can
  PUT into their `tenant_secrets`:

  - `MAINNET_ENABLED_STRATEGIES = "*all 21 strategy names*"` →
    bypasses the audit-C3 mainnet fail-closed allowlist
  - `MAX_TOTAL_EXPOSURE_USD = "999999999"` → defeats the
    cross-position margin cap
  - `SIGNAL_SIZE_MAX_MULTIPLIER = "1000"` → 100× notional via
    sized-signal route (audit H8 bypass)
  - `TRADE_RATE_ALARM_ENABLED = "false"` → silences the spam-trade
    auto-pause that would have caught hash_momentum 2026-05-09
  - `TAKER_FEE_RATE = "0"` → corrupt PnL accounting
  - `TELEGRAM_EVENTS = ""` → silences audit Telegram alerts
  - `HL_ORDER_TIMEOUT_SECONDS = "0.001"` → DoS the bot
  - any of the strategy-specific tunables that may be added later

  All of these are tenant-self-harm in isolation, but the
  *intentional* operator policy — encoded in
  `mainnet_enabled_strategies` defaulting empty and the explicit
  C3 allowlist semantics — is broken. The `API_KEY` and
  `KILL_SWITCH` cases ARE blocked (they're in systemEnv) but the
  unaudited list of bot env vars has 8+ holes today.

- **Exploit.** Authenticate, set passphrase, unlock, then:
  ```
  PUT /api/tenant/me/secrets/MAINNET_ENABLED_STRATEGIES
       {"value": "all,21,strategy,names"}
  PUT /api/tenant/me/secrets/MAX_TOTAL_EXPOSURE_USD
       {"value": "10000000"}
  POST /api/tenant/me/bots {"mode": "mainnet"}
  ```
  The mainnet bot starts with the operator's expected hard caps
  silently disabled.

- **Recommendation.** Two complementary fixes:
  1. **Allowlist the env keys a tenant can set**, not blocklist —
     the only env vars a tenant *should* be able to inject are
     `HYPERLIQUID_PRIVATE_KEY`, `HYPERLIQUID_ACCOUNT_ADDRESS`,
     `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
     `VAULT_TRACKING_ADDRESS`. Reject everything else at the
     `secrets/[key]` PUT route. This is the same shape as
     `requiredSecretsForMode` — define a `TENANT_ALLOWED_SECRETS`
     constant and validate against it.
  2. **Move every operator-policy env var into `getOrchestratorSystemEnv`**
     so even if a future code change loosens the allowlist, system
     env wins. Specifically: `MAX_TOTAL_EXPOSURE_USD`,
     `SIGNAL_SIZE_MAX_MULTIPLIER`, `MAINNET_ENABLED_STRATEGIES`,
     `TAKER_FEE_RATE`, `TRADE_RATE_ALARM_*`, `HL_*_TIMEOUT_SECONDS`.

  Sketch:
  ```ts
  const ALLOWED_TENANT_SECRETS = new Set([
    "HYPERLIQUID_PRIVATE_KEY",
    "HYPERLIQUID_ACCOUNT_ADDRESS",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "VAULT_TRACKING_ADDRESS",
  ]);
  function validKey(k: unknown): k is string {
    return typeof k === "string"
        && KEY_PATTERN.test(k)
        && ALLOWED_TENANT_SECRETS.has(k);
  }
  ```

---

### H-1 — `API_KEY` collapses the auth boundary between per-tenant bots inside the docker network

- **Severity:** High (impact: high — cross-tenant bot control;
  likelihood: medium — needs code execution within a tenant bot,
  which is possible in narrow ways via env injection).
- **Location:**
  `bot/hypertrade/api.py:37-48` (`_require_auth` returns None when
  `settings.api_key == ""`),
  `dashboard/src/lib/bot-orchestrator.ts:142` (every per-tenant bot
  receives the same `API_KEY = process.env.API_KEY` from the
  dashboard's env).

- **Description.** Two related issues:

  1. The dashboard injects the SAME `API_KEY` into every tenant's bot
     container. Per-tenant bots run on the shared docker network
     (`hypertrade_default`); container `hypertrade-bot-<short>-mainnet`
     is reachable from `hypertrade-bot-<other>-paper` via Docker DNS.
     A tenant who manages to execute any code inside their own bot
     (see C-1 for one vector — hostile env vars; future Python
     dependency RCE is another) can hit
     `http://hypertrade-bot-<other>-mainnet:8002/api/control/flat-all`
     with the shared `X-Api-Key` and force-close another tenant's
     positions. They can also read `/api/positions` and
     `/api/control/state` for arbitrary tenants.

  2. When `API_KEY` is unset (the `.env.example` default and the
     current operator deployment per
     `dashboard/src/lib/bot-api.ts:60-62` comment), `_require_auth`
     returns None — no auth at all. Any process on the docker
     network (i.e. any compromised container) can call any control
     endpoint on any bot. The `flat_all` and `set_leverage`
     endpoints can liquidate tenant positions and burn margin in a
     single call.

- **Exploit.** Same prerequisites as C-1 (env-var injection into the
  attacker's own bot — e.g. a malicious `MATPLOTLIBRC` or future
  unsanitized key triggering Python startup code) or any future bot
  RCE; once code runs in any container on the hypertrade network,
  cross-tenant control is trivial.

- **Recommendation.**
  1. **Per-tenant API key.** Generate `API_KEY` per
     `tenant_bots` row at start time, store it in the row, inject as
     env on container start, and have the dashboard fetch it from the
     row when proxying. The dashboard already knows which row
     it's targeting via `tenantBotFetch`. Same shape as
     `tenant-pg-role` rotation.
  2. **Fail-closed when `settings.api_key == ""`** instead of
     globally disabling auth. If you really need an "unauth" mode
     for paper, gate it behind `EXCHANGE_MODE=paper` only.
  3. **Network segmentation.** Put each tenant's bot on its own
     Docker network or use Docker user-defined network policies so
     bots can't reach each other. Only the dashboard needs to
     reach all bots.

---

### H-2 — No rate-limit / lockout on `/api/auth/login`, `/api/tenant/me/unlock`, `/api/auth/oidc/start`

- **Severity:** High (impact: high — full account takeover via
  passphrase brute-force; likelihood: medium for short
  passphrases).
- **Location:**
  `dashboard/src/app/api/auth/login/route.ts` (no rate-limit),
  `dashboard/src/app/api/tenant/me/unlock/route.ts` (no
  rate-limit, no audit on failed attempt),
  `dashboard/src/app/api/auth/oidc/start/route.ts` (no rate-limit).
  Compare with
  `dashboard/src/app/api/tenant/me/telegram/send-unlock-link/route.ts:62`
  which DOES rate-limit — the helper exists; it's just not wired in
  here.

- **Description.** `/api/tenant/me/unlock` runs Argon2id then verifies
  HMAC. Argon2id at 64 MiB / 3 iters / 4 parallelism takes ~100ms on
  modern server CPUs — an attacker can mount ~10 guesses/sec from a
  single IP indefinitely. With the 12-char minimum and no upper
  complexity requirement, `passphrase = "Letmein!2024"` is a viable
  guess; common-word-stuffing wordlists make this realistic. After
  N guesses succeeds, attacker has K → can decrypt every
  `tenant_secrets` row → has the HL private key + Telegram token.

  `/api/auth/login` additionally **leaks user existence via timing**:
  ```ts
  if (username !== storedUser) {
    return NextResponse.json({ error: "invalid-credentials" }, { status: 401 });
  }
  ```
  The username comparison short-circuits before bcrypt runs (~250ms
  difference), letting an attacker enumerate the basic_user via
  timing, then focus brute-force on the password.

  `/api/auth/oidc/start` writes a state cookie and 302s to the
  provider on every call — at scale, an attacker can spam this to
  fill the upstream IdP's state cache or your own logs.

- **Exploit.** Brute-force passphrase from a single browser tab.
  Even with a strong passphrase, lack of failed-attempt audit logs
  means no one notices.

- **Recommendation.** Wire `checkRateLimit` into all three routes
  with sane buckets. Suggested:
  - `/api/tenant/me/unlock`: 10 attempts per 15 min per
    `(tenant_id, ip)`, audit each failure.
  - `/api/auth/login`: 10 attempts per 15 min per `(ip)` AND per
    `(username)`. Make username comparison constant-time (run
    bcrypt against a dummy hash on user-not-found so timing is
    flat).
  - `/api/auth/oidc/start`: 60/min per ip.

  Sketch for unlock:
  ```ts
  const ip = req.headers.get("x-forwarded-for")?.split(",")[0] ?? "unknown";
  const rl = await checkRateLimit("unlock-attempt", `${tenant.id}:${ip}`, 10, 900);
  if (!rl.allowed) return Response.json({ error: "rate-limited" }, { status: 429 });
  ```

---

### H-3 — Logout does not revoke the session cookie or clear the K-cache

- **Severity:** High (impact: high — full secret access for 7 days
  after "logout"; likelihood: low without separate cookie-theft
  vector, but should still be defence in depth).
- **Location:**
  `dashboard/src/app/api/auth/logout/route.ts:6-14` (only clears
  the response cookie),
  `dashboard/src/lib/auth.ts:126-160` (HMAC session is stateless;
  no server-side revocation list),
  `dashboard/src/lib/crypto/k-cache.ts:27` (default 7-day TTL).

- **Description.** Sessions are HMAC-signed JWT-style; there is no
  per-session record server-side, so "logout" can only ask the
  browser to drop the cookie. Anyone who has previously copied the
  cookie value (XSS — limited by httpOnly but not zero; shoulder-
  surfing; clipboard sniffer; physical access; corrupt browser
  extension) can keep using it until the 7-day exp regardless of
  logout.

  Worse, the K-cache is keyed by `sha256(cookie_value).slice(0, 32)`
  — so the same stolen cookie also resolves the cached K. An
  attacker doesn't even need to brute-force the passphrase.

- **Exploit.** Steal cookie via any side channel → `curl -b cookie
  ...` to `/api/tenant/me/secrets` and `/bots/[id]/start` — never
  prompted for passphrase, since K-cache is intact.

- **Recommendation.**
  1. Maintain a per-session server record in Redis
     (`session:<sha256(cookie)>` → `{tenant_id, exp, revoked}`).
     Verify-session checks the record. Logout sets `revoked=true`
     OR deletes the record. K-cache clear on the same key.
  2. Lower K-cache TTL to ~24h — 7 days is excessive for a
     decryption key; users re-typing a passphrase once a day is a
     normal trade-off.
  3. On password change / passphrase change (when implemented),
     bulk-revoke all sessions for that tenant.
  4. Even without server-side sessions: have logout call
     `clearKey(tenant.id, sessionId)` so at least the K-cache
     evicts.

---

### H-4 — `/unlock` deeplink page leaks tenant email/display-name to anyone with a valid token

- **Severity:** High → Medium (impact: medium — PII leak; likelihood:
  low — token is signed and short-lived).
- **Location:** `dashboard/src/app/unlock/page.tsx:55-75`.

- **Description.** The `/unlock?token=...` route is in
  `PUBLIC_PATHS` (proxy.ts:25) so it's reachable without auth. The
  page validates the signed token and then SELECTs the tenant's
  `email` and `displayName` and renders them in the HTML. Anyone
  who screenshot-shares or forwards a Telegram unlock-link DM
  exposes the recipient's email to the new viewer.

  The tenant's K is safe (the POST still requires a session +
  passphrase), but in a multi-tenant context, leaking tenant
  identities is a privacy break — operator's tenant table is the
  one place a tenant's identifier lives, and the token-bearer
  shouldn't see it.

- **Exploit.** Forward a Telegram unlock DM to anyone (e.g. by
  accident in a chat). The forwarder reveals their identity to
  whoever opens it, even if they don't know the passphrase.

- **Recommendation.** Don't render the email or displayName on the
  page — show a generic "Unlock your bot" with no PII. The
  unlock POST already knows whose tenant to unlock based on the
  authenticated session.

---

### M-1 — Telegram `/link` code is brute-forceable across the keyspace

- **Severity:** Medium (impact: high if successful — attacker's
  chat_id becomes linked to victim's tenant, receives future unlock
  DMs; likelihood: low under realistic conditions but non-zero).
- **Location:** `bot/hypertrade/notify/telegram.py:441-445`
  (5-second-per-cmd cooldown is the only throttle),
  `dashboard/src/app/api/tenant/me/telegram/link/route.ts:49-54`
  (6-digit code, `randomInt(0, 1_000_000)`).

- **Description.** The 6-digit codespace is 10⁶. The bot-side
  `_handle_command` cooldown is keyed by command name only (not
  per-chat) and is 5s, so all `/link` traffic globally shares one
  bucket: max ~1 attempt / 5s ≈ 17,280 attempts/day. The code TTL
  is 600s. For each victim with an active code, an attacker
  averaging 1 attempt every 5s gets ~120 attempts in the window —
  hit probability ~120 × 10⁻⁶ = ~0.012% per victim per code. With
  N tenants minting codes per day, expected hits = N × P_per_mint.
  Low for a small deployment but rises linearly. Worse: the
  cooldown silently drops legitimate `/link` attempts when an
  attacker is brute-forcing, breaking the feature for real users.

  On a successful guess, `upsert_telegram_link` writes the
  attacker's chat_id against the victim's tenant_id (UNIQUE on
  chat_id but not on tenant_id; older link is overwritten by
  most-recent /link semantics in the repo). All future
  unlock-link DMs go to the attacker.

- **Exploit.** Spam `/link 000001`, `/link 000002`, ... from a fresh
  Telegram account. With 10⁶ codes and the global cooldown, full
  scan ≈ 58 days; targeted scan during a 10-min victim window has
  the percentages above. Hit → attacker's chat receives unlock
  link → user clicks (still needs passphrase) — but social
  engineering becomes easy: "Click here to unlock your bot, but
  first install this 2FA app …".

- **Recommendation.**
  1. **Per-chat rate-limit on /link**: e.g. 5 attempts per 30 min
     per chat_id, hard-fail beyond. Use the existing
     `checkRateLimit` (move it to a shared helper that bot can
     reach, or implement the same Redis pattern bot-side).
  2. **Wider codespace**: 12-character base32 (≈ 10¹⁸) makes brute
     force computationally infeasible regardless of throttle.
  3. **Bind the code to the chat_id**: when the dashboard mints a
     code, require the user to specify the destination Telegram
     username; refuse `/link` from any other chat. Adds friction
     but eliminates the brute-force vector entirely.

---

### M-2 — `tenants.is_active` is a dead column — disabled tenants can still log in and operate

- **Severity:** Medium (impact: high if operator relies on it for
  offboarding; likelihood: medium — looks like the intended
  offboarding mechanism).
- **Location:**
  `dashboard/src/lib/db.ts:166` (`is_active` column),
  `dashboard/src/lib/audit-log.ts:30` (`tenant.disabled` action
  exists),
  `dashboard/src/lib/tenant.ts:65-105` and
  `dashboard/src/lib/tenant-server.ts:47-104`
  (`getCurrentTenant` and `requireTenantServer` never check
  `is_active`).

- **Description.** The `tenants.is_active` flag is non-null with
  default true, suggesting an offboarding flow. But neither
  `getCurrentTenant` nor `requireTenantServer` filters on it, and
  no API route checks it before granting access. An operator who
  flips `is_active=false` for a tenant believes they've revoked
  access; in reality the tenant continues to log in, unlock, run
  bots, and place trades.

- **Recommendation.** Add `eq(tenants.isActive, true)` to both
  resolvers, and short-circuit `requireTenant` with a 403 when the
  row exists but is inactive (so the user gets a clear "account
  disabled" message rather than being treated as a fresh signup
  via the autoCreate path).

---

### M-3 — Auto-create-tenant-on-first-sight has no operator gate

- **Severity:** Medium (impact: medium — anyone the OIDC IdP issues
  a token to gets a free tenant; likelihood: depends on IdP config).
- **Location:** `dashboard/src/lib/tenant.ts:64-104`,
  `dashboard/src/lib/tenant-server.ts:76-103`.

- **Description.** `getCurrentTenant` autocreates a tenant on first
  sight of any new `authentik_sub`. The `INVITE_ONBOARDING.md` doc
  expects "operator decides who gets in by adding their Authentik
  group membership" — but nothing in the dashboard code checks for
  that group. If the operator's Authentik instance is configured
  to allow social-login signups (or any IdP federation that
  exposes the issuer broadly), every signup flow that completes
  OIDC auth gets a fresh tenant row + can mint a passphrase + can
  start a bot.

- **Recommendation.**
  1. Pass the OIDC `groups` claim through to the session and check
     for an operator-defined group on first auto-create. Today
     the session payload only has `sub, iat, exp` — extend it.
  2. Or remove auto-create entirely and require the operator to
     pre-provision the `tenants` row (simpler, matches the
     "invite-only" intent in the doc). 401 with a clear "ask the
     operator to add you" when the row is missing.

---

### M-4 — Failed unlock attempts are not audited

- **Severity:** Medium (impact: medium — no detection of brute-force
  in progress; likelihood: medium).
- **Location:**
  `dashboard/src/app/api/tenant/me/unlock/route.ts:64-66`.

- **Description.** A wrong passphrase returns 401 silently — no
  `appendAuditLog` write, no Sentry hook, no Telegram alert.
  Combined with H-2 (no rate limit), there is zero detection
  surface for a brute-force in progress.

- **Recommendation.** Audit-log every unlock attempt
  (`passphrase.unlock-failed`), and trigger an operator Telegram
  notification after N failures within a window.

---

### M-5 — `_DASHBOARD_ORIGIN` defaults to `*` for CORS

- **Severity:** Medium (impact: low — bot endpoints don't accept
  cookies, but reflective leakage of paused/state etc. via fetch();
  likelihood: low).
- **Location:** `bot/hypertrade/api.py:22`.

- **Description.** `_DASHBOARD_ORIGIN = os.getenv("DASHBOARD_URL", "*")`.
  The orchestrator does inject DASHBOARD_URL via systemEnv, but the
  default is `*`. If a misconfigured deploy ever runs without
  `DASHBOARD_URL`, any browser visiting attacker.com can fetch the
  bot's `/api/control/state` (which leaks open positions, equity,
  paused-state) when API_KEY is empty. Bot doesn't set
  `Access-Control-Allow-Credentials: true` so it's read-only PII
  leak, not session theft, but still wrong.

- **Recommendation.** Drop the `*` fallback; refuse to start (or
  just refuse to set the CORS header) if `DASHBOARD_URL` is unset.
  Better: don't set ACAO at all server-side — the dashboard
  always fetches via server-side proxy in `bot-api.ts`, so CORS is
  never needed for the in-band path.

---

### L-1 — `/unlock` page is in PUBLIC_PATHS but POST requires auth — UX trap, not break

- **Severity:** Low.
- **Location:** `dashboard/src/proxy.ts:25`,
  `dashboard/src/app/api/tenant/me/unlock/route.ts:33`.

- **Description.** Documented in the proxy.ts comment. Not a
  vulnerability per se — the unlock POST does require a session,
  so a passphrase typed by an unauthenticated user goes nowhere.
  The risk is users not understanding why their passphrase didn't
  "work" and trying alternate passphrases unprotected against
  H-2's rate-limit gap.

- **Recommendation.** Either (a) move /unlock out of PUBLIC_PATHS
  and rely on the standard session redirect to /login, or (b) make
  the page itself check auth status and render a
  "sign in first" UI before showing the passphrase prompt.

---

### L-2 — OIDC state cookie is unsigned (relies on state-string equality only)

- **Severity:** Low (a properly-implemented OIDC state check is
  enough; just noting for completeness).
- **Location:** `dashboard/src/lib/oidc.ts:43-44` (encodeStateBundle
  is base64 JSON, no signature).

- **Description.** The state bundle (`code_verifier`, `state`,
  `next`) is stored in the user's cookie unsigned. Integrity rests
  on the IdP echoing the same `state` back — which is the standard
  pattern. But the `code_verifier` and `next` aren't covered by
  the `state` echo; if an attacker can swap the cookie before the
  callback returns (e.g. via a separate XSS or cookie-jar issue),
  they could swap the PKCE verifier or open-redirect target.
  Defence in depth: sign the cookie with `SESSION_SECRET` like
  unlock-token already does.

- **Recommendation.** HMAC-sign the state bundle.

---

### L-3 — Bot's `_command_cooldown_seconds` keyed by command name, not chat

- **Severity:** Low (UX/availability + accomplice to M-1 brute
  force).
- **Location:** `bot/hypertrade/notify/telegram.py:441-445`.

- **Description.** A single global 5s cooldown per command
  globally throttles all chats — meaning during a brute-force
  attack on `/link`, legitimate users find their `/link` silently
  ignored. Convert to a per-chat dict.

---

### L-4 — `tenants.email` defaulted from `authentik_sub` is not validated

- **Severity:** Low.
- **Location:** `dashboard/src/lib/tenant.ts:88` (and identical in
  tenant-server.ts:91).

- **Description.** First-sight tenant creation copies
  `session.sub` into `email` and `displayName`. If `sub` is just
  a UUID or username (not actually email), the `email` field
  contains garbage. UI elsewhere may treat it as a real email
  (display in /unlock, send-to address in future flows). Match
  on the OIDC `email` claim instead of `sub` when available.

---

### Informational

- **I-1** — Argon2id parameters are documented as **immutable**
  forever. There is no `kdf_version` column. This is fine in v1
  but worth tracking: if Argon2id parameters need to be raised, every
  tenant's verifier becomes invalid. Add `tenants.kdf_version` now
  while the schema is fluid.

- **I-2** — `K` is cached in Redis as plaintext base64. Per the
  k-cache.ts comment, this is intentional for v1 (operator with
  Redis access can already read every container's env vars). Worth
  re-examining when multi-tenant SaaS use becomes the operating
  posture.

- **I-3** — `dashboard/src/app/api/healthz/route.ts` is public. I
  did not read it but the placement suggests a thin liveness probe
  — confirm it doesn't include version, build SHA, or runtime
  config in the payload (those would be fingerprinting aids).

---

## 3. Positive observations — what I verified is correctly handled

These are findings I checked for and concluded are genuinely
well-handled, not just "I didn't look":

- **`tenant-pg-role.ts:provisionRole`** — race-safe CREATE-OR-ALTER
  via `EXCEPTION WHEN duplicate_object`, strict role-name regex
  defended even after self-construction, password
  single-quote-escaped despite being base64url-only. Sequence
  grants are explicit and exclude dashboard-only sequences.
  SHARED_TABLES (vaults) correctly omit DELETE — a tenant role
  can't wipe the vault scanner data.
- **RLS coverage** — alembic 0010 + 0014 cover the 9 data tables
  + `tenant_telegram_links`. The bot connects as the per-tenant
  role so an SQL-injection-style attack in a strategy can't escape
  tenant scope.
- **Dashboard `queries.ts`** — every exported query takes
  `tenantId` as the first arg and includes
  `eq(table.tenantId, tenantId)`. TypeScript's `notNull` on the
  Drizzle column makes a missing arg a build error. Call sites in
  `app/page.tsx`, `app/status/page.tsx` pass it correctly.
- **`requireOperator`** — strict `=== true` (not `!isOperator`),
  guarding TLS/configure, TLS/config GET, auth/configure,
  events/SSE.
- **`fetchAuthConfig` failure semantics** — explicitly does NOT
  cache `disabled` on Redis errors; returns null so proxy.ts
  fail-closes. Comment is accurate; tested in
  `auth-config.test.ts`.
- **`signSession` / `verifySession`** — HMAC-SHA256, payload+sig,
  `timingSafeEqual`, length check before compare. Body parse is
  try/catch'd. exp is enforced and required to be a number.
- **`encryptSecret` / `decryptSecret`** — AES-256-GCM with random
  12-byte nonce, GCM auth tag standard layout, key/nonce length
  asserted, ciphertext-too-short check before slicing. Each
  encrypt produces a fresh ciphertext for the same plaintext (no
  re-use detection).
- **`mintUnlockToken` / `verifyUnlockToken`** — fail-closed when
  `secret == ""` (otherwise signing with an empty key is
  forgeable). Domain-separator (`unlock-token-v1`) prevents cookie
  signature reuse. exp enforced.
- **`/api/tenant/me/telegram/link`** — uses `randomInt`
  (CSPRNG-backed); SET NX prevents cross-tenant collisions; reverse
  pointer prevents Redis-key churn. Bot side uses GETDEL
  (atomic) for one-shot consumption.
- **`buildSpec` env order** — comment + code explicitly put
  systemEnv after decryptedSecrets so `API_KEY`, `DATABASE_URL`,
  etc. cannot be overridden from secrets. The handful of vars
  this *doesn't* cover is C-1 above.
- **`containerName` derivation** — 16-hex of tenant UUID; no
  external input; well within Docker's 63-char name limit.
- **`tenantBotFetch`** — resolves tenant via session, looks up
  `tenant_bots` row by both `tenantId` AND `mode` AND
  `is_running=true`. No way to proxy to another tenant's bot via
  parameter manipulation.
- **`safeNext`** — rejects `//`, `/login`, `/api/`. Built into
  every login redirect path.
- **`secrets/[key]` PUT** — KEY pattern enforced (length-bounded);
  4KB value cap; requires unlock; uses `onConflictDoUpdate` with
  composite key.
- **OIDC** — uses official `openid-client` library, PKCE+state,
  callback validates state via `expectedState`, redirect_uri
  resolved through `PUBLIC_URL`, secret never sent to client (and
  `server-only` on auth-config.ts enforces that at build time).
- **`ensureSessionSecret`** — atomic `SET NX` so concurrent
  first-init produces consistent secret; 48-byte random.
- **bcrypt cost 12**, `@node-rs/bcrypt` (Rust binding, work
  off-thread), constant-time compare.
- **`requireUnlockedKey`** — keyed by `(tenant_id, sha256(cookie))`
  — can't load another tenant's K even with their session ID.
- **Bot's `_require_auth`** uses `hmac.compare_digest` (constant
  time). Right primitive choice, aside from the empty-key case
  noted in H-1.
- **Bot orchestrator restartPolicy `unless-stopped`, memory + CPU
  caps** — DoS-bounded per tenant.
- **`@node-rs/argon2` `hashRaw`** is used (raw bytes) so the AES
  key isn't a string-encoding round-trip. `makeVerifier` uses
  HMAC-SHA-256 over a domain string (correct primitive — not
  Argon2id-on-key, which would be wasted work).
- **Telegram `send-unlock-link`** — does HTML-escape the URL
  before stuffing it into a `parse_mode=HTML` message; comment
  explicitly notes the API_KEY-shared-secret assumption.
- **Pre-commit hook** — `.githooks/pre-commit` blocks
  secret-shaped strings; CLAUDE.md §0 enforces the policy.
- **No committed `.env`** — `git log --diff-filter=A` shows no
  `.env` file ever committed; `bot/.env` is gitignored.
- **No git-history secret leakage** found via `git log -p` grep
  for hex/base64 token patterns.

---

## 4. Out of scope / not investigated

- **Bot-internal Python code paths** beyond `api.py`,
  `notify/telegram.py`, `config.py` — strategies, runner,
  exchange, db/repo not audited beyond skim. RLS ensures any bug
  there stays within a tenant, so the cross-tenant blast radius is
  bounded.
- **`docker.ts` failure modes around concurrent create+start** —
  briefly read; no obvious issues but didn't fuzz.
- **Caddy admin API** (`caddy-admin.ts`) — only verified that the
  TLS configure route is operator-gated and the admin port isn't
  bound externally per CLAUDE.md.
- **Dependency CVE scan** — did not run `npm audit` /
  `pip-audit`. Pin versions to minor (e.g.
  `playwright>=1.58,<1.59`-style) per CLAUDE.md guidance.
- **Argon2id timing-side-channel resistance** — implementation is
  `@node-rs/argon2`'s hashRaw, which uses the reference C
  implementation. Not separately verified.
- **Cloudflare Tunnel + Caddy ingress headers** — assumed correct
  per CLAUDE.md; didn't verify that `X-Forwarded-For` /
  `X-Real-IP` are sanitized before being used by future
  rate-limit code.
- **Kanyl-bullen/xupertrade public repo SCM hygiene** — branch
  protection on master assumed correct per
  `reference_xupertrade_branch_protection.md`. Did not verify
  CODEOWNERS, required reviews, or signed-commit settings on
  GitHub.

---

*End of report.*
