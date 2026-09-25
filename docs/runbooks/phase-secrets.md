# Secrets management — Phase

Operator runbook, moved from `CLAUDE.md` § 3 on 2026-09-24. Every command
here touches the production host; an agent runs one only when the
operator asks for it in the session. Placeholders such as `$DEPLOY_HOST`
follow `CLAUDE.md` § 0 — never commit real values.

**Source of truth: a self-hosted [Phase](https://phase.dev) instance.**
All runtime secrets (HL keys, Telegram token+chat, API_KEY, public URL,
Caddy host, vault tracking address, mainnet allowlist, dashboard sign-in
`AUTH_MODE` + `OIDC_*`) live there in the
`hypertrade` app's `Development` env. The host has the `phase` CLI
installed + authenticated via service token. **No `.env` file on the
host** — anything that previously lived there is now in Phase.

The deploy command ([deploy.md](deploy.md)) wraps everything in `phase run` so secrets are
injected as env vars only for the lifetime of the docker-compose call —
they're never written to disk on the host.

To add or change a secret:
- Locally: `phase secrets create KEY` or `phase secrets update KEY`. The
  CLI asks for the value, hidden, or reads it from stdin
  (`printf %s "$VALUE" | phase secrets update KEY`); `--random base64url
  --length 32` generates one instead, as `scripts/rotate-postgres-password.sh`
  documents. There is no `KEY=VALUE` form: the whole string becomes the key.
- Or via the Phase web UI on the operator's instance
- Then redeploy (host pulls fresh values from Phase on the next `phase run`)

Bootstrap on a new host (one-time):
1. Create a service token in Phase UI → User Settings → Tokens
2. `scp .phase.json` from the operator's local clone to `/opt/hypertrade/`
   on the host (the file links the host to the operator's app/env IDs;
   it is NOT a secret but is gitignored because it's per-instance)
3. `ssh root@$DEPLOY_HOST 'cd /opt/hypertrade && phase auth --mode token'`
   (paste token)
4. Verify: `ssh root@$DEPLOY_HOST 'cd /opt/hypertrade && phase secrets list'`
   should show the keys

The Phase instance URL itself (operator's hostname for their Phase web
UI) is treated as private operator info — see `CLAUDE.md` § 0. Use `$PHASE_URL`
as a placeholder if a doc example needs to refer to it.

## Side-services owner and the vault address (roadmap NU-7)

Two dashboard-side keys decide where Telegram, HODL, the vault scanner and
the key-expiry reminders run:

| Key | Default | What it does |
|---|---|---|
| `HYPERTRADE_SERVICES_OWNER_MODE` | `paper` | The mode whose bot is the services owner. The orchestrator sets `TELEGRAM_ENABLED` and `SERVICES_OWNER` on that bot only and gives it the 1 GiB memory cap; `/hodl`, `/vaults` and the unlock DM read it. |
| `VAULT_TRACKING_ADDRESS` | empty | The operator's vault wallet (a public address, but private operator info — never in the repo). Injected on the operator tenant's owner bot, where it wins over the Credentials-page slot. The paper owner has no mainnet account to fall back on, so `/vaults` lists no holdings without it. |

Both reach the dashboard container through `docker-compose.yml`, so a
change needs the dashboard recreated and then the affected bots
restarted (Stop, then Start), because a bot reads its env once at spawn.

**Changing the owner, or rolling this out the first time:**

1. Set `VAULT_TRACKING_ADDRESS` (and the owner mode, if not paper) in Phase.
2. Deploy the dashboard ([deploy.md](deploy.md)).
3. Restart the **old** owner first, so it comes back with Telegram and the
   side services off, then the new owner, then the rest. Only one bot may
   poll Telegram `getUpdates` with a token at a time; two collide.
4. Check the new owner's log for `Services owner: this PAPER bot runs …`
   and no `has no VAULT_TRACKING_ADDRESS` warning, and that `/vaults`
   lists the holdings.

Decision 5.12: stop or move the owner only once a new owner runs. If it
is missing for 10 minutes while another bot of the tenant runs, the
dashboard's heartbeat watchdog sends one operator alert per day.
