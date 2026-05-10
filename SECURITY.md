# Security Policy

`hypertrade` is a self-hosted crypto-trading bot platform. The
deployment model has TWO trust tiers:

1. **Single-operator deployment** — one person runs the deploy on
   their own host with their own money. This is what `master`
   started life as and is still the default story.
2. **Multi-tenant deployment** — operator hosts the deploy on a
   shared host; multiple authenticated users (tenants) each run
   their own bots with their own HyperLiquid keys. Multi-tenancy
   landed in 2026-05-10 across phases 1–5 of
   [`docs/plans/multi-tenancy.md`](docs/plans/multi-tenancy.md).

The repo is public so other people can read and adapt the code.
Trust assumptions differ between the two tiers and this policy
covers both.

---

## Supported versions

| Branch | Status |
|---|---|
| `master` | actively maintained — security fixes land here |
| anything else | unsupported (no release branches today) |

Deployments lag `master` by zero-to-a-few-hours; there is no
separate release cadence to track.

## Reporting a vulnerability

Use **GitHub's Private Vulnerability Reporting** for this repo:

1. Go to the repo's [Security tab](https://github.com/kanylbullen/hypertrade/security)
2. Click **Report a vulnerability**
3. Describe the issue with enough detail to reproduce

PVR is the only supported channel — it routes the report through
GitHub without exposing it publicly until fixed. Do not file public
issues for security bugs and do not email the maintainer (no public
email is associated with this project).

If GitHub PVR is unavailable when you need it, open a regular issue
with the title "SECURITY — please contact me" and **no details**;
the maintainer will reach out.

### What to include

- Affected file(s) and line number(s) on `master` HEAD
- Reproduction steps or proof-of-concept
- Impact assessment (what can an attacker do, in what configuration)
- Which deployment tier the bug applies to (single-operator,
  multi-tenant, or both)
- Any suggested fix

### Response timeline

This is a personal project, not a commercial service. Realistic
expectations:

- Acknowledgement: within **3 business days**
- Triage + initial assessment: within **1 week**
- Fix or "won't-fix" decision:
  - **CRITICAL** (RCE, mainnet credential leak, tenant boundary
    bypass): within **7 days** best-effort
  - **HIGH** (privilege escalation, secret-at-rest leakage): within
    **14 days**
  - **MEDIUM/LOW**: within **30 days** best-effort
- Coordinated public disclosure: **90 days** after fix, or earlier
  if the maintainer agrees a sooner publish helps the ecosystem

If a vulnerability is actively exploited in the wild against a
running deployment, the timeline collapses — the fix ships as soon
as it's verified.

---

## Threat model

### Tier 1: Single-operator deployment

The original threat model. The operator runs `hypertrade` on a host
they own, with their own HyperLiquid private key, against their own
money. The bot's HTTP API and dashboard are gated by `API_KEY`
and/or OIDC.

**Trusted parties:**
- Operator (full root on host, full DB access)
- HyperLiquid (we trust the exchange and its SDK)

**Adversary surface:**
- Internet-facing dashboard / bot HTTP API
- Code-injection paths (crafted candle data, manipulated DB rows)
- Supply chain (npm, PyPI, base images)

### Tier 2: Multi-tenant deployment

Multiple users sign in via Authentik OIDC and each runs their own
bot. Operator hosts the deploy but **must not** be able to read
tenants' secrets at rest.

**Trust hierarchy:**
- Authentik (single point of identity trust — operator-managed)
- Operator (privileged role for ops: deploy, scale, kill any bot;
  CANNOT read tenant secrets at rest in the DB)
- Tenants (mutually untrusting peers; can only read/write their
  own data)

**v1 scope-of-protection** (per
[`docs/plans/multi-tenancy.md`](docs/plans/multi-tenancy.md) §4):

> Trust model B protects against an operator who peeks at the
> database (or a backup of it). It does NOT protect against an
> operator who actively wants to read running-bot secrets — once a
> tenant unlocks and starts their bot, the decrypted secrets are in
> the container's env vars and visible via `docker inspect`. If the
> threat model includes "I don't trust the operator at all", this v1
> isn't enough. v2 hardening (tmpfs-mount injection) is documented
> but deferred.

In practice: anyone running their own crypto-trading bot already
trusts the host they deploy on. The protection here is against
casual DB peeks, backups, and DBAs — not against a malicious
operator with root and ill intent.

---

## In scope

Findings against the **code on `master`** in any of these classes
are in scope:

### Always in scope

- **Authentication bypass** on the dashboard (`/api/control/*`,
  `/api/auth/*`, `/api/tenant/*`) or the bot HTTP API (paths gated
  by `_require_auth`)
- **Remote code execution** via crafted input
- **Credential exposure** — token/key/secret leak paths in code,
  logs, error messages, or any JSON response
- **Privilege escalation** — any way to use a less-trusted
  credential to invoke order placement or reach API_KEY-gated
  endpoints
- **Trade-execution integrity** — anything that lets an unauthorized
  caller induce a bot to place / cancel / modify orders, or
  manipulate DB position state into a state that triggers unwanted
  exchange-side trades
- **TLS / Caddy misconfiguration** — anything that lets traffic
  cross the wire in plaintext when the operator has TLS enabled
- **Container escape / Docker misconfiguration** — anything that
  lets a compromise of one container reach Postgres / Redis /
  another bot mode unexpectedly
- **Supply-chain** — compromised dependency, malicious upstream,
  build-time injection

### Multi-tenancy-specific (Tier 2)

The multi-tenant deployment introduces new attack classes. **All
treated as CRITICAL** since a single bug can cross the tenant
boundary in production:

- **Tenant boundary bypass** — tenant A reads tenant B's
  positions, trades, equity, or any other DB row. The
  Phase 5 RLS layer
  ([`bot/alembic/versions/0010_rls_policies.py`](bot/alembic/versions/0010_rls_policies.py))
  is the load-bearing defence; bypasses are tenant-cross-leak.
- **Cross-tenant credential exposure** — tenant A is able to read
  any of tenant B's encrypted secrets, the K-cache entry, the
  passphrase salt/verifier, or the tenant's PG role password.
- **Privilege escalation: tenant → operator** — tenant gains
  ability to enumerate other tenants, stop another tenant's bot,
  call any admin-scoped endpoint (currently only `postgres`
  superuser SQL access exists; admin HTTP routes are PLANNED in
  a later phase), or otherwise act on another tenant's behalf.
- **Secret-at-rest leakage** — `tenant_secrets` rows readable
  without the tenant's passphrase; the Argon2id salt or verifier
  ever logged in plaintext.
- **Webhook forgery** — once Phase 4 lands: forging Telegram
  updates against `/api/telegram/<tenant_id>` to make another
  tenant's bot execute commands.
- **Resource starvation by single tenant** — a tenant whose bot
  spam-trades fast enough to exhaust the shared HL connection's
  rate limit, starve others' DB connections, or fill disk.
- **Authentik account takeover** — out of our control directly,
  but document mitigations: the dashboard SHOULD enforce a
  recently-issued OIDC token for high-risk actions (passphrase
  change, bot start), and SHOULD log Authentik sub mismatches
  loudly.

### Strategy-state corruption with money impact

The 2026-05-09 hash_momentum spam class (now mitigated by trade-
rate alarm + parity check) is in scope; similar new classes are
too. This applies in both single-operator and multi-tenant tiers.

## Out of scope

These are NOT security issues — please file a regular issue or
discussion instead:

- **Paper-mode bugs** — paper trading uses fake money in memory;
  bugs are correctness issues, not security
- **Strategy PnL questions** — losing money is part of trading;
  the bot doesn't promise profitable strategies, only faithful
  execution
- **HyperLiquid outages** — third-party availability isn't ours to
  fix (the bot has init-retry + transient-error suppression to
  ride through outages cleanly)
- **Telegram bot abuse from the configured chat owner** — `/flat`,
  `/pause`, etc. by definition trust the chat owner; the bot does
  not authenticate individual messages beyond the chat-ID gate
- **Self-DoS via the operator's own host configuration** — opening
  a bot's port to the public internet without setting `API_KEY` is
  an operational issue, not a code bug
- **Findings only reachable with the HyperLiquid private key
  already compromised** — at that point an attacker can do anything
  HL allows the wallet to do; that's outside the bot's defence
  surface
- **Issues that require the operator to clone the repo and use a
  malicious config** — the threat model assumes the operator runs
  the unmodified `master` code with a sane configuration

### Multi-tenancy-specific out-of-scope

- **Operator with host root + memory access reading tenant
  secrets from a running container's memory or env vars** — this
  is the v1 limitation explicitly documented in plan §4. Tenants
  who don't trust the operator have no recourse here in v1.
- **Authentik internal bugs** — out of our control; report to the
  Authentik project. We trust Authentik for identity.
- **Tenant losing their own passphrase** — by design,
  passphrase loss = secrets unrecoverable. No reset, no recovery
  email, no operator override (that's the entire point of trust
  model B's at-rest protection). Document loudly in the UI.
- **Tenant's own Authentik account compromise** — that tenant's
  data is at risk; not our bug. Tenant should secure their
  Authentik account (2FA, etc).

---

## What you'll get back

- A clear yes/no on whether the report is in scope
- If in scope: regular updates as the fix progresses
- A credit in the eventual commit message + PR description
  (unless you'd rather stay anonymous — say so in the report)
- **No bounty** — this is not a commercial project and there is no
  budget for paid disclosure

Thank you for taking the time to look at the code carefully.
