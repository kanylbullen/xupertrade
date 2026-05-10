# Security Policy

`hypertrade` is a self-hosted personal crypto-trading bot run by a
single operator with their own money on HyperLiquid. The repo is
public so other people can read and adapt the code, but the running
deployment is single-tenant and not a hosted service.

That context shapes what counts as a security issue here, how to
report one, and what response to expect.

---

## Supported versions

| Branch | Status |
|---|---|
| `master` | actively maintained — security fixes land here |
| anything else | unsupported (no release branches today) |

Deployments lag `master` by zero-to-a-few-hours; there is no separate
release cadence to track.

## Reporting a vulnerability

Use **GitHub's Private Vulnerability Reporting** for this repo:

1. Go to the repo's [Security tab](https://github.com/kanylbullen/hypertrade/security)
2. Click **Report a vulnerability**
3. Describe the issue with enough detail to reproduce

Private Vulnerability Reporting is the only supported channel — it
routes the report through GitHub without exposing it publicly until
fixed. Do not file public issues for security bugs and do not email
the maintainer (no public email is associated with this project).

If GitHub PVR is unavailable when you need it, open a regular issue
with the title "SECURITY — please contact me" and **no details**;
the maintainer will reach out.

### What to include

- Affected file(s) and line number(s) on `master` HEAD
- Reproduction steps or proof-of-concept
- Impact assessment (what can an attacker do, in what configuration)
- Any suggested fix

### Response timeline

This is a personal project, not a commercial service. Realistic
expectations:

- Acknowledgement: within **3 business days**
- Triage + initial assessment: within **1 week**
- Fix or "won't-fix" decision: within **30 days** for High/Critical;
  best-effort for lower severity
- Coordinated public disclosure: **90 days** after fix, or earlier
  if the maintainer agrees a sooner publish helps the ecosystem

If a vulnerability is actively exploited in the wild against the
operator's running deployment, the timeline collapses — the fix
ships as soon as it's verified.

---

## In scope

Findings against the **code on `master`** in any of these classes are
in scope:

- **Authentication bypass** on the dashboard (`/api/control/*`,
  `/api/auth/*`) or the bot HTTP API (paths gated by `_require_auth`)
- **Remote code execution** via crafted input
- **Credential exposure** — token/key/secret leak paths in code,
  logs, error messages, or the dashboard JSON responses
- **Privilege escalation** — any way to use a less-trusted credential
  (e.g. an OIDC user without API_KEY) to invoke order placement or
  reach API_KEY-gated endpoints
- **Trade-execution integrity** — anything that lets an unauthorized
  caller induce the bot to place / cancel / modify orders, or
  manipulate the DB position state into a state that triggers
  unwanted exchange-side trades
- **Strategy-state corruption with money impact** — the
  2026-05-09 hash_momentum spam class (now mitigated by trade-rate
  alarm + parity check) is in scope; similar new classes are too
- **TLS / Caddy misconfiguration** — anything that lets traffic
  cross the wire in plaintext when the operator has TLS enabled
- **Container escape / Docker misconfiguration** — anything that
  lets a compromise of one container reach Postgres / Redis /
  another bot mode unexpectedly
- **Supply-chain** — compromised dependency, malicious upstream,
  build-time injection (covered by the secret-scan + gitleaks CI
  workflows but new vectors welcome)

## Out of scope

These are NOT security issues — please file a regular issue or
discussion instead:

- **Paper-mode bugs** — paper trading uses fake money in memory; bugs
  are correctness issues, not security
- **Strategy PnL questions** — losing money is part of trading; the
  bot doesn't promise profitable strategies, only faithful execution
- **HyperLiquid outages** — third-party availability isn't ours to
  fix (the bot now has init-retry + transient-error suppression to
  ride through outages cleanly)
- **Telegram bot abuse from the configured chat owner** — `/flat`,
  `/pause`, etc. by definition trust the chat owner; the bot does
  not authenticate individual messages beyond the chat-ID gate
- **Self-DoS via the operator's own host configuration** — opening
  the bot's `:8001` port to the public internet without setting
  `API_KEY` is an operational issue, not a code bug
- **Findings only reachable with the HyperLiquid private key already
  compromised** — at that point an attacker can do anything HL allows
  the wallet to do; that's outside the bot's defense surface
- **Issues that require the operator to clone the repo and use a
  malicious config** — the threat model assumes the operator runs
  the unmodified `master` code with their own configuration

---

## What you'll get back

- A clear yes/no on whether the report is in scope
- If in scope: regular updates as the fix progresses
- A credit in the eventual commit message + PR description
  (unless you'd rather stay anonymous — say so in the report)
- **No bounty** — this is not a commercial project and there is no
  budget for paid disclosure

Thank you for taking the time to look at the code carefully.
