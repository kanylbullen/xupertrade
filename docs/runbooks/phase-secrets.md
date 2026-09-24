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
- Locally: `phase secrets create KEY=VALUE` or `phase secrets update KEY=VALUE`
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
